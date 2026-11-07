"""Authenticated HTTP surface for the otherwise loopback-only ACE-Step engine."""
from contextlib import asynccontextmanager
import hmac
import os

from fastapi import FastAPI, Request, Response
import httpx

MAX_UPLOAD = 12 * 1024 * 1024


def create_app(token, transport=None):
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError('SONG_RENDER_TOKEN must contain at least 32 ASCII characters without spaces.')
    expected = ('Bearer ' + token).encode('ascii')

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(base_url='http://127.0.0.1:8085', transport=transport,
                                     timeout=30, trust_env=False) as upstream:
            app.state.upstream = upstream
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get('/health')
    async def health():
        try:
            result = await app.state.upstream.get('/props', timeout=3)
            result.raise_for_status()
            models = result.json()['models']
            if all(models.get(kind) for kind in ('embedding', 'dit', 'vae')):
                return {'status': 'ok'}
        except (httpx.HTTPError, KeyError, ValueError, TypeError, AttributeError):
            pass
        return Response(status_code=503)

    @app.api_route('/synth', methods=['POST'])
    @app.api_route('/job', methods=['GET', 'POST'])
    async def forward(request: Request):
        auth = request.headers.get('authorization', '').encode('latin-1')
        if not hmac.compare_digest(auth, expected):
            return Response(status_code=401, headers={'WWW-Authenticate': 'Bearer'})
        route = request.url.path
        params = {}
        body = bytearray()
        headers = {}
        if route == '/synth':
            content_type = request.headers.get('content-type', '')
            if not content_type.startswith('multipart/form-data;'):
                return Response(status_code=415)
            async for chunk in request.stream():
                if len(body) + len(chunk) > MAX_UPLOAD:
                    return Response(status_code=413)
                body.extend(chunk)
            headers['Content-Type'] = content_type
        else:
            job_id = request.query_params.get('id', '')
            if not job_id.isascii() or not job_id.isdecimal() or len(job_id) > 20:
                return Response(status_code=400)
            params['id'] = job_id
            if request.method == 'POST':
                if request.query_params.get('cancel') != '1':
                    return Response(status_code=400)
                params['cancel'] = '1'
            elif request.query_params.get('result') == '1':
                params['result'] = '1'
        try:
            upstream = await app.state.upstream.request(request.method, route, params=params,
                                                        headers=headers, content=bytes(body))
        except httpx.HTTPError:
            return Response(status_code=503)
        return Response(upstream.content, status_code=upstream.status_code,
                        media_type=upstream.headers.get('content-type'),
                        headers={'Cache-Control': 'no-store'})

    return app


def from_env():
    return create_app(os.environ.get('SONG_RENDER_TOKEN', ''))
