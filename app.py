"""Agenthood marketplace and music studio."""
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from accounts import router as accounts
from market import router as market, init_market, dispatch
from music import router as studio, init_music, recover_music
from storage import connect, ROOT


async def llm(provider,system,user):
    if provider=='darkbloom':
        base=os.getenv('DARKBLOOM_BASE_URL','').rstrip('/')
        key=os.getenv('DARKBLOOM_API_KEY','')
        model=os.getenv('DARKBLOOM_MODEL','')
    elif provider=='ollama':
        base=os.getenv('OLLAMA_BASE_URL','http://localhost:11434/v1').rstrip('/')
        key='ollama'; model=os.getenv('OLLAMA_MODEL','')
    else: return None
    if not base or not model: return None
    try:
        async with httpx.AsyncClient(timeout=40, follow_redirects=False) as client:
            r=await client.post(base+'/chat/completions',json={'model':model,'messages':[{'role':'system','content':system},{'role':'user','content':user}],'temperature':0.2,'max_tokens':1300},headers={'Authorization':'Bearer '+key})
            r.raise_for_status()
            return r.json()['choices'][0]['message']['content']
    except (httpx.HTTPError,KeyError,IndexError,TypeError) as exc:
        raise RuntimeError(f'{provider} inference failed') from exc

async def cognee_push(content):
    if os.getenv('ENABLE_COGNEE','false').lower()!='true': return 'disabled'
    try:
        import cognee
        await cognee.add(content)
        await cognee.cognify()
        return 'synced'
    except Exception as e:
        return f'error: {str(e)[:130]}'

def init_db():
    with connect() as c:
        # Shared studio capacity table. The earlier demo's orders, memory,
        # metrics and admin payment claims are deliberately outside the new API.
        c.execute("""CREATE TABLE IF NOT EXISTS agents(
            id TEXT PRIMARY KEY, name TEXT, ticker TEXT UNIQUE, skill TEXT,
            description TEXT, base_price REAL, total_capacity INTEGER,
            available_capacity INTEGER, success INTEGER DEFAULT 0,
            failures INTEGER DEFAULT 0, provider TEXT DEFAULT 'local')""")
    init_market()
    init_music()


@asynccontextmanager
async def lifespan(app):
    init_db()
    recover_music()
    async with dispatch():
        yield


app = FastAPI(title='Agenthood', version='1.0.0', lifespan=lifespan)
app.include_router(accounts)
app.include_router(market)
app.include_router(studio)
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')


@app.middleware('http')
async def response_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'private, no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(_request, exc):
    # Validation errors must not reflect passwords, endpoint credentials or brief text.
    return JSONResponse({'detail': [{'loc': e['loc'], 'msg': e['msg'], 'type': e['type']}
                                   for e in exc.errors()]}, status_code=422)


@app.get('/')
def index():
    return FileResponse(ROOT / 'static' / 'index.html')


@app.get('/studio')
def music_studio_page():
    return FileResponse(ROOT / 'static' / 'studio.html')
