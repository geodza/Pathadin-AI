import hashlib
import math
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from .protocol import VERSION, canonical, digest
from .slide import png, is_tissue

EDGE = 512

def prepare(slide, folder, target, batch_size, filter_blank, progress, cancelled, workers=None, tissue_mask=None):
    folder = Path(folder)
    folder.mkdir(parents=True,exist_ok=True)
    slide.level_for_mpp(target)
    pixel_only = getattr(slide,'pixel_only',False)
    target_x,target_y = slide.mpp if target == 'native' else (target,target)
    total = math.ceil(slide.dimensions[0]*slide.mpp[0]/(EDGE*target_x))*math.ceil(slide.dimensions[1]*slide.mpp[1]/(EDGE*target_y))
    progress(0,total)
    overview = png(slide.overview())
    (folder/'overview.png').write_bytes(overview)
    tiles, rejected = [], []
    # A reviewed inclusion mask is authoritative: later heuristics must not discard painted-in tissue.
    apply_filter = filter_blank and tissue_mask is None
    mask_metadata = tissue_mask.snapshot(folder) if tissue_mask else None
    bounds = getattr(slide,'scan_bounds',None) if apply_filter else None
    def outside(rect):
        if not bounds: return False
        x,y,w,h = rect
        bx,by,bw,bh = bounds
        return x+w <= bx or y+h <= by or x >= bx+bw or y >= by+bh
    def process(i,rect):
        if cancelled(): raise InterruptedError('Preparation cancelled')
        image,content_size,level = slide.tile(rect,target,EDGE)
        if apply_filter and not is_tissue(image): return None,rect,False
        tid = f't{i:07d}'
        data = png(image)
        (folder/(tid+'.png')).write_bytes(data)
        return {'id':tid, 'rect':rect, 'content_pixels':content_size, 'source_level':level,
                'physical_size_um':None if pixel_only else [rect[2]*slide.mpp[0],rect[3]*slide.mpp[1]],
                'effective_mpp':None if pixel_only else [rect[2]*slide.mpp[0]/content_size[0],rect[3]*slide.mpp[1]/content_size[1]],
                'sha256':hashlib.sha256(data).hexdigest()},None,False
    worker_count = max(1,min(4,workers or os.cpu_count() or 1))
    # Bounded queue: at most 2 * workers images in flight, in deterministic grid order.
    # Slide decoding remains protected by its lock; PNG encoding/filtering/writes overlap.
    pool = ThreadPoolExecutor(max_workers=worker_count,thread_name_prefix='wsi-prepare')
    queue = deque()
    positions = iter(enumerate(slide.grid(target,EDGE)))
    scanned_outside = 0
    mask_excluded = 0
    done,last_update = 0,time.monotonic()
    try:
        def submit_one():
            nonlocal done,last_update,scanned_outside,mask_excluded
            for i,rect in positions:
                if cancelled(): raise InterruptedError('Preparation cancelled')
                skip_mask = tissue_mask is not None and not tissue_mask.intersects(rect)
                skip_bounds = outside(rect)
                if skip_mask or skip_bounds:
                    rejected.append(rect)
                    mask_excluded += int(skip_mask)
                    scanned_outside += int(skip_bounds)
                    done += 1
                    if time.monotonic()-last_update >= 0.5:
                        progress(done,total)
                        last_update = time.monotonic()
                    continue
                queue.append(pool.submit(process,i,rect))
                return True
            return False
        for _ in range(worker_count*2):
            if not submit_one(): break
        while queue:
            if cancelled(): raise InterruptedError('Preparation cancelled')
            tile,reject,skipped = queue.popleft().result()
            if tile is not None: tiles.append(tile)
            else: rejected.append(reject)
            scanned_outside += int(skipped)
            done += 1
            if time.monotonic()-last_update >= 0.5:
                progress(done,total)
                last_update = time.monotonic()
            submit_one()
    finally:
        pool.shutdown(wait=True,cancel_futures=True)
    if not tiles: raise ValueError('No tiles retained. Disable blank rejection to inspect this slide.')
    tiles.sort(key=lambda t:t['id'])
    rejected.sort(key=lambda r:(r[1],r[0]))
    batches = [[t['id'] for t in tiles[i:i+batch_size]] for i in range(0,len(tiles),batch_size)]
    manifest = {'protocol':VERSION, 'slide':slide.info(), 'target_mpp':target, 'analysis_mpp':([None,None] if pixel_only else [target_x,target_y]), 'tile_edge':EDGE,
                'encoding':'RGB PNG, compression level 1; partial tiles white-padded', 'tissue_mask':mask_metadata, 'filter':{'enabled':apply_filter,'version':'nonwhite-v1','min_fraction':0.005, 'scanner_bounds':bounds, 'scanner_bounds_rejected':scanned_outside},
                'overview':{'id':'overview','sha256':hashlib.sha256(overview).hexdigest()},
                'tiles':tiles,'rejected_rects':rejected,'batches':batches,
                'coverage':{'mode':'exhaustive_reviewed_mask' if tissue_mask else 'exhaustive_retained_grid',
                    'mask_excluded_tiles':mask_excluded, 'native_tiles_decoded':total-mask_excluded-scanned_outside,'retained_tiles':len(tiles),
                    'rejected_tiles':len(rejected),'total_grid_tiles':len(tiles)+len(rejected),
                    'retained_grid_coverage':1.0,
                    'retained_area_mm2':None if pixel_only else sum(t['physical_size_um'][0]*t['physical_size_um'][1] for t in tiles)/1e6,
                    'note':('Every grid tile touching the reviewed mask is included. Mask accuracy requires pathologist review.' if tissue_mask else 'All retained tiles are scheduled. Blank rejection is fallible; this is not a measured tissue coverage percentage.')}}
    manifest['fingerprint'] = digest(manifest)
    (folder/'manifest.json').write_text(canonical(manifest),encoding='utf-8')
    progress(total,total)
    return manifest

def verify_images(folder, manifest, ids):
    by_id = {t['id']:t for t in manifest['tiles']}
    images = []
    extras={t['id']:t for t in manifest.get('auxiliary_images',[])}
    locators=manifest.get('batch_locators',[])
    locator=[]
    if locators:
        index=manifest['batches'].index(list(ids));item=locators[index];extras[item['id']]=item;locator=[item['id']]
    ordered=([] if manifest.get('omit_overview') else ['overview'])+locator
    for tid in ids:
        ordered.append(tid)
        ordered.extend(k for k,v in extras.items() if v.get('parent_tile_id')==tid)
    for tid in ordered:
        data = (Path(folder)/(tid+'.png')).read_bytes()
        expected = manifest['overview']['sha256'] if tid == 'overview' else (by_id.get(tid) or extras[tid])['sha256']
        if hashlib.sha256(data).hexdigest() != expected: raise ValueError('Cached image hash mismatch: '+tid)
        images.append((tid,data))
    # Conservative portable payload guard, not a universal provider limit.
    if sum(len(b)*4//3 for _,b in images) > 18_000_000:
        raise ValueError('Batch exceeds 18 MB encoded-image guard; prepare again with fewer tiles per batch')
    return images
