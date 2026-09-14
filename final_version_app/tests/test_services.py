"""Real service lifecycle and authenticated proxy regression tests."""
import json, os, socket, sys, time
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from fastapi.testclient import TestClient
from final_version_app.domain.services import ServiceManager
from final_version_app.tests.test_workspace_auth import make_app, CREDENTIALS

def setup_service(tmp_path, monkeypatch):
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    monkeypatch.setenv('AGENT_PREVIEW_PORTS',str(port))
    monkeypatch.setenv('AGENT_PUBLIC_URL','https://testserver')
    (tmp_path/'index.html').write_text('PREVIEW_REAL_HTTP',encoding='utf-8')
    manager=ServiceManager(tmp_path)
    command=f'"{sys.executable}" -m http.server {port} --bind 127.0.0.1'
    return manager,port,command

def test_real_service_restart_and_stop(tmp_path,monkeypatch):
    manager,port,command=setup_service(tmp_path,monkeypatch)
    try:
        result=manager.start(command,'.',port)
        assert result['listening']
        assert httpx.get(f'http://127.0.0.1:{port}',trust_env=False).text=='PREVIEW_REAL_HTTP'
        assert manager.start(command,'.',port)['running']
        manager.stop(port)
        assert not manager.listening(port)
        records=json.loads(manager.path.read_text(encoding='utf-8'));records[str(port)]['enabled']=True
        manager.path.write_text(json.dumps(records),encoding='utf-8')
        restored=ServiceManager(tmp_path)
        try:
            restored.restore()
            assert restored.status(port)[0]['listening']
        finally:restored.stop(port)
    finally:manager.stop(port)

def test_disallowed_port_and_directory(tmp_path,monkeypatch):
    import pytest
    manager,port,command=setup_service(tmp_path,monkeypatch)
    with pytest.raises(ValueError):manager.start(command,'.',8000)
    with pytest.raises(ValueError):manager.start(command,'..',port)

def test_preview_ticket_proxy_and_logout_revocation(tmp_path,monkeypatch):
    manager,port,command=setup_service(tmp_path,monkeypatch)
    monkeypatch.setattr('final_version_app.domain.services.get_service_manager',lambda:manager)
    try:
        manager.start(command,'.',port)
        app=make_app(tmp_path)
        with TestClient(app,base_url='https://testserver') as client:
            assert client.get('/api/services').status_code==401
            assert client.get(f'/internal-preview/{port}/').status_code==401
            assert client.post('/api/auth/setup',json=CREDENTIALS).status_code==201
            opened=client.get(f'/api/services/{port}/open',follow_redirects=False)
            assert opened.status_code==303
            target=urlsplit(opened.headers['location'])
            handoff=f'/internal-preview/{port}{target.path}?{target.query}'
            authorized=client.get(handoff,follow_redirects=False)
            assert authorized.status_code==303
            assert client.get(handoff,follow_redirects=False).status_code==401
            page=client.get(f'/internal-preview/{port}/')
            assert page.status_code==200 and page.text=='PREVIEW_REAL_HTTP'
            assert client.post(f'/internal-preview/{port}/',headers={'origin':'https://evil.example'}).status_code==403
            assert client.get('/internal-preview/8000/').status_code==404
            assert client.post('/api/auth/logout').status_code==200
            assert client.get(f'/internal-preview/{port}/').status_code==401
    finally:manager.stop(port)

