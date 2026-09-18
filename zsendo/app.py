import asyncio
import threading
registry_lock = threading.RLock()

import io

import json

import os

import secrets

import subprocess

import sys

import uuid

import base64

from pathlib import Path

from dotenv import load_dotenv



BASE = Path(__file__).resolve().parent.parent

load_dotenv(BASE/'.env')



from fastapi import FastAPI, HTTPException, Request

from fastapi.responses import HTMLResponse, Response, JSONResponse

from fastapi.staticfiles import StaticFiles

from starlette.middleware.trustedhost import TrustedHostMiddleware

from pydantic import BaseModel, Field

from typing import Literal

from typing import Annotated

from .slide import Slide, OPENSLIDE_ERROR

from .engine import Engine

from .adapters import configured

from .protocol import system_prompt

from .tissue_mask import MaskStore



app = FastAPI(title='Pathadin AI · WSI research', docs_url=None, redoc_url=None)

app.add_middleware(TrustedHostMiddleware,allowed_hosts=['127.0.0.1','localhost','testserver'])

app.mount('/static',StaticFiles(directory=BASE/'static'),name='static')

TOKEN = secrets.token_urlsafe(32)

DATA = Path(os.environ.get('ZSENDO_DATA',str(BASE/'data')))

engine = Engine(DATA/'runs')

mask_store = MaskStore(DATA/'masks')

slides = {}

tasks = set()

registry_file = DATA/'slides.json'

registry = json.loads(registry_file.read_text(encoding='utf-8')) if registry_file.exists() else {}



@app.middleware('http')

async def local_only(request: Request, call_next):

    if request.method not in {'GET','HEAD','OPTIONS'} and not secrets.compare_digest(request.headers.get('x-zsendo-token',''),TOKEN):

        return JSONResponse({'detail':'Local session token required'},status_code=403)

    if request.method=='POST' and getattr(app.state,'folder_queue_active',lambda:False)() and (request.url.path in {'/api/prepare','/api/context/preview'} or request.url.path.endswith('/start') or request.url.path.endswith('/repair-batches') or request.url.path=='/api/cache/clear'):
        return JSONResponse({'detail':'A folder queue is active. Pause it and wait for the current slide before starting other work.'},status_code=409)
    response = await call_next(request)

    response.headers['X-Content-Type-Options'] = 'nosniff'

    response.headers['Referrer-Policy'] = 'no-referrer'

    if request.url.path in {'/','/static/app.js','/static/style.css','/static/workspace.js'}:

        response.headers['Cache-Control'] = 'no-store'

    return response



def start(coro):

    task = asyncio.create_task(coro)

    tasks.add(task)

    task.add_done_callback(tasks.discard)



def get_slide(sid):

    if sid not in registry: raise HTTPException(404,'Unknown slide')

    if sid not in slides:

        r = registry[sid]

        try: slides[sid] = Slide(r.get('path'),r.get('manual_mpp'),r.get('demo',False))

        except Exception as exc: raise HTTPException(400,str(exc))

    return slides[sid]



def get_job(jid):

    try: return engine.get(jid)

    except KeyError: raise HTTPException(404,'Unknown run')



@app.get('/',response_class=HTMLResponse)

def index():

    return (BASE/'static'/'index.html').read_text(encoding='utf-8').replace('__SESSION_TOKEN__',TOKEN)



@app.get('/api/config')

def config():

    return {'providers':configured(),'openslide_error':OPENSLIDE_ERROR,

            'slides':[{'id':sid,'name':v['name'],'demo':v.get('demo',False)} for sid,v in list(registry.items())],

            'runs':[{'id':j['id'],'organ':j['organ'],'status':j['status'],'created_at':j['created_at'],

                     'slide_id':j['slide_id']} for j in sorted(engine.jobs.values(),key=lambda j:j['created_at'],reverse=True)]}



class OpenInput(BaseModel):

    path: str = ''

    manual_mpp: tuple[float,float] | None = None

    demo: bool = False



@app.post('/api/slides')

