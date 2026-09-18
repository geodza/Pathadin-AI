"""Repartition prepared image groups without altering their image pixels."""
import copy,hashlib,io
from pathlib import Path
from types import SimpleNamespace
from PIL import Image
from .fields import locator
from .protocol import digest,canonical

LIMIT=18_000_000

def repack(folder,manifest,limit=LIMIT,cancelled=lambda:False):
    folder=Path(folder);m=copy.deepcopy(manifest)
    if not m.get('field_settings'):raise ValueError('Automatic splitting is available for architectural-field runs')
    byid={t['id']:t for t in m['tiles']};aux=m.get('auxiliary_images',[])
    sizes={}
    for item in [m['overview']]+m['tiles']+aux:
        if cancelled():raise InterruptedError('Preparation cancelled')
        data=(folder/(item['id']+'.png')).read_bytes()
        if hashlib.sha256(data).hexdigest()!=item['sha256']:raise ValueError('Cached image hash mismatch: '+item['id'])
        sizes[item['id']]=4*((len(data)+2)//3)
    overview=Image.open(folder/'overview.png').convert('RGB');slide=SimpleNamespace(dimensions=m['slide']['dimensions'])
    batches=[];locators=[]
    def add(ids):
        if cancelled():raise InterruptedError('Preparation cancelled')
        data=locator(slide,[byid[t] for t in ids],overview)
        total=sizes['overview']+4*((len(data)+2)//3)+sum(sizes[t] for t in ids)+sum(sizes[a['id']] for a in aux if a['parent_tile_id'] in ids)
        if total>limit:
            if len(ids)==1:raise ValueError('One field plus context exceeds the image limit. Choose a smaller architectural output size and preview again.')
            mid=len(ids)//2;add(ids[:mid]);add(ids[mid:]);return
        tid=f'locator-{len(batches):06d}';(folder/(tid+'.png')).write_bytes(data)
        batches.append(ids);locators.append({'id':tid,'sha256':hashlib.sha256(data).hexdigest(),'encoded_image_bytes':total})
    for ids in m['batches']:add(ids)
    m['batches']=batches;m['batch_locators']=locators
    m['packing']={'method':'split-oversized-only-v1','image_limit_bytes':limit,'original_batch_count':len(manifest['batches']),'actual_batch_count':len(batches)}
    m.pop('fingerprint',None);m['fingerprint']=digest(m)
    (folder/'manifest.json').write_text(canonical(m),encoding='utf-8')
    return m
