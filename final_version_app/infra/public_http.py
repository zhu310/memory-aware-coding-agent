"""Bounded public HTTP fetching with DNS pinning and per-hop SSRF checks."""
import http.client,ipaddress,socket,ssl,time
from urllib.parse import urlsplit,urljoin

def public_connection(address,timeout=15,source_address=None):
    host,port=address
    answers=socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)
    if not answers or any(not ipaddress.ip_address(a[4][0]).is_global for a in answers):
        raise ValueError('Only public internet addresses are allowed')
    error=None
    for family,kind,proto,_,target in answers:
        sock=socket.socket(family,kind,proto);sock.settimeout(timeout)
        try:sock.connect(target);return sock
        except OSError as exc:error=exc;sock.close()
    raise error or OSError('No reachable address')

def fetch_public(url,max_bytes=4_000_000,accept='text/html,text/plain,application/json'):
    deadline=time.monotonic()+30
    for _ in range(5):
        parsed=urlsplit(url)
        if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('An HTTP(S) URL without credentials is required')
        port=parsed.port or (443 if parsed.scheme=='https' else 80)
        if port not in {80,443}:raise ValueError('Only public HTTP(S) ports are allowed')
        if parsed.scheme=='https':connection=http.client.HTTPSConnection(parsed.hostname,port,timeout=15,context=ssl.create_default_context())
        else:connection=http.client.HTTPConnection(parsed.hostname,port,timeout=15)
        connection._create_connection=public_connection
        try:
            path=parsed.path or '/'
            if parsed.query:path+='?'+parsed.query
            connection.request('GET',path,headers={'User-Agent':'CodingAgentResearch/1.0','Accept':accept,'Accept-Encoding':'identity'})
            response=connection.getresponse()
            if response.status in {301,302,303,307,308}:
                location=response.getheader('Location')
                if not location:raise ValueError('Redirect has no target')
                url=urljoin(url,location);continue
            if response.status>=400:raise ValueError(f'Website returned HTTP {response.status}')
            if response.getheader('Content-Encoding','identity') not in {'','identity'}:raise ValueError('Unsupported compressed response')
            length=response.getheader('Content-Length')
            if length and int(length)>max_bytes:raise ValueError('Remote document exceeds the size limit')
            parts=[];size=0
            while True:
                if time.monotonic()>deadline:raise ValueError('Remote document exceeded total read deadline')
                chunk=response.read1(min(65536,max_bytes+1-size))
                if not chunk:break
                parts.append(chunk);size+=len(chunk)
                if size>max_bytes:raise ValueError('Remote document exceeds the size limit')
            content=b''.join(parts)
            return {'url':url,'content_type':response.getheader('Content-Type',''),'content':content}
        finally:connection.close()
    raise ValueError('Too many redirects')