def open_slide(body:OpenInput):
    with registry_lock:
    
        if body.manual_mpp and any(not 0.001 <= v <= 100 for v in body.manual_mpp):
    
            raise HTTPException(400,'Manual MPP values must be finite and between 0.001 and 100')
    
        try: slide = Slide(body.path,body.manual_mpp,body.demo)
    
        except Exception as exc: raise HTTPException(400,str(exc))
    
        signature = None
    
        if slide.path:
    
            source = Path(slide.path)
    
            signature = [source.stat().st_size,source.stat().st_mtime_ns]
    
            if source.suffix.lower() == '.mrxs':
    
                # Include sidecar metadata so edited/replaced data gets a fresh slide ID.
    
                signature.append(sorted((str(p.relative_to(source.with_suffix(''))),p.stat().st_size,p.stat().st_mtime_ns)
    
                                        for p in source.with_suffix('').rglob('*') if p.is_file()))
    
            signature = json.loads(json.dumps(signature))
    
        sid = next((key for key,value in reversed(list(registry.items()))
    
                    if not body.demo and value.get('path') == slide.path and value.get('source_signature') == signature
    
                    and value.get('manual_mpp') == (list(body.manual_mpp) if body.manual_mpp else None)),uuid.uuid4().hex)
    
        slides[sid] = slide
    
        registry[sid] = {'path':slide.path,'manual_mpp':list(body.manual_mpp) if body.manual_mpp else None,'demo':body.demo,
    
                         'name':'Synthetic slide' if body.demo else Path(slide.path).name,'source_signature':signature}
    
        registry_file.parent.mkdir(parents=True,exist_ok=True)
    
        temp = registry_file.with_suffix('.tmp')
    
        temp.write_text(json.dumps(registry),encoding='utf-8')
    
        temp.replace(registry_file)
    
        return {'id':sid,'name':registry[sid]['name'],**slide.info()}
    
    
    

@app.post('/api/browse')

async def browse():

    def choose():

        try:

            result = subprocess.run([sys.executable,'-m','zsendo.picker'],cwd=BASE,capture_output=True,timeout=180,

                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)

            if result.returncode: raise ValueError('File picker could not start. Paste the full WSI path instead.')

            data = json.loads(result.stdout.decode('utf-8'))

            if data.get('error'): raise ValueError(data['error'])

            return data['path']

        except subprocess.TimeoutExpired: raise ValueError('File picker timed out')

    try: return {'path':await asyncio.to_thread(choose)}

    except ValueError as exc: raise HTTPException(400,str(exc))



@app.get('/api/slides/{sid}')

def slide_info(sid:str): return {'id':sid,'name':registry.get(sid,{}).get('name'),**get_slide(sid).info()}



@app.get('/api/slides/{sid}/slide.dzi')

def dzi(sid:str): return Response(get_slide(sid).dzi(),media_type='application/xml')



@app.get('/api/slides/{sid}/slide_files/{level}/{coord}.jpeg')

def dz_tile(sid:str,level:int,coord:str):

    try:

        col,row = map(int,coord.split('_'))

        im = get_slide(sid).deepzoom_tile(level,col,row)

        buffer = io.BytesIO()

        im.convert('RGB').save(buffer,'JPEG',quality=90)

        return Response(buffer.getvalue(),media_type='image/jpeg',headers={'Cache-Control':'private, max-age=3600'})

    except (ValueError,KeyError): raise HTTPException(404,'Invalid deep zoom tile')



class FieldSettings(BaseModel):

    mode: Literal['physical','relative','magnification'] = 'physical'

    size: float = Field(default=500,gt=0,le=2000)

    output_edge: Literal[0,256,512,768,1024,2048] = 1024

    overlap: float = Field(default=0,ge=0,le=0.3)

    detail: bool = True

    batch_size: int = Field(default=4,ge=1,le=16)



class PrepareInput(BaseModel):

    field_settings: FieldSettings | None = None

    preview_plan_hash: str | None = None

    slide_id: str

    organ: str = Field(min_length=1,max_length=120)

    menu: Literal['open','uterine'] = 'open'

    target_mpp: Literal['native'] | Annotated[float, Field(ge=0.1,le=8)] = 'native'

    batch_size: int = Field(default=8,ge=1,le=16)

    filter_blank: bool = True

    mask_revision: str | None = Field(default=None,pattern=r'^[0-9a-f]{32}$')



@app.post('/api/slides/{sid}/mask/generate')

def generate_mask(sid:str):

    return mask_store.generate(sid,get_slide(sid)).metadata



def load_mask(sid,revision=None):

    get_slide(sid)

    try: return mask_store.load(sid,revision)

    except FileNotFoundError: raise HTTPException(404,'No saved tissue mask')

    except ValueError as exc: raise HTTPException(400,str(exc))



@app.get('/api/slides/{sid}/mask')

def mask_info(sid:str,revision:str|None=None):

    return load_mask(sid,revision).metadata



