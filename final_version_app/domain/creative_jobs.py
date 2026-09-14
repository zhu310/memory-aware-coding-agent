"""Durable owned Office/music jobs; retries never blindly resubmit billed requests."""
from __future__ import annotations
import base64,json,os,threading,time,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import httpx
from final_version_app.storage.assets import get_asset_store

class CreativeJobs:
    def __init__(self,store=None):
        self.store=store or get_asset_store();self.lock=threading.RLock();self.futures={};self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='creative-job')
        with self.store.connect() as db:db.execute('CREATE TABLE IF NOT EXISTS creative_jobs (id TEXT PRIMARY KEY,request_key TEXT UNIQUE,payload TEXT,state TEXT,result TEXT,error TEXT,created REAL,updated REAL)')
    def status(self,job_id):
        with self.store.connect() as db:row=db.execute('SELECT * FROM creative_jobs WHERE id=?',(job_id,)).fetchone()
        if not row:raise ValueError('Unknown job in this workspace')
        item=dict(row);item.pop('request_key');item['payload']=json.loads(item['payload']);item['result']=json.loads(item['result']) if item['result'] else None;return item
    def list(self):
        with self.store.connect() as db:ids=[r[0] for r in db.execute('SELECT id FROM creative_jobs ORDER BY created DESC LIMIT 50')]
        return [self.status(i) for i in ids]
    def update(self,job_id,**fields):
        fields['updated']=time.time()
        with self.store.connect() as db:db.execute('UPDATE creative_jobs SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),job_id))
    def start(self,kind,request_id,**params):
        if not 1<=len(request_id)<=128:raise ValueError('Stable request_id required')
        if kind=='office':
            if not os.getenv('AGENT_CREATIVE_URL'):raise ValueError('Office rendering worker is not configured')
            meta=self.store.metadata(params['file_id']);suffix=Path(meta['name']).suffix.lower()
            if suffix not in {'.docx','.xlsx'} or params.get('format','pdf') not in {'pdf','xlsx'}:raise ValueError('Office requires DOCX/XLSX and PDF/XLSX output')
            if params.get('format')=='xlsx' and suffix!='.xlsx':raise ValueError('Only XLSX can be recalculated')
        elif kind=='music':
            if not os.getenv('ELEVENLABS_API_KEY'):raise ValueError('Music generation is not configured; provide an ElevenLabs API key. No request was submitted.')
            if not params.get('prompt','').strip() or len(params['prompt'])>4100 or not 3<=params.get('seconds',30)<=180:raise ValueError('Provide a prompt and 3-180 seconds')
        else:raise ValueError('Unknown creative job kind')
        payload=json.dumps({'kind':kind,**params},sort_keys=True,ensure_ascii=False)
        with self.lock:
            with self.store.connect() as db:
                row=db.execute('SELECT id,payload FROM creative_jobs WHERE request_key=?',(request_id,)).fetchone()
                if row:
                    if row['payload']!=payload:raise ValueError('request_id already used with different parameters')
                    return self.status(row['id'])
                count=db.execute("SELECT COUNT(*) FROM creative_jobs WHERE state IN ('queued','running','saving','cancel_requested')").fetchone()[0]
                if count>=3:raise ValueError('Creative queue is full')
                if kind=='music' and db.execute('SELECT COUNT(*) FROM creative_jobs WHERE created>? AND payload LIKE ?',(time.time()-86400,'%"kind": "music"%')).fetchone()[0]>=10:raise ValueError('Daily music quota reached')
                job='job_'+uuid.uuid4().hex;now=time.time();db.execute('INSERT INTO creative_jobs VALUES (?,?,?,\'queued\',NULL,NULL,?,?)',(job,request_id,payload,now,now))
            self.futures[job]=self.executor.submit(self._run,job)
        return self.status(job)
    def _run(self,job_id):
        spec=self.status(job_id)['payload'];cache=self.store.directory/(job_id+'.output');output_format=spec.get('format','mp3')
        try:
            with self.lock:
                if self.status(job_id)['state'] in {'cancelled','cancel_requested'}:self.update(job_id,state='cancelled');return
                self.update(job_id,state='saving' if cache.exists() else 'running',error=None)
            if not cache.exists():
                if spec['kind']=='office':
                    meta=self.store.metadata(spec['file_id']);url=os.environ['AGENT_CREATIVE_URL'].rstrip('/')+'/render'
                    with httpx.Client(trust_env=False,timeout=420) as client:
                        response=client.post(url,params={'job_id':job_id,'name':meta['name'],'format':output_format},headers={'Authorization':'Bearer '+os.environ['AGENT_BROWSER_TOKEN'],'Content-Type':'application/octet-stream'},content=self.store.path(spec['file_id']).read_bytes())
                    if response.status_code>=400:raise ValueError(f'Office worker HTTP {response.status_code}: '+str(response.json().get('detail','conversion failed'))[:200])
                    data=base64.b64decode(response.json()['data'],validate=True)
                else:
                    body={'prompt':spec['prompt'],'music_length_ms':spec['seconds']*1000,'force_instrumental':spec.get('instrumental',True),'model_id':os.getenv('ELEVENLABS_MUSIC_MODEL','music_v1')}
                    with httpx.Client(trust_env=False,timeout=240) as client:
                        with client.stream('POST','https://api.elevenlabs.io/v1/music',params={'output_format':'mp3_44100_128'},headers={'xi-api-key':os.environ['ELEVENLABS_API_KEY']},json=body) as response:
                            if response.status_code>=400:
                                self.update(job_id,state='failed',error=f'Music provider HTTP {response.status_code}');return
                            parts=[];size=0
                            for part in response.iter_bytes():
                                with self.lock:
                                    if self.status(job_id)['state']=='cancel_requested':self.update(job_id,state='cancelled',error='Local retrieval cancelled; provider generation may still be billed.');return
                                size+=len(part)
                                if size>20_000_000:raise ValueError('Music exceeds output limit')
                                parts.append(part)
                            data=b''.join(parts)
                temporary=cache.with_suffix('.tmp');temporary.write_bytes(data);temporary.replace(cache)
            with self.lock:
                if self.status(job_id)['state'] in {'cancel_requested','cancelled'}:cache.unlink(missing_ok=True);self.update(job_id,state='cancelled');return
                self.update(job_id,state='saving')
                name=('preview-' if output_format=='pdf' else 'recalculated-' if output_format=='xlsx' else 'music-')+job_id+'.'+output_format
                asset=self.store.create(name,cache.read_bytes(),{'creative_job':job_id,'parent_file_id':spec.get('file_id'),'operation':spec['kind']},asset_id='file_'+uuid.uuid5(uuid.NAMESPACE_URL,job_id).hex)
            # A successful provider response is not enough: validate the saved artifact.
            future=self.store.jobs.get(asset['id'])
            if future:future.result(timeout=55)
            asset=self.store.metadata(asset['id'])
            if asset['status']!='ready':raise ValueError('Generated artifact failed validation: '+str(asset.get('error','unknown')))
            with self.lock:
                if self.status(job_id)['state']=='cancel_requested':self.update(job_id,state='cancelled');return
                self.update(job_id,state='completed',result=json.dumps({'assets':[asset]}));cache.unlink(missing_ok=True)
        except Exception as exc:
            with self.lock:
                state=self.status(job_id)['state'];cancel=state in {'cancel_requested','cancelled'}
                self.update(job_id,state='cancelled' if cancel else 'unknown' if spec['kind']=='music' and not cache.exists() else 'failed',error=str(exc)[:500])
    def cancel(self,job_id):
        with self.lock:
            job=self.status(job_id)
            if job['state'] in {'completed','failed','cancelled','unknown'}:return job
            self.update(job_id,state='cancelled' if job['state']=='queued' else 'cancel_requested')
        if job['payload']['kind']=='office':
            try:
                with httpx.Client(trust_env=False,timeout=10) as client:client.post(os.environ['AGENT_CREATIVE_URL'].rstrip('/')+'/cancel/'+job_id,headers={'Authorization':'Bearer '+os.environ['AGENT_BROWSER_TOKEN']}).raise_for_status()
            except Exception:self.update(job_id,error='Cancellation recorded; worker may run until its timeout.')
        return self.status(job_id)
    def check(self,job_id,wait_seconds=15):
        if not 0<=wait_seconds<=30:raise ValueError('wait_seconds must be 0-30')
        deadline=time.monotonic()+wait_seconds
        while time.monotonic()<deadline:
            state=self.status(job_id)
            if state['state'] not in {'queued','running','saving','cancel_requested'}:return state
            time.sleep(.25)
        return self.status(job_id)
    def recover(self):
        # Recover every outstanding job, not only the UI's recent page.
        with self.store.connect() as db:ids=[r[0] for r in db.execute("SELECT id FROM creative_jobs WHERE state IN ('queued','running','saving','cancel_requested')")]
        for job_id in ids:
            job=self.status(job_id)
            if job['state']=='cancel_requested':self.update(job_id,state='cancelled');continue
            if job['payload']['kind']=='music' and job['state']=='running' and not (self.store.directory/(job_id+'.output')).exists():self.update(job_id,state='unknown',error='Provider submission was interrupted. Do not automatically create another billed request.');continue
            with self.lock:
                if job_id not in self.futures or self.futures[job_id].done():self.futures[job_id]=self.executor.submit(self._run,job_id)

_jobs=None
_jobs_lock=threading.Lock()
def get_creative_jobs():
    global _jobs
    with _jobs_lock:
        if _jobs is None:_jobs=CreativeJobs()
        return _jobs
