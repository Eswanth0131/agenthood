"""Client and WAV handling for local or Tinfoil-hosted ACE-Step."""
import asyncio
from array import array
from email import policy
from email.parser import BytesParser
from io import BytesIO
from ipaddress import ip_address
import json
import math
import os
import re
import sys
import time
import wave

import httpx
from starlette.concurrency import run_in_threadpool

from studio_references import production_cues
from studio_vocal import lyric_sections


def endpoint():
    return os.getenv('AGENTHOOD_SONG_URL', '').strip().rstrip('/')


def configured():
    return bool(endpoint())


def is_local(url):
    try:
        return url.host == 'localhost' or ip_address(url.host).is_loopback
    except ValueError:
        return False


def hosted():
    return configured() and not is_local(httpx.URL(endpoint()))


def render_request(project, arrangement, lyrics, feedback):
    tags = {'verse': 'Verse', 'build': 'Pre-Chorus', 'chorus': 'Chorus', 'bridge': 'Bridge'}
    sections = lyric_sections(lyrics, project['song_name'])
    sequence = ('verse', 'build', 'chorus')
    if any('final chorus' in s['name'].lower() for s in arrangement.get('sections', [])):
        sequence += ('bridge', 'chorus')
    sung = '\n\n'.join(f'[{tags[key]}]\n' + '\n'.join(sections[key][:4])
                       for key in sequence if sections[key])
    notes = ('C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B')
    caption = ['Original radio pop song with a clear melodic lead singing the supplied words',
               project['brief'], *production_cues(project['brief']),
               'A focused intimate verse, lifted sung hook and a balanced modern mix']
    if feedback:
        caption.append('Performance direction: ' + feedback)
    return {
        'caption': '. '.join(caption),
        'lyrics': sung,
        'bpm': arrangement['tempo'],
        'keyscale': f"{notes[arrangement.get('root', 48) % 12]} {arrangement.get('mode', 'major')}",
        'timesignature': '4',
        'duration': round(arrangement['bars'] * 240 / arrangement['tempo'], 2),
        'task_type': 'cover',
        'audio_cover_strength': .25,
        'inference_steps': 12,
        'output_format': 'wav16',
        'synth_batch_size': 1,
        'seed': -1,
    }


def read_take(content):
    with wave.open(BytesIO(content), 'rb') as audio:
        channels, sample_width, rate, frames, *_ = audio.getparams()
        if (channels, sample_width) != (2, 2) or not 16000 <= rate <= 96000 or not rate <= frames <= rate * 180:
            raise ValueError('The song model returned an unsupported WAV format.')
        samples = array('h', audio.readframes(frames))
    if sys.byteorder == 'big':
        samples.byteswap()
    if len(samples) != frames * channels or max(map(abs, samples), default=0) < 330:
        raise ValueError('The song model returned silent or incomplete audio.')
    return rate, samples


def decode_result(response):
    content_type = response.headers.get('content-type', '')
    if content_type.startswith('multipart/mixed'):
        message = BytesParser(policy=policy.default).parsebytes(
            f'Content-Type: {content_type}\r\n\r\n'.encode('ascii') + response.content
        )
        content = next((part.get_payload(decode=True) for part in message.iter_parts()
                        if part.get_content_type() in ('audio/wav', 'audio/x-wav')), None)
    else:
        content = response.content if content_type.startswith(('audio/wav', 'audio/x-wav')) else None
    if not content:
        raise ValueError('The song model did not return a WAV take.')
    try:
        read_take(content)
    except (wave.Error, EOFError) as exc:
        raise ValueError('The song model returned an invalid WAV take.') from exc
    return content