@app.get('/api/slides/{sid}/mask.png')

def mask_image(sid:str,revision:str|None=None):

    return Response(load_mask(sid,revision).display_png(),media_type='image/png',headers={'Cache-Control':'no-store'})



class MaskReviewInput(BaseModel):

    revision: str = Field(pattern=r'^[0-9a-f]{32}$')

    png_base64: str = Field(max_length=24_000_000)



@app.post('/api/slides/{sid}/mask/review')

def review_mask(sid:str,body:MaskReviewInput):

    get_slide(sid)

    try: return mask_store.review(sid,body.revision,body.png_base64).metadata

    except FileNotFoundError: raise HTTPException(404,'Mask revision not found')

    except (ValueError,OSError) as exc: raise HTTPException(400,str(exc))



@app.post('/api/prepare')

async def prepare_run(body:PrepareInput):

    slide = get_slide(body.slide_id)

    if not all(slide.mpp): raise HTTPException(400,'Missing MPP: reopen with manual calibration')

    if body.target_mpp != 'native' and body.target_mpp < max(slide.mpp): raise HTTPException(400,'Target is finer than native resolution. Choose a coarser MPP; upsampling is not new detail.')

    if not body.organ.strip(): raise HTTPException(400,'Enter the organ')

    tissue_mask = None

    if body.mask_revision:

        tissue_mask = load_mask(body.slide_id,body.mask_revision)

        if not tissue_mask.metadata['reviewed']: raise HTTPException(400,'Review and save the tissue mask before preparation')

        if tissue_mask.metadata['slide_dimensions'] != list(slide.dimensions): raise HTTPException(400,'Mask belongs to different slide dimensions')

    if body.field_settings:

        from .fields import plan

        settings=body.field_settings.model_dump()

        if settings['mode']=='relative' and settings['size']>100:raise HTTPException(400,'Percentage must be at most 100')

        try: planned=plan(slide,settings,tissue_mask)

        except ValueError as exc:raise HTTPException(400,str(exc))

        if body.preview_plan_hash!=planned['plan_hash']:raise HTTPException(400,'Preview these field settings before preparing')

    jid = engine.new(body.slide_id,body.organ.strip(),body.menu,body.target_mpp,body.batch_size,body.filter_blank)

    engine.jobs[jid]['settings']['mask_revision'] = body.mask_revision

    if body.field_settings:engine.jobs[jid]['settings']['field_settings']=body.field_settings.model_dump()

    start(asyncio.to_thread(engine.prepare,jid,slide,tissue_mask))

    return {'id':jid}



class ModelInput(BaseModel):

    provider: Literal['openai','anthropic','gemini','demo']

    model: str = Field(min_length=1,max_length=180,pattern=r'^[a-zA-Z0-9._:/-]+$')



class RunInput(BaseModel):

    concurrency: int = Field(default=1,ge=1,le=4)

    reuse_completed: bool = True

    models: list[ModelInput] = Field(min_length=1,max_length=8)

    max_calls: int = Field(default=500,ge=1,le=100000)

    max_output_tokens: int = Field(default=8000,ge=1000,le=32000)



@app.post('/api/runs/{jid}/start')

async def run(jid:str,body:RunInput):

    job = get_job(jid)

    if job['status'] not in {'prepared','partial','cancelled','interrupted','complete'} or 'manifest' not in job:

        raise HTTPException(409,'Run must have a prepared bundle and no active work')

    if job.get('cache_cleared'): raise HTTPException(409,'Cached images were cleared. Prepare a new run; saved reports remain available.')

    models = [m.model_dump() for m in body.models]

    if len({(m['provider'],m['model']) for m in models}) != len(models): raise HTTPException(400,'Duplicate model selection')

    if job['manifest']['slide']['demo']:

        if any(m['provider']!='demo' for m in models): raise HTTPException(400,'Synthetic demo only uses simulated models')

    elif any(m['provider']=='demo' for m in models): raise HTTPException(400,'Simulated models are only allowed on the synthetic demo')

    for m in models:

        if m['provider'] != 'demo' and not configured()[m['provider']]['configured']:

            raise HTTPException(400,'Missing API key: '+m['provider'])

    if job['estimated_calls_per_model'] > body.max_calls: raise HTTPException(400,'Request budget is smaller than the planned call count')

    old = engine.jobs[jid]

    if old.get('max_output_tokens') and old['max_output_tokens'] != body.max_output_tokens:

        raise HTTPException(400,'Output token setting is frozen on resume. Prepare a new run to change it.')

    old['status'] = 'running'

    start(engine.run(jid,models,body.max_calls,body.max_output_tokens,body.concurrency,body.reuse_completed))

    return {'id':jid}



