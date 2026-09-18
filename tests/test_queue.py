import os,tempfile,time,unittest,csv,io
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from fastapi.testclient import TestClient
from zsendo.queue import scan_folder

class FolderTests(unittest.TestCase):
 def test_mrxs_companions_not_scanned(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);(p/'case.mrxs').write_bytes(b'');(p/'case').mkdir();(p/'case'/'internal.jpg').write_bytes(b'');(p/'other.png').write_bytes(b'');(p/'nested').mkdir();(p/'nested'/'real.svs').write_bytes(b'')
   self.assertEqual(len(scan_folder(tmp)),2);self.assertEqual(len(scan_folder(tmp,True)),3)

class QueueApiTests(unittest.TestCase):
 def test_review_gate_and_cap_resume_and_csv(self):
  from zsendo.app import app,TOKEN,engine
  from zsendo import adapters
  original=adapters.call
  async def mock(provider,*args,**kwargs):return await original('demo',*args,**kwargs)
  with tempfile.TemporaryDirectory() as tmp,TestClient(app) as c,patch.dict(os.environ,{'OPENAI_API_KEY':'test-only-placeholder'}),patch.object(adapters,'call',mock):
   headers={'x-zsendo-token':TOKEN}
   paths=[]
   for i in range(2):
    p=Path(tmp)/f'slide{i}.png';Image.new('RGB',(64,64),(190,110,150)).save(p);paths.append(str(p))
   body={'entries':[{'path':p,'organ':'test'} for p in paths],'fields':{'mode':'relative','size':100,'output_edge':256,'detail':False,'batch_size':1},'models':[{'provider':'openai','model':'test-fixture'}],'mask_mode':'review','request_cap':1}
   q=c.post('/api/queues',json=body,headers=headers);self.assertEqual(q.status_code,200,q.text);qid=q.json()['id']
   duplicate=c.post('/api/queues',json=body,headers=headers).json();self.assertEqual(duplicate['id'],qid);self.assertTrue(duplicate['reused_queue'])
   def control(action,cap=1):
    r=c.post(f'/api/queues/{qid}/control',json={'action':action,'request_cap':cap},headers=headers);self.assertEqual(r.status_code,200,r.text)
   def wait():
    for _ in range(200):
     r=c.get(f'/api/queues/{qid}').json()
     if r['status'] not in {'running','preflight'}:return r
     time.sleep(.03)
    self.fail('queue did not finish')
   control('preflight');q=wait();self.assertTrue(all(e['status']=='needs_review' for e in q['entries']));self.assertEqual(q['attempts'],0)
   control('start');q=wait();self.assertEqual(q['attempts'],0)
   # Review masks explicitly, not by changing the automatic metadata.
   import base64
   for e in q['entries']:
    sid=e['slide_id'];png=c.get(f'/api/slides/{sid}/mask.png').content
    response=c.post(f'/api/slides/{sid}/mask/review',json={'revision':e['mask_revision'],'png_base64':base64.b64encode(png).decode()},headers=headers);self.assertEqual(response.status_code,200)
   control('start');q=wait();self.assertEqual(q['status'],'paused');self.assertEqual(q['attempts'],1)
   control('start',10);q=wait();self.assertEqual(q['status'],'complete',q);self.assertEqual(q['attempts'],4);self.assertTrue(all(e['mask_reviewed'] for e in q['entries']))
   control('start',10);self.assertEqual(wait()['attempts'],4)
   rows=list(csv.reader(io.StringIO(c.get(f'/api/queues/{qid}/results.csv').text.lstrip('\ufeff'))));self.assertEqual(len(rows),3)

 def test_bad_slide_does_not_stop_automatic_queue(self):
  from zsendo.app import app,TOKEN
  from zsendo import adapters
  original=adapters.call
  async def mock(provider,*args,**kwargs):return await original('demo',*args,**kwargs)
  with tempfile.TemporaryDirectory() as tmp,TestClient(app) as c,patch.dict(os.environ,{'OPENAI_API_KEY':'test-only-placeholder'}),patch.object(adapters,'call',mock):
   p=Path(tmp)/'good.png';Image.new('RGB',(32,32),'pink').save(p);h={'x-zsendo-token':TOKEN}
   body={'entries':[{'path':str(Path(tmp)/'missing.svs'),'organ':'test'},{'path':str(p),'organ':'test'}],'fields':{'mode':'relative','size':100,'output_edge':256,'detail':False},'models':[{'provider':'openai','model':'test'}],'mask_mode':'automatic','request_cap':10}
   q=c.post('/api/queues',json=body,headers=h).json();qid=q['id']
   c.post(f'/api/queues/{qid}/control',json={'action':'start','request_cap':10},headers=h)
   for _ in range(200):
    q=c.get(f'/api/queues/{qid}').json()
    if q['status'] not in {'running','preflight'}:break
    time.sleep(.03)
   self.assertEqual([e['status'] for e in q['entries']],['failed','complete']);self.assertFalse(q['entries'][1]['mask_reviewed'])

 def test_pause_after_current_slide(self):
  import asyncio,threading
  from zsendo.app import app,TOKEN
  from zsendo import adapters
  entered=threading.Event();original=adapters.call
  async def mock(provider,*args,**kwargs):
   entered.set();await asyncio.sleep(.15);return await original('demo',*args,**kwargs)
  with tempfile.TemporaryDirectory() as tmp,TestClient(app) as c,patch.dict(os.environ,{'OPENAI_API_KEY':'test-only-placeholder'}),patch.object(adapters,'call',mock):
   entries=[]
   for i in range(2):
    p=Path(tmp)/f'p{i}.png';Image.new('RGB',(32,32),'pink').save(p);entries.append({'path':str(p),'organ':'test'})
   h={'x-zsendo-token':TOKEN};body={'entries':entries,'fields':{'mode':'relative','size':100,'output_edge':256,'detail':False},'models':[{'provider':'openai','model':'fixture'}],'mask_mode':'automatic'}
   qid=c.post('/api/queues',json=body,headers=h).json()['id']
   c.post(f'/api/queues/{qid}/control',json={'action':'start'},headers=h);self.assertTrue(entered.wait(5))
   c.post(f'/api/queues/{qid}/control',json={'action':'pause'},headers=h)
   for _ in range(150):
    q=c.get(f'/api/queues/{qid}').json()
    if q['status']=='paused':break
    time.sleep(.03)
   self.assertEqual(q['status'],'paused');self.assertEqual(q['entries'][0]['status'],'complete');self.assertEqual(q['entries'][1]['status'],'pending')

class PersistenceTests(unittest.TestCase):
 def test_restarted_queue_waits_for_explicit_resume(self):
  import json
  from fastapi import FastAPI
  from zsendo.queue import install
  from zsendo.app import FieldSettings,ModelInput,OpenInput
  from zsendo.engine import Engine
  with tempfile.TemporaryDirectory() as tmp:
   data=Path(tmp);(data/'queues').mkdir();qid='a'*32
   (data/'queues'/f'{qid}.json').write_text(json.dumps({'id':qid,'status':'running','created_at':'test','entries':[],'current':None,'request_cap':10}))
   engine=Engine(data/'runs');app=FastAPI();install(app,engine,data,lambda b:None,OpenInput,lambda sid:None,None,FieldSettings,ModelInput,lambda:{})
   with TestClient(app) as c:
    q=c.get(f'/api/queues/{qid}').json();self.assertEqual(q['status'],'paused');self.assertFalse(app.state.folder_queue_active())
