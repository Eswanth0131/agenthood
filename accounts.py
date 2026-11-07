"""Password authentication and opaque, revocable sessions."""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from storage import connect

router = APIRouter(prefix='/api/auth', tags=['accounts'])
COOKIE = 'agenthood_session'
TTL = 86400


def init_accounts(c):
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, role TEXT NOT NULL,
            salt BLOB NOT NULL, password_hash BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(
            digest TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
            expires INTEGER NOT NULL);
    """)


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=10, max_length=128)


class Registration(Credentials):
    role: Literal['buyer', 'seller']


def email_key(email):
    email = email.strip().lower()
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
        raise HTTPException(422, 'Enter a valid email address')
    return email


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024**2)


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def same_origin(request: Request):
    origin = request.headers.get('origin')
    expected = os.getenv('AGENTHOOD_ORIGIN') or str(request.base_url).rstrip('/')
    if (origin and origin != expected) or request.headers.get('sec-fetch-site') == 'cross-site':
        raise HTTPException(403, 'Cross-origin request blocked')


def new_session(request, response, user_id):
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with connect() as c:
        c.execute('DELETE FROM sessions WHERE expires<=? OR digest=?',
                  (now, digest(request.cookies.get(COOKIE, ''))))
        c.execute('INSERT INTO sessions VALUES(?,?,?)', (digest(token), user_id, now + TTL))
    response.set_cookie(COOKIE, token, httponly=True, samesite='strict',
                        secure=os.getenv('AGENTHOOD_SECURE_COOKIES') == 'true', max_age=TTL)
    response.headers['Cache-Control'] = 'no-store'


def current_user(request: Request):
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        same_origin(request)
    with connect() as c:
        row = c.execute("""SELECT u.id,u.email,u.role FROM users u JOIN sessions s ON s.user_id=u.id
                        WHERE s.digest=? AND s.expires>?""",
                        (digest(request.cookies.get(COOKIE, '')), int(time.time()))).fetchone()
    if not row:
        raise HTTPException(401, 'Sign in first')
    return dict(row)


def buyer(user=Depends(current_user)):
    if user['role'] != 'buyer':
        raise HTTPException(403, 'Buyer account required')
    return user


def seller(user=Depends(current_user)):
    if user['role'] != 'seller':
        raise HTTPException(403, 'Seller account required')
    return user


@router.post('/register', status_code=201, dependencies=[Depends(same_origin)])
def register(data: Registration, request: Request, response: Response):
    user = {'id': secrets.token_hex(16), 'email': email_key(data.email), 'role': data.role}
    salt = secrets.token_bytes(16)
    hashed = password_hash(data.password, salt)
    try:
        with connect() as c:
            c.execute('INSERT INTO users VALUES(?,?,?,?,?)', (*user.values(), salt, hashed))
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, 'Email already registered') from exc
    new_session(request, response, user['id'])
    return user


@router.post('/login', dependencies=[Depends(same_origin)])
def login(data: Credentials, request: Request, response: Response):
    with connect() as c:
        user = c.execute('SELECT * FROM users WHERE email=?', (email_key(data.email),)).fetchone()
    hashed = password_hash(data.password, user['salt'] if user else bytes(16))
    if not user or not hmac.compare_digest(hashed, user['password_hash']):
        raise HTTPException(401, 'Invalid email or password')
    new_session(request, response, user['id'])
    return {key: user[key] for key in ('id', 'email', 'role')}


@router.get('/me')
def me(user=Depends(current_user)):
    return user


@router.post('/logout', dependencies=[Depends(same_origin)])
def logout(request: Request, response: Response):
    with connect() as c:
        c.execute('DELETE FROM sessions WHERE digest=?', (digest(request.cookies.get(COOKIE, '')),))
    response.delete_cookie(COOKIE)
    return {'signed_out': True}
