"""Vision provider contracts, idempotent image jobs and public HTTP boundaries."""
import io,json,time
import httpx,pytest
from PIL import Image
from final_version_app.storage.assets import AssetStore
from final_version_app.domain import vision
from final_version_app.infra.public_http import public_connection,fetch_public

def png_bytes():
    out=io.BytesIO();Image.new('RGB',(120,80),'blue').save(out,format='PNG');return out.getvalue()

def wait_job(jobs,job):
    jobs.futures[job['id']].result(timeout=10);return jobs.status(job['id'])

def test_vision_uses_image_data_and_reports_usage(tmp_path,monkeypatch):
    store=AssetStore(tmp_path);asset=store.create('chart.png',png_bytes());store.jobs[asset['id']].result(timeout=10)
    monkeypatch.setattr(vision,'get_asset_store',lambda:store);monkeypatch.setenv('ALIYUN_API_KEY','test-secret')
    seen=[]
    class Client:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,url,**kw):
            seen.append(kw['json'])
            return httpx.Response(200,json={'choices':[{'message':{'content':'The image is blue.'}}],'usage':{'prompt_tokens':12,'completion_tokens':5,'total_tokens':17}})
    monkeypatch.setattr(vision.httpx,'Client',Client)
    result=vision.understand_image(asset['id'],'What color is this?')
    assert seen[0]['messages'][1]['content'][0]['image_url']['url'].startswith('data:image/png;base64,')
    assert result['usage']['total_tokens']==17 and 'blue' in result['answer']
    assert 'test-secret' not in json.dumps(result)
    store.executor.shutdown()

def test_async_generation_persists_result_and_deduplicates(tmp_path,monkeypatch):
    store=AssetStore(tmp_path);jobs=vision.ImageJobs(store);monkeypatch.setenv('ALIYUN_API_KEY','test-secret');calls=[]
    class Client:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,url,**kw):
            calls.append(url);assert kw['headers']['X-DashScope-Async']=='enable'
            assert '/image-generation/generation' in url
            return httpx.Response(200,json={'output':{'task_id':'vendor-task'}})
        def get(self,url,**kw):return httpx.Response(200,request=httpx.Request('GET',url),json={'output':{'task_status':'SUCCEEDED','choices':[{'message':{'content':[{'image':'https://example.com/result.png'}]}}]},'usage':{'output_image_count':1}})
    monkeypatch.setattr(vision.httpx,'Client',Client)
    monkeypatch.setattr(vision,'fetch_public',lambda *args,**kw:{'content':png_bytes()})
    job=jobs.start('A blue square','request-001');result=wait_job(jobs,job)
    assert result['state']=='completed'
    file_id=result['result']['assets'][0]['file_id'];assert store.path(file_id).read_bytes()==png_bytes()
    assert jobs.start('A blue square','request-001')['id']==job['id'];assert len(calls)==1
    with pytest.raises(ValueError):jobs.start('Different prompt','request-001')
    recovered=vision.ImageJobs(store);assert recovered.status(job['id'])['result']['assets'][0]['file_id']==file_id
    jobs.executor.shutdown();recovered.executor.shutdown();store.executor.shutdown()

def test_ambiguous_submit_is_not_retried(tmp_path,monkeypatch):
    store=AssetStore(tmp_path);jobs=vision.ImageJobs(store);monkeypatch.setenv('ALIYUN_API_KEY','test-secret');calls=[]
    class Client:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,*args,**kw):calls.append(1);raise httpx.ReadTimeout('Connection lost after submit')
    monkeypatch.setattr(vision.httpx,'Client',Client)
    job=jobs.start('A blue square','ambiguous');assert wait_job(jobs,job)['state']=='unknown'
    jobs.start('A blue square','ambiguous');jobs.recover();assert len(calls)==1
    jobs.executor.shutdown();store.executor.shutdown()

def test_private_addresses_and_unsafe_urls_are_blocked(monkeypatch):
    import socket
    monkeypatch.setattr(socket,'getaddrinfo',lambda *args,**kw:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',80))])
    with pytest.raises(ValueError,match='public'):public_connection(('rebind.example',80))
    for url in ['file:///etc/passwd','http://user:pass@example.com','http://example.com:8000/']:
        with pytest.raises(ValueError):fetch_public(url)

def test_image_job_wait_returns_when_background_job_finishes(tmp_path,monkeypatch):
    import threading,time
    from final_version_app.domain.vision import ImageJobs
    from final_version_app.storage.assets import AssetStore
    store=AssetStore(tmp_path);jobs=ImageJobs(store)
    state={'id':'synthetic','state':'running'}
    monkeypatch.setattr(jobs,'status',lambda job_id:dict(state))
    worker=threading.Thread(target=lambda:(time.sleep(.05),state.update(state='completed')));worker.start()
    started=time.monotonic();result=jobs.check('synthetic',2)
    assert result['state']=='completed' and time.monotonic()-started<1.5
    assert jobs.check('synthetic',0)['state']=='completed'
    worker.join();jobs.executor.shutdown();store.executor.shutdown()