async def song_client(base):
    url = httpx.URL(base)
    if url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
        raise ValueError('Set AGENTHOOD_SONG_URL to the renderer origin, without credentials or a path.')
    if is_local(url) and url.scheme == 'http':
        return httpx.AsyncClient(base_url=base, timeout=30, trust_env=False)
    repo = os.getenv('AGENTHOOD_SONG_TINFOIL_REPO', '').strip()
    token = os.getenv('AGENTHOOD_SONG_TOKEN', '').strip()
    if url.scheme != 'https' or not url.host or not re.fullmatch(r'[\w.-]+/[\w.-]+', repo) or len(token) < 32:
        raise ValueError('Hosted singing requires an HTTPS origin, a Tinfoil config repository, and a renderer token.')
    try:
        from tinfoil import SecureClient
    except ImportError as exc:
        raise ValueError('Install requirements-tinfoil.txt to enable hosted singing.') from exc
    try:
        secure = SecureClient(enclave=url.host, repo=repo, transport='tls')
        client = await run_in_threadpool(secure.make_secure_async_http_client)
    except Exception as exc:
        raise ValueError('Tinfoil verification failed. Check the container domain and measured release.') from exc
    client.base_url = url
    client.timeout = httpx.Timeout(30)
    client.headers['Authorization'] = f'Bearer {token}'
    return client


async def generate_take(request, backing, progress):
    base = endpoint()
    if not base:
        raise ValueError('Start the ACE-Step server and set AGENTHOOD_SONG_URL to enable model singing.')
    async with await song_client(base) as client:
        job_id = None
        status = None
        try:
            with backing.open('rb') as track:
                response = await client.post('/synth', data={'request': json.dumps(request)},
                                             files={'audio': (backing.name, track, 'audio/wav')})
            response.raise_for_status()
            job_id = response.json().get('id')
            if not isinstance(job_id, str) or not job_id:
                raise ValueError('The song model did not accept the job.')
            progress(job_id)
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                status_response = await client.get('/job', params={'id': job_id})
                status_response.raise_for_status()
                status = status_response.json().get('status')
                if status == 'done':
                    result = await client.get('/job', params={'id': job_id, 'result': 1})
                    result.raise_for_status()
                    return decode_result(result)
                if status in ('failed', 'cancelled'):
                    raise ValueError('The song model did not finish this take. Check its server log and retry.')
                await asyncio.sleep(1)
            raise ValueError('The song model timed out. Retry once its queue is clear.')
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise ValueError('The renderer denied access. Check the Tinfoil deployment token.') from exc
            if exc.response.status_code in (501, 503):
                raise ValueError('ACE-Step is not ready to render. Check the renderer and model files.') from exc
            raise ValueError('The ACE-Step server rejected this request. Check its log and retry.') from exc
        except httpx.ConnectError as exc:
            raise ValueError('Could not connect to ACE-Step. Check that the renderer is running.') from exc
        except (httpx.HTTPError, AttributeError, json.JSONDecodeError) as exc:
            raise ValueError('Could not complete the song request. Check the ACE-Step server and retry.') from exc
        finally:
            if job_id and status not in ('done', 'failed', 'cancelled'):
                try:
                    await client.post('/job', params={'id': job_id, 'cancel': 1}, timeout=3)
                except httpx.HTTPError:
                    pass


def master_take(source, dest, feedback):
    rate, samples = read_take(source.read_bytes())
    width = 1.13 if 'wider' in feedback.lower() else 1.0
    for i in range(0, len(samples), 2):
        mid = (samples[i] + samples[i+1]) * .5
        side = (samples[i] - samples[i+1]) * .5 * width
        fade = min(1.0, (i + 2) / (rate * .04), (len(samples) - i) / (rate * .12))
        samples[i] = round(max(-32767, min(32767, (mid + side) * fade)))
        samples[i+1] = round(max(-32767, min(32767, (mid - side) * fade)))
    peak = max(map(abs, samples))
    if not peak:
        raise ValueError('The model take has no audible audio to master.')
    gain = 32767 * math.pow(10, -1 / 20) / peak
    result = array('h', (round(sample * gain) for sample in samples))
    if sys.byteorder == 'big':
        result.byteswap()
    with wave.open(str(dest), 'wb') as audio:
        audio.setparams((2, 2, rate, 0, 'NONE', 'not compressed'))
        audio.writeframes(result.tobytes())
    return len(result) / 2 / rate
