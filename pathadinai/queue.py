"""Persistent, sequential folder studies. No model calls during preflight."""
import asyncio,csv,io,json,os,shutil,uuid,time
from .protocol import digest
from pathlib import Path
from fastapi import HTTPException
from fastapi.responses import Response
from pydantic import BaseModel,Field
from typing import Literal
from .fields import plan
from .engine import now

EXTENSIONS={'.svs','.mrxs','.ndpi','.scn','.vms','.vmu','.bif','.tif','.tiff','.svslide','.jpg','.jpeg','.png','.webp','.bmp'}
def scan_folder(folder,recursive=False):
    root=Path(folder.strip().strip('"')).resolve()
    if not root.is_dir():raise ValueError('Folder does not exist')
    found=[]
    for directory,dirs,files in os.walk(root,followlinks=False):
        companions={Path(n).stem.casefold() for n in files if Path(n).suffix.lower()=='.mrxs'}
        dirs[:]=[d for d in dirs if d.casefold() not in companions and not (Path(directory)/d).is_symlink() and not getattr(Path(directory)/d,'is_junction',lambda:False)()]
        for name in sorted(files):
            p=Path(directory)/name
            if p.suffix.lower() in EXTENSIONS and not p.is_symlink():found.append(str(p.resolve()))
            if len(found)>5000:raise ValueError('More than 5000 images; choose a smaller folder')
        if not recursive:break
    return sorted(set(found),key=str.casefold)

class Scan(BaseModel):
    folder:str
    recursive:bool=False
class Entry(BaseModel):
    path:str
    organ:str=Field(min_length=1,max_length=120)
class Control(BaseModel):
    action:Literal['preflight','start','pause','cancel_current','retry_failed']
    request_cap:int=Field(default=1000,ge=1,le=1000000)