@app.get('/api/runs/{jid}')

def run_info(jid:str):

    job = get_job(jid)

    # Keep polling compact: detailed raw calls remain in JSON export.

    for result in job['models']:

        result['call_count'] = len(result['calls'])

        result['completed_calls'] = sum(bool(c.get('read')) for c in result['calls'].values())

        result['usage'] = [c.get('usage',{}) for c in result['calls'].values() if c.get('usage')]

        result.pop('calls',None)

        result.pop('geojson',None)

    return job



@app.post('/api/runs/{jid}/cancel')

def cancel(jid:str):

    get_job(jid)

    engine.cancel(jid)

    return {'status':'cancellation requested; an in-flight request may finish'}



@app.get('/api/runs/{jid}/export')

def export(jid:str):

    job = get_job(jid)

    job['system_prompt'] = next((m['system_prompt'] for m in job['models'] if m.get('system_prompt')),system_prompt(job['menu']))

    if job.get('manifest',{}).get('tissue_mask'):

        job['tissue_mask_png_base64'] = base64.b64encode((engine.root/jid/'bundle'/'tissue-mask.png').read_bytes()).decode('ascii')

    return JSONResponse(job,headers={'Content-Disposition':f'attachment; filename="pathadin-ai-{jid[:8]}.json"'})



@app.get('/api/runs/{jid}/geojson/{index}')

def export_geojson(jid:str,index:int):

    job = get_job(jid)

    if not 0 <= index < len(job['models']): raise HTTPException(404,'Unknown model')

    result = job['models'][index]

    if 'geojson' not in result: raise HTTPException(409,'Annotations available after successful synthesis')

    return JSONResponse(result['geojson'],headers={'Content-Disposition':f'attachment; filename="pathadin-ai-{jid[:8]}-model{index}.geojson"'})



@app.post('/api/models/openai')

async def available_openai_models():

    import httpx

    key = os.environ.get('OPENAI_API_KEY')

    if not key: raise HTTPException(400,'Configure your OpenAI API key first')

    try:

        async with httpx.AsyncClient(timeout=20) as client:

            response = await client.get('https://api.openai.com/v1/models',headers={'Authorization':'Bearer '+key})

        if response.is_error: raise HTTPException(502,f'OpenAI model list returned HTTP {response.status_code}')

        rows = sorted(response.json().get('data',[]),key=lambda m:m.get('created',0),reverse=True)

        return {'models':[m['id'] for m in rows], 'note':'Account models, newest creation date first; image support is not provided by this endpoint.'}

    except httpx.HTTPError: raise HTTPException(502,'Could not reach OpenAI model list; retry or enter an ID manually')



class PreviewInput(BaseModel):

    slide_id: str

    field_settings: FieldSettings

    mask_revision: str | None = None

    batch_index: int = Field(default=-1,ge=-1)



@app.post('/api/v2/preview')

