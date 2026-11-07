import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from accounts import COOKIE
from app import app
from example_endpoint import Request as EndpointRequest, work
from storage import connect


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENTHOOD_DB', str(tmp_path / 'market.db'))
    monkeypatch.setenv('DARKBLOOM_BASE_URL', '')
    monkeypatch.setenv('OLLAMA_MODEL', '')
    monkeypatch.setenv('ENABLE_COGNEE', 'false')


@pytest.fixture
def site(clean_env):
    with TestClient(app) as client:
        yield client


def account(client, name, role='buyer'):
    r = client.post('/api/auth/register', json={
        'email': f'{name}@example.test', 'role': role, 'password': 'demo-password-123'})
    assert r.status_code == 201, r.text
    return r.json()


def second(name, role='buyer'):
    client = TestClient(app)
    account(client, name, role)
    return client


def request_quote(client, listing='text-analysis', price=100, brief='apple apple orange pear'):
    return client.post('/api/market/quotes', json={
        'listing_id': listing, 'brief': brief, 'budget_cents': price})


def hire(client, quote, **options):
    return client.post('/api/market/jobs', json={'quote_id': quote, **options})


def wait_job(client, jid, state):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        result = client.get('/api/market/jobs/' + jid)
        assert result.status_code == 200, result.text
        data = result.json()
        if data['status'] == state:
            return data
        assert data['status'] != 'failed', data
        time.sleep(.04)
    pytest.fail(f'Expected {state}, last job state {data}')


def decision(client, job, action, feedback=''):
    return client.post('/api/market/jobs/' + job['id'] + '/decision', json={
        'action': action, 'feedback': feedback, 'delivery_id': job['delivery_id']})


def draft(owner, **kwargs):
    listing = dict(name='Term Count Pro', capabilities='Word and frequency analysis',
                   price_cents=60, capacity=1, tool='text_analysis', active=False)
    listing.update(kwargs)
    r = owner.post('/api/market/seller/listings', json=listing)
    assert r.status_code == 201, r.text
    return r.json()['id'], listing


def publish(owner, lid, listing, credential=''):
    route = '/api/market/seller/listings/' + lid
    r = owner.post(route + '/verify', json={'credential': credential})
    assert r.status_code == 200, r.text
    listing['active'] = True
    r = owner.put(route, json=listing)
    assert r.status_code == 200 and r.json()['dispatchable'], r.text


def test_marketplace_hire_approval_and_settlement(site):
    owner = second('seller', 'seller')
    intruder = second('other', 'seller')
    account(site, 'buyer')
    lid, listing = draft(owner)
    route = '/api/market/seller/listings/' + lid
    assert intruder.put(route, json=listing).status_code == 404
    assert site.put(route, json=listing).status_code == 403
    assert owner.put(route, json={**listing, 'active': True}).status_code == 409
    assert request_quote(site, lid).status_code == 404
    publish(owner, lid, listing)
    assert request_quote(site, lid, 59).status_code == 409

    stale = request_quote(site, lid).json()['id']
    listing['price_cents'] = 65
    assert owner.put(route, json=listing).status_code == 200
    assert hire(site, stale).status_code == 409

    rival = second('rival')
    q1 = request_quote(site, lid).json()['id']
    q2 = request_quote(rival, lid).json()['id']
    assert hire(rival, q1).status_code == 404
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = [pool.submit(hire, site, q1), pool.submit(hire, rival, q2)]
        first, second_response = [claim.result() for claim in claims]
    assert sorted([first.status_code, second_response.status_code]) == [201, 409]
    winner, loser, accepted, quote = ((site, rival, first, q1) if first.status_code == 201
                                      else (rival, site, second_response, q2))
    jid = accepted.json()['id']
    assert hire(winner, quote).json()['id'] == jid
    review_path = f'/api/market/jobs/{jid}/review'
    assert winner.post(review_path, json={'stars': 5}).status_code == 409

    job = wait_job(winner, jid, 'awaiting_approval')
    assert json.loads(job['result'])['most_frequent_words'][0] == ['apple', 2]
    assert job['mode'] == 'Algorithmic text analysis'
    assert job['terms']['price_cents'] == 65 and job['terms']['budget_cents'] == 100
    assert loser.get(f'/api/market/jobs/{jid}').status_code == 404
    assert intruder.get(f'/api/market/jobs/{jid}').status_code == 404
    seller_view = owner.get(f'/api/market/jobs/{jid}').json()
    assert 'result' not in seller_view and 'brief' not in seller_view['terms']
    assert owner.post(f'/api/market/jobs/{jid}/decision', json={'action': 'cancel'}).status_code == 403
    assert decision(winner, job, 'revise', 'blue blue green').status_code == 200
    revised = wait_job(winner, jid, 'awaiting_approval')
    assert revised['revised']
    assert decision(winner, job, 'approve').status_code == 409
    assert decision(winner, revised, 'revise', 'another revision').status_code == 409
    assert decision(winner, revised, 'approve', 'buyer approval note').status_code == 200

    final = wait_job(winner, jid, 'delivered')
    assert json.loads(final['result'])['most_frequent_words'][0] == ['blue', 2]
    assert [d['phase'] for d in final['deliveries']] == ['draft', 'revision', 'final']
    assert decision(winner, final, 'accept').status_code == 200
    assert decision(winner, final, 'accept').status_code == 409
    assert winner.post(review_path, json={'stars': 5, 'comment': 'Good counts'}).status_code == 201
    assert winner.post(review_path, json={'stars': 5}).status_code == 409
    ledger = owner.get('/api/market/ledger').json()
    assert ledger['simulated_settled_cents'] == 65
    assert ledger['verified_revenue_cents'] == ledger['verified_payment_count'] == 0
    assert site.post('/api/admin/payment-proof', json={'payment_reference': 'invented'}).status_code == 404
    assert request_quote(loser, lid).status_code == 201
    assert next(x for x in site.get('/api/market/listings').json() if x['id'] == lid)['rating']['n'] == 1


