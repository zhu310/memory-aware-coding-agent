"""Authenticated binary upload, status and artifact download routes."""
import json,os,subprocess,sys,threading,uuid
from fastapi import HTTPException,Request
from fastapi.responses import FileResponse

def install_file_routes(app,store):
    thumbnail_lock=threading.Lock()
    @app.post('/api/files',status_code=201)
    async def upload(request:Request,name:str):
        chunks=[];size=0
        async for chunk in request.stream():
            size+=len(chunk)
            if size>20_000_000:raise HTTPException(413,'单个文件不能超过20 MB。')
            chunks.append(chunk)
        try:return store.create(name,b''.join(chunks),{'source':'user_upload'})
        except ValueError as exc:raise HTTPException(422,str(exc)) from exc
    @app.get('/api/files')
    def files():return {'files':store.list()}
    @app.get('/api/files/{file_id}')
    def status(file_id:str):
        try:return store.metadata(file_id)
        except ValueError as exc:raise HTTPException(404,str(exc)) from exc
    @app.get('/api/files/{file_id}/content')
    def download(file_id:str):
        try:
            metadata=store.metadata(file_id)
            inline=metadata['status']=='ready' and (metadata['media_type'] in {'image/png','image/jpeg','image/webp','image/gif','application/pdf'} or metadata['media_type'].startswith(('audio/','video/')))
            return FileResponse(store.path(file_id),media_type=metadata['media_type'],filename=metadata['name'],content_disposition_type='inline' if inline else 'attachment',
                headers={'X-Content-Type-Options':'nosniff','Content-Security-Policy':"sandbox; default-src 'none'"})
        except ValueError as exc:raise HTTPException(404,str(exc)) from exc
    @app.get('/api/files/{file_id}/thumbnail')
    def thumbnail(file_id:str):
        try:
            metadata=store.metadata(file_id)
            if metadata['status']!='ready' or not metadata['media_type'].startswith('image/'):raise HTTPException(409,'Image is not ready')
            target=store.path(file_id).parent/'thumbnail-320.jpg'
            if not target.exists():
                if not thumbnail_lock.acquire(timeout=20):raise HTTPException(429,'Thumbnail worker is busy')
                temporary=target.with_name('thumbnail-'+uuid.uuid4().hex+'.tmp')
                try:
                    if not target.exists():
                        options={'source':str(store.path(file_id)),'name':metadata['name'],'destination':str(temporary),'max_edge':320,'output_format':'JPEG'}
                        environment={k:v for k,v in os.environ.items() if k.upper() in {'PATH','HOME','PYTHONPATH','SYSTEMROOT','WINDIR','TMP','TEMP','TMPDIR'}}
                        result=subprocess.run([sys.executable,'-m','final_version_app.image_prepare',json.dumps(options)],env=environment,capture_output=True,timeout=30)
                        if result.returncode:raise HTTPException(422,'Unable to render thumbnail')
                        temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True);thumbnail_lock.release()
            return FileResponse(target,media_type='image/jpeg',headers={'X-Content-Type-Options':'nosniff'})
        except ValueError as exc:raise HTTPException(404,str(exc)) from exc
        except subprocess.TimeoutExpired as exc:raise HTTPException(504,'Thumbnail rendering timed out') from exc

    @app.on_event('startup')
    def recover_file_jobs():store.recover()
