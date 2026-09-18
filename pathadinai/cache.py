"""Explicit removal of inactive run image caches; reports and masks stay intact."""
import hashlib
import json
import re
from fastapi import HTTPException
from pydantic import BaseModel

class ClearInput(BaseModel):
    run_id: str
    snapshot: str

def install(app,engine,get_job):
    def inventory(jid):
        if not re.fullmatch(r'[0-9a-f]{32}',jid):raise HTTPException(400,'Invalid run ID')
        job=get_job(jid)
        if job['status'] in {'running','queued','preparing'} or job.get('experiment_id'):
            raise HTTPException(409,'Active runs and comparison caches are protected')
        root=engine.root.resolve();run=engine.root/jid;folder=run/'bundle'
        if run.is_symlink() or getattr(run,'is_junction',lambda:False)() or folder.is_symlink() or getattr(folder,'is_junction',lambda:False)():
            raise HTTPException(409,'Linked cache directories cannot be cleaned')
        if folder.resolve()!=root/jid/'bundle':raise HTTPException(409,'Invalid cache location')
        files=[]
        for p in sorted(folder.glob('*.png')):
            if p.name=='tissue-mask.png':continue
            if p.is_symlink() or not p.is_file():raise HTTPException(409,'Unexpected linked cache file')
            st=p.stat();files.append((p.name,st.st_size,st.st_mtime_ns))
        snapshot=hashlib.sha256(json.dumps(files).encode()).hexdigest()
        return folder,files,snapshot

    @app.get('/api/cache')
    async def cache_list():
        rows=[]
        for jid in list(engine.jobs):
            try:
                folder,files,snapshot=inventory(jid)
                if files:
                    job=get_job(jid);rows.append({'id':jid,'organ':job['organ'],'status':job['status'],'bytes':sum(f[1] for f in files),'files':len(files),'snapshot':snapshot})
            except HTTPException:continue
        return {'runs':rows,'note':'Only inactive analysis image caches are listed. Reports, GeoJSON, masks, regions, original slides and API keys are preserved. Cleared runs cannot resume or export tiles. Comparison caches are protected.'}

    @app.post('/api/cache/clear')
    async def cache_clear(body:ClearInput):
        folder,files,snapshot=inventory(body.run_id)
        if snapshot!=body.snapshot:raise HTTPException(409,'Cache changed. Refresh the list before clearing.')
        job=engine.jobs[body.run_id];job['cache_cleared']=True;engine.save(job)
        for name,_,_ in files:(folder/name).unlink()
        return {'removed_files':len(files),'removed_bytes':sum(f[1] for f in files)}
