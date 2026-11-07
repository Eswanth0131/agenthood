"""Listings, fixed-price quotes and simulated, auditable work contracts."""
import asyncio
import fcntl
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager, contextmanager, suppress
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from accounts import buyer, current_user, init_accounts, seller
from execution import configured_model, endpoint_fingerprint, model_verification, perform, verify_tool
from storage import connect, database, now

router = APIRouter(prefix='/api/market', tags=['marketplace'])
log = logging.getLogger(__name__)
ACTIVE = "('queued','running','awaiting_approval','delivered')"
HOLD_SECONDS = 86400


def init_market():
    with connect() as c:
        init_accounts(c)
        c.executescript("""
            CREATE TABLE IF NOT EXISTS listings(
                id TEXT PRIMARY KEY, seller_id TEXT REFERENCES users(id), name TEXT NOT NULL,
                capabilities TEXT NOT NULL, price_cents INTEGER NOT NULL, capacity INTEGER NOT NULL,
                tool TEXT NOT NULL, endpoint TEXT NOT NULL, active INTEGER NOT NULL,
                verification TEXT, verified_at TEXT, version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS quotes(
                id TEXT PRIMARY KEY, buyer_id TEXT NOT NULL REFERENCES users(id),
                listing_id TEXT NOT NULL REFERENCES listings(id), version INTEGER NOT NULL,
                brief TEXT NOT NULL, budget_cents INTEGER NOT NULL, price_cents INTEGER NOT NULL,
                expires INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, quote_id TEXT UNIQUE NOT NULL REFERENCES quotes(id),
                buyer_id TEXT NOT NULL REFERENCES users(id), seller_id TEXT REFERENCES users(id),
                listing_id TEXT NOT NULL REFERENCES listings(id), terms TEXT NOT NULL,
                status TEXT NOT NULL, phase TEXT NOT NULL, revised INTEGER NOT NULL DEFAULT 0,
                execution_id TEXT NOT NULL, result TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT '',
                feedback TEXT NOT NULL DEFAULT '', use_memory INTEGER NOT NULL,
                hold_until INTEGER NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS job_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(id),
                actor TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS buyer_memory(
                id TEXT PRIMARY KEY, buyer_id TEXT NOT NULL REFERENCES users(id),
                note TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS job_deliveries(
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), phase TEXT NOT NULL,
                result TEXT NOT NULL, mode TEXT NOT NULL, at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS job_deliveries_job ON job_deliveries(job_id,at);
            CREATE TABLE IF NOT EXISTS job_reviews(
                job_id TEXT PRIMARY KEY REFERENCES jobs(id), stars INTEGER NOT NULL, comment TEXT NOT NULL,
                at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS job_capacity ON jobs(listing_id,status);
            CREATE INDEX IF NOT EXISTS job_events_job ON job_events(job_id,id);
        """)
        c.execute("""INSERT OR IGNORE INTO listings VALUES
            ('text-analysis',NULL,'Text Analyzer','Word counts and term frequencies for supplied text.',
             30,4,'text_analysis','',1,'builtin:text_analysis:v1',?,1)""", (now(),))
        provider = configured_model()
        if provider:
            # Platform-managed adapter. A health check still gates a successful delivery.
            c.execute("""INSERT INTO listings VALUES
                ('model-writer',NULL,'Model Writer','Draft and revise written deliverables using live inference.',
                 95,2,'model','',1,?,NULL,1)
                ON CONFLICT(id) DO UPDATE SET verification=excluded.verification,
                version=listings.version+1 WHERE listings.verification<>excluded.verification""",
                (model_verification(provider),))


@contextmanager
def write():
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        yield c


def uid():
    return secrets.token_hex(16)


