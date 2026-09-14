"""Authentication boundaries and durable conversation restoration."""
from time import sleep
from fastapi.testclient import TestClient
from final_version_app.api import create_app
from final_version_app.storage.auth_store import COOKIE
from final_version_app.tests.test_runtime_protocol import build_test_runtime, append_assistant

CREDENTIALS={"username":"owner", "password":"correct-horse-123"}

def make_app(tmp_path):
    def execute(messages, _services, _tools, _system, *, observer, cancellation):
        append_assistant(messages, observer, "reply-"+str(sum(m.type=='human' for m in messages)))
    runtime=build_test_runtime(tmp_path, execute)
    return create_app(lambda:runtime, auth_path=tmp_path/'auth.sqlite3')

def wait_done(client, thread, count=1):
    for _ in range(100):
        events=client.get(f'/api/threads/{thread}/events').json()['events']
        if sum(e['kind']=='turn_completed' for e in events)>=count:return events
        sleep(.01)
    raise AssertionError('Turn did not complete')

def test_every_workspace_endpoint_requires_login(tmp_path):
    client=TestClient(make_app(tmp_path))
    for method,path,body in [('get','/api/threads',None),('get','/api/threads/thr_any/events',None),('post','/api/runs',{'intent':'hello'}),('post','/api/threads/thr_any/turns',{'intent':'hello'}),('post','/api/threads/thr_any/interrupt',{})]:
        response=client.request(method,path,json=body)
        assert response.status_code==401
    assert client.get('/api/auth/status').json()=={'configured':False,'authenticated':False,'username':None}

def test_cookie_logout_password_and_origin_protection(tmp_path):
    app=make_app(tmp_path);client=TestClient(app)
    assert client.post('/api/auth/setup',json=CREDENTIALS,headers={'Origin':'https://evil.example'}).status_code==403
    response=client.post('/api/auth/setup',json=CREDENTIALS)
    assert response.status_code==201
    cookie=response.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'samesite=strict' in cookie and 'max-age=604800' in cookie
    token=client.cookies.get(COOKIE)
    assert client.post('/api/auth/setup',json=CREDENTIALS).status_code==409
    assert client.post('/api/auth/logout').status_code==200
    assert client.get('/api/threads').status_code==401
    assert app.state.auth_store.authenticate(token) is None
    assert client.post('/api/auth/login',json={**CREDENTIALS,'password':'incorrect-123'}).status_code==401
    assert client.post('/api/auth/login',json=CREDENTIALS).status_code==200
    assert client.get('/api/threads').headers['cache-control']=='no-store'
    with app.state.auth_store.connect() as db:
        assert db.execute('SELECT password_hash FROM owner').fetchone()[0]!=CREDENTIALS['password']
        assert db.execute('SELECT digest FROM sessions').fetchone()[0]!=client.cookies.get(COOKIE)

def test_history_and_session_survive_new_app_and_continue(tmp_path):
    first=TestClient(make_app(tmp_path))
    # Represents a workspace chat created before authentication was introduced.
    runtime=first.app.state.runtime_service.runtime
    old=runtime.start_thread(title='Existing chat',metadata={'source':'workspace-api'})
    runtime.run_turn(old.thread_id,'original request')
    assert first.post('/api/auth/setup',json=CREDENTIALS).status_code==201
    assert first.get('/api/threads').json()['threads'][0]['thread_id']==old.thread_id
    second=TestClient(make_app(tmp_path));second.cookies.update(first.cookies)
    assert second.get('/api/auth/status').json()['authenticated'] is True
    events=second.get(f'/api/threads/{old.thread_id}/events').json()['events']
    assert any(e['kind']=='turn_completed' and e['payload']['assistant_text']=='reply-1' for e in events)
    assert second.post(f'/api/threads/{old.thread_id}/turns',json={'intent':'follow up'}).status_code==202
    events=wait_done(second,old.thread_id,2)
    assert events[-1]['payload']['assistant_text']=='reply-2'
    assert second.post('/api/auth/logout').status_code==200
    third=TestClient(make_app(tmp_path))
    assert third.get('/api/threads').status_code==401
    assert third.post('/api/auth/login',json=CREDENTIALS).status_code==200
    assert len(third.get('/api/threads').json()['threads'])==1

def test_expiry_and_bruteforce_limit(tmp_path):
    app=make_app(tmp_path);client=TestClient(app)
    client.post('/api/auth/setup',json=CREDENTIALS)
    with app.state.auth_store.connect() as db:db.execute('UPDATE sessions SET expires=0')
    assert client.get('/api/threads').status_code==401
    for _ in range(10):
        assert client.post('/api/auth/login',json={**CREDENTIALS,'password':'wrong-password'}).status_code==401
    assert client.post('/api/auth/login',json=CREDENTIALS).status_code==429
