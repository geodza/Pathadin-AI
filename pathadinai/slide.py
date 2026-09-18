import io
import math
import os
import threading
from pathlib import Path
from PIL import Image, ImageDraw, ImageChops, ImageOps

OPENSLIDE_ERROR = None
_dll_handle = None
try:
    if os.name == 'nt' and os.environ.get('OPENSLIDE_PATH'):
        _dll_handle = os.add_dll_directory(os.environ['OPENSLIDE_PATH'])
    import openslide
    from openslide.deepzoom import DeepZoomGenerator
except (ImportError, OSError) as exc:
    openslide = None
    OPENSLIDE_ERROR = str(exc)

def rgb(image):
    if image.mode == 'RGB': return image
    if image.mode == 'RGBA':
        base = Image.new('RGB', image.size, 'white')
        base.paste(image, mask=image.getchannel('A'))
        return base
    return image.convert('RGB')

def is_tissue(image, min_fraction=0.005):
    """Deterministic conservative non-white filter; not a lesion detector.

    Retains colored or dark pixels. Artifact may pass; pale tissue can still be lost.
    Can be disabled completely for literal exhaustive grid coverage.
    """
    thumb = rgb(image).copy()
    thumb.thumbnail((128,128))
    r,g,b = thumb.split()
    high = ImageChops.lighter(ImageChops.lighter(r,g),b)
    low = ImageChops.darker(ImageChops.darker(r,g),b)
    dark = high.point([255 if n < 225 else 0 for n in range(256)])
    saturated = ImageChops.subtract(high,low).point([255 if n > 12 else 0 for n in range(256)])
    nonwhite = low.point([255 if n < 245 else 0 for n in range(256)])
    mask = ImageChops.lighter(dark,ImageChops.darker(saturated,nonwhite))
    kept = mask.histogram()[255]
    return kept / (thumb.width * thumb.height) >= min_fraction

def png(image):
    out = io.BytesIO()
    rgb(image).save(out, format='PNG', optimize=False, compress_level=1)
    return out.getvalue()

