"""Integration coverage for the studio, audio deliveries and public review repricing."""
from array import array
from concurrent.futures import ThreadPoolExecutor
import io
import json
import math
from pathlib import Path
from threading import Event
import wave
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient
import app as application
from app import app
import music
import studio_vocal
from studio_references import production_cues


@pytest.fixture
def studio(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENTHOOD_DB', str(tmp_path / 'music.db'))
    monkeypatch.setattr(music, 'OUTPUT', tmp_path / 'outputs')
    monkeypatch.setenv('ENABLE_COGNEE', 'false')
    requests = []
    def speech(text, seconds, voice, output):
        requests.append(text)
        return array('f', (.31 * math.sin(i * .13) * (i % 3200) / 3200 for i in range(6500)))
    monkeypatch.setattr(studio_vocal, 'speech_samples', speech)
    monkeypatch.setattr(studio_vocal, 'SAY', Path(__file__))
    with TestClient(app) as client:
        assert client.post('/api/auth/register', json={
            'email': 'studio@example.test', 'password': 'a-studio-password', 'role': 'buyer',
        }).status_code == 201
        client.speech_requests = requests
        yield client


def test_music_studio_project_end_to_end(studio):
    client = studio
    catalog=client.get('/api/music/catalog').json()
    assert len(catalog)==28
    assert all(len([a for a in catalog if a['skill']==r])==4 for r in music.ROLES)
    resp=client.post('/api/music/projects',json={'customer':'Studio','brief':'Produce original energetic pop music with positive chorus','song_name':'Bright Side','budget':6,'provider':'demo'})
    assert resp.status_code==200,resp.text
    pid=resp.json()['project']['id']
    stage_wavs = {
        'composer': ['melody.wav', 'composition.wav'],
        'arranger': ['arrangement.wav'],
        'instrumentalist': ['drums.wav', 'bass.wav', 'keys.wav', 'instrumental.wav'],
        'singer': ['lyric_vocal.wav', 'pre_mix.wav'],
        'producer': ['final_mix.wav'],
    }
    base = f'/api/music/projects/{pid}'
    for stage in music.ROLES:
        r=client.post(f'/api/music/projects/{pid}/advance')
        assert r.status_code==200,(stage,r.text)
        delivery = r.json()['hires'][-1]
        assert delivery['files'] and delivery['mode'] and delivery['status'] == 'completed'
        for filename in stage_wavs.get(stage, []):
            assert filename in delivery['files']
            response = client.get(f'{base}/files/{filename}')
            assert response.headers['content-type'].startswith('audio/')
            with wave.open(io.BytesIO(response.content)) as wav:
                assert wav.getnchannels() == 2
                assert wav.getnframes() > wav.getframerate()
                samples = array('h', wav.readframes(wav.getnframes()))
                assert max(samples) > 1000
            partial = client.get(f'{base}/files/{filename}', headers={'Range': 'bytes=0-43'})
            assert partial.status_code == 206
            assert partial.content.startswith(b'RIFF')
        if stage == 'composer':
            previous = client.get(f'{base}/files/score.json').json()
            assert client.post(f'{base}/advance').status_code == 409
            revision = client.post(f'{base}/approve', json={'approved': False, 'feedback': 'higher, 108 BPM'})
            assert revision.status_code == 200, revision.text
            assert revision.json()['project']['status'] == 'awaiting_approval'
            assert revision.json()['project']['spent'] == r.json()['project']['spent']
            assert revision.json()['hires'][-1]['agent_id'] == delivery['agent_id']
            latest = client.get(f'{base}/files/score.json').json()
            assert latest['tempo'] == 108
            assert [n[1] for n in latest['lead']] == [n[1] + 12 for n in previous['lead']]
            assert client.get(f"{base}/deliveries/{delivery['id']}/score.json").json() == previous
        if r.json()['project']['status']=='awaiting_approval':
            approved=client.post(f'/api/music/projects/{pid}/approve',json={'approved':True,'feedback':'higher chorus'})
            assert approved.status_code==200
    p=client.get(f'/api/music/projects/{pid}').json()
    assert p['project']['status']=='complete'
    assert len(p['hires'])==8
    assert any('Bright Side' in phrase for phrase in client.speech_requests)
    assert 'final_mix.wav' in p['files']
    assert len(client.get(f'/api/music/projects/{pid}/files/final_mix.wav').content)>10000
    arranged = client.get(f'{base}/files/arrangement.json').json()
    performed = client.get(f'{base}/files/performance.json').json()
    assert arranged['tempo'] == performed['tempo'] == 108
    assert arranged['bars'] > 8
    assert arranged['sections'] == performed['sections']
    assert performed['drums'] and arranged['bass'] != performed['bass']
    with zipfile.ZipFile(io.BytesIO(client.get(f'{base}/files/bundle.zip').content)) as bundle:
        assert set(bundle.namelist()) == set(p['files'])
        assert bundle.testzip() is None
    aid=p['hires'][0]['agent_id']
    previous=next(a for a in client.get('/api/music/catalog').json() if a['id']==aid)['base_price']
    review=client.post(f'/api/music/projects/{pid}/review',json={'agent_id':aid,'rating':5,'comment':'Excellent songwriting'})
    assert review.status_code==200
    updated=next(a for a in client.get('/api/music/catalog').json() if a['id']==aid)['base_price']
    assert updated>previous
    assert client.post(f'/api/music/projects/{pid}/review',json={'agent_id':aid,'rating':1}).status_code==409
    assert client.get('/studio').status_code==200


def test_provider_progress_retry_and_marketing(studio, monkeypatch):
    entered = Event()
    release = Event()
    prompts = []

    async def fake_endpoint(request):
        assert str(request.url) == 'https://model.example/v1/chat/completions'
        assert request.headers['Authorization'] == 'Bearer test-key'
        payload = json.loads(request.content)
        assert payload['model'] == 'test-model'
        prompts.append(payload['messages'])
        if len(prompts) == 1:
            return httpx.Response(200, json={'choices': [{'message': {'content': '  '}}]})
        if len(prompts) == 2:
            entered.set()
            import asyncio
            while not release.is_set():
                await asyncio.sleep(.01)
        return httpx.Response(200, json={'choices': [{'message': {'content': 'Model draft ' + str(len(prompts))}}]})

    async_client = httpx.AsyncClient
    monkeypatch.setattr(application.httpx, 'AsyncClient',
                        lambda **kwargs: async_client(transport=httpx.MockTransport(fake_endpoint), **kwargs))
    monkeypatch.setenv('DARKBLOOM_BASE_URL', 'https://model.example/v1')
    monkeypatch.setenv('DARKBLOOM_API_KEY', 'test-key')
    monkeypatch.setenv('DARKBLOOM_MODEL', 'test-model')
    project = studio.post('/api/music/projects', json={'customer': 'Studio', 'song_name': 'First light', 'allow_processing': True}).json()['project']
    base = '/api/music/projects/' + project['id']
    failed = studio.post(base + '/advance')
    assert failed.status_code == 422
    state = studio.get(base).json()
    assert state['project']['status'] == 'retryable'
    assert state['project']['spent'] == 0
    assert state['files'] == []
    assert not any(e['event'] == 'real_model_inference' for e in state['events'])
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(studio.post, base + '/advance')
        try:
            assert entered.wait(timeout=5)
            active = studio.get(base).json()
            assert active['project']['status'] == 'running'
            assert active['hires'][-1]['status'] == 'working'
            assert active['files'] == []
            assert studio.post(base + '/advance').status_code == 409
            worker = next(a for a in studio.get('/api/music/catalog').json() if a['id'] == active['hires'][-1]['agent_id'])
            assert worker['available_capacity'] == worker['total_capacity'] - 1
        finally:
            release.set()
        assert pending.result(timeout=10).status_code == 200
    revised = studio.post(base + '/approve', json={'approved': False, 'feedback': 'Change the hook'})
    assert revised.status_code == 200
    revision_prompt = json.loads(prompts[-1][-1]['content'])
    assert revision_prompt['previous_draft'].strip() == 'Model draft 2'
    assert revision_prompt['revision_request'] == 'Change the hook'
    assert revised.json()['hires'][-1]['mode'] == 'Model generated · darkbloom'
    assert studio.get(base + '/files/lyrics.txt').text.strip() == 'Model draft 3'
    assert studio.post(base + '/approve', json={'approved': True}).status_code == 200
    # A pre-upgrade project could already have a flat final mix and be ready for marketing.
    with music.connect() as c:
        c.execute("UPDATE music_projects SET stage=6 WHERE id=?", (project['id'],))
    legacy_mix = music.path(project['id'], 'final_mix.wav')
    legacy_mix.write_bytes(b'legacy')
    marketed = studio.post(base + '/advance')
    assert marketed.status_code == 200
    assert marketed.json()['project']['status'] == 'complete'
    assert marketed.json()['hires'][-1]['mode'] == 'Model generated · darkbloom'
    assert studio.get(base + '/files/marketing.md').text.strip() == 'Model draft 4'
    assert 'legacy wordless guide' in json.loads(prompts[-1][-1]['content'])['music']
    assert studio.get(base + '/files/final_mix.wav').content == b'legacy'


def test_restart_refunds_interrupted_specialist(studio):
    p = studio.post('/api/music/projects', json={'customer': 'Studio', 'provider': 'demo'}).json()['project']
    with music.project_lock(p['id']):
        _, agent, hid, *_ = music.start_delivery(p['id'], False, '')
        partial = music.path(p['id'], f'deliveries/{hid}/lyrics.txt')
        partial.parent.mkdir(parents=True)
        partial.write_text('unfinished')
        music.recover_music()
        assert partial.exists()
    music.recover_music()
    snapshot = studio.get('/api/music/projects/' + p['id']).json()
    assert snapshot['project']['spent'] == 0
    assert snapshot['project']['status'] == 'retryable'
    assert snapshot['hires'][0]['status'] == 'failed'
    assert snapshot['files'] == []
    assert not partial.exists()
    row = next(a for a in studio.get('/api/music/catalog').json() if a['id'] == agent['id'])
    assert row['available_capacity'] == row['total_capacity']

def test_existing_complete_project_can_add_lyric_vocal(studio):
    project = studio.post('/api/music/projects', json={'customer': 'Studio', 'provider': 'demo'}).json()['project']
    pid = project['id']
    base = '/api/music/projects/' + pid
    folder = music.path(pid, 'score.json').parent
    folder.mkdir(parents=True)
    (folder / 'score.json').write_text(json.dumps(music.audio.compose('Short pop demo', 'Afterglow', 0)))
    (folder / 'lyrics.txt').write_text('Afterglow\n[Verse]\nMeet me by the water\n[Chorus]\nFollow the light')
    (folder / 'final_mix.wav').write_bytes(b'previous mix')
    with music.connect() as c:
        c.execute("UPDATE music_projects SET stage=7,status='complete',spent=2 WHERE id=?", (pid,))
        for role in ('singer', 'producer'):
            c.execute('INSERT INTO music_hires VALUES(?,?,?,?,?,?,?,?,?)',
                      (role, pid, 'music-'+role+'-1', role, .5, 1, 'completed', '', music.stamp()))
    response = studio.post(base + '/lyric-vocals')
    assert response.status_code == 200, response.text
    snapshot = response.json()
    assert snapshot['project']['status'] == 'complete'
    assert snapshot['project']['spent'] == 2
    assert [h['role'] for h in snapshot['hires'][-2:]] == ['singer', 'producer']
    assert studio.speech_requests == ['Meet me by the water', 'Follow the light']
    assert 'lyric_vocal.wav' in snapshot['files']
    assert studio.get(base + '/files/final_mix.wav').content.startswith(b'RIFF')
    assert (folder / 'final_mix.wav').read_bytes() == b'previous mix'
    assert studio.post(base + '/lyric-vocals').status_code == 409


def test_rihanna_references_translate_to_different_production_cues():
    from studio_audio import compose
    loud = ' '.join(production_cues('Use Rihanna’s Loud as a reference'))
    anti = ' '.join(production_cues('Use Rihanna’s ANTI as a reference'))
    assert not production_cues('Make the bass loud')
    assert compose(loud, 'Demo', 0)['tempo'] > compose(anti, 'Demo', 0)['tempo']
    assert compose(anti, 'Demo', 0)['mode'] == 'minor'


def test_silent_speech_delivers_wordless_guide_and_final_mix(studio, monkeypatch):
    def unavailable(*args):
        raise studio_vocal.SpeechUnavailable('speech engine returned no samples')

    monkeypatch.setattr(studio_vocal, 'speech_samples', unavailable)
    base = '/api/music/projects/' + studio.post('/api/music/projects', json={
        'customer': 'Studio', 'provider': 'demo'
    }).json()['project']['id']
    for role in music.ROLES:
        r = studio.post(base + '/advance')
        assert r.status_code == 200, r.text
        data = r.json()
        if role == 'singer':
            assert data['hires'][-1]['mode'] == 'Algorithmic fallback · wordless melody guide'
            assert 'vocal_guide.wav' in data['files'] and 'lyric_vocal.wav' not in data['files']
            assert studio.post(base + '/lyric-vocals').status_code == 422
        if data['project']['status'] == 'awaiting_approval':
            assert studio.post(base + '/approve', json={'approved': True}).status_code == 200
    assert 'wordless melody guide' in studio.get(base + '/files/marketing.md').text
    assert studio.get(base + '/files/final_mix.wav').content.startswith(b'RIFF')


def test_tinfoil_take_requires_owner_consent_and_verified_transport(studio, monkeypatch):
    import sys
    from types import SimpleNamespace

    operations = []
    renderer_token = 'a' * 32
    root = 'singing.example.containers.tinfoil.dev'
    repo = 'studio/example-config'
    monkeypatch.setenv('AGENTHOOD_SONG_URL', 'https://' + root)
    monkeypatch.setenv('AGENTHOOD_SONG_TINFOIL_REPO', repo)
    monkeypatch.setenv('AGENTHOOD_SONG_TOKEN', renderer_token)
    track = io.BytesIO()
    with wave.open(track, 'wb') as wav:
        wav.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))
        wav.writeframes(array('h', [850, 1200, -900, -1250] * 48000).tobytes())
    generated = track.getvalue()

    def endpoint(request):
        assert request.url.host == root
        assert request.headers['Authorization'] == 'Bearer ' + renderer_token
        operations.append(request.url.path)
        if request.url.path == '/synth':
            assert b'Meet me by the water' in request.content
            assert b'RIFF' in request.content
            return httpx.Response(200, json={'id': '1'})
        if not request.url.params.get('result'):
            return httpx.Response(200, json={'status': 'done'})
        return httpx.Response(200, content=generated, headers={'Content-Type': 'audio/wav'})

    class SecureClient:
        def __init__(self, *, enclave, repo, transport):
            assert transport == 'tls'
            assert enclave == root and repo == 'studio/example-config'
            operations.append('verification')
            if reject_verification:
                raise RuntimeError('wrong attestation')

        def make_secure_async_http_client(self):
            return httpx.AsyncClient(transport=httpx.MockTransport(endpoint))

    monkeypatch.setitem(sys.modules, 'tinfoil', SimpleNamespace(SecureClient=SecureClient))
    pid = studio.post('/api/music/projects', json={'customer': 'Studio', 'provider': 'demo'}).json()['project']['id']
    base = '/api/music/projects/' + pid
    folder = music.path(pid, 'score.json').parent
    folder.mkdir(parents=True)
    (folder / 'score.json').write_text(json.dumps(music.audio.compose('Short pop demo', 'Afterglow', 0)))
    (folder / 'lyrics.txt').write_text('Afterglow\n[Verse]\nMeet me by the water\n[Chorus]\nFollow the light')
    with music.connect() as c:
        c.execute("UPDATE music_projects SET stage=4,status='working' WHERE id=?", (pid,))

    rejected = studio.post(base + '/advance')
    assert rejected.status_code == 422
    assert studio.get(base).json()['hires'] == []
    assert operations == []

    reject_verification = True
    rejected = studio.post(base + '/advance', json={'allow_hosted_song': True})
    assert rejected.status_code == 422
    assert 'verification failed' in rejected.json()['detail']
    assert studio.get(base).json()['project']['spent'] == 0
    assert operations == ['verification']

    reject_verification = False
    response = studio.post(base + '/advance', json={'allow_hosted_song': True})
    assert response.status_code == 200, response.text
    assert response.json()['hires'][-1]['mode'] == 'Model sung performance · ACE-Step on Tinfoil'
    assert studio.get(base + '/files/model_take.wav').content == generated
    assert not studio.speech_requests
    assert studio.post(base + '/approve', json={'approved': False, 'feedback': 'Try again'}).status_code == 422
    assert studio.post(base + '/approve', json={'approved': True}).status_code == 200
    assert studio.post(base + '/advance').status_code == 200
    with wave.open(io.BytesIO(studio.get(base + '/files/final_mix.wav').content)) as wav:
        assert wav.getframerate() == 48000
    assert operations == ['verification', 'verification', '/synth', '/job', '/job']