class Listing(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')

    name: str = Field(min_length=2, max_length=100)
    capabilities: str = Field(min_length=8, max_length=1200)
    price_cents: int = Field(strict=True, ge=1, le=100000)
    capacity: int = Field(strict=True, ge=1, le=50)
    tool: Literal['text_analysis', 'model', 'endpoint']
    endpoint: str = Field(default='', max_length=80, pattern=r'^[a-zA-Z0-9_-]*$')
    active: bool = False


class Verify(BaseModel):
    credential: str = Field(default='', max_length=256)


class QuoteRequest(BaseModel):
    listing_id: str = Field(max_length=80)
    brief: str = Field(min_length=10, max_length=8000)
    budget_cents: int = Field(strict=True, ge=1, le=100000)


class Hire(BaseModel):
    quote_id: str = Field(max_length=80)
    allow_processing: bool = False
    use_memory: bool = False


class Decision(BaseModel):
    action: Literal['approve', 'revise', 'accept', 'cancel']
    delivery_id: str = Field(default='', max_length=80)
    feedback: str = Field(default='', max_length=2000)


class Review(BaseModel):
    stars: int = Field(strict=True, ge=1, le=5)
    comment: str = Field(default='', max_length=800)


class Note(BaseModel):
    note: str = Field(min_length=1, max_length=1200)


def event(c, job_id, actor, name, **details):
    c.execute('INSERT INTO job_events(job_id,actor,event,detail,at) VALUES(?,?,?,?,?)',
              (job_id, actor, name, json.dumps(details), now()))


def occupied(c, listing_id):
    return c.execute(f'SELECT COUNT(*) FROM jobs WHERE listing_id=? AND status IN {ACTIVE}',
                     (listing_id,)).fetchone()[0]


def available_listing(row):
    if not row['active'] or not row['verification']:
        return False
    if row['tool'] == 'model':
        return row['verification'] == model_verification(configured_model())
    if row['tool'] == 'endpoint':
        try:
            return endpoint_fingerprint(row['endpoint']) == row['verification']
        except (ValueError, TypeError, KeyError, AttributeError):
            return False
    return True


def listing_view(c, row, own=False):
    fields = ('id', 'name', 'capabilities', 'price_cents', 'capacity', 'tool', 'active', 'verified_at', 'version')
    view = {key: row[key] for key in fields}
    if own:
        view['endpoint'] = row['endpoint']
    rating = c.execute("""SELECT COUNT(*) AS n, AVG(r.stars) AS average FROM job_reviews r
                          JOIN jobs j ON j.id=r.job_id WHERE j.listing_id=?""", (row['id'],)).fetchone()
    return dict(view, available_capacity=row['capacity'] - occupied(c, row['id']),
                dispatchable=available_listing(row), rating=dict(rating))


@router.get('/listings')
def browse():
    with connect() as c:
        return [listing_view(c, r) for r in c.execute('SELECT * FROM listings ORDER BY name')
                if available_listing(r)]


@router.get('/seller/listings')
def seller_listings(user=Depends(seller)):
    with connect() as c:
        return [listing_view(c, r, own=True) for r in c.execute(
            'SELECT * FROM listings WHERE seller_id=? ORDER BY name', (user['id'],))]


def owned_listing(c, lid, seller_id):
    row = c.execute('SELECT * FROM listings WHERE id=? AND seller_id=?', (lid, seller_id)).fetchone()
    if not row:
        raise HTTPException(404, 'Listing not found')
    return row


def endpoint_name(data):
    if data.tool == 'endpoint' and not data.endpoint:
        raise HTTPException(422, 'Supply the operator-approved endpoint name')
    return data.endpoint if data.tool == 'endpoint' else ''


@router.post('/seller/listings', status_code=201)
def create_listing(data: Listing, user=Depends(seller)):
    lid = uid()
    if data.active:
        raise HTTPException(409, 'Verify the draft before publishing')
    with write() as c:
        c.execute('INSERT INTO listings VALUES(?,?,?,?,?,?,?,?,0,NULL,NULL,1)',
                  (lid, user['id'], data.name, data.capabilities, data.price_cents, data.capacity,
                   data.tool, endpoint_name(data)))
        return listing_view(c, owned_listing(c, lid, user['id']), own=True)


@router.put('/seller/listings/{lid}')
def update_listing(lid: str, data: Listing, user=Depends(seller)):
    endpoint = endpoint_name(data)
    with write() as c:
        old = owned_listing(c, lid, user['id'])
        if data.capacity < occupied(c, lid):
            raise HTTPException(409, 'Capacity is reserved by active jobs')
        changed = (data.tool, endpoint) != (old['tool'], old['endpoint'])
        if data.active and (changed or not old['verification']):
            raise HTTPException(409, 'Save the draft and verify the tool before publishing')
        c.execute("""UPDATE listings SET name=?,capabilities=?,price_cents=?,capacity=?,tool=?,endpoint=?,
                  active=?,verification=?,verified_at=?,version=version+1 WHERE id=?""",
                  (data.name, data.capabilities, data.price_cents, data.capacity, data.tool, endpoint,
                   data.active, None if changed else old['verification'],
                   None if changed else old['verified_at'], lid))
        return listing_view(c, owned_listing(c, lid, user['id']), own=True)


@router.post('/seller/listings/{lid}/verify')
async def verify_listing(lid: str, data: Verify, user=Depends(seller)):
    with connect() as c:
        listing = owned_listing(c, lid, user['id'])
    try:
        async with asyncio.timeout(45):
            verification = await verify_tool(listing['tool'], listing['endpoint'], data.credential)
    except PermissionError as exc:
        raise HTTPException(403, 'Invalid endpoint credential') from exc
    except Exception as exc:
        raise HTTPException(422, 'The execution probe failed. Check tool configuration and try again.') from exc
    with write() as c:
        if c.execute("""UPDATE listings SET verification=?,verified_at=?,version=version+1
                     WHERE id=? AND version=?""", (verification, now(), lid, listing['version'])).rowcount != 1:
            raise HTTPException(409, 'Listing changed during verification. Try again.')
        return listing_view(c, owned_listing(c, lid, user['id']), own=True)


@router.post('/quotes', status_code=201)
def get_quote(data: QuoteRequest, user=Depends(buyer)):
    brief = data.brief.strip()
    if len(brief) < 10:
        raise HTTPException(422, 'Describe the task in at least 10 characters')
    with write() as c:
        listing = c.execute('SELECT * FROM listings WHERE id=?', (data.listing_id,)).fetchone()
        if not listing or not available_listing(listing):
            raise HTTPException(404, 'Listing unavailable')
        if occupied(c, listing['id']) >= listing['capacity']:
            raise HTTPException(409, 'Agent is at capacity')
        if listing['price_cents'] > data.budget_cents:
            raise HTTPException(409, 'Quote exceeds the job budget')
        qid = uid()
        expires = int(time.time()) + 600
        c.execute('INSERT INTO quotes VALUES(?,?,?,?,?,?,?,?)',
                  (qid, user['id'], listing['id'], listing['version'], brief,
                   data.budget_cents, listing['price_cents'], expires))
        return {'id': qid, 'price_cents': listing['price_cents'], 'budget_cents': data.budget_cents,
                'listing': listing_view(c, listing), 'expires_at': expires, 'payment_mode': 'simulated'}


@router.post('/jobs', status_code=201)
def hire(data: Hire, user=Depends(buyer)):
    with write() as c:
        quote = c.execute('SELECT * FROM quotes WHERE id=? AND buyer_id=?',
                          (data.quote_id, user['id'])).fetchone()
        if not quote:
            raise HTTPException(404, 'Quote not found')
        existing = c.execute('SELECT id,status FROM jobs WHERE quote_id=?', (quote['id'],)).fetchone()
        if existing:
            return dict(existing)
        listing = c.execute('SELECT * FROM listings WHERE id=?', (quote['listing_id'],)).fetchone()
        if (quote['expires'] <= time.time() or listing['version'] != quote['version']
                or listing['price_cents'] != quote['price_cents'] or not available_listing(listing)):
            raise HTTPException(409, 'Quote expired or listing changed. Request a new quote.')
        if occupied(c, listing['id']) >= listing['capacity']:
            raise HTTPException(409, 'Agent is at capacity')
        if listing['tool'] in ('model', 'endpoint') and not data.allow_processing:
            raise HTTPException(422, 'Consent to sending the brief and feedback to the execution provider')
        if data.use_memory and listing['tool'] != 'model':
            raise HTTPException(422, 'Saved notes can only be shared with the configured model')
        terms = {key: listing[key] for key in ('name', 'capabilities', 'price_cents', 'tool', 'endpoint', 'verification', 'version')}
        terms.update(brief=quote['brief'], budget_cents=quote['budget_cents'],
                     payment_mode='simulated', processing_consent=data.allow_processing,
                     drafts_included=1, revisions_included=1, final_delivery_included=True)
        jid = uid()
        c.execute("""INSERT INTO jobs(id,quote_id,buyer_id,seller_id,listing_id,terms,status,phase,
                  execution_id,use_memory,hold_until,created_at) VALUES(?,?,?,?,?,?,'queued','draft',?,?,?,?)""",
                  (jid, quote['id'], user['id'], listing['seller_id'], listing['id'], json.dumps(terms),
                   uid(), data.use_memory, int(time.time()) + HOLD_SECONDS, now()))
        event(c, jid, user['id'], 'hired', quote_id=quote['id'], price_cents=quote['price_cents'],
              budget_cents=quote['budget_cents'], payment_mode='simulated', use_memory=data.use_memory)
        return {'id': jid, 'status': 'queued'}


def job_for(c, jid, user):
    column = 'buyer_id' if user['role'] == 'buyer' else 'seller_id'
    row = c.execute(f'SELECT * FROM jobs WHERE id=? AND {column}=?', (jid, user['id'])).fetchone()
    if not row:
        raise HTTPException(404, 'Job not found')
    return row


def job_view(row, user):
    terms = json.loads(row['terms'])
    # Seller dashboards show delivery status and commercial terms, not buyer content.
    terms.pop('verification')
    terms.pop('endpoint')
    if user['role'] == 'seller':
        terms.pop('brief')
    view = {key: row[key] for key in ('id', 'listing_id', 'status', 'phase', 'mode', 'revised', 'created_at', 'hold_until')}
    view.update(terms=terms, delivery_id=row['execution_id'], payment_status='simulated_settled' if row['status'] == 'completed' else 'simulated_unpaid')
    if user['role'] == 'buyer':
        view.update(result=row['result'], feedback=row['feedback'])
    return view


@router.get('/jobs')
def jobs(user=Depends(current_user)):
    column = 'buyer_id' if user['role'] == 'buyer' else 'seller_id'
    with connect() as c:
        return [job_view(row, user) for row in c.execute(
            f'SELECT * FROM jobs WHERE {column}=? ORDER BY created_at DESC LIMIT 100', (user['id'],))]


@router.get('/jobs/{jid}')
def job(jid: str, user=Depends(current_user)):
    with connect() as c:
        row = job_for(c, jid, user)
        events = []
        for e in c.execute('SELECT * FROM job_events WHERE job_id=? ORDER BY id', (jid,)):
            detail = json.loads(e['detail'])
            if user['role'] == 'seller':
                detail.pop('feedback', None)
            events.append({'event': e['event'], 'at': e['at'], 'detail': detail,
                           'actor': 'worker' if e['actor'] == 'worker' else 'buyer'})
        review = c.execute('SELECT stars,comment,at FROM job_reviews WHERE job_id=?', (jid,)).fetchone()
        view = dict(job_view(row, user), events=events, review=dict(review) if review else None)
        if user['role'] == 'buyer':
            view['deliveries'] = [dict(d) for d in c.execute('''
                SELECT id,phase,result,mode,at FROM job_deliveries WHERE job_id=? ORDER BY at''', (jid,))]
        return view


@router.post('/jobs/{jid}/decision')
def decide(jid: str, data: Decision, user=Depends(buyer)):
    with write() as c:
        row = job_for(c, jid, user)
        status = row['status']
        if data.action != 'cancel' and data.delivery_id != row['execution_id']:
            raise HTTPException(409, 'Refresh the delivery before making a decision')
        if row['hold_until'] <= time.time():
            raise HTTPException(409, 'Job hold expired')
        if data.action == 'cancel' and status in ('queued', 'awaiting_approval', 'delivered'):
            c.execute("UPDATE jobs SET status='canceled' WHERE id=?", (jid,))
        elif data.action == 'accept' and status == 'delivered':
            c.execute("UPDATE jobs SET status='completed' WHERE id=?", (jid,))
            event(c, jid, 'worker', 'simulated_settlement', price_cents=json.loads(row['terms'])['price_cents'])
        elif status == 'awaiting_approval' and data.action in ('approve', 'revise'):
            if data.action == 'revise' and (row['revised'] or not data.feedback.strip()):
                raise HTTPException(409, 'One draft revision is included and requires feedback')
            revised = data.action == 'revise'
            worker_feedback = data.feedback
            if not revised and json.loads(row['terms'])['tool'] == 'text_analysis':
                worker_feedback = row['feedback'] if row['revised'] else ''
            c.execute("""UPDATE jobs SET status='queued',phase=?,feedback=?,execution_id=?,
                         revised=MAX(revised,?),hold_until=? WHERE id=?""",
                      ('revision' if revised else 'final', worker_feedback, uid(),
                       revised, int(time.time()) + HOLD_SECONDS, jid))
        else:
            raise HTTPException(409, 'Action is unavailable in the current job state')
        event(c, jid, user['id'], data.action, feedback=data.feedback, delivery_id=data.delivery_id or None)
    return job(jid, user)


@router.post('/jobs/{jid}/review', status_code=201)
def review_job(jid: str, data: Review, user=Depends(buyer)):
    with write() as c:
        row = job_for(c, jid, user)
        if row['status'] != 'completed':
            raise HTTPException(409, 'Accept the delivery before reviewing')
        inserted = c.execute('INSERT OR IGNORE INTO job_reviews VALUES(?,?,?,?)',
                             (jid, data.stars, data.comment, now()))
        if inserted.rowcount != 1:
            raise HTTPException(409, 'Job already reviewed')
        event(c, jid, user['id'], 'review_saved', stars=data.stars)
    return {'saved': True}


@router.get('/memory')
def memories(user=Depends(buyer)):
    with connect() as c:
        return [dict(row) for row in c.execute("""SELECT id,note,created_at FROM buyer_memory
                  WHERE buyer_id=? ORDER BY created_at DESC LIMIT 50""", (user['id'],))]


@router.post('/memory', status_code=201)
def add_note(data: Note, user=Depends(buyer)):
    mid = uid()
    with write() as c:
        c.execute('INSERT INTO buyer_memory VALUES(?,?,?,?)', (mid, user['id'], data.note, now()))
    return {'id': mid}


@router.delete('/memory/{mid}')
def delete_note(mid: str, user=Depends(buyer)):
    with write() as c:
        if c.execute('DELETE FROM buyer_memory WHERE id=? AND buyer_id=?', (mid, user['id'])).rowcount != 1:
            raise HTTPException(404, 'Note not found')
    return {'deleted': True}


@router.get('/ledger')
def ledger(user=Depends(current_user)):
    column = 'buyer_id' if user['role'] == 'buyer' else 'seller_id'
    with connect() as c:
        settled = [json.loads(row['terms'])['price_cents'] for row in c.execute(
            f"SELECT terms FROM jobs WHERE {column}=? AND status='completed'", (user['id'],))]
    return {'simulated_settled_jobs': len(settled), 'simulated_settled_cents': sum(settled),
            'verified_payments_enabled': False, 'verified_payment_count': 0, 'verified_revenue_cents': 0}


def claim():
    with write() as c:
        for row in c.execute(f'SELECT id FROM jobs WHERE status IN {ACTIVE} AND hold_until<=?', (int(time.time()),)):
            c.execute("UPDATE jobs SET status='canceled' WHERE id=?", (row['id'],))
            event(c, row['id'], 'worker', 'hold_expired')
        row = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
        if not row:
            return None
        c.execute("UPDATE jobs SET status='running' WHERE id=?", (row['id'],))
        event(c, row['id'], 'worker', 'started', phase=row['phase'], execution_id=row['execution_id'])
        memory = []
        if row['use_memory']:
            memory = [r['note'] for r in c.execute("""SELECT note FROM buyer_memory WHERE buyer_id=?
                       ORDER BY created_at DESC LIMIT 4""", (row['buyer_id'],))]
        return dict(row) | json.loads(row['terms']), memory


def fail(jid, reason):
    with write() as c:
        if c.execute("UPDATE jobs SET status='failed' WHERE id=? AND status='running'", (jid,)).rowcount:
            event(c, jid, 'worker', 'failed', reason=reason)


async def worker():
    while True:
        claimed = claim()
        if not claimed:
            await asyncio.sleep(.2)
            continue
        job, memory = claimed
        jid = job['id']
        try:
            async with asyncio.timeout(45):
                result, mode = await perform(job, memory)
        except asyncio.CancelledError:
            fail(jid, 'Worker stopped during execution. Submit a new job if needed.')
            raise
        except Exception as exc:
            log.warning('Job %s failed (%s)', jid, type(exc).__name__)
            fail(jid, 'Execution failed. Check provider availability or contact the seller.')
        else:
            status = 'delivered' if job['phase'] == 'final' else 'awaiting_approval'
            with write() as c:
                c.execute('UPDATE jobs SET status=?,result=?,mode=?,hold_until=? WHERE id=?',
                          (status, result, mode, int(time.time()) + HOLD_SECONDS, jid))
                c.execute('INSERT INTO job_deliveries VALUES(?,?,?,?,?,?)',
                          (job['execution_id'], jid, job['phase'], result, mode, now()))
                event(c, jid, 'worker', status, phase=job['phase'], mode=mode,
                      execution_id=job['execution_id'])


@asynccontextmanager
async def dispatch():
    # A single dispatcher per database makes crash recovery safe. Queued jobs
    # survive restarts; interrupted remote calls are never replayed automatically.
    with open(str(database().resolve()) + '.worker.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Run one Agenthood process per database') from exc
        with write() as c:
            for row in c.execute("SELECT id FROM jobs WHERE status='running'"):
                c.execute("UPDATE jobs SET status='failed' WHERE id=?", (row['id'],))
                event(c, row['id'], 'worker', 'failed', reason='Worker interrupted by restart')
        task = asyncio.create_task(worker())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
