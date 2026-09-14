"""Managed workspace web services with durable launch specifications."""
from __future__ import annotations
import json, os, signal, socket, subprocess, threading, time
from pathlib import Path
from final_version_app.config import WORKDIR

class ServiceManager:
    def __init__(self, root=WORKDIR):
        self.root=Path(root).resolve()
        self.directory=self.root/'.agent_runtime/services'
        self.directory.mkdir(parents=True,exist_ok=True)
        self.path=self.directory/'registry.json'
        self.lock=threading.RLock()
        self.processes={}
        self.records=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}

    @staticmethod
    def allowed_ports():
        legacy=os.getenv('AGENT_PREVIEW_PORTS','')
        if legacy:return {int(p) for p in legacy.split(',') if p.strip()}
        reserved={8000,8090,8091}
        return set(range(1024,65536))-reserved

    def port_policy(self):
        return {'allowed_ports':sorted(self.allowed_ports()) if os.getenv('AGENT_PREVIEW_PORTS') else [],'automatic_port':True,'internal_port_range':[1024,65535],'reserved_ports':[8000,8090,8091],'max_services':int(os.getenv('AGENT_MAX_SERVICES','3'))}

    def _public_port(self,port):
        configured=os.getenv('AGENT_PREVIEW_PUBLIC_PORTS','')
        base=int(os.getenv('AGENT_PREVIEW_PUBLIC_BASE','0'))
        if not configured and not base:return port
        count=int(os.getenv('AGENT_PREVIEW_PUBLIC_COUNT','16'))
        pool=[int(p) for p in configured.split(',')] if configured else list(range(base,base+count))
        existing=self.records.get(str(port),{}).get('public_port')
        occupied={v.get('public_port') for k,v in self.records.items() if k!=str(port) and v.get('enabled')}
        selected=existing if existing in pool and existing not in occupied else next((p for p in pool if p not in occupied),None)
        if selected is None:raise ValueError('Preview capacity reached; stop an unused service')
        for k,v in self.records.items():
            if k!=str(port) and v.get('public_port')==selected:v.pop('public_port',None)
        return selected

    def resolve_public_port(self,public_port):
        with self.lock:
            matches=[v['port'] for v in self.records.values() if v.get('enabled') and v.get('public_port',v['port'])==public_port]
            if len(matches)!=1:raise ValueError('Unknown preview')
            return matches[0]

    def _save(self):
        temp=self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.records,ensure_ascii=False,indent=2),encoding='utf-8')
        temp.replace(self.path)

    @staticmethod
    def listening(port):
        try:
            with socket.create_connection(('127.0.0.1',port),timeout=.3):return True
        except OSError:return False

    def status(self, port=None):
        with self.lock:
            result=[]
            for key, spec in self.records.items():
                if port is not None and int(key)!=port:continue
                proc=self.processes.get(key)
                alive=proc is not None and proc.poll() is None
                result.append({**spec,'running':alive,'listening':alive and self.listening(int(key)),
                    'exit_code':proc.returncode if proc and not alive else None,
                    'preview_url':os.getenv('AGENT_PUBLIC_URL','').rstrip('/')+f'/api/services/{key}/open',
                    'log_path':str(self.directory/f'{key}.log')})
            return result

    def logs(self, port):
        if str(port) not in self.records:raise ValueError('Unknown service')
        path=self.directory/f'{port}.log'
        if not path.exists():return ''
        with path.open('rb') as f:
            f.seek(max(0,path.stat().st_size-16000))
            return f.read().decode('utf-8',errors='replace')

    def start(self, command, cwd=".", port=0):
        if port and port not in self.allowed_ports():raise ValueError("Use a free unprivileged port (1024-65535), excluding runtime ports 8000, 8090 and 8091")
        if not command.strip() or len(command)>4000:raise ValueError('Provide a foreground service command (no nohup or trailing &)')
        target=(self.root/cwd).resolve()
        if not target.is_relative_to(self.root) or not target.is_dir():raise ValueError('cwd must be a directory inside the workspace')
        with self.lock:
            if not port:
                identical=next((v for v in self.records.values() if v.get("enabled") and v["command"]==command and v["cwd"]==str(target.relative_to(self.root))),None)
                if identical:port=identical["port"]
                else:
                    for _ in range(100):
                        with socket.socket() as candidate:
                            candidate.bind(("127.0.0.1",0));port=candidate.getsockname()[1]
                        if port in self.allowed_ports() and not self.records.get(str(port),{}).get("enabled"):break
                    else:raise ValueError("Unable to allocate a free service port")
            key=str(port); proc=self.processes.get(key)
            if proc and proc.poll() is None:
                spec=self.records[key]
                if spec['command']!=command or spec['cwd']!=str(target.relative_to(self.root)):
                    raise ValueError('Port is managed by another command; stop it explicitly first')
                return self.status(port)[0]
            if self.listening(port):raise ValueError('Port is occupied by an unmanaged process; inspect and stop that process first')
            if sum(p.poll() is None for p in self.processes.values())>=int(os.getenv('AGENT_MAX_SERVICES','3')):
                raise ValueError('Service capacity reached; stop an unused service first')
            public_port=self._public_port(port)
            self.records[key]={'port':port,'public_port':public_port,'command':command,'cwd':str(target.relative_to(self.root)),'enabled':True,'created_at':self.records.get(key,{}).get('created_at',time.time())}
            self._save()
            # Application servers must not inherit model keys or the API configuration.
            environment={k:v for k,v in os.environ.items() if k.upper() in {'PATH','HOME','LANG','LC_ALL','TMP','TEMP','TMPDIR','SYSTEMROOT','COMSPEC','WINDIR'}}
            environment.update(PORT=str(port),HOST='0.0.0.0',PYTHONUNBUFFERED='1',PYTHONIOENCODING='utf-8')
            log=self.directory/f'{port}.log'
            if log.exists() and log.stat().st_size>10_000_000:log.replace(log.with_suffix('.previous.log'))
            try:
                with log.open('ab',buffering=0) as out:
                    proc=subprocess.Popen(command.replace("{port}",str(port)),shell=True,cwd=target,env=environment,stdin=subprocess.DEVNULL,
                        stdout=out,stderr=subprocess.STDOUT,start_new_session=os.name!='nt',
                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0)
                self.processes[key]=proc
            except Exception:
                self.records[key]['enabled']=False;self._save();raise
        deadline=time.monotonic()+8
        while time.monotonic()<deadline and proc.poll() is None and not self.listening(port):time.sleep(.1)
        result=self.status(port)[0]
        if result['listening']:
            import httpx
            try:
                response=httpx.get(f'http://127.0.0.1:{port}/',trust_env=False,timeout=5)
                result.update(http_status=response.status_code,http_ready=response.status_code<500)
            except httpx.HTTPError:result.update(http_status=None,http_ready=False)
        else:result['http_ready']=False
        if not result['listening']:result['diagnostic']=self.logs(port)
        return result

    def stop(self, port):
        with self.lock:
            key=str(port)
            if key not in self.records:raise ValueError('Unknown service')
            self.records[key]['enabled']=False;self._save()
            proc=self.processes.get(key)
            if proc and proc.poll() is None:
                if os.name=='nt':
                    subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True)
                    proc.wait(timeout=5)
                else:
                    os.killpg(proc.pid,signal.SIGTERM)
                    try:proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait(timeout=3)
            deadline=time.monotonic()+3
            while time.monotonic()<deadline and self.listening(port):time.sleep(.05)
            return self.status(port)[0]

    def restore(self):
        for spec in list(self.records.values()):
            if spec.get('enabled'):
                try:self.start(spec['command'],spec['cwd'],spec['port'])
                except Exception as exc:print(f"Service {spec['port']} restore failed: {type(exc).__name__}",flush=True)

_manager=None
_manager_lock=threading.Lock()
def get_service_manager():
    global _manager
    with _manager_lock:
        if _manager is None:_manager=ServiceManager()
        return _manager
