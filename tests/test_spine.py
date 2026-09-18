import asyncio
import base64
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from PIL import Image
from pathadinai.adapters import payload, call, demo_read
from pathadinai.bundle import prepare, verify_images
from pathadinai.engine import Engine, synthesis_calls
from pathadinai.protocol import validate_read, canonical, digest, geojson, system_prompt
from pathadinai.slide import Slide, is_tissue

def fixture(ids=('t0000000',)):
    return json.loads(demo_read(canonical({'tile_ids':list(ids)})))

class ProtocolTests(unittest.TestCase):
    def test_fast_filter_matches_original_threshold(self):
        import random
        rng=random.Random(7)
        for size in [(128,128),(512,512),(50,70)]:
            im=Image.frombytes('RGB',size,bytes(rng.randrange(256) for _ in range(size[0]*size[1]*3)))
            thumb=im.copy();thumb.thumbnail((128,128))
            pixels=thumb.get_flattened_data() if hasattr(thumb,'get_flattened_data') else thumb.getdata()
            fraction=sum(1 for r,g,b in pixels if max(r,g,b)<225 or (max(r,g,b)-min(r,g,b)>12 and min(r,g,b)<245))/(thumb.width*thumb.height)
            for threshold in [.005,.5,.99]:self.assertEqual(is_tissue(im,threshold),fraction>=threshold)

    def test_workers_preserve_manifest_and_scanner_bounds_only_skip_outside(self):
        with tempfile.TemporaryDirectory() as tmp:
            slide=Slide(demo=True);slide.dimensions=(1536,512);slide.scan_bounds=[512,0,512,512]
            with patch('pathadinai.bundle.is_tissue',return_value=True):
                with patch.object(slide,'tile',wraps=slide.tile) as reader:
                    a=prepare(slide,Path(tmp)/'a','native',8,True,lambda *a:None,lambda:False,workers=1)
                    self.assertEqual(reader.call_count,1)
                b=prepare(slide,Path(tmp)/'b','native',8,True,lambda *a:None,lambda:False,workers=4)
                self.assertEqual(a['fingerprint'],b['fingerprint'])
                self.assertEqual(a['tiles'][0]['rect'],[512,0,512,512])
                self.assertEqual(a['filter']['scanner_bounds_rejected'],2)
                c=prepare(slide,Path(tmp)/'c','native',8,False,lambda *a:None,lambda:False)
                self.assertEqual(len(c['tiles']),3)

    def test_grade_gate_and_menu(self):
        read=fixture(); read.update(primary_dx='endometrioid_carcinoma',grade='G2')
        self.assertEqual(validate_read(canonical(read),['t0000000'],'uterine')[0]['grade'],'G2')
        read['primary_dx']='leiomyosarcoma'
        parsed,flags=validate_read(canonical(read),['t0000000'],'uterine')
        self.assertIsNone(parsed['grade'])
        self.assertEqual(parsed['primary_dx'],'leiomyosarcoma')
        self.assertIn('unsupported_grade_omitted_requires_review',flags)
        self.assertIn('model proposed G2',parsed['limitations'][-1])
        read.update(grade=None,primary_dx='not_on_menu')
        self.assertIn('off_menu',validate_read(canonical(read),['t0000000'],'uterine')[1])

    def test_unknown_or_missing_ids_rejected(self):
        read=fixture()
        with self.assertRaises(ValueError): validate_read(canonical(read),['different'],'open')
        with self.assertRaises(ValueError): validate_read(canonical(read),['t0000000','t0000001'],'open')
        read['regions'][0]['tile_ids']=['invented']
        with self.assertRaises(ValueError): validate_read(canonical(read),['t0000000'],'open')

    def test_count_prose_flag_and_estimate_separation(self):
        read=fixture();read['explanation']='There are 12 mitoses per HPF.'
        self.assertIn('possible_count_in_prose_requires_review',validate_read(canonical(read),['t0000000'],'open')[1])
        read['explanation']='No measured count.'
        read['count_based_estimates']=[{'label':'Ki67','estimate':'20%','note':'estimate only'}]
        self.assertEqual(validate_read(canonical(read),['t0000000'],'open')[1],[])

    def test_prompt_frozen(self):
        p=system_prompt('uterine')
        for text in ['JSON object only','count_based_estimates','insufficient_evidence','organ','No answers']:
            self.assertIn(text,p)
        self.assertEqual(digest(p),digest(system_prompt('uterine')))

    def test_glass_filter(self):
        self.assertFalse(is_tissue(Image.new('RGB',(512,512),'white')))
        self.assertTrue(is_tissue(Image.new('RGB',(512,512),(222,155,194))))
        self.assertTrue(is_tissue(Image.new('RGB',(512,512),(100,100,100))))

    def test_geojson_closed_rings_original_pixels(self):
        m={'slide':{'dimensions':[1000,900],'mpp':[.5,.25]},'tiles':[{'id':'t0000000','rect':[100,200,512,256]}]}
        result=geojson([('final',fixture())],m,'x','hash')
        ring=result['features'][0]['geometry']['coordinates'][0]
        self.assertEqual(ring,[[100,200],[612,200],[612,456],[100,456],[100,200]])
        self.assertEqual(result['coordinate_system']['units'],'pixels')

    def test_anisotropic_grid_covers_edges_without_gaps(self):
        slide=Slide(demo=True);slide.mpp=[.5,.75];slide.dimensions=(2051,1599)
        grid=list(slide.grid(1.0));self.assertEqual(sum(w*h for x,y,w,h in grid),2051*1599)
        self.assertEqual(max(x+w for x,y,w,h in grid),2051)
        self.assertEqual(max(y+h for x,y,w,h in grid),1599)
        slide.downsamples=[1,2,4];self.assertEqual(slide.level_for_mpp(1),0)
        self.assertEqual(slide.level_for_mpp(2),1)
        slide.mpp=[None,.5]
        with self.assertRaises(ValueError):slide.level_for_mpp(1)

    def test_partial_tile_scale_and_white_padding(self):
        slide=Slide(demo=True)
        image,size,level=slide.tile([0,0,200,100],1)
        self.assertEqual(size,[100,50]);self.assertEqual(image.size,(512,512))
        self.assertEqual(image.getpixel((511,511)),(255,255,255))

    def test_native_preserves_pixels_and_both_calibration_axes(self):
        slide=Slide(demo=True);slide.mpp=[0.249,0.251]
        rect=list(slide.grid('native'))[1]
        self.assertEqual(rect,[512,0,512,512])
        image,size,level=slide.tile(rect,'native')
        self.assertEqual(level,0);self.assertEqual(size,[512,512])
        self.assertEqual(image.tobytes(),slide.image.crop((512,0,1024,512)).tobytes())

    def test_manifest_reproducible_and_tamper_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            slide=Slide(demo=True)
            m1=prepare(slide,Path(tmp)/'a',1,8,True,lambda *a:None,lambda:False)
            m2=prepare(slide,Path(tmp)/'b',1,8,True,lambda *a:None,lambda:False)
            self.assertEqual(m1['fingerprint'],m2['fingerprint'])
            self.assertEqual(len({tid for batch in m1['batches'] for tid in batch}),len(m1['tiles']))
            self.assertEqual(m1['coverage']['retained_grid_coverage'],1)
            images=verify_images(Path(tmp)/'a',m1,m1['batches'][0])
            self.assertEqual(images[0][0],'overview')
            (Path(tmp)/'a'/'overview.png').write_bytes(b'corrupt')
            with self.assertRaises(ValueError):verify_images(Path(tmp)/'a',m1,m1['batches'][0])
            m2['tiles'][0]['rect'][0]+=1
            old=m2.pop('fingerprint')
            self.assertNotEqual(digest(m2),old)

    def test_adapter_preserves_bytes_order_and_prompt(self):
        images=[('overview',b'PNG1'),('t01',b'PNG2')]
        for provider in ['openai','anthropic','gemini']:
            _,_,body=payload(provider,'model','SYSTEM','USER',images,8000)
            if provider=='openai':
                blocks=body['input'][0]['content'];data=[base64.b64decode(b['image_url'].split(',')[1]) for b in blocks if b['type']=='input_image'];self.assertEqual(body['instructions'],'SYSTEM')
            elif provider=='anthropic':
                blocks=body['messages'][0]['content'];data=[base64.b64decode(b['source']['data']) for b in blocks if b['type']=='image'];self.assertEqual(body['system'],'SYSTEM')
            else:
                blocks=body['contents'][0]['parts'];data=[base64.b64decode(b['inlineData']['data']) for b in blocks if 'inlineData' in b];self.assertEqual(body['systemInstruction']['parts'][0]['text'],'SYSTEM')
            self.assertEqual(data,[b'PNG1',b'PNG2']);self.assertIn('USER',canonical(body))

