"""V2 architectural fields with explicit scale, deterministic plans and byte previews."""

import hashlib

import io

import math

import re

from pathlib import Path

from PIL import Image, ImageDraw

from .slide import rgb, png

from .protocol import canonical, digest

from .engine import synthesis_calls





def fragment_bounds(mask):

    """8-connected run-length components at the reviewed mask's own resolution."""

    image=mask.image

    parents=[];boxes=[]

    def root(i):

        while parents[i]!=i:

            parents[i]=parents[parents[i]];i=parents[i]

        return i

    previous=[]

    raw=image.tobytes();width=image.width

    for y in range(image.height):

        runs=[];cursor=0

        for match in re.finditer(b'\xff+',raw[y*width:(y+1)*width]):

            a,b=match.span();i=len(parents);parents.append(i);boxes.append([a,y,b,y+1])

            while cursor<len(previous) and previous[cursor][1]<a:cursor+=1

            j=cursor

            while j<len(previous) and previous[j][0]<=b:

                old=root(previous[j][2]);current=root(i)

                if old!=current:parents[old]=current

                j+=1

            runs.append((a,b,i))

        previous=runs

    merged={}

    for i,box in enumerate(boxes):

        key=root(i)

        if key not in merged:merged[key]=box.copy()

        else:

            v=merged[key];v[:]=[min(v[0],box[0]),min(v[1],box[1]),max(v[2],box[2]),max(v[3],box[3])]

    sx,sy=mask.width/image.width,mask.height/image.height

    return sorted([[math.floor(a*sx),math.floor(b*sy),math.ceil(c*sx)-math.floor(a*sx),math.ceil(d*sy)-math.floor(b*sy)] for a,b,c,d in merged.values()],key=lambda b:(b[1],b[0]))





