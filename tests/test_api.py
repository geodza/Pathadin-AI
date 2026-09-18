import os
import tempfile
import time
import unittest

# Set before app import: these API tests never write to the user's saved runs.
_temp = tempfile.TemporaryDirectory()
os.environ['PATHADINAI_DATA'] = _temp.name
from fastapi.testclient import TestClient
from pathadinai.app import app, TOKEN

class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client=TestClient(app)
        cls.client.__enter__()
        cls.headers={'x-pathadinai-token':TOKEN}

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None,None,None)

    def test_mutations_require_local_session_token(self):
        self.assertEqual(self.client.post('/api/slides',json={'demo':True}).status_code,403)
        self.assertEqual(self.client.get('/',headers={'host':'evil.example'}).status_code,400)

    def test_mask_review_required_and_preparation_uses_frozen_revision(self):
        import base64
        sid=self.client.post('/api/slides',json={'demo':True},headers=self.headers).json()['id']
        mask=self.client.post(f'/api/slides/{sid}/mask/generate',json={},headers=self.headers).json()
        body={'slide_id':sid,'organ':'test','mask_revision':mask['revision']}
        self.assertEqual(self.client.post('/api/prepare',json=body,headers=self.headers).status_code,400)
        image=self.client.get(f'/api/slides/{sid}/mask.png').content
        reviewed=self.client.post(f'/api/slides/{sid}/mask/review',json={'revision':mask['revision'],'png_base64':base64.b64encode(image).decode()},headers=self.headers).json()
        self.assertTrue(reviewed['reviewed']);self.assertFalse(reviewed['human_edited'])
        body['mask_revision']=reviewed['revision']
        response=self.client.post('/api/prepare',json=body,headers=self.headers)
        self.assertEqual(response.status_code,200);jid=response.json()['id']
        for _ in range(100):
            job=self.client.get(f'/api/runs/{jid}').json()
            if job['status'] not in {'queued','preparing'}:break
            time.sleep(.03)
        self.assertEqual(job['status'],'prepared')
        self.assertEqual(job['manifest']['tissue_mask']['revision'],reviewed['revision'])
        exported=self.client.get(f'/api/runs/{jid}/export').json()
        self.assertIn('tissue_mask_png_base64',exported)

    def test_reopening_unchanged_source_reuses_registration(self):
        from pathlib import Path
        from unittest.mock import Mock,patch
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'case.svs';path.write_bytes(b'fixture')
            slide=Mock();slide.path=str(path);slide.info.return_value={'dimensions':[100,100]}
            with patch('pathadinai.app.Slide',return_value=slide):
                first=self.client.post('/api/slides',json={'path':str(path)},headers=self.headers).json()
                again=self.client.post('/api/slides',json={'path':str(path)},headers=self.headers).json()
                self.assertEqual(first['id'],again['id'])
                path.write_bytes(b'changed fixture')
                changed=self.client.post('/api/slides',json={'path':str(path)},headers=self.headers).json()
                self.assertNotEqual(first['id'],changed['id'])

    def test_demo_routes_and_saved_bundle(self):
        response=self.client.post('/api/slides',json={'demo':True},headers=self.headers)
        self.assertEqual(response.status_code,200)
        slide=response.json();sid=slide['id']
        self.assertEqual(self.client.get(f'/api/slides/{sid}/slide.dzi').status_code,200)
        self.assertEqual(self.client.get(f'/api/slides/{sid}/slide_files/12/0_0.jpeg').status_code,200)
        self.assertEqual(self.client.get(f'/api/slides/{sid}/slide_files/12/-1_0.jpeg').status_code,404)
        r=self.client.post('/api/prepare',json={'slide_id':sid,'organ':'Uterine corpus'},headers=self.headers)
        self.assertEqual(r.status_code,200);jid=r.json()['id']
        for _ in range(100):
            job=self.client.get(f'/api/runs/{jid}').json()
            if job['status'] not in {'queued','preparing'}:break
            time.sleep(.03)
        self.assertEqual(job['status'],'prepared')
        self.assertEqual(job['manifest']['target_mpp'],'native')
        self.assertEqual(job['manifest']['analysis_mpp'],[0.5,0.5])
        self.assertEqual(self.client.post(f'/api/runs/{jid}/start',json={'models':[{'provider':'openai','model':'test'}]},headers=self.headers).status_code,400)
        self.assertEqual(self.client.post(f'/api/runs/{jid}/start',json={'models':[{'provider':'demo','model':'fixture'}]},headers=self.headers).status_code,200)
        for _ in range(100):
            job=self.client.get(f'/api/runs/{jid}').json()
            if job['status']!='running':break
            time.sleep(.03)
        self.assertEqual(job['status'],'complete')
        geo=self.client.get(f'/api/runs/{jid}/geojson/0').json()
        self.assertEqual(geo['type'],'FeatureCollection')
        export=self.client.get(f'/api/runs/{jid}/export').json()
        self.assertIn('system_prompt',export)
        self.assertNotIn('path',export['manifest']['slide'])
        self.assertIn('calls',export['models'][0])

    def test_v2_preview_guard_and_saved_export(self):
        import base64,io,zipfile
        sid=self.client.post('/api/slides',json={'demo':True},headers=self.headers).json()['id']
        settings={'mode':'relative','size':100,'output_edge':1024,'detail':True,'batch_size':4}
        body={'slide_id':sid,'organ':'testis','field_settings':settings}
        self.assertEqual(self.client.post('/api/prepare',json=body,headers=self.headers).status_code,400)
        preview=self.client.post('/api/v2/preview',json={'slide_id':sid,'field_settings':settings},headers=self.headers)
        self.assertEqual(preview.status_code,200)
        data=preview.json();body['preview_plan_hash']=data['plan_hash']
        changed={**body,'field_settings':{**settings,'size':50}}
        self.assertEqual(self.client.post('/api/prepare',json=changed,headers=self.headers).status_code,400)
        response=self.client.post('/api/prepare',json=body,headers=self.headers)
        self.assertEqual(response.status_code,200);jid=response.json()['id']
        for _ in range(150):
            job=self.client.get(f'/api/runs/{jid}').json()
            if job['status'] not in {'queued','preparing'}:break
            time.sleep(.03)
        self.assertEqual(job['status'],'prepared')
        saved=self.client.get(f'/api/runs/{jid}/batch/0').json()
        original={im['id']:im['png_base64'] for im in data['images']}
        for im in saved['images']:
            if im['id'] in original:self.assertEqual(im['png_base64'],original[im['id']])
        archive=self.client.get(f'/api/runs/{jid}/tiles.zip')
        self.assertEqual(archive.status_code,200)
        with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
            self.assertIn('manifest.json',z.namelist())
            self.assertEqual(z.read('t0000000.png'),base64.b64decode(original['t0000000']))

    def test_regions_context_blinding_and_revision_conflict(self):
        sid=self.client.post('/api/slides',json={'demo':True},headers=self.headers).json()['id']
        body={'revision':None,'regions':[{'id':'roi1','name':'NAME_CANARY','kind':'tumour','points':[[1000,1000],[1500,1000],[1500,1500],[1000,1500]]}], 'reference_diagnosis':'REFERENCE_CANARY','reference_note':'SECRET_NOTE'}
        saved=self.client.post(f'/api/slides/{sid}/regions',json=body,headers=self.headers)
        self.assertEqual(saved.status_code,200)
        self.assertEqual(self.client.post(f'/api/slides/{sid}/regions',json=body,headers=self.headers).status_code,409)
        loaded=self.client.get(f'/api/slides/{sid}/regions').json();self.assertEqual(loaded['reference_diagnosis'],'REFERENCE_CANARY')
        e=self.client.post('/api/context/preview',json={'slide_id':sid,'region_id':'roi1','organ':'testis','conditions':['native','500_native']},headers=self.headers)
        self.assertEqual(e.status_code,200);eid=e.json()['id']
        response=self.client.post(f'/api/context/{eid}/start',json={'models':[{'provider':'demo','model':'fixture'}],'concurrency':2},headers=self.headers)
        self.assertEqual(response.status_code,200)
        for _ in range(150):
            status=self.client.get(f'/api/context/{eid}').json()
            if all(r['status']=='complete' for r in status['runs']):break
            time.sleep(.03)
        self.assertTrue(all(r['status']=='complete' for r in status['runs']))
        for r in status['runs']:
            text=self.client.get('/api/runs/'+r['run_id']+'/export').text
            for secret in ['NAME_CANARY','REFERENCE_CANARY','SECRET_NOTE']:self.assertNotIn(secret,text)
            evidence=self.client.get('/api/runs/'+r['run_id']+'/evidence').json()
            self.assertEqual(len(evidence['models'][0]['stages']),2)
        region_export=self.client.get(f'/api/slides/{sid}/regions.geojson').json()
        self.assertEqual(region_export['reference_diagnosis'],'REFERENCE_CANARY')
        self.assertEqual(region_export['features'][0]['geometry']['coordinates'][0][0],region_export['features'][0]['geometry']['coordinates'][0][-1])

    def test_repair_keeps_original_run(self):
        sid=self.client.post('/api/slides',json={'demo':True},headers=self.headers).json()['id']
        settings={'mode':'relative','size':100,'output_edge':1024,'detail':True,'batch_size':4}
        preview=self.client.post('/api/v2/preview',json={'slide_id':sid,'field_settings':settings},headers=self.headers).json()
        result=self.client.post('/api/prepare',json={'slide_id':sid,'organ':'test','field_settings':settings,'preview_plan_hash':preview['plan_hash']},headers=self.headers).json()
        jid=result['id']
        for _ in range(150):
            job=self.client.get(f'/api/runs/{jid}').json()
            if job['status']=='prepared':break
            time.sleep(.03)
        old=self.client.get(f'/api/runs/{jid}/export').json()
        repaired=self.client.post(f'/api/runs/{jid}/repair-batches',json={},headers=self.headers)
        self.assertEqual(repaired.status_code,200)
        new=self.client.get('/api/runs/'+repaired.json()['id']+'/export').json()
        self.assertEqual(new['parent_run_id'],jid);self.assertEqual(new['status'],'prepared')
        self.assertEqual(new['manifest']['tiles'],old['manifest']['tiles'])
        self.assertEqual(self.client.get(f'/api/runs/{jid}/export').json(),old)

if __name__=='__main__':unittest.main()
