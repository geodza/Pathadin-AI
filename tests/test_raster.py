import unittest,tempfile
from pathlib import Path
from PIL import Image
from zsendo.slide import Slide
from zsendo.bundle import prepare
class RasterTests(unittest.TestCase):
 def test_native_png_and_jpeg(self):
  with tempfile.TemporaryDirectory() as tmp:
   for ext in ['png','jpg']:
    path=Path(tmp)/('image.'+ext);Image.new('RGB',(600,300),'pink').save(path)
    slide=Slide(path)
    self.assertFalse(slide.demo);self.assertEqual(slide.info()['mpp'],[None,None])
    self.assertIn('600',slide.dzi());self.assertIsNotNone(slide.deepzoom_tile(10,0,0))
    m=prepare(slide,Path(tmp)/ext,'native',8,False,lambda *a:None,lambda:False)
    self.assertEqual(len(m['tiles']),2);self.assertIsNone(m['coverage']['retained_area_mm2'])
    self.assertIsNone(m['tiles'][0]['physical_size_um'])
    with self.assertRaises(ValueError):slide.level_for_mpp(.5)