def test_real_websocket_preview_and_origin_boundary(tmp_path,monkeypatch):
    import pytest
    from starlette.websockets import WebSocketDisconnect
    manager,port,_=setup_service(tmp_path,monkeypatch)
    monkeypatch.setattr('final_version_app.domain.services.get_service_manager',lambda:manager)
    (tmp_path/'echo.py').write_text('from websockets.sync.server import serve\ndef echo(ws):\n for msg in ws: ws.send(msg)\nwith serve(echo,"127.0.0.1",'+str(port)+') as server:server.serve_forever()\n')
    try:
        manager.start(f'"{sys.executable}" echo.py','.',port)
        app=make_app(tmp_path)
        with TestClient(app,base_url='https://testserver') as client:
            client.post('/api/auth/setup',json=CREDENTIALS)
            opened=client.get(f'/api/services/{port}/open',follow_redirects=False)
            target=urlsplit(opened.headers['location'])
            client.get(f'/internal-preview/{port}{target.path}?{target.query}',follow_redirects=False)
            with client.websocket_connect(f'wss://testserver/internal-preview/{port}/echo',headers={'origin':f'https://testserver:{port}'}) as socket:
                socket.send_text('real-ws-echo');assert socket.receive_text()=='real-ws-echo'
                socket.send_bytes(b'bytes');assert socket.receive_bytes()==b'bytes'
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(f'wss://testserver/internal-preview/{port}/echo',headers={'origin':'https://evil.example'}):pass
            client.post('/api/auth/logout')
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(f'wss://testserver/internal-preview/{port}/echo',headers={'origin':f'https://testserver:{port}'}):pass
    finally:manager.stop(port)

def test_dynamic_ports_public_mapping_reuse_and_restore(tmp_path,monkeypatch):
    monkeypatch.delenv('AGENT_PREVIEW_PORTS',raising=False)
    monkeypatch.setenv('AGENT_PREVIEW_PUBLIC_PORTS','31000,31001')
    monkeypatch.setenv('AGENT_PREVIEW_PUBLIC_COUNT','2')
    manager=ServiceManager(tmp_path)
    for name in ['first','second','third']:
        folder=tmp_path/name;folder.mkdir();(folder/'index.html').write_text(name)
    command=f'"{sys.executable}" -m http.server {{port}} --bind 127.0.0.1'
    active=[]
    try:
        first=manager.start(command,'first');active.append(first['port'])
        second=manager.start(command,'second');active.append(second['port'])
        assert first['port']!=second['port'] and first['public_port']!=second['public_port']
        assert first['http_ready'] and second['http_ready']
        assert manager.start(command,'first')['port']==first['port']
        assert manager.resolve_public_port(first['public_port'])==first['port']
        manager.stop(first['port']);active.remove(first['port'])
        third=manager.start(command,'third');active.append(third['port'])
        assert third['public_port']==first['public_port']
        assert manager.resolve_public_port(third['public_port'])==third['port']
        for port in active:manager.stop(port)
        for port in active:manager.records[str(port)]['enabled']=True
        manager._save();restored=ServiceManager(tmp_path)
        try:
            restored.restore()
            assert len([v for v in restored.status() if v['listening']])==2
            assert restored.resolve_public_port(third['public_port'])==third['port']
        finally:
            for port in active:restored.stop(port)
    finally:
        for port in active:manager.stop(port)


def test_dynamic_public_proxy_auth_and_root_assets(tmp_path,monkeypatch):
    monkeypatch.delenv('AGENT_PREVIEW_PORTS',raising=False)
    monkeypatch.setenv('AGENT_PREVIEW_PUBLIC_PORTS','31000,31001')
    monkeypatch.setenv('AGENT_PUBLIC_URL','https://testserver')
    manager=ServiceManager(tmp_path)
    monkeypatch.setattr('final_version_app.domain.services.get_service_manager',lambda:manager)
    (tmp_path/'index.html').write_text('<link href="/style.css">generic')
    (tmp_path/'style.css').write_text('body {color: blue}')
    app=make_app(tmp_path)
    with TestClient(app,base_url='https://testserver') as client:
        assert client.post('/api/services',json={'command':'echo nope'}).status_code==401
        client.post('/api/auth/setup',json=CREDENTIALS)
        service=client.post('/api/services',json={'command':f'"{sys.executable}" -m http.server {{port}}','cwd':'.'}).json()
        port=service['port'];public=service['public_port']
        try:
            assert client.get(f'/service-preview/{public}/').status_code==401
            target=urlsplit(client.get(f'/api/services/{port}/open',follow_redirects=False).headers['location'])
            assert target.port==public
            assert client.get(f'/service-preview/{public}{target.path}?{target.query}',follow_redirects=False).status_code==303
            assert client.get(f'/service-preview/{public}/style.css').text=='body {color: blue}'
            assert client.get(f'/service-preview/{public+1}/').status_code==404
            client.post('/api/auth/logout')
            assert client.get(f'/service-preview/{public}/').status_code==401
        finally:manager.stop(port)