class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_exhaustive_hierarchy_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine=Engine(tmp);slide=Slide(demo=True)
            jid=engine.new('slide','Uterine corpus','uterine',1,1,False);engine.prepare(jid,slide)
            n=len(engine.get(jid)['manifest']['batches'])
            self.assertGreater(n,4)
            await engine.run(jid,[{'provider':'demo','model':'fixture-A'},{'provider':'demo','model':'fixture-B'}],100,8000)
            job=engine.get(jid);self.assertEqual(job['status'],'complete')
            for r in job['models']:
                self.assertEqual(r['completed_batches'],n)
                self.assertEqual(len(r['calls']),n+synthesis_calls(n))
                self.assertEqual(len([c for c in r['calls'].values() if c['image_ids']]),n)
                # Every input batch participates at level zero; none is selected by diagnostic score.
                groups=[json.loads(c['user_prompt'])['reports'] for k,c in r['calls'].items() if k.startswith('synthesis-000')]
                self.assertEqual(sum(len(g) for g in groups),n)
                self.assertTrue(r['geojson']['features'])
            for i in range(n):
                key=f'batch-{i:06d}'
                self.assertEqual(job['models'][0]['calls'][key]['request_fingerprint'],job['models'][1]['calls'][key]['request_fingerprint'])
            before=job['models'][0]['request_attempts']
            await engine.run(jid,[{'provider':'demo','model':'fixture-A'}],100,8000)
            self.assertEqual(engine.get(jid)['models'][0]['request_attempts'],before)
            restored=Engine(tmp);self.assertEqual(restored.get(jid)['status'],'complete')

    async def test_budget_and_resume_preserve_completed_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine=Engine(tmp);jid=engine.new('s','organ','open',1,8,False);engine.prepare(jid,Slide(demo=True))
            models=[{'provider':'demo','model':'fixture'}]
            await engine.run(jid,models,1,8000)
            job=engine.get(jid);self.assertEqual(job['status'],'partial');self.assertEqual(job['models'][0]['request_attempts'],1)
            first=job['models'][0]['calls']['batch-000000']['request_fingerprint']
            await engine.run(jid,models,30,8000)
            job=engine.get(jid);self.assertEqual(job['status'],'complete')
            self.assertEqual(job['models'][0]['calls']['batch-000000']['request_fingerprint'],first)
            self.assertEqual(job['models'][0]['request_attempts'],job['estimated_calls_per_model'])

    async def test_cancel_preparation(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine=Engine(tmp);jid=engine.new('s','organ','open',1,8,True);engine.cancel(jid);engine.prepare(jid,Slide(demo=True))
            self.assertEqual(engine.get(jid)['status'],'cancelled')

    async def test_mock_provider_response(self):
        def handler(request):
            data=json.loads(request.content)
            self.assertEqual(data['store'],False)
            return httpx.Response(200,json={'id':'test','model':'resolved','status':'completed',
                'output':[{'type':'message','content':[{'type':'output_text','text':canonical(fixture())}]}],
                'usage':{'input_tokens':10,'output_tokens':20}})
        with patch.dict(os.environ,{'OPENAI_API_KEY':'test-only'}):
            result=await call('openai','requested','system','user',[],8000,lambda:False,transport=httpx.MockTransport(handler))
            self.assertEqual(result['resolved_model'],'resolved');self.assertEqual(result['usage']['input_tokens'],10)

    async def test_other_provider_response_shapes(self):
        responses={
            'anthropic':{'id':'a','model':'resolved-a','stop_reason':'end_turn','content':[{'type':'text','text':canonical(fixture())}],'usage':{'input_tokens':4}},
            'gemini':{'responseId':'g','modelVersion':'resolved-g','candidates':[{'finishReason':'STOP','content':{'parts':[{'text':'hidden','thought':True},{'text':canonical(fixture())}]}}],'usageMetadata':{'promptTokenCount':5}}
        }
        with patch.dict(os.environ,{'ANTHROPIC_API_KEY':'test-a','GEMINI_API_KEY':'test-g'}):
            for provider,data in responses.items():
                result=await call(provider,'model','system','user',[],8000,lambda:False,
                                  transport=httpx.MockTransport(lambda request:httpx.Response(200,json=data)))
                self.assertEqual(json.loads(result['raw'])['primary_dx'],'insufficient_evidence')
                self.assertTrue(result['usage'])

    async def test_retry_attempt_cap_is_enforced_before_next_request(self):
        attempts=[]
        def on_attempt():
            if attempts:raise ValueError('attempt cap')
            attempts.append(1)
        async def no_wait(seconds):pass
        with patch.dict(os.environ,{'OPENAI_API_KEY':'test-only'}),patch('pathadinai.adapters.asyncio.sleep',no_wait):
            with self.assertRaisesRegex(ValueError,'attempt cap'):
                await call('openai','model','system','user',[],8000,lambda:False,
                           transport=httpx.MockTransport(lambda request:httpx.Response(429)),on_attempt=on_attempt)
        self.assertEqual(len(attempts),1)

    async def test_invalid_json_retained_as_failed_not_silent_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine=Engine(tmp);jid=engine.new('s','organ','open',1,8,True);engine.prepare(jid,Slide(demo=True))
            async def bad(*args,**kwargs):return {'raw':'not JSON','usage':{},'attempts':1}
            with patch('pathadinai.adapters.call',bad):await engine.run(jid,[{'provider':'demo','model':'bad'}],100,8000)
            result=engine.get(jid)['models'][0]
            self.assertEqual(result['status'],'failed');self.assertEqual(result['calls']['batch-000000']['raw'],'not JSON')

if __name__=='__main__':unittest.main()