def test_accounts_memory_csrf_and_studio_scope(site):
    assert site.get('/api/music/projects').status_code == 401
    account(site, 'alice')
    bob = second('bob')
    owner = second('no-studio-seller', 'seller')
    assert owner.get('/api/music/projects').status_code == 403
    invalid = site.post('/api/auth/login', json={'email': 'x@y.com', 'password': 'bad'})
    assert invalid.status_code == 422 and 'input' not in invalid.json()['detail'][0]
    note = site.post('/api/market/memory', json={'note': 'private buyer note'}).json()['id']
    assert bob.get('/api/market/memory?owner=alice').json() == []
    assert bob.delete(f'/api/market/memory/{note}').status_code == 404
    assert site.post('/api/market/memory', headers={'Origin': 'https://unrelated.example'},
                     json={'note': 'bad origin'}).status_code == 403
    assert site.delete(f'/api/market/memory/{note}').status_code == 200

    data = {'customer': 'Buyer-controlled label', 'provider': 'demo'}
    pid = site.post('/api/music/projects', json=data).json()['project']['id']
    assert bob.get('/api/music/projects').json() == []
    for suffix in ('', '/files/lyrics.txt', '/deliveries/deadbeef/lyrics.txt'):
        assert bob.get(f'/api/music/projects/{pid}{suffix}').status_code == 404
    assert bob.post(f'/api/music/projects/{pid}/advance').status_code == 404
    assert bob.post(f'/api/music/projects/{pid}/approve', json={'approved': True}).status_code == 404
    assert bob.post(f'/api/music/projects/{pid}/review', json={'agent_id': 'x', 'rating': 5}).status_code == 404

    stolen = site.cookies.get(COOKIE)
    assert site.post('/api/auth/logout').status_code == 200
    assert site.get('/api/auth/me', headers={'Cookie': f'{COOKIE}={stolen}'}).status_code == 401
    assert site.post('/api/auth/login', json={'email': 'alice@example.test',
                                             'password': 'demo-password-123'}).status_code == 200
    assert site.get(f'/api/music/projects/{pid}').status_code == 200


@pytest.fixture
def remote(monkeypatch):
    client = httpx.AsyncClient

    def serve(callback):
        def handle(request):
            assert request.url.host == 'seller.example.test'
            status, result = callback(request.headers, json.loads(request.content))
            return httpx.Response(status, json=result)

        monkeypatch.setattr(httpx, 'AsyncClient',
                            lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))
        return 'https://seller.example.test'

    return serve


