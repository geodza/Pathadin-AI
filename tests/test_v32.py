import tempfile,unittest
from pathlib import Path
from PIL import Image,ImageDraw
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pathadinai.fields import plan,central_batch
from pathadinai.slide import Slide
from pathadinai.tissue_mask import TissueMask
from pathadinai.engine import Engine
from pathadinai.cache import install

class GeometryTests(unittest.TestCase):
 def test_nominal_scale_and_overlap(self):
  s=Slide(demo=True);s.objective='40';s.mpp=[.25,.25];s.dimensions=(8192,8192)
  settings=dict(mode='magnification',size=10,output_edge=1024,detail=False,batch_size=1)
  p=plan(s,settings);f=p['fields'][0]
  self.assertEqual(f['rect'][2:],[4096,4096]);self.assertEqual(f['physical_size_um'],[1024,1024]);self.assertEqual(f['effective_mpp'],[1,1])
  q=plan(s,{**settings,'overlap':.2});self.assertGreater(len(q['fields']),len(p['fields']));self.assertEqual(q['fields'][1]['rect'][0],3277)
  with self.assertRaises(ValueError):plan(s,{**settings,'size':80})
  with self.assertRaises(ValueError):plan(s,{**settings,'output_edge':0})
  s.objective=None
  with self.assertRaises(ValueError):plan(s,settings)
 def test_center_avoids_small_edge_fragment_and_hole(self):
  s=Slide(demo=True);s.dimensions=(100,100)
  im=Image.new('L',(100,100));d=ImageDraw.Draw(im);d.rectangle((0,0,2,2),fill=255);d.rectangle((20,20,99,99),fill=255);d.rectangle((45,45,75,75),fill=0)
  mask=TissueMask(im,{'slide_dimensions':[100,100],'revision':'test'})
  p=plan(s,dict(mode='relative',size=10,output_edge=1024,detail=False,batch_size=1),mask)
  index=central_batch(p,mask);f=next(f for f in p['fields'] if f['id']==p['batches'][index][0]);x,y,w,h=f['rect']
  self.assertEqual(f['fragment'],2);self.assertEqual(im.getpixel((int(x+w/2),int(y+h/2))),255)

class CacheTests(unittest.TestCase):
 def test_explicit_cleanup_preserves_reports_and_mask_and_protects_active(self):
  with tempfile.TemporaryDirectory() as tmp:
   e=Engine(Path(tmp));jid=e.new('slide','test','open','native',4,False);job=e.jobs[jid];job['status']='complete';e.save(job)
   folder=Path(tmp)/jid/'bundle';folder.mkdir();(folder/'overview.png').write_bytes(b'images');(folder/'tissue-mask.png').write_bytes(b'mask');(folder/'manifest.json').write_text('{}')
   app=FastAPI();install(app,e,e.get)
   with TestClient(app) as c:
    row=c.get('/api/cache').json()['runs'][0]
    self.assertEqual(c.post('/api/cache/clear',json={'run_id':jid,'snapshot':'wrong'}).status_code,409)
    job['status']='running'
    self.assertEqual(c.post('/api/cache/clear',json={'run_id':jid,'snapshot':row['snapshot']}).status_code,409)
    job['status']='complete'
    self.assertEqual(c.post('/api/cache/clear',json={'run_id':jid,'snapshot':row['snapshot']}).status_code,200)
    self.assertFalse((folder/'overview.png').exists());self.assertTrue((folder/'tissue-mask.png').exists());self.assertTrue((Path(tmp)/jid/'job.json').exists());self.assertTrue(e.get(jid)['cache_cleared'])
    self.assertEqual(c.get('/api/cache').json()['runs'],[])
