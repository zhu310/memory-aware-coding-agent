"""Account routing and global capacity boundaries, with independent fake backends."""
import json
import httpx
from fastapi.testclient import TestClient
from final_version_app.gateway import create_gateway

def gateway(tmp_path,monkeypatch):
    slots={'owner':{'username':'admin','url':'http://owner'},'beta01':{'username':'betaadmin01','url':'http://beta01'}}
    config=tmp_path/'slots.json';config.write_text(json.dumps(slots))
    state={'owner':0,'beta01':0}
    async def upstream(request):
        host=request.url.host
        expected='admin' if host=='owner' else 'betaadmin01'
        authenticated=f'agent_workspace_session={host}-secret' in request.headers.get('cookie','')
        if request.url.path=='/api/auth/login':
            body=json.loads(request.content)
            if body['username']!=expected or body['password']!='correct-password':return httpx.Response(401,json={'detail':'Incorrect credentials'})
            return httpx.Response(200,json={'authenticated':True,'configured':True,'username':expected},headers={'set-cookie':f'agent_workspace_session={host}-secret; HttpOnly; Secure; Path=/api'})
        if request.url.path=='/api/auth/status':return httpx.Response(200,json={'authenticated':authenticated,'username':expected if authenticated else None})
        if request.url.path=='/api/health':return httpx.Response(200,json={'queue':{'inflight':state[host]}})
        if not authenticated:return httpx.Response(401,json={'detail':'Please sign in'})
        if request.url.path=='/api/runs':state[host]+=1;return httpx.Response(202,json={'thread_id':host+'-thread'})
        return httpx.Response(200,json={'workspace':host})
    monkeypatch.setenv('AGENT_PUBLIC_URL','https://testserver')
    monkeypatch.setenv('AGENT_GLOBAL_MAX_RUNS','1')
    async def transport(request):
        response=await upstream(request)
        return httpx.Response(response.status_code,headers=response.headers,stream=httpx.ByteStream(response.content))
    app=create_gateway(config,transport=httpx.MockTransport(transport))
    return TestClient(app,base_url='https://testserver'),state

def test_account_login_switch_and_forged_slot(tmp_path,monkeypatch):
    client,state=gateway(tmp_path,monkeypatch)
    assert client.get('/api/threads').status_code==401
    response=client.post('/api/auth/login',json={'username':'betaadmin01','password':'correct-password'})
    assert response.status_code==200
    assert client.get('/api/threads').json()['workspace']=='beta01'
    assert client.get('/api/admin/accounts').status_code==403
    forged=client.get('/api/threads',headers={'cookie':'agent_workspace_slot=owner; agent_workspace_session=beta01-secret'})
    assert forged.status_code==401
    assert client.post('/api/auth/login',json={'username':'admin','password':'correct-password'}).status_code==200
    assert client.get('/api/threads').json()['workspace']=='owner'
    assert len(client.get('/api/admin/accounts').json()['accounts'])==2

def test_capacity_and_cross_origin(tmp_path,monkeypatch):
    client,state=gateway(tmp_path,monkeypatch)
    assert client.post('/api/runs',json={'intent':'test'}).status_code==401
    assert client.post('/api/auth/login',json={'username':'admin','password':'correct-password'},headers={'origin':'https://testserver:2478'}).status_code==403
    client.post('/api/auth/login',json={'username':'admin','password':'correct-password'})
    assert client.post('/api/runs',json={'intent':'test'}).status_code==202
    assert client.post('/api/runs',json={'intent':'second'}).status_code==429
    state['owner']=0
    assert client.post('/api/runs',json={'intent':'after completion'}).status_code==202