def install(app,engine,data,open_slide,OpenInput,get_slide,mask_store,FieldSettings,ModelInput,configured,release_slide=lambda sid:None):
    class Create(BaseModel):
        entries:list[Entry]=Field(min_length=1,max_length=5000)
        fields:FieldSettings
        models:list[ModelInput]=Field(min_length=1,max_length=8)
        menu:Literal['open','uterine']='open'
        mask_mode:Literal['review','automatic']='review'
        concurrency:int=Field(default=1,ge=1,le=4)
        request_cap:int=Field(default=1000,ge=1,le=1000000)
    root=data/'queues';root.mkdir(parents=True,exist_ok=True)
    queues={};workers={}
    def save(q):
        temp=root/(q['id']+'.tmp');temp.write_text(json.dumps(q,ensure_ascii=False),encoding='utf-8');temp.replace(root/(q['id']+'.json'))
    for f in root.glob('*.json'):
        try:
            q=json.loads(f.read_text(encoding='utf-8'))
            if q['status'] in {'running','preflight'}:q['status']='paused';q['message']='Interrupted. Resume explicitly.'
            queues[q['id']]=q
        except (ValueError,OSError):pass
    def get(qid):
        if qid not in queues:raise HTTPException(404,'Unknown queue')
        return queues[qid]
    def active():return any(not t.done() for t in workers.values())
    def attempts(q):return sum(sum(m.get('request_attempts',0) for m in engine.jobs.get(e.get('run_id'),{}).get('models',[])) for e in q['entries'])
    app.state.folder_queue_active=active
    def state(q):
        result=json.loads(json.dumps(q));result['attempts']=attempts(q)
        for e in result['entries']:
            j=engine.jobs.get(e.get('run_id'))
            if j:
                e['run_status']=j['status'];e['progress']=j.get('progress');e['reports_saved']=sum(sum(bool(c.get('read')) for c in m['calls'].values()) for m in j['models'])
        return result
    async def preflight_entry(q,e):
        info=await asyncio.to_thread(open_slide,OpenInput(path=e['path']))
        sid=info['id']
        if e.get('run_id') and sid!=e.get('slide_id'):raise ValueError('Source changed since preparation. Create a new queue for this slide.')
        e['slide_id']=sid;slide=get_slide(sid)
        try:mask=mask_store.load(sid)
        except FileNotFoundError:mask=await asyncio.to_thread(mask_store.generate,sid,slide)
        if list(slide.dimensions)!=mask.metadata['slide_dimensions']:raise ValueError('Mask dimensions do not match slide')
        e['mask_reviewed']=mask.metadata['reviewed'];e['mask_revision']=mask.metadata['revision']
        def thumbnail():
            from PIL import Image
            im=slide.overview(256).convert('RGB');alpha=mask.image.resize(im.size).point(lambda v:int(v*.3));im.paste(Image.new('RGB',im.size,(30,180,100)),(0,0),alpha);im.save(root/(q['id']+'.'+e['id']+'.png'))
        await asyncio.to_thread(thumbnail)
        if not mask.image.getbbox():raise ValueError('Empty tissue mask. Open this slide and correct its mask.')
        p=await asyncio.to_thread(plan,slide,q['fields'],mask)
        e['planned_calls']=p['planned_calls']*len(q['models']);e['fields']=len(p['fields'])
        e['status']='needs_review' if q['mask_mode']=='review' and not mask.metadata['reviewed'] else 'ready'
        e.pop('error',None)
        return slide,mask
    async def worker(q,analyze):
        q['status']='running' if analyze else 'preflight';q['pause']=False;save(q)
        try:
            for e in q['entries']:
                if q['pause']:break
                if e['status'] in {'complete','failed','skipped'}:continue
                q['current']=e['id'];save(q);entry_started=time.monotonic()
                try:
                    slide,mask=await preflight_entry(q,e);save(q)
                    if e.pop('skip_requested',False):e['status']='skipped';continue
                    if not analyze or e['status']=='needs_review':continue
                    free=shutil.disk_usage(engine.root).free
                    if free<512*1024*1024:raise ValueError('Less than 512 MB free disk space. Free space before retrying.')
                    if attempts(q)>=q['request_cap']:q['pause']=True;q['message']='Queue request cap reached; increase it to resume.';break
                    jid=e.get('run_id');j=engine.jobs.get(jid)
                    if j and j.get('cache_cleared'):raise ValueError('Images were cleared. Create a new queue for this slide.')
                    if not j:
                        jid=engine.new(e['slide_id'],e['organ'],q['menu'],'native',q['fields']['batch_size'],False);e['run_id']=jid;j=engine.jobs[jid]
                        j['settings']['field_settings']=q['fields'];j['settings']['mask_revision']=mask.metadata['revision'];j['queue_id']=q['id'];save(q)
                    if not j.get('manifest'):
                        engine.cancels.setdefault(jid,__import__('threading').Event()).clear()
                        e['status']='preparing';save(q)
                        await asyncio.to_thread(engine.prepare,jid,slide,mask)
                        if j['status']!='prepared':raise ValueError(j.get('error','Preparation cancelled'))
                    if e.pop('skip_requested',False):e['status']='skipped';continue
                    e['mask_revision']=j['manifest'].get('tissue_mask',{}).get('revision');e['mask_reviewed']=j['manifest'].get('tissue_mask',{}).get('reviewed',False)
                    e['planned_calls']=j['estimated_calls_per_model']*len(q['models'])
                    # Plans can expand during payload packing. Never authorize beyond the queue cap.
                    def guard():
                        if attempts(q)>=q['request_cap']:raise ValueError('Queue request cap reached')
                    e['status']='analyzing';save(q)
                    await engine.run(jid,q['models'],1000000,8000,q['concurrency'],True,attempt_guard=guard)
                    if e.get('skip_requested'):e['status']='skipped';e.pop('skip_requested',None)
                    elif j['status']=='complete':e['status']='complete'
                    else:
                        errors='; '.join(m.get('error','') for m in j['models']) or j.get('error','Analysis incomplete')
                        e['error']=errors;e['status']='failed'
                        if any(word in errors.lower() for word in ['queue request cap','401','403','429','quota','api key','insufficient_quota']):
                            q['pause']=True;q['message']='Account/limit issue: '+errors;e['status']='ready'
                    save(q)
                except Exception as exc:
                    e['error']=getattr(exc,'detail',str(exc));e['status']='skipped' if e.pop('skip_requested',False) else 'failed';save(q)
                finally:
                    e['elapsed_s']=round(e.get('elapsed_s',0)+time.monotonic()-entry_started,2)
                    if e.get('slide_id'):release_slide(e['slide_id'])
            q['status']='paused' if q['pause'] else 'complete' if all(e['status'] in {'complete','skipped'} for e in q['entries']) else 'attention' if any(e['status'] in {'failed','needs_review'} for e in q['entries']) else 'ready'
        except asyncio.CancelledError:
            q['status']='paused';q['message']='Interrupted; resume to continue.';raise
        finally:q['current']=None;save(q)

    @app.post('/api/folder/scan')
    async def scan(body:Scan):
        try:return {'paths':await asyncio.to_thread(scan_folder,body.folder,body.recursive)}
        except (ValueError,OSError) as exc:raise HTTPException(400,str(exc))
    @app.get('/api/queues')
    def listing():return [{'id':q['id'],'created_at':q['created_at'],'status':q['status'],'count':len(q['entries'])} for q in sorted(queues.values(),key=lambda q:q['created_at'],reverse=True)]
    @app.post('/api/queues')
    def create(body:Create):
        models=[m.model_dump() for m in body.models]
        if any(m['provider']=='demo' for m in models):raise HTTPException(400,'Folder queues require real image models; synthetic demo is separate')
        if len({(m['provider'],m['model']) for m in models})!=len(models):raise HTTPException(400,'Duplicate model selection')
        entries=[];seen=set()
        for e in body.entries:
            path=str(Path(e.path.strip().strip('"')).resolve());key=path.casefold()
            if key in seen:continue
            if not e.organ.strip():raise HTTPException(400,'Enter an organ for every selected slide')
            seen.add(key);entries.append({'id':uuid.uuid4().hex,'path':path,'organ':e.organ.strip(),'status':'pending'})
        sources=[]
        for e in entries:
            p=Path(e['path'])
            try:
                st=p.stat();signature=[st.st_size,st.st_mtime_ns]
                if p.suffix.lower()=='.mrxs':signature.append(sorted((str(f.relative_to(p.with_suffix(''))),f.stat().st_size,f.stat().st_mtime_ns) for f in p.with_suffix('').rglob('*') if f.is_file()))
            except OSError:signature=None
            sources.append([e['path'].casefold(),e['organ'],signature])
        identity=digest({'sources':sources,'fields':body.fields.model_dump(),'models':models,'menu':body.menu,'mask_mode':body.mask_mode})
        existing=next((old for old in list(queues.values()) if old.get('identity')==identity),None)
        if existing:return {**state(existing),'reused_queue':True}
        q={'identity':identity,'id':uuid.uuid4().hex,'created_at':now(),'status':'ready','entries':entries,'fields':body.fields.model_dump(),'models':models,'menu':body.menu,'mask_mode':body.mask_mode,'concurrency':body.concurrency,'request_cap':body.request_cap,'current':None,'pause':False}
        queues[q['id']]=q;save(q);return state(q)
    @app.get('/api/queues/{qid}')
    def info(qid:str):return state(get(qid))
    @app.post('/api/queues/{qid}/control')
    async def control(qid:str,body:Control):
        q=get(qid)
        if body.action=='pause':q['pause']=True;q['message']='Will pause after the current slide.';save(q);return state(q)
        if body.action=='cancel_current':
            e=next((e for e in q['entries'] if e['id']==q.get('current')),None)
            if e:
                e['skip_requested']=True
                if e.get('run_id'):engine.cancel(e['run_id'])
            save(q);return state(q)
        if active() or any(j['status'] in {'queued','preparing','running'} for j in engine.jobs.values()):raise HTTPException(409,'Wait for active work to finish before starting a queue')
        if body.action!='preflight':
            for m in q['models']:
                if not configured()[m['provider']]['configured']:raise HTTPException(400,'Missing API key: '+m['provider'])
        q['request_cap']=body.request_cap;q.pop('message',None)
        if body.action in {'retry_failed','preflight'}:
            for e in q['entries']:
                if e['status']=='failed':e['status']='pending'
        task=asyncio.create_task(worker(q,body.action!='preflight'));workers[qid]=task
        return state(q)
    @app.get('/api/queues/{qid}/mask/{eid}.png')
    def thumbnail(qid:str,eid:str):
        q=get(qid)
        if not any(e['id']==eid for e in q['entries']):raise HTTPException(404,'Unknown queue entry')
        path=root/(qid+'.'+eid+'.png')
        if not path.exists():raise HTTPException(404,'Generate masks first')
        return Response(path.read_bytes(),media_type='image/png')

    @app.get('/api/queues/{qid}/results.csv')
    def results(qid:str):
        q=get(qid);out=io.StringIO();writer=csv.writer(out)
        writer.writerow(['slide','organ','status','model','diagnosis','explanation','limitations','mask_reviewed','attempts','processing_seconds','usage_json','run_id','report_url','geojson_url'])
        def safe(v):
            s=str(v)
            return "'"+s if s.lstrip().startswith(('=','+','-','@')) else s
        for e in q['entries']:
            j=engine.jobs.get(e.get('run_id'),{});models=j.get('models') or [{}]
            for i,m in enumerate(models):
                f=m.get('final') or {};jid=e.get('run_id','')
                row=[Path(e['path']).name,e['organ'],e['status'],m.get('key',''),f.get('primary_dx',''),f.get('explanation',''),json.dumps(f.get('limitations',[]))+' '+e.get('error',''),e.get('mask_reviewed',''),m.get('request_attempts',0),e.get('elapsed_s',0),json.dumps([c.get('usage') for c in m.get('calls',{}).values() if c.get('usage')]),jid,f'/api/runs/{jid}/export' if jid else '',f'/api/runs/{jid}/geojson/{i}' if f else '']
                writer.writerow([safe(v) for v in row])
        return Response('﻿'+out.getvalue(),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="pathadin-queue-{qid[:8]}.csv"'})
