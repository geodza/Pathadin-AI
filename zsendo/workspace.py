"""Independent reviewed regions and persisted, blinded context comparisons."""
import base64
import copy
import hashlib
import json
import math
import threading
import uuid
from pathlib import Path
from typing import Literal
from pydantic import BaseModel,Field
from fastapi import HTTPException
from fastapi.responses import JSONResponse,Response
from .regions import validate_polygon,measurements
from .fields import render,output_size
from .slide import png
from .protocol import canonical,digest

class RegionInput(BaseModel):
    id:str=Field(pattern=r'^[a-zA-Z0-9-]{1,64}$')
    name:str=Field(min_length=1,max_length=120)
    kind:Literal['tumour','normal','hotspot','artifact','tissue','exclusion']
    points:list[tuple[float,float]]=Field(min_length=3,max_length=128)
class RegionSave(BaseModel):
    revision:str|None=None
    regions:list[RegionInput]=Field(max_length=40)
    reference_diagnosis:str=Field(default='',max_length=2000)
    reference_note:str=Field(default='',max_length=4000)
class ContextInput(BaseModel):
    slide_id:str
    region_id:str
    organ:str=Field(min_length=1,max_length=120)
    conditions:list[Literal['native','500','1000','500_native','1000_native','overview_1000_native','region']]=Field(min_length=1,max_length=7)
class ModelChoice(BaseModel):
    provider:Literal['openai','anthropic','gemini','demo']
    model:str=Field(min_length=1,max_length=180,pattern=r'^[a-zA-Z0-9._:/-]+$')
class ContextStart(BaseModel):
    models:list[ModelChoice]=Field(min_length=1,max_length=8)
    concurrency:int=Field(default=1,ge=1,le=4)
    max_calls:int=Field(default=20,ge=2,le=1000)

CONDITIONS={'native':['native'],'500':['500'],'1000':['1000'],'500_native':['500','native'],
            '1000_native':['1000','native'],'overview_1000_native':['overview','1000','native'],'region':['region']}