def plan(slide,settings,mask=None):

    mode=settings['mode'];value=settings['size'];edge=settings['output_edge']

    if mode=='physical' and (getattr(slide,'pixel_only',False) or not all(slide.mpp)):

        raise ValueError('Physical field size needs calibrated microns per pixel. Choose percentage mode for uncalibrated images.')

    if mode=='magnification':

        try: objective=float(slide.objective)

        except (TypeError,ValueError): raise ValueError('Nominal magnification requires scanner objective metadata. Use physical size or percentage instead.')

        if not math.isfinite(objective) or objective<=0 or value>objective or not edge:

            raise ValueError('Choose a magnification no higher than the scanner objective and a 256–2048 pixel output.')

    overlap=settings.get('overlap',0)

    if not 0<=overlap<=0.3: raise ValueError('Overlap must be between 0 and 30 percent')

    regions=fragment_bounds(mask) if mask else [[0,0,*slide.dimensions]]

    if not regions:raise ValueError('The selected tissue mask is empty')

    fields=[]

    for region_index,(x,y,w,h) in enumerate(regions):

        if mode=='physical':fw,fh=[max(1,round(value/m)) for m in slide.mpp]

        elif mode=='magnification':fw=fh=max(1,round(edge*objective/value))

        else:fw,fh=max(1,round(w*value/100)),max(1,round(h*value/100))

        for yy in range(y,y+h,max(1,round(fh*(1-overlap)))):

            for xx in range(x,x+w,max(1,round(fw*(1-overlap)))):

                right,bottom=slide.dimensions if mode!='relative' else (x+w,y+h)

                rect=[xx,yy,min(fw,right-xx),min(fh,bottom-yy)]

                if mask and not mask.intersects(rect):continue

                if len(fields)>=50000:raise ValueError('More than 50,000 fields. Increase field size or refine the mask.')

                ow,oh=output_size(rect,edge)

                if ow*oh>16_000_000:raise ValueError('A native field exceeds 16 million pixels. Select a 256–2048 pixel architectural view or a smaller field.')

                field={'id':f't{len(fields):07d}','rect':rect,'fragment':region_index+1,

                       'content_pixels':[ow,oh], 'physical_size_um':None if getattr(slide,'pixel_only',False) else [rect[2]*slide.mpp[0],rect[3]*slide.mpp[1]],

                       'effective_mpp':None if getattr(slide,'pixel_only',False) else [rect[2]*slide.mpp[0]/ow,rect[3]*slide.mpp[1]/oh]}

                if settings['detail']:

                    dw,dh=min(512,rect[2]),min(512,rect[3])

                    field['detail_rect']=[xx+(rect[2]-dw)//2,yy+(rect[3]-dh)//2,dw,dh]

                fields.append(field)

    if not fields:raise ValueError('No fields intersect the selected tissue')

    batches=[]

    # Keep neighbors from one fragment together; do not mix separate fragments in a batch.

    for f in fields:

        if not batches or len(batches[-1])>=settings['batch_size'] or fields[int(batches[-1][0][1:])]['fragment']!=f['fragment']:batches.append([])

        batches[-1].append(f['id'])

    result={'version':'pathadin-fields-v2','settings':settings,'slide':slide.info(),

            'mask_revision':mask.metadata['revision'] if mask else None,'regions':regions,'fields':fields,'batches':batches,

            'planned_calls':len(batches)+synthesis_calls(len(batches))}

    result['plan_hash']=digest(result)

    return result





def output_size(rect,edge):

    w,h=rect[2:];scale=1 if edge==0 else min(1,edge/max(w,h))

    return max(1,round(w*scale)),max(1,round(h*scale))





def render(slide,rect,size):

    x,y,w,h=rect;ow,oh=size

    with slide.lock:

        if slide.demo or slide.raster:

            return slide.image.resize((ow,oh),Image.Resampling.LANCZOS,box=(x,y,x+w,y+h))

        ratio=min(w/ow,h/oh)

        level=max(i for i,d in enumerate(slide.downsamples) if d<=ratio)

        ds=slide.downsamples[level]

        im=rgb(slide.handle.read_region((x,y),level,(math.ceil(w/ds),math.ceil(h/ds))))

        return im.resize((ow,oh),Image.Resampling.LANCZOS,box=(0,0,w/ds,h/ds))





def field_images(slide,field):

    result=[(field['id'],png(render(slide,field['rect'],field['content_pixels'])))]

    if 'detail_rect' in field:

        rect=field['detail_rect'];result.append((field['id']+'-detail',png(render(slide,rect,rect[2:]))))

    return result





def locator(slide,fields,overview=None):

    im=(overview or slide.overview(1024)).copy();draw=ImageDraw.Draw(im)

    sx,sy=im.width/slide.dimensions[0],im.height/slide.dimensions[1]

    for f in fields:

        x,y,w,h=f['rect'];draw.rectangle((x*sx,y*sy,(x+w)*sx,(y+h)*sy),outline='#00a85a',width=2)

        draw.text((x*sx,y*sy),f['id'],fill='#005c35')

    return png(im)





def prepare_fields(slide,folder,settings,mask,progress,cancelled):

    p=plan(slide,settings,mask);folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)

    overview=png(slide.overview(1024));(folder/'overview.png').write_bytes(overview)

    fields=p['fields'];aux=[]

    for i,f in enumerate(fields):

        if cancelled():raise InterruptedError('Preparation cancelled')

        for tid,data in field_images(slide,f):

            (folder/(tid+'.png')).write_bytes(data)

            if tid==f['id']:f['sha256']=hashlib.sha256(data).hexdigest()

            else:aux.append({'id':tid,'parent_tile_id':f['id'],'rect':f['detail_rect'],'sha256':hashlib.sha256(data).hexdigest(),'kind':'native_center_detail'})

        progress(i+1,len(fields))

    locators=[];byid={f['id']:f for f in fields}

    for i,ids in enumerate(p['batches']):

        if cancelled():raise InterruptedError('Preparation cancelled')

        tid=f'locator-{i:06d}';data=locator(slide,[byid[t] for t in ids]);(folder/(tid+'.png')).write_bytes(data)

        locators.append({'id':tid,'sha256':hashlib.sha256(data).hexdigest()})

    manifest={'protocol':'wsi-fields-v2','slide':slide.info(),'target_mpp':'native','analysis_mpp':slide.info()['mpp'],

              'field_settings':settings,'plan_hash':p['plan_hash'],'tile_edge':settings['output_edge'],

              'overview':{'id':'overview','sha256':hashlib.sha256(overview).hexdigest()},

              'tiles':fields,'auxiliary_images':aux,'batch_locators':locators,'batches':p['batches'],'rejected_rects':[],

              'tissue_mask':mask.snapshot(folder) if mask else None,

              'coverage':{'mode':'all_mask_intersecting_fields' if mask else 'full_image_fields',

                          'retained_tiles':len(fields),'rejected_tiles':0,'retained_area_mm2':None,

                          'note':'All fields touching the mask are included. Architectural images may be resized. Native center details are samples, not exhaustive native coverage. Fragment bounding boxes may overlap.'}}

    manifest['fingerprint']=digest(manifest);(folder/'manifest.json').write_text(canonical(manifest),encoding='utf-8')

    from .packing import repack

    return repack(folder,manifest,cancelled=cancelled)





def central_batch(planned,mask=None):

    """Preview a field centered on actual tissue in the largest fragment bbox."""

    regions=planned['regions'];fragment=max(range(len(regions)),key=lambda i:regions[i][2]*regions[i][3])+1

    x,y,w,h=regions[fragment-1];cx,cy=x+w/2,y+h/2

    candidates=[f for f in planned['fields'] if f['fragment']==fragment]

    def score(f):

        a,b,c,d=f['rect'];px,py=a+c/2,b+d/2

        on_tissue=True

        if mask:

            mx=min(mask.image.width-1,max(0,int(px*mask.image.width/mask.width)))

            my=min(mask.image.height-1,max(0,int(py*mask.image.height/mask.height)))

            on_tissue=bool(mask.image.getpixel((mx,my)))

        return (not on_tissue,((px-cx)/w)**2+((py-cy)/h)**2)

    chosen=min(candidates,key=score)['id']

    return next(i for i,b in enumerate(planned['batches']) if chosen in b)

