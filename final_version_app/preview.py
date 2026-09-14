"""Isolated-origin previews with one-use handoff tickets and revocable sessions."""
import hashlib, html, os, secrets, time
from http.cookies import SimpleCookie
from urllib.parse import urlsplit
import httpx
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask
from final_version_app.storage.auth_store import COOKIE

HOP={'connection','keep-alive','proxy-authenticate','proxy-authorization','te','trailer','transfer-encoding','upgrade','host','content-length'}
def private_cookie(name):return name.startswith(('agent_workspace_', 'agent_preview_'))

def install_preview_routes(app, auth, manager):
    with auth.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS preview_tokens (digest TEXT PRIMARY KEY, parent TEXT NOT NULL, port INTEGER NOT NULL, expires REAL NOT NULL, ticket INTEGER NOT NULL)')

    def mint(parent,port,ticket):
        token=secrets.token_urlsafe(32)
        with auth.connect() as db:
            db.execute('DELETE FROM preview_tokens WHERE expires<=?',(time.time(),))
            db.execute('INSERT INTO preview_tokens VALUES (?,?,?,?,?)',(auth.digest(token),parent,port,time.time()+(60 if ticket else 3600),int(ticket)))
        return token

    def validate(token,port,ticket=False):
        if not token or len(token)>256:return None
        with auth.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT p.parent FROM preview_tokens p JOIN sessions s ON s.digest=p.parent WHERE p.digest=? AND p.port=? AND p.ticket=? AND p.expires>? AND s.expires>?',
                (auth.digest(token),port,int(ticket),time.time(),time.time())).fetchone()
            if row and ticket:db.execute('DELETE FROM preview_tokens WHERE digest=?',(auth.digest(token),))
            return row[0] if row else None

    def registered(port):
        records=manager.status(port)
        if port not in manager.allowed_ports() or not records or not records[0]['enabled']:
            raise HTTPException(404,'Preview service is not registered.')
        return records[0]

    @app.get('/api/services')
    def list_services():return {'services':manager.status(),**manager.port_policy()}

    @app.get('/api/services/{port}/open')
    def open_service(port:int,request:Request):
        spec=registered(port)
        public=os.getenv('AGENT_PUBLIC_URL','').rstrip('/')
        parsed=urlsplit(public)
        if parsed.scheme!='https' or not parsed.hostname:raise HTTPException(503,'HTTPS preview host is not configured.')
        token=mint(auth.digest(request.cookies[COOKIE]),port,True)
        response=RedirectResponse(f'https://{parsed.hostname}:{spec.get("public_port",port)}/__agent_open?ticket={token}',status_code=303)
        response.headers['Referrer-Policy']='no-referrer'
        return response

    from fastapi import WebSocket, WebSocketDisconnect
    @app.websocket('/internal-preview/{port}/{path:path}')
    async def websocket_proxy(websocket: WebSocket, port: int, path: str):
        import asyncio
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed
        try: registered(port)
        except HTTPException:
            await websocket.close(code=1008);return
        expected=f"https://{urlsplit(os.getenv('AGENT_PUBLIC_URL','')).hostname}:{registered(port).get('public_port',port)}"
        if websocket.headers.get('origin') != expected or not validate(websocket.cookies.get(f'agent_preview_{port}'),port):
            await websocket.close(code=1008);return
        cookies='; '.join(f'{k}={v}' for k,v in websocket.cookies.items() if not private_cookie(k))
        headers={'Cookie':cookies} if cookies else {}
        protocols=[p.strip() for p in websocket.headers.get('sec-websocket-protocol','').split(',') if p.strip()]
        target=str(httpx.URL(f'ws://127.0.0.1:{port}/').copy_with(path='/'+path,query=websocket.url.query.encode()))
        try:
            async with connect(target,additional_headers=headers,origin=expected,subprotocols=protocols or None,max_size=1_048_576,open_timeout=5,proxy=None) as upstream:
                await websocket.accept(subprotocol=upstream.subprotocol)
                async def to_upstream():
                    while True:
                        message=await websocket.receive()
                        if message['type']=='websocket.disconnect':return
                        await upstream.send(message.get('bytes') if message.get('bytes') is not None else message.get('text',''))
                async def to_browser():
                    async for message in upstream:
                        if isinstance(message,bytes):await websocket.send_bytes(message)
                        else:await websocket.send_text(message)
                tasks=[asyncio.create_task(to_upstream()),asyncio.create_task(to_browser())]
                try:await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:task.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)
        except (OSError,ConnectionClosed,WebSocketDisconnect):pass
        finally:
            try:await websocket.close()
            except RuntimeError:pass

    @app.api_route('/internal-preview/{port}/{path:path}',methods=['GET','HEAD','POST','PUT','PATCH','DELETE','OPTIONS'])
    async def proxy(port:int,path:str,request:Request):
        registered(port)
        cookie=f'agent_preview_{port}'
        if path=='__agent_open':
            parent=validate(request.query_params.get('ticket'),port,True)
            if not parent:raise HTTPException(401,'Preview link expired; reopen it from the workspace.')
            response=RedirectResponse('/',status_code=303)
            response.set_cookie(cookie,mint(parent,port,False),httponly=True,secure=True,samesite='strict',max_age=3600,path='/')
            response.headers['Referrer-Policy']='no-referrer'
            response.headers['Cache-Control']='no-store'
            return response
        if not validate(request.cookies.get(cookie),port):
            public=html.escape(os.getenv('AGENT_PUBLIC_URL',''),quote=True)
            return HTMLResponse(f'<meta charset="utf-8"><p>请先登录工作台，然后打开应用预览。</p><a href="{public}/api/services/{port}/open">打开预览</a>',status_code=401,headers={'Cache-Control':'no-store'})
        if request.method not in {'GET','HEAD','OPTIONS'}:
            origin=request.headers.get('origin')
            expected=f"https://{urlsplit(os.getenv('AGENT_PUBLIC_URL','')).hostname}:{registered(port).get('public_port',port)}"
            if origin and origin!=expected:raise HTTPException(403,'Preview origin not allowed.')
        # Never forward the workspace session or another preview's cookie.
        cookies='; '.join(f'{k}={v}' for k,v in request.cookies.items() if not private_cookie(k))
        headers={k:v for k,v in request.headers.items() if k.lower() not in HOP|{'cookie','forwarded','x-forwarded-host','x-forwarded-proto','x-forwarded-for'}}
        if cookies:headers['cookie']=cookies
        headers['host']=request.headers.get('host',f'127.0.0.1:{port}')
        headers['accept-encoding']='identity'
        client=httpx.AsyncClient(trust_env=False,timeout=httpx.Timeout(60,connect=3),follow_redirects=False)
        # Fixed loopback + registered integer port prevents arbitrary upstream URL access.
        target=httpx.URL(f'http://127.0.0.1:{port}/').copy_with(path='/'+path,query=request.url.query.encode())
        try:
            upstream=await client.send(client.build_request(request.method,target,headers=headers,content=request.stream()),stream=True)
        except httpx.HTTPError:
            await client.aclose()
            raise HTTPException(502,'Preview service is unavailable; inspect service_status and its logs.')
        async def close():
            await upstream.aclose();await client.aclose()
        response=StreamingResponse(upstream.aiter_raw(),status_code=upstream.status_code,background=BackgroundTask(close))
        for name,value in upstream.headers.multi_items():
            if name.lower() in HOP|{'set-cookie','access-control-allow-origin','access-control-allow-credentials','x-frame-options'}:continue
            response.headers.append(name,value)
        for value in upstream.headers.get_list('set-cookie'):
            parsed=SimpleCookie()
            try:parsed.load(value)
            except Exception:continue
            if parsed and all(not private_cookie(k) for k in parsed):response.headers.append('set-cookie',value)
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['X-Frame-Options']='DENY'
        return response

    @app.api_route('/service-preview/{public_port}/{path:path}',methods=['GET','HEAD','POST','PUT','PATCH','DELETE','OPTIONS'])
    async def public_proxy(public_port:int,path:str,request:Request):
        try:port=manager.resolve_public_port(public_port)
        except ValueError:raise HTTPException(404,'Unknown preview')
        return await proxy(port,path,request)

    @app.websocket('/service-preview/{public_port}/{path:path}')
    async def public_websocket(websocket:WebSocket,public_port:int,path:str):
        try:port=manager.resolve_public_port(public_port)
        except ValueError:
            await websocket.close(code=1008);return
        await websocket_proxy(websocket,port,path)

    from pydantic import BaseModel, Field
    class LaunchRequest(BaseModel):
        command:str=Field(min_length=1,max_length=4000)
        cwd:str='.'
        port:int=Field(default=0,ge=0,le=65535)

    @app.post('/api/services')
    def launch_service(payload:LaunchRequest):
        try:return manager.start(payload.command,payload.cwd,payload.port)
        except ValueError as exc:raise HTTPException(409,str(exc))

    @app.post('/api/services/{port}/stop')
    def stop_service(port:int):
        try:return manager.stop(port)
        except ValueError as exc:raise HTTPException(404,str(exc))

    @app.get('/api/services/{port}/logs')
    def service_logs(port:int):
        try:return {'port':port,'logs':manager.logs(port)}
        except ValueError as exc:raise HTTPException(404,str(exc))
