from deploy.tinfoil import gateway

from fastapi.testclient import TestClient
import httpx


def test_gateway_requires_auth_and_only_exposes_song_routes(monkeypatch):
    operations = []
    ready = False

    def upstream(request):
        operations.append((request.method, request.url.path))
        assert 'authorization' not in request.headers
        if request.url.path == '/props':
            return httpx.Response(200, json={'models': {'embedding': ['qwen'], 'dit': ['turbo'],
                                                       'vae': ['vae'] if ready else []}})
        if request.url.path == '/synth':
            assert request.headers['content-type'] == 'multipart/form-data; boundary=demo'
            assert request.content == b'demo audio'
            return httpx.Response(200, json={'id': '12'})
        if request.url.params.get('result') == '1':
            return httpx.Response(200, content=b'demo take',
                                  headers={'Content-Type': 'multipart/mixed; boundary=audio'})
        assert request.url.params['cancel'] == '1'
        return httpx.Response(200, json={'status': 'cancelled'})

    headers = {'Authorization': 'Bearer ' + 'a'*32, 'Content-Type': 'multipart/form-data; boundary=demo'}
    with TestClient(gateway.create_app('a'*32, transport=httpx.MockTransport(upstream))) as client:
        assert client.get('/health').status_code == 503
        ready = True
        assert client.get('/health').json() == {'status': 'ok'}
        assert client.get('/logs', headers=headers).status_code == 404
        assert client.post('/synth').status_code == 401
        assert client.get('/job?id=12').status_code == 401
        assert client.get('/job?id=bad', headers=headers).status_code == 400
        assert client.post('/job?id=12', headers=headers).status_code == 400
        assert client.post('/synth', headers=headers, content=b'demo audio').json() == {'id': '12'}
        take = client.get('/job?id=12&result=1', headers=headers)
        assert take.content == b'demo take'
        assert take.headers['content-type'] == 'multipart/mixed; boundary=audio'
        assert client.post('/job?id=12&cancel=1', headers=headers).json() == {'status': 'cancelled'}
        monkeypatch.setattr(gateway, 'MAX_UPLOAD', 2)
        assert client.post('/synth', headers=headers, content=b'long').status_code == 413
    assert operations == [('GET', '/props'), ('GET', '/props'), ('POST', '/synth'),
                          ('GET', '/job'), ('POST', '/job')]
