"""Conservative overview mask, immutable revisions, and level-zero intersection tests."""
import base64
import hashlib
import io
import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image, ImageChops, ImageFilter
from .slide import rgb

MAX_EDGE = 4096
ALGORITHM = 'overview-color-v1-margin3'

def encode(mask):
    stream = io.BytesIO()
    mask.save(stream,'PNG',compress_level=1)
    return stream.getvalue()

def detect(overview):
    image = rgb(overview)
    r,g,b = image.split()
    high = ImageChops.lighter(ImageChops.lighter(r,g),b)
    low = ImageChops.darker(ImageChops.darker(r,g),b)
    dark = high.point([255 if v < 225 else 0 for v in range(256)])
    color = ImageChops.subtract(high,low).point([255 if v > 8 else 0 for v in range(256)])
    nonwhite = low.point([255 if v < 248 else 0 for v in range(256)])
    candidate = ImageChops.lighter(dark,ImageChops.darker(color,nonwhite))
    # Three overview pixels of safety margin, before manual correction.
    return candidate.filter(ImageFilter.MaxFilter(7))

class TissueMask:
    def __init__(self,image,metadata):
        self.image = image.convert('L').point([0]+[255]*255)
        self.metadata = metadata
        self.width,self.height = metadata['slide_dimensions']

    def intersects(self,rect):
        x,y,w,h = rect
        mw,mh = self.image.size
        # Floor minima and ceil maxima include every mask pixel touched by this tile.
        left,top = max(0,math.floor(x*mw/self.width)),max(0,math.floor(y*mh/self.height))
        right,bottom = min(mw,math.ceil((x+w)*mw/self.width)),min(mh,math.ceil((y+h)*mh/self.height))
        return right > left and bottom > top and self.image.crop((left,top,right,bottom)).getbbox() is not None

    def snapshot(self,folder):
        data = encode(self.image)
        if hashlib.sha256(data).hexdigest() != self.metadata['sha256']:
            raise ValueError('Mask snapshot hash mismatch')
        (Path(folder)/'tissue-mask.png').write_bytes(data)
        return dict(self.metadata)

    def display_png(self):
        display = Image.new('RGBA',self.image.size,(42,171,127,255))
        display.putalpha(self.image)
        return encode(display)

class MaskStore:
    def __init__(self,root):
        self.root = Path(root)

    def folder(self,sid):
        if len(sid)!=32 or any(c not in '0123456789abcdef' for c in sid): raise ValueError('Invalid slide ID')
        return self.root/sid

    def save(self,sid,image,dimensions,reviewed=False,parent=None,edited=False):
        folder=self.folder(sid);folder.mkdir(parents=True,exist_ok=True)
        revision=uuid.uuid4().hex
        mask=image.convert('L').point([0]+[255]*255)
        selected=mask.histogram()[255]
        if reviewed and not selected: raise ValueError('Mask is empty. Paint tissue in before saving, or turn off mask use.')
        data=encode(mask)
        metadata={'revision':revision,'sha256':hashlib.sha256(data).hexdigest(),
                  'algorithm':ALGORITHM,'slide_dimensions':list(dimensions),'mask_dimensions':list(mask.size),
                  'level_zero_pixels_per_mask_pixel':[dimensions[0]/mask.width,dimensions[1]/mask.height],
                  'reviewed':reviewed,'human_edited':edited,'parent_revision':parent,
                  'created_at':datetime.now(timezone.utc).isoformat(),
                  'selected_fraction':selected/(mask.width*mask.height),
                  'scope':'tissue inclusion only, not diagnostic region selection'}
        (folder/(revision+'.png')).write_bytes(data)
        (folder/(revision+'.json')).write_text(json.dumps(metadata),encoding='utf-8')
        # Atomic latest pointer; earlier revisions are immutable.
        temp=folder/(revision+'.tmp')
        temp.write_text(json.dumps({'revision':revision}),encoding='utf-8')
        temp.replace(folder/'latest.json')
        return TissueMask(mask,metadata)

    def generate(self,sid,slide):
        return self.save(sid,detect(slide.overview(MAX_EDGE)),slide.dimensions)

    def load(self,sid,revision=None):
        folder=self.folder(sid)
        if revision is None: revision=json.loads((folder/'latest.json').read_text())['revision']
        if len(revision)!=32 or any(c not in '0123456789abcdef' for c in revision): raise ValueError('Invalid mask revision')
        metadata=json.loads((folder/(revision+'.json')).read_text(encoding='utf-8'))
        data=(folder/(revision+'.png')).read_bytes()
        if hashlib.sha256(data).hexdigest()!=metadata['sha256']: raise ValueError('Saved mask hash mismatch')
        with Image.open(io.BytesIO(data)) as image:
            if list(image.size)!=metadata['mask_dimensions']: raise ValueError('Mask dimensions mismatch')
            return TissueMask(image.copy(),metadata)

    def review(self,sid,revision,png_base64):
        previous=self.load(sid,revision)
        if len(png_base64)>24_000_000: raise ValueError('Mask upload too large')
        try: data=base64.b64decode(png_base64,validate=True)
        except Exception as exc: raise ValueError('Invalid mask encoding') from exc
        with Image.open(io.BytesIO(data)) as image:
            if image.format!='PNG' or image.mode!='RGBA' or list(image.size)!=previous.metadata['mask_dimensions']:
                raise ValueError('Edited mask must be an RGBA PNG matching the generated mask dimensions')
            mask=image.getchannel('A').point([0]+[255]*255)
        edited=previous.metadata['human_edited'] or mask.tobytes()!=previous.image.tobytes()
        return self.save(sid,mask,previous.metadata['slide_dimensions'],True,revision,edited)
