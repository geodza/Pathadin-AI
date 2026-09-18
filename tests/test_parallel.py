import asyncio
import tempfile
import unittest
from unittest.mock import patch
from pathadinai.engine import Engine
from pathadinai.slide import Slide
from pathadinai import adapters

class ParallelTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_limit_order_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine=Engine(tmp);jid=engine.new('s','organ','open',1,1,False)
            engine.prepare(jid,Slide(demo=True))
            original=adapters.call;active=0;peak=0
            async def tracked(*args,**kwargs):
                nonlocal active,peak
                active+=1;peak=max(peak,active)
                try: return await original(*args,**kwargs)
                finally: active-=1
            models=[{'provider':'demo','model':'fixture'}]
            with patch.object(adapters,'call',tracked):
                await engine.run(jid,models,200,8000,4)
            result=engine.get(jid)['models'][0]
            self.assertEqual(result['status'],'complete');self.assertGreater(peak,1);self.assertLessEqual(peak,4)
            for i,ids in enumerate(engine.get(jid)['manifest']['batches']):
                self.assertEqual(result['calls'][f'batch-{i:06d}']['read']['assessed_tile_ids'],ids)
            count=result['request_attempts']
            await engine.run(jid,models,200,8000,2)
            self.assertEqual(engine.get(jid)['models'][0]['request_attempts'],count)
            await engine.run(jid,models,200,8000,4,False)
            self.assertGreater(engine.get(jid)['models'][0]['request_attempts'],count)