def test_external_endpoint_challenge_execution_failure_and_consent(site, remote, monkeypatch):
    secret = 'secret-credential-for-endpoint-testing'
    monkeypatch.setenv('AGENTHOOD_EXAMPLE_SECRET', secret)
    seen = []

    def callback(headers, payload):
        seen.append(payload)
        if payload.get('brief', '').startswith('fail'):
            return 500, {'error': 'private server exception'}
        return 200, work(EndpointRequest(**payload), headers['Authorization'])

    url = remote(callback)
    monkeypatch.setenv('AGENTHOOD_ENDPOINTS', json.dumps({'seller-work': {'url': url, 'secret': secret}}))
    account(site, 'buyer')
    owner = second('remote-seller', 'seller')
    lid, listing = draft(owner, tool='endpoint', endpoint='seller-work')
    verify = '/api/market/seller/listings/' + lid + '/verify'
    assert owner.post(verify, json={'credential': 'fake'}).status_code == 403
    assert not seen
    publish(owner, lid, listing, secret)
    assert seen[0]['operation'] == 'verify'
    assert secret not in site.get('/api/market/listings').text
    quote = request_quote(site, lid, brief='fail this remote delivery').json()['id']
    assert hire(site, quote).status_code == 422
    assert len(seen) == 1
    jid = hire(site, quote, allow_processing=True).json()['id']
    failed = wait_job(site, jid, 'failed')
    assert 'private server exception' not in json.dumps(failed)
    assert site.get('/api/market/ledger').json()['simulated_settled_cents'] == 0
    quote = request_quote(site, lid).json()['id']
    assert hire(site, quote, allow_processing=True, use_memory=True).status_code == 422
    jid = hire(site, quote, allow_processing=True).json()['id']
    result = wait_job(site, jid, 'awaiting_approval')
    assert 'sample endpoint' in result['result']
    assert result['mode'] == 'External endpoint response'
    assert set(seen[-1]) == {'operation', 'job_id', 'phase', 'execution_id', 'brief', 'previous_output', 'feedback'}
    assert decision(site, result, 'cancel').status_code == 200
    monkeypatch.setenv('AGENTHOOD_ENDPOINTS', '{}')
    assert request_quote(site, lid).status_code == 404


def test_model_http_calls_and_opt_in_buyer_memory(clean_env, remote, monkeypatch):
    prompts = []

    def callback(headers, payload):
        assert headers['Authorization'] == 'Bearer test-model-key'
        messages = payload['messages']
        prompts.append(json.loads(messages[-1]['content']))
        return 200, {'choices': [{'message': {'content': f'Live response {len(prompts)}'}}]}

    monkeypatch.setenv('DARKBLOOM_BASE_URL', remote(callback) + '/v1')
    monkeypatch.setenv('DARKBLOOM_MODEL', 'fixture-model')
    monkeypatch.setenv('DARKBLOOM_API_KEY', 'test-model-key')
    with TestClient(app) as client:
        account(client, 'first')
        rival = second('second')
        client.post('/api/market/memory', json={'note': 'private first account'})
        rival.post('/api/market/memory', json={'note': 'private second account'})
        quote = request_quote(client, 'model-writer').json()['id']
        jid = hire(client, quote, allow_processing=True, use_memory=True).json()['id']
        job = wait_job(client, jid, 'awaiting_approval')
        assert job['result'] == 'Live response 1'
        assert job['mode'] == 'Live model · darkbloom'
        assert prompts[0]['buyer_notes'] == ['private first account']
        assert decision(client, job, 'approve', 'expand the summary').status_code == 200
        final = wait_job(client, jid, 'delivered')
        assert final['result'] == 'Live response 2'
        assert prompts[1]['feedback'] == 'expand the summary'
        assert prompts[1]['previous_output'] == 'Live response 1'
        quote = request_quote(rival, 'model-writer').json()['id']
        jid = hire(rival, quote, allow_processing=True).json()['id']
        wait_job(rival, jid, 'awaiting_approval')
        assert prompts[-1]['buyer_notes'] == []


def test_restart_marks_lost_job_failed_and_restores_capacity(clean_env):
    with TestClient(app) as client:
        account(client, 'buyer')
        jar = dict(client.cookies)
        jid = hire(client, request_quote(client).json()['id']).json()['id']
        wait_job(client, jid, 'awaiting_approval')
    with connect() as c:
        c.execute("UPDATE jobs SET status='running' WHERE id=?", (jid,))
    with TestClient(app, cookies=jar) as client:
        failed = wait_job(client, jid, 'failed')
        assert failed['events'][-1]['detail']['reason'] == 'Worker interrupted by restart'
        listing = next(x for x in client.get('/api/market/listings').json() if x['id'] == 'text-analysis')
        assert listing['available_capacity'] == listing['capacity']
