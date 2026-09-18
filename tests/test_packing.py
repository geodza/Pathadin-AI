import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from zsendo.slide import Slide
from zsendo.fields import prepare_fields
from zsendo.packing import repack
from zsendo.bundle import verify_images
class PackingTests(unittest.TestCase):
 def test_split_preserves_fields_pixels_and_safe_batches(self):
  with tempfile.TemporaryDirectory() as tmp:
   s=Slide(demo=True);m=prepare_fields(s,tmp,{'mode':'physical','size':500,'output_edge':1024,'detail':True,'batch_size':4},None,lambda *a:None,lambda:False)
   before={t['id']:(Path(tmp)/(t['id']+'.png')).read_bytes() for t in m['tiles']}
   original=len(m['batches']);maxsize=max(l['encoded_image_bytes'] for l in m['batch_locators'])
   # Use an artificial smaller guard to exercise the same production splitter.
   import copy
   singles=copy.deepcopy(m);singles['batches']=[[t['id']] for t in m['tiles']]
   single=repack(tmp,singles)
   limit=max(l['encoded_image_bytes'] for l in single['batch_locators'])+100
   self.assertLess(limit,maxsize)
   packed=repack(tmp,m,limit=limit)
   self.assertGreater(len(packed['batches']),original)
   self.assertEqual([t for ids in packed['batches'] for t in ids],[t for ids in m['batches'] for t in ids])
   for t,b in before.items():self.assertEqual((Path(tmp)/(t+'.png')).read_bytes(),b)
   for item in packed['batch_locators']:self.assertLessEqual(item['encoded_image_bytes'],limit)
   for ids in packed['batches']:verify_images(tmp,packed,ids)
 def test_single_field_guard(self):
  with tempfile.TemporaryDirectory() as tmp:
   m=prepare_fields(Slide(demo=True),tmp,{'mode':'relative','size':100,'output_edge':1024,'detail':False,'batch_size':1},None,lambda *a:None,lambda:False)
   with self.assertRaisesRegex(ValueError,'One field'):repack(tmp,m,limit=1)