def context_manifest(slide,points,views,folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    xs,ys=zip(*points);cx,cy=(min(xs)+max(xs))/2,(min(ys)+max(ys))/2
    tiles=[]
    for view in views:
        if view in {'500','1000'}:
            if not all(slide.info()['mpp']):raise ValueError('Micron views require calibration; choose native or selected-region context')
            w,h=[max(1,round(int(view)/m)) for m in slide.mpp]
        elif view=='native':w=h=512
        if view=='overview':rect=[0,0,*slide.dimensions]
        elif view=='region':
            x,y=math.floor(min(xs)),math.floor(min(ys));rect=[x,y,math.ceil(max(xs))-x,math.ceil(max(ys))-y]
        else:
            x,y=max(0,math.floor(cx-w/2)),max(0,math.floor(cy-h/2))
            right,bottom=min(slide.dimensions[0],math.ceil(cx+w/2)),min(slide.dimensions[1],math.ceil(cy+h/2))
            rect=[x,y,right-x,bottom-y]
        size=output_size(rect,0 if view=='native' else 1024)
        tid=f't{len(tiles):07d}';data=png(render(slide,rect,size));(folder/(tid+'.png')).write_bytes(data)
        tiles.append({'id':tid,'rect':rect,'view':view,'content_pixels':list(size),'effective_mpp':None if not all(slide.info()['mpp']) else [rect[2]*slide.mpp[0]/size[0],rect[3]*slide.mpp[1]/size[1]],'physical_size_um':None if not all(slide.info()['mpp']) else [rect[2]*slide.mpp[0],rect[3]*slide.mpp[1]],'sha256':hashlib.sha256(data).hexdigest()})
    overview=png(slide.overview(1024));(folder/'overview.png').write_bytes(overview)
    m={'protocol':'context-experiment-v3','context_experiment':True,'omit_overview':True,
       'slide':slide.info(),'target_mpp':'native','analysis_mpp':slide.info()['mpp'],
       'overview':{'id':'overview','sha256':hashlib.sha256(overview).hexdigest()},'tiles':tiles,'batches':[[t['id'] for t in tiles]],
       'rejected_rects':[],'coverage':{'retained_tiles':len(tiles),'rejected_tiles':0,'retained_area_mm2':None,'mode':'selected_location_only','note':'Controlled selected-location experiment, not whole-slide coverage.'},
       'center_pixels':[cx,cy]}
    m['fingerprint']=digest(m);(folder/'manifest.json').write_text(canonical(m),encoding='utf-8')
    return m


def install(app,get_slide,get_job,engine,DATA,start,configured):
    lock=threading.RLock();root=DATA/'regions';experiments=DATA/'experiments';experiments.mkdir(parents=True,exist_ok=True)
    def read_regions(sid):
        slide=get_slide(sid);folder=root/sid
        if (folder/'latest.json').exists():return json.loads((folder/'latest.json').read_text(encoding='utf-8'))
        return {'revision':None,'regions':[],'reference_diagnosis':'','reference_note':'','slide_dimensions':list(slide.dimensions)}
    def read_experiment(eid):
        if len(eid)!=32 or any(c not in '0123456789abcdef' for c in eid):raise HTTPException(404,'Unknown experiment')
        try:return json.loads((experiments/(eid+'.json')).read_text(encoding='utf-8'))
        except FileNotFoundError:raise HTTPException(404,'Unknown experiment')
    def save_experiment(e):
        path=experiments/(e['id']+'.json');temp=path.with_suffix('.tmp');temp.write_text(canonical(e),encoding='utf-8');temp.replace(path)

    @app.get('/api/slides/{sid}/regions')
    def regions_get(sid:str):
        data=read_regions(sid);return {**data,'measurements':measurements(data['regions'],get_slide(sid).info()['mpp'])}

    @app.post('/api/slides/{sid}/regions')
    def regions_save(sid:str,body:RegionSave):
        slide=get_slide(sid);rows=[r.model_dump() for r in body.regions]
        if len({r['id'] for r in rows})!=len(rows):raise HTTPException(400,'Duplicate region ID')
        if sum(len(r['points']) for r in rows)>1024:raise HTTPException(400,'Limit this workspace to 1024 total vertices')
        try:
            for r in rows:validate_polygon(r['points'],slide.dimensions)
        except ValueError as exc:raise HTTPException(400,str(exc))
        with lock:
            old=read_regions(sid)
            if old['revision']!=body.revision:raise HTTPException(409,'Regions changed elsewhere. Reload before editing.')
            data=body.model_dump();data['revision']=uuid.uuid4().hex;data['slide_dimensions']=list(slide.dimensions)
            folder=root/sid;folder.mkdir(parents=True,exist_ok=True)
            (folder/(data['revision']+'.json')).write_text(canonical(data),encoding='utf-8')
            temp=folder/'latest.tmp';temp.write_text(canonical(data),encoding='utf-8');temp.replace(folder/'latest.json')
        return {**data,'measurements':measurements(rows,slide.info()['mpp'])}

    @app.get('/api/slides/{sid}/regions.geojson')
    def regions_export(sid:str):
        data=read_regions(sid);slide=get_slide(sid)
        return JSONResponse({'type':'FeatureCollection','coordinate_system':slide.info(),'reference_diagnosis':data['reference_diagnosis'],'reference_note':data['reference_note'],
              'revision':data['revision'],'measurements':measurements(data['regions'],slide.info()['mpp']),
              'features':[{'type':'Feature','id':r['id'],'properties':{'name':r['name'],'classification':{'name':r['kind']}},'geometry':{'type':'Polygon','coordinates':[r['points']+[r['points'][0]]]}} for r in data['regions']]},headers={'Content-Disposition':'attachment; filename="reviewed-regions.geojson"'})

    @app.post('/api/context/preview')
    def context_preview(body:ContextInput):
        slide=get_slide(body.slide_id);saved=read_regions(body.slide_id)
        region=next((r for r in saved['regions'] if r['id']==body.region_id),None)
        if not region:raise HTTPException(400,'Save and select a region first')
        if len(set(body.conditions))!=len(body.conditions):raise HTTPException(400,'Duplicate conditions')
        if not body.organ.strip():raise HTTPException(400,'Enter organ')
        if any(any(v in {'500','1000'} for v in CONDITIONS[c]) for c in body.conditions) and not all(slide.info()['mpp']):raise HTTPException(400,'500/1000 micron conditions require calibration; choose native or selected region')
        e={'id':uuid.uuid4().hex,'slide_id':body.slide_id,'region_revision':saved['revision'],'region_id':region['id'],'organ':body.organ.strip(),'runs':[]}
        for condition in body.conditions:
            jid=engine.new(body.slide_id,e['organ'],'open','native',8,False)
            try:
                m=context_manifest(slide,region['points'],CONDITIONS[condition],engine.root/jid/'bundle')
                from .bundle import verify_images
                verify_images(engine.root/jid/'bundle',m,m['batches'][0])
            except ValueError as exc:
                engine.jobs[jid]['status']='failed';engine.jobs[jid]['error']=str(exc);engine.save(engine.jobs[jid]);raise HTTPException(400,str(exc))
            job=engine.jobs[jid];job['manifest']=m;job['estimated_calls_per_model']=2;job['status']='prepared';job['experiment_id']=e['id'];engine.save(job)
            e['runs'].append({'condition':condition,'run_id':jid})
        save_experiment(e);return e

    @app.get('/api/context')
    def context_list(sid:str):
        get_slide(sid)
        return [json.loads(p.read_text(encoding='utf-8')) for p in sorted(experiments.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True) if json.loads(p.read_text(encoding='utf-8'))['slide_id']==sid]

    @app.get('/api/context/{eid}')
    def context_status(eid:str):
        e=read_experiment(eid);rows=[]
        for run in e['runs']:
            j=get_job(run['run_id'])
            rows.append({**run,'status':j['status'],'models':[{'model':m['key'],'status':m['status'],'phase':m.get('phase'),'final':m.get('final'),'error':m.get('error'),'request_attempts':m.get('request_attempts',0),'latency_s':m.get('latency_s'),'usage':[c['usage'] for c in m['calls'].values() if c.get('usage')]} for m in j['models']]})
        return {**e,'runs':rows}

    @app.post('/api/context/{eid}/start')
    async def context_start(eid:str,body:ContextStart):
        e=read_experiment(eid);models=[m.model_dump() for m in body.models];slide=get_slide(e['slide_id'])
        if len({(m['provider'],m['model']) for m in models})!=len(models):raise HTTPException(400,'Duplicate models')
        for m in models:
            if slide.demo and m['provider']!='demo':raise HTTPException(400,'Synthetic slide requires simulated models')
            if not slide.demo and m['provider']=='demo':raise HTTPException(400,'Demo model requires synthetic image')
            if m['provider']!='demo' and not configured()[m['provider']]['configured']:raise HTTPException(400,'API key missing for '+m['provider'])
        if any(get_job(r['run_id'])['status'] in {'running','queued','preparing'} for r in e['runs']):raise HTTPException(409,'Experiment already active')
        e['cancelled']=False;save_experiment(e)
        for r in e['runs']:engine.jobs[r['run_id']]['status']='queued';engine.save(engine.jobs[r['run_id']])
        async def execute():
            try:
                for r in e['runs']:
                    if read_experiment(eid).get('cancelled'):break
                    await engine.run(r['run_id'],models,body.max_calls,8000,body.concurrency,True)
            finally:
                for r in e['runs']:
                    j=engine.jobs[r['run_id']]
                    if j['status']=='queued':j['status']='interrupted';engine.save(j)
        start(execute());return {'id':eid,'planned_calls_per_model':len(e['runs'])*2}

    @app.get('/api/runs/{jid}/evidence')
    def evidence(jid:str):
        j=get_job(jid)
        return {'models':[{'model':m['key'],'stages':[{'id':cid,'read':c.get('read'),'error':c.get('error'),'image_ids':c.get('image_ids',[]),'source_reports':json.loads(c['user_prompt']).get('reports',[]) if c.get('user_prompt') else []} for cid,c in m['calls'].items()]} for m in j['models']]}

    @app.post('/api/context/{eid}/cancel')
    def context_cancel(eid:str):
        e=read_experiment(eid);e['cancelled']=True;save_experiment(e)
        for r in e['runs']:engine.cancel(r['run_id'])
        return {'status':'Cancellation requested; in-flight requests may finish'}
