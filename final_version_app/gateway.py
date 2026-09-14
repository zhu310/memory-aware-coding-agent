"""Stateless account routing gateway; never executes agent tools or holds Docker access.

Each account is the owner of a distinct runtime container and persistent volume.
The slot cookie is routing metadata only: every request is authenticated again by
that runtime's independent session database. Forging a slot grants no access.
"""
from __future__ import annotations
import asyncio,json,os
from pathlib import Path
import httpx
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import JSONResponse,StreamingResponse
from starlette.background import BackgroundTask

SLOT_COOKIE='agent_workspace_slot'
HOP={'host','connection','keep-alive','proxy-authenticate','proxy-authorization','te','trailer','transfer-encoding','upgrade','content-length'}

def create_gateway(config_path=None,transport=None):
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    path=Path(config_path or os.environ['AGENT_GATEWAY_CONFIG'])
    slots=json.loads(path.read_text(encoding='utf-8'))
    lock=asyncio.Lock()
    def client():return httpx.AsyncClient(trust_env=False,timeout=httpx.Timeout(90,connect=3),follow_redirects=False,transport=transport)
    def slot_for(request):return request.cookies.get(SLOT_COOKIE,'owner') if request.cookies.get(SLOT_COOKIE,'owner') in slots else 'owner'

    @app.get('/gateway-health')
    def health():return {'status':'ok','accounts':len(slots)}

    @app.api_route('/api/{route:path}',methods=['GET','HEAD','POST','PUT','PATCH','DELETE','OPTIONS'])
    async def dispatch(route:str,request:Request):
        public=os.getenv('AGENT_PUBLIC_URL','https://103.117.123.28').rstrip('/')
        origin=request.headers.get('origin')
        if request.method not in {'GET','HEAD','OPTIONS'} and origin and origin!=public:
            raise HTTPException(403,'Origin not allowed.')
        slot=slot_for(request)
        payload=None
        if route=='auth/login' and request.method=='POST':
            try:payload=await request.json()
            except Exception:raise HTTPException(422,'Invalid login payload.')
            if not isinstance(payload,dict):raise HTTPException(422,'Invalid login payload.')
            username=payload.get('username')
            slot=next((key for key,value in slots.items() if value['username']==username),'owner')
        headers={key:value for key,value in request.headers.items() if key.lower() not in HOP}
        headers['host']=request.headers.get('host', '103.117.123.28')
        headers['x-forwarded-proto']='https'
        if route=='admin/accounts':
            async with client() as http:
                response=await http.get(slots['owner']['url']+'/api/auth/status',headers=headers)
            if slot!='owner' or not response.json().get('authenticated'):raise HTTPException(403,'Owner administrator required.')
            return {'accounts':[{'username':value['username'],'role':'workspace_admin','isolated_workspace':True} for value in slots.values()]}
        target=slots[slot]['url']+'/api/'+route
        if request.url.query:target+='?'+request.url.query
        http=client()
        async def send():
            if payload is not None:body=json.dumps(payload).encode()
            else:body=request.stream()
            return await http.send(http.build_request(request.method,target,headers=headers,content=body),stream=True)
        try:
            if request.method=='POST' and (route=='runs' or route.endswith('/turns')):
                async with lock:
                    # Verify identity before consuming shared execution capacity.
                    identity=await http.get(slots[slot]['url']+'/api/auth/status',headers=headers)
                    if not identity.json().get('authenticated'):raise HTTPException(401,'Please sign in.')
                    async def inflight(value):
                        try:
                            state=await http.get(value['url']+'/api/health')
                            return int(state.json().get('queue',{}).get('inflight',0))
                        except (httpx.HTTPError,ValueError):return 0
                    total=sum(await asyncio.gather(*(inflight(v) for v in slots.values())))
                    if total>=int(os.getenv('AGENT_GLOBAL_MAX_RUNS','2')):raise HTTPException(429,'当前有任务正在执行，请稍后重试。')
                    upstream=await send()
            else:upstream=await send()
        except HTTPException:
            await http.aclose();raise
        except httpx.HTTPError:
            await http.aclose();raise HTTPException(503,'工作区服务暂时不可用，正在等待恢复。')
        async def close():
            await upstream.aclose();await http.aclose()
        response=StreamingResponse(upstream.aiter_raw(),status_code=upstream.status_code,background=BackgroundTask(close))
        for key,value in upstream.headers.multi_items():
            if key.lower() not in HOP:response.headers.append(key,value)
        if route=='auth/login' and upstream.status_code==200:
            response.set_cookie(SLOT_COOKIE,slot,max_age=7*24*3600,httponly=True,secure=True,samesite='strict',path='/api')
        response.headers['Cache-Control']='no-store'
        return response
    return app

if __name__=='__main__':
    import uvicorn
    uvicorn.run(create_gateway(),host='0.0.0.0',port=8090)