def preview_fields(body:PreviewInput):

    from .fields import plan,field_images,locator,central_batch

    slide=get_slide(body.slide_id)

    mask=load_mask(body.slide_id,body.mask_revision) if body.mask_revision else None

    if mask and (not mask.metadata['reviewed'] or mask.metadata['slide_dimensions']!=list(slide.dimensions)):

        raise HTTPException(400,'Save and review a mask for this image first')

    settings=body.field_settings.model_dump()

    if settings['mode']=='relative' and settings['size']>100:raise HTTPException(400,'Percentage must be at most 100')

    try:

        planned=plan(slide,settings,mask)

        index=central_batch(planned,mask) if body.batch_index<0 else min(body.batch_index,len(planned['batches'])-1)

        selected=[f for f in planned['fields'] if f['id'] in planned['batches'][index]]

        from .slide import png

        images=[('overview',png(slide.overview(1024))),('locator',locator(slide,selected))]

        for f in selected:images.extend(field_images(slide,f))

        encoded_size=sum(len(data)*4//3 for _,data in images)

        return {'plan_hash':planned['plan_hash'],'batch_index':index,'batch_count':len(planned['batches']),

                'field_count':len(planned['fields']),'planned_calls':planned['planned_calls'],'fields':selected,

                'payload_ok':encoded_size<=18_000_000,'encoded_bytes':encoded_size,

                'images':[{'id':tid,'png_base64':base64.b64encode(data).decode(),'sha256':__import__('hashlib').sha256(data).hexdigest()} for tid,data in images]}

    except ValueError as exc:raise HTTPException(400,str(exc))



@app.get('/api/runs/{jid}/batch/{index}')

def preview_saved(jid:str,index:int):

    from .bundle import verify_images

    job=get_job(jid);manifest=job.get('manifest')

    if not manifest or not 0<=index<len(manifest['batches']):raise HTTPException(404,'Batch not found')

    ids=manifest['batches'][index]

    try:images=verify_images(engine.root/jid/'bundle',manifest,ids)

    except (OSError,ValueError) as exc:raise HTTPException(400,str(exc))

    return {'batch_index':index,'batch_count':len(manifest['batches']),'fields':[t for t in manifest['tiles'] if t['id'] in ids],

            'images':[{'id':tid,'png_base64':base64.b64encode(data).decode(),'sha256':__import__('hashlib').sha256(data).hexdigest()} for tid,data in images]}



@app.get('/api/runs/{jid}/tiles.zip')

def export_tiles(jid:str):

    import tempfile,zipfile

    from fastapi.responses import FileResponse

    from starlette.background import BackgroundTask

    job=get_job(jid);m=job.get('manifest')

    if not m:raise HTTPException(409,'Prepare fields first')

    from .bundle import verify_images

    folder=engine.root/jid/'bundle'

    tmp=tempfile.NamedTemporaryFile(suffix='.zip',delete=False);path=Path(tmp.name);tmp.close()

    try:

        with zipfile.ZipFile(path,'w',zipfile.ZIP_STORED) as archive:

            archive.writestr('manifest.json',__import__('json').dumps(m))

            seen=set()

            for ids in m['batches']:

                for tid,data in verify_images(folder,m,ids):

                    if tid not in seen:archive.writestr(tid+'.png',data);seen.add(tid)

    except Exception:

        path.unlink(missing_ok=True);raise

    return FileResponse(path,filename=f'pathadin-{jid[:8]}-tiles.zip',background=BackgroundTask(path.unlink,missing_ok=True))



from .workspace import install as install_workspace

install_workspace(app,get_slide,get_job,engine,DATA,start,configured)



@app.post('/api/runs/{jid}/repair-batches')

def repair_batches(jid:str):

    import copy,shutil

    from .packing import repack

    from .engine import synthesis_calls

    original=get_job(jid)

    if original['status'] in {'running','queued','preparing'}:raise HTTPException(409,'Stop the current run before repairing batches')

    if not original.get('manifest',{}).get('field_settings'):raise HTTPException(400,'Select a prepared architectural-field run')

    manifest=original['manifest']

    from .protocol import digest

    if digest({k:v for k,v in manifest.items() if k!='fingerprint'})!=manifest['fingerprint']:raise HTTPException(400,'Manifest fingerprint mismatch')

    nid=engine.new(original['slide_id'],original['organ'],original['menu'],original['settings']['target_mpp'],original['settings']['batch_size'],original['settings']['filter_blank'])

    new=engine.jobs[nid];new['settings']=copy.deepcopy(original['settings']);new['parent_run_id']=jid

    try:

        folder=engine.root/nid/'bundle';shutil.copytree(engine.root/jid/'bundle',folder)

        new['manifest']=repack(folder,manifest)

        new['estimated_calls_per_model']=len(new['manifest']['batches'])+synthesis_calls(len(new['manifest']['batches']))

        new['models']=copy.deepcopy(original['models'])

        for model in new['models']:

            model['status']='queued';model['completed_batches']=0

            for key in ['final','geojson','error','finished_at']:model.pop(key,None)

            model['calls']={k:v for k,v in model['calls'].items() if k.startswith('batch-')}

        if original.get('max_output_tokens'):new['max_output_tokens']=original['max_output_tokens']

        new['max_calls_per_model']=original.get('max_calls_per_model',500)

        new['status']='prepared';engine.save(new)

        return {'id':nid,'batch_count':len(new['manifest']['batches']),'original_batch_count':len(manifest['batches'])}

    except (ValueError,OSError) as exc:

        new['status']='failed';new['error']=str(exc);engine.save(new);raise HTTPException(400,str(exc))



from .cache import install as install_cache

install_cache(app,engine,get_job)


from .queue import install as install_queue
install_queue(app,engine,DATA,open_slide,OpenInput,get_slide,mask_store,FieldSettings,ModelInput,configured,lambda sid:slides.pop(sid,None))