class Slide:
    def __init__(self, path=None, manual_mpp=None, demo=False):
        self.raster = bool(path and Path(str(path).strip().strip(chr(34))).suffix.lower() in {".jpg",".jpeg",".png",".bmp",".webp"})
        self.pixel_only = self.raster and not manual_mpp
        self.demo = demo
        self.lock = threading.RLock()
        self.path = str(path) if path else None
        self.handle = None
        self.scan_bounds = None
        if self.raster:
            self.path = str(Path(str(path).strip().strip(chr(34))).expanduser().resolve())
            with Image.open(self.path) as source:
                self.image = rgb(ImageOps.exif_transpose(source).convert('RGBA'))
            self.dimensions = self.image.size
            self.mpp = list(manual_mpp) if manual_mpp else [1.0,1.0]
            self.mpp_source = 'manual override' if manual_mpp else 'uncalibrated pixels'
            self.objective = None
            self.vendor = 'standard image'
            self.downsamples = [1.0]
        elif demo:
            self.dimensions = (4096,3072)
            self.mpp = [0.5,0.5]
            self.mpp_source = 'synthetic'
            self.objective = None
            self.vendor = 'synthetic demo, not tissue'
            self.image = Image.new('RGB', self.dimensions, '#fffdfa')
            draw = ImageDraw.Draw(self.image)
            import random
            rng = random.Random(19)
            for cx,cy,rx,ry in [(1150,1350,820,1050),(2900,1550,720,900)]:
                draw.ellipse((cx-rx,cy-ry,cx+rx,cy+ry), fill='#e8adc4')
                for _ in range(1600):
                    x,y = rng.randrange(cx-rx,cx+rx),rng.randrange(cy-ry,cy+ry)
                    if ((x-cx)/rx)**2+((y-cy)/ry)**2 < 0.96:
                        r = rng.randrange(3,12)
                        draw.ellipse((x-r,y-r,x+r,y+r),fill=rng.choice(['#ab729e','#c081a6','#945b93']))
                for _ in range(35):
                    x,y = rng.randrange(cx-rx//2,cx+rx//2),rng.randrange(cy-ry//2,cy+ry//2)
                    draw.ellipse((x-55,y-85,x+55,y+85),fill='#fff3e9',outline='#ad689d',width=12)
            draw.text((90,70),'SYNTHETIC DEMO - NO DIAGNOSTIC CONTENT', fill='#785778',font_size=42)
            self.downsamples = [1.0]
        else:
            if openslide is None: raise ValueError('OpenSlide unavailable: ' + str(OPENSLIDE_ERROR))
            p = Path(str(path).strip().strip('"')).expanduser().resolve()
            self.path = str(p)
            if not p.is_file(): raise ValueError('Slide file does not exist')
            if p.suffix.lower() not in {'.svs','.mrxs','.ndpi','.scn','.tif','.tiff','.vms','.vmu','.bif'}:
                raise ValueError('Select an OpenSlide-compatible WSI file')
            if p.suffix.lower() == '.mrxs' and not p.with_suffix('').is_dir():
                raise ValueError('Mirax requires its same-named sibling folder of .dat files')
            self.handle = openslide.OpenSlide(str(p))
            self.dimensions = self.handle.dimensions
            props = self.handle.properties
            try:
                bx,by,bw,bh = [int(props['openslide.bounds-'+k]) for k in ('x','y','width','height')]
                if bx >= 0 and by >= 0 and bw > 0 and bh > 0 and bx+bw <= self.dimensions[0] and by+bh <= self.dimensions[1]:
                    self.scan_bounds = [bx,by,bw,bh]
            except (KeyError,ValueError): pass
            def positive(value):
                try:
                    v = float(value)
                    return v if math.isfinite(v) and v > 0 else None
                except (TypeError,ValueError): return None
            self.mpp = [positive(props.get('openslide.mpp-x')), positive(props.get('openslide.mpp-y'))]
            self.mpp_source = 'metadata'
            if manual_mpp:
                self.mpp = list(manual_mpp)
                self.mpp_source = 'manual override'
            self.objective = props.get('openslide.objective-power')
            self.vendor = props.get('openslide.vendor','unknown')
            self.downsamples = list(self.handle.level_downsamples)
            self.dz = DeepZoomGenerator(self.handle, tile_size=254, overlap=1, limit_bounds=False)

    def info(self):
        return {'dimensions':list(self.dimensions), 'mpp':([None,None] if self.pixel_only else self.mpp), 'pixel_only':self.pixel_only, 'raster':self.raster, 'mpp_source':self.mpp_source,
                'objective':self.objective, 'vendor':self.vendor, 'demo':self.demo,
                'levels':len(self.downsamples),
                'coordinate_frame':'openslide_uncropped_level_zero',
                'scanner_bounds':self.scan_bounds,
                'bounds_relative_offset':self.scan_bounds[:2] if self.scan_bounds else None,
                'reader_version':getattr(openslide,'__library_version__',None) if not self.demo else None}

    def level_for_mpp(self, target):
        if self.pixel_only and target != 'native': raise ValueError('Enter calibrated MPP to use physical resolution; native pixels need no calibration')
        if not all(self.mpp): raise ValueError('Missing MPP-X/Y: enter a calibrated manual value before preparing')
        if target == 'native': return 0
        # Use a source level at least as fine as target on BOTH axes.
        valid = [i for i,d in enumerate(self.downsamples) if max(self.mpp)*d <= target]
        return valid[-1] if valid else 0

    def overview(self, edge=1024):
        with self.lock:
            if self.demo or self.raster:
                im = self.image.copy()
                im.thumbnail((edge,edge))
                return im
            return rgb(self.handle.get_thumbnail((edge,edge)))

    def tile(self, rect, target, edge=512):
        x,y,w,h = rect
        level = self.level_for_mpp(target)
        ds = self.downsamples[level]
        with self.lock:
            if self.demo or self.raster: image = self.image.crop((x,y,x+w,y+h))
            else: image = rgb(self.handle.read_region((x,y),level,(math.ceil(w/ds),math.ceil(h/ds))))
        # Partial tiles preserve physical scale and are padded, never stretched.
        target_x,target_y = self.mpp if target == 'native' else (target,target)
        ow = max(1,min(edge,round(w*self.mpp[0]/target_x)))
        oh = max(1,min(edge,round(h*self.mpp[1]/target_y)))
        if target != 'native':
            image = rgb(image).resize((ow,oh),Image.Resampling.LANCZOS,
                                     box=(0,0,w if (self.demo or self.raster) else w/ds,h if (self.demo or self.raster) else h/ds))
        if image.size == (edge,edge): return image,[ow,oh],level
        canvas = Image.new('RGB',(edge,edge),'white')
        canvas.paste(image,(0,0))
        return canvas, [ow,oh], level

    def grid(self,target,edge=512):
        self.level_for_mpp(target)
        width,height = self.dimensions
        # Round boundaries independently, avoiding cumulative drift.
        target_x,target_y = self.mpp if target == 'native' else (target,target)
        step_x,step_y = edge*target_x/self.mpp[0],edge*target_y/self.mpp[1]
        if min(step_x,step_y) < 1: raise ValueError('Requested scale is below one source pixel per tile')
        for row in range(math.ceil(height/step_y)):
            y = round(row*step_y)
            for col in range(math.ceil(width/step_x)):
                x = round(col*step_x)
                yield [x,y,min(width,round((col+1)*step_x))-x,min(height,round((row+1)*step_y))-y]

    def dzi(self):
        if not (self.demo or self.raster): return self.dz.get_dzi('jpeg')
        w,h = self.dimensions
        return f'<Image TileSize="254" Overlap="1" Format="jpeg" xmlns="http://schemas.microsoft.com/deepzoom/2008"><Size Width="{w}" Height="{h}"/></Image>'

    def deepzoom_tile(self,level,col,row):
        with self.lock:
            if not (self.demo or self.raster): return self.dz.get_tile(level,(col,row))
            max_level = math.ceil(math.log2(max(self.dimensions)))
            if not 0 <= level <= max_level: raise ValueError('Invalid level')
            scale = 2**(max_level-level)
            w,h = (math.ceil(d/scale) for d in self.dimensions)
            if col < 0 or row < 0 or col*254 >= w or row*254 >= h: raise ValueError('Invalid tile')
            x0,y0 = max(0,col*254-1),max(0,row*254-1)
            x1,y1 = min(w,(col+1)*254+1),min(h,(row+1)*254+1)
            return self.image.crop((x0*scale,y0*scale,min(x1*scale,self.dimensions[0]),min(y1*scale,self.dimensions[1]))).resize((x1-x0,y1-y0))
