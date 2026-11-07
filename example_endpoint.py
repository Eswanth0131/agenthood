"""Local sample seller service. Run it separately with uvicorn on 127.0.0.1:8091."""
import hmac
import os
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from execution import analyze_text

app = FastAPI(title='Agenthood sample seller endpoint')


class Request(BaseModel):
    operation: Literal['verify', 'execute']
    nonce: str = Field(default='', max_length=64)
    brief: str = Field(default='', max_length=8000)


@app.post('/work')
def work(data: Request, authorization: str = Header(default='')):
    secret = os.environ.get('AGENTHOOD_EXAMPLE_SECRET', '')
    if len(secret) < 32 or not hmac.compare_digest(authorization.encode(), f'Bearer {secret}'.encode()):
        raise HTTPException(403, 'Endpoint credential required')
    if data.operation == 'verify':
        if len(data.nonce) != 64:
            raise HTTPException(422, 'Expected a 64-character nonce')
        return {'signature': hmac.digest(secret.encode(), data.nonce.encode(), 'sha256').hex()}
    if not data.brief.strip():
        raise HTTPException(422, 'Provide source text')
    return {'output': 'Algorithmic text analysis from the sample endpoint\n' + analyze_text(data.brief)}
