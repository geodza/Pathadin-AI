import unittest,tempfile,io,asyncio
from pathlib import Path
from PIL import Image,ImageDraw
from pathadinai.slide import Slide
from pathadinai.fields import plan,field_images,prepare_fields,fragment_bounds,locator
from pathadinai.tissue_mask import TissueMask
from pathadinai.bundle import verify_images
from pathadinai.engine import Engine
from pathadinai.protocol import digest

class FieldTests(unittest.TestCase):
 def test_physical_geometry_anisotropic(self):
  s=Slide(demo=True);s.mpp=[.125,.25]
  p=plan(s,{'mode':'physical','size':500,'output_edge':1024,'detail':True,'batch_size':4})
  f=p['fields'][0]
  self.assertEqual(f['rect'],[0,0,4000,2000]);self.assertEqual(f['content_pixels'],[1024,512]);self.assertEqual(f['physical_size_um'],[500,500])
  self.assertEqual(f['detail_rect'][2:],[512,512])
 def test_small_fragment_still_gets_large_physical_context(self):
  s=Slide(demo=True)
  im=Image.new('L',(100,100));ImageDraw.Draw(im).rectangle((0,0,1,1),fill=255)
  mask=TissueMask(im,{'slide_dimensions':list(s.dimensions),'revision':'test'})
  p=plan(s,{'mode':'physical','size':500,'output_edge':1024,'detail':True,'batch_size':4},mask)
  self.assertEqual(p['fields'][0]['rect'],[0,0,1000,1000])
 def test_relative_fragments_and_hash(self):
  s=Slide(demo=True);s.dimensions=(100,100)
  im=Image.new('L',(100,100));d=ImageDraw.Draw(im);d.rectangle((0,0,19,19),fill=255);d.rectangle((60,60,99,99),fill=255)
  mask=TissueMask(im,{'slide_dimensions':[100,100],'revision':'test'})
  self.assertEqual(fragment_bounds(mask),[[0,0,20,20],[60,60,40,40]])
  settings={'mode':'relative','size':50,'output_edge':1024,'detail':False,'batch_size':4}
  p=plan(s,settings,mask)
  self.assertEqual(len(p['fields']),8);self.assertEqual(p['fields'][0]['rect'][2:],[10,10]);self.assertEqual(p['fields'][4]['rect'][2:],[20,20])
  self.assertEqual(len(p['batches']),2)
  settings={**settings,'size':25};self.assertNotEqual(p['plan_hash'],plan(s,settings,mask)['plan_hash'])
 def test_preview_equals_prepared_bytes(self):
  with tempfile.TemporaryDirectory() as tmp:
   image=Path(tmp)/'test.png';Image.new('RGB',(1200,700),'pink').save(image);s=Slide(image)
   settings={'mode':'relative','size':50,'output_edge':1024,'detail':True,'batch_size':2}
   p=plan(s,settings);expected=field_images(s,p['fields'][0])
   m=prepare_fields(s,Path(tmp)/'bundle',settings,None,lambda *a:None,lambda:False)
   actual=dict(verify_images(Path(tmp)/'bundle',m,m['batches'][0]))
   for tid,data in expected:self.assertEqual(actual[tid],data)
   self.assertEqual(m['plan_hash'],p['plan_hash']);self.assertIsNone(m['tiles'][0]['effective_mpp'])
   self.assertEqual(actual['locator-000000'],locator(s,p['fields'][:2]))
   with self.assertRaises(ValueError):plan(s,{**settings,'mode':'physical','size':500})
 def test_native_guard(self):
  s=Slide(demo=True);s.mpp=[.01,.01];s.dimensions=(20000,20000)
  with self.assertRaises(ValueError):plan(s,{'mode':'physical','size':1000,'output_edge':0,'detail':False,'batch_size':1})

class FieldEngineTests(unittest.IsolatedAsyncioTestCase):
 async def test_v2_pipeline(self):
  with tempfile.TemporaryDirectory() as tmp:
   e=Engine(tmp);s=Slide(demo=True);jid=e.new('slide','testis','open','native',4,False)
   e.jobs[jid]['settings']['field_settings']={'mode':'physical','size':500,'output_edge':1024,'detail':True,'batch_size':4}
   e.prepare(jid,s);self.assertEqual(e.get(jid)['status'],'prepared')
   await e.run(jid,[{'provider':'demo','model':'fixture'}],100,8000,4)
   j=e.get(jid);self.assertEqual(j['status'],'complete');self.assertEqual(j['manifest']['protocol'],'wsi-fields-v2')
   self.assertEqual(j['manifest']['fingerprint'],digest({k:v for k,v in j['manifest'].items() if k!='fingerprint'}))
