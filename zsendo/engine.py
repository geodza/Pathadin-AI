import asyncio
import copy
import json
import math
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from . import adapters
from .bundle import prepare, verify_images
from .protocol import VERSION, canonical, digest, system_prompt, validate_read, geojson, agreement

def now(): return datetime.now(timezone.utc).isoformat()

def synthesis_calls(n):
    total = 0
    while n > 1:
        n = math.ceil(n/4)
        total += n
    return max(1,total)

class Engine:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True,exist_ok=True)
        self.lock = threading.RLock()
        self.jobs = {}
        self.cancels = {}
        for p in self.root.glob('*/job.json'):
            try:
                job = json.loads(p.read_text(encoding='utf-8'))
                if job['status'] in {'preparing','running','queued'}: job['status'] = 'interrupted'
                self.jobs[job['id']] = job
            except (ValueError,OSError): pass

    def save(self,job):
        with self.lock:
            folder = self.root/job['id']
            folder.mkdir(exist_ok=True)
            temp = folder/'job.tmp'
            temp.write_text(canonical(job),encoding='utf-8')
            temp.replace(folder/'job.json')

    def get(self,jid):
        with self.lock: return copy.deepcopy(self.jobs[jid])

    def new(self,slide_id,organ,menu,target,batch_size,filter_blank):
        jid = uuid.uuid4().hex
        job = {'id':jid,'slide_id':slide_id,'organ':organ,'menu':menu,'created_at':now(),
               'protocol':VERSION,'status':'queued','progress':{'done':0,'total':0},'models':[],
               'settings':{'target_mpp':target,'batch_size':batch_size,'filter_blank':filter_blank}}
        self.jobs[jid] = job
        self.cancels[jid] = threading.Event()
        self.save(job)
        return jid

    def prepare(self,jid,slide,tissue_mask=None):
        job = self.jobs[jid]
        job['status'] = 'preparing'
        self.save(job)
        started = time.monotonic()
        def progress(done,total):
            elapsed = time.monotonic()-started
            rate = done/elapsed if elapsed > 0 else 0
            job['progress'] = {'done':done,'total':total,'elapsed_s':round(elapsed,1),
                               'tiles_per_second':round(rate,1),
                               'eta_s':round((total-done)/rate) if rate > 0 else None}
            self.save(job)
        try:
            s = job['settings']
            if s.get('field_settings'):
                from .fields import prepare_fields
                manifest=prepare_fields(slide,self.root/jid/'bundle',s['field_settings'],tissue_mask,progress,self.cancels[jid].is_set)
            else:
                manifest = prepare(slide,self.root/jid/'bundle',s['target_mpp'],s['batch_size'],s['filter_blank'],progress,self.cancels[jid].is_set,tissue_mask=tissue_mask)
            job['manifest'] = manifest
            job['estimated_calls_per_model'] = len(manifest['batches'])+synthesis_calls(len(manifest['batches']))
            job['status'] = 'prepared'
        except InterruptedError: job['status'] = 'cancelled'
        except Exception as exc:
            job['status'],job['error'] = 'failed',str(exc)
        self.save(job)

    async def run(self,jid,models,max_calls,max_tokens,concurrency=1,reuse_completed=True,attempt_guard=None):
        job = self.jobs[jid]
        manifest = job['manifest']
        to_hash = {k:v for k,v in manifest.items() if k != 'fingerprint'}
        if digest(to_hash) != manifest['fingerprint']:
            job['status'],job['error'] = 'failed','Manifest fingerprint mismatch'
            self.save(job)
            return
        job['status'] = 'running'
        job['started_at'] = now()
        job['concurrency'],job['reuse_completed'] = concurrency,reuse_completed
        job['max_calls_per_model'],job['max_output_tokens'] = max_calls,max_tokens
        self.cancels.setdefault(jid,threading.Event()).clear()
        cancel = self.cancels[jid].is_set
        # One active request per provider; independent providers may progress together.
        semaphores = {p:asyncio.Semaphore(concurrency) for p in ['openai','anthropic','gemini','demo']}
        previous = {r['key']:r for r in job['models']}
        job['models'] = list(previous.values())
        selected = []
        for model in models:
            key = model['provider']+':'+model['model']
            if key not in previous:
                result = {'key':key,**model,'calls':{},'status':'queued','flags':[]}
                job['models'].append(result)
            else: result = previous[key]
            selected.append(result)
        self.save(job)

        async def run_model(result):
            if result['status'] == 'complete' and reuse_completed: return
            result['status'] = 'running'
            result.pop('error',None)
            system = system_prompt(job['menu'])
            if manifest.get('field_settings'):
                system += '\nV2 fields: first assess architecture and normal organ structures using the overview and large fields. Images ending -detail are native center crops of their parent tile, not separate tile IDs. Locator images mark fields on the overview. Cite parent tile IDs only. Resized architectural fields and sampled native center crops do not provide exhaustive cellular detail. Consider benign mimics and insufficient evidence; do not infer malignancy from atypical-looking cells alone.'
            if manifest.get('context_experiment'):
                system += '\nThis is a controlled context experiment on one selected location, not exhaustive whole-slide assessment. Supplied views may differ in scale. Use only the supplied views, consider normal architecture and benign mimics, and state when context is insufficient. Do not assume malignancy from the selection of a region.'
            result['system_prompt']=system
            result['prompt_hash'],result['bundle_fingerprint'] = digest(system),manifest['fingerprint']
            result['transport_settings'] = {'max_output_tokens':max_tokens,'openai_detail':'high' if result['provider']=='openai' else None,
                                             'temperature':'provider default','synthesis_fan_in':4}
            tiles = {t['id']:t for t in manifest['tiles']}
            async def invoke(call_id,task,ids,images,require_all):
                user = canonical(task)
                request_fingerprint = digest({'system':system,'user':user,'images':[(tid,__import__('hashlib').sha256(b).hexdigest()) for tid,b in images]})
                cached = result['calls'].get(call_id)
                if reuse_completed and cached and cached.get('read') and cached.get('request_fingerprint') == request_fingerprint:
                    return cached['read']
                if cancel(): raise InterruptedError('Cancelled')
                record = {'stage':call_id,'request_fingerprint':request_fingerprint,'user_prompt':user,
                          'image_ids':[i for i,_ in images],'started_at':now()}
                if cached:
                    result.setdefault('failed_call_history',[]).append(cached)
                result['calls'][call_id] = record
                self.save(job)
                def on_attempt():
                    if attempt_guard: attempt_guard()
                    if result.get('request_attempts',0) >= max_calls:
                        raise ValueError('Request-attempt budget exhausted; increase budget to resume')
                    result['request_attempts'] = result.get('request_attempts',0)+1
                    self.save(job)
                try:
                    async with semaphores[result['provider']]:
                        response = await adapters.call(result['provider'],result['model'],system,user,images,max_tokens,cancel,on_attempt=on_attempt)
                except Exception as exc:
                    record['error'] = str(exc)
                    self.save(job)
                    raise
                record.update(response)
                self.save(job)
                if response.get('error'): raise ValueError(response['error'])
                read,flags = validate_read(response['raw'],ids,job['menu'],require_all)
                record['read'],record['flags'] = read,flags
                result['flags'] = sorted(set(result['flags']+flags))
                self.save(job)
                return read
            try:
                batches = manifest['batches']
                reports = [None] * len(batches)
                result['completed_batches'] = 0
                async def read_batch(i):
                    ids = batches[i]
                    images = await asyncio.to_thread(verify_images,self.root/jid/'bundle',manifest,ids)
                    task = {'stage':'batch','organ':job['organ'],'target_mpp':manifest['target_mpp'],
                            'analysis_mpp':manifest.get('analysis_mpp',[manifest['target_mpp']]*2),
                            'tile_ids':ids,'manifest':[tiles[t] for t in ids],
                            'instruction':('Assess every large architectural field in organ context. Use native center crops only as supporting detail; cite parent field IDs.' if manifest.get('field_settings') else 'Assess every supplied detailed tile; overview is context only.')}
                    reports[i] = await invoke(f'batch-{i:06d}',task,ids,images,True)
                    result['completed_batches'] += 1
                    result['phase'] = f"Read {result['completed_batches']}/{len(batches)} batches"
                    self.save(job)
                # Bound image loading as well as HTTP requests; drain siblings on failure.
                for offset in range(0,len(batches),concurrency):
                    outcomes = await asyncio.gather(*(read_batch(i) for i in range(offset,min(offset+concurrency,len(batches)))),return_exceptions=True)
                    for outcome in outcomes:
                        if isinstance(outcome,BaseException): raise outcome
                # Frozen four-way tree: every batch report participates, no top-k filtering.
                level = 0
                while True:
                    group_count=math.ceil(len(reports)/4)
                    combined=[None]*group_count
                    async def synthesize_group(group_index):
                        i=group_index*4
                        group=reports[i:i+4]
                        allowed=sorted(set(t for r in group for region in r['regions'] for t in region['tile_ids']))
                        task={'stage':'synthesis','organ':job['organ'],'reports':group,
                              'instruction':'Integrate all reports. Cite only their evidence tile IDs. assessed_tile_ids may be empty; you receive reports, not new images. State that this is a hierarchical synthesis.'}
                        if len(canonical(task))>100_000:raise ValueError('Synthesis exceeds context guard; no reports were silently dropped')
                        combined[group_index]=await invoke(f'synthesis-{level:03d}-{group_index:06d}',task,allowed,[],False)
                        result['phase']=f'Synthesizing level {level+1}: {sum(x is not None for x in combined)}/{group_count} reports saved'
                        self.save(job)
                    for offset in range(0,group_count,concurrency):
                        result['phase']=f'Synthesizing level {level+1}: {offset}/{group_count} reports saved'
                        outcomes=await asyncio.gather(*(synthesize_group(i) for i in range(offset,min(offset+concurrency,group_count))),return_exceptions=True)
                        for outcome in outcomes:
                            if isinstance(outcome,BaseException):raise outcome
                    if len(combined) == 1:
                        result['final'] = combined[0]
                        break
                    reports,level = combined,level+1
                result['status'] = 'complete'
                result['finished_at'] = now()
                result['phase'] = 'Complete'
                reads = [(k,c['read']) for k,c in result['calls'].items() if k.startswith('batch-') and 'read' in c]
                reads.append(('final',result['final']))
                result['geojson'] = geojson(reads,manifest,result['key'],manifest['fingerprint'])
            except InterruptedError:
                result['status'] = 'cancelled'
            except Exception as exc:
                result['status'],result['error'] = 'failed',str(exc)
            result['latency_s'] = round(sum(c.get('latency_s',0) for c in result['calls'].values()),2)
            # Partial evidence remains reviewable even if synthesis or a later batch fails.
            if result['status'] != 'complete':
                reads = [(k,c['read']) for k,c in result['calls'].items() if k.startswith('batch-') and 'read' in c]
                if reads:
                    result['geojson'] = geojson(reads,manifest,result['key'],manifest['fingerprint'])
                    result['geojson']['partial'] = True
            self.save(job)

        await asyncio.gather(*(run_model(r) for r in selected))
        job['status'] = 'complete' if all(r['status']=='complete' for r in job['models']) else ('cancelled' if cancel() else 'partial')
        job['agreement'] = agreement(job['models']) if not manifest['slide']['demo'] else 'Synthetic demo — no model comparison'
        self.save(job)

    def cancel(self,jid):
        self.cancels.setdefault(jid,threading.Event()).set()
