import unittest,tempfile,json
from pathlib import Path
from pathadinai.regions import validate_polygon,measurements
from pathadinai.workspace import context_manifest,CONDITIONS
from pathadinai.slide import Slide
from pathadinai.bundle import verify_images

def rect(x,y,w,h):return [[x,y],[x+w,y],[x+w,y+h],[x,y+h]]
class MeasurementTests(unittest.TestCase):
 def test_union_exclusion_denominator(self):
  regions=[{'kind':'tumour','points':rect(0,0,10,10)},{'kind':'tumour','points':rect(5,0,10,10)},{'kind':'exclusion','points':rect(0,0,2,10)},{'kind':'tissue','points':rect(0,0,20,10)}]
  m=measurements(regions,[.5,.25]);self.assertAlmostEqual(m['tumour_area_px2'],130);self.assertAlmostEqual(m['tissue_area_px2'],180);self.assertAlmostEqual(m['tumour_area_percent'],130/180*100);self.assertAlmostEqual(m['tumour_area_mm2'],130*.5*.25/1e6)
 def test_crossing_edges_of_distinct_polygons(self):
  m=measurements([{'kind':'tumour','points':[[0,0],[4,0],[0,4]]},{'kind':'tumour','points':[[0,0],[4,0],[4,4]]}],[None,None])
  self.assertAlmostEqual(m['tumour_area_px2'],12);self.assertIsNone(m['tumour_area_mm2'])
 def test_invalid_polygon(self):
  with self.assertRaises(ValueError):validate_polygon([[0,0],[10,10],[0,10],[10,0]],(100,100))
  with self.assertRaises(ValueError):validate_polygon(rect(-1,0,10,10),(100,100))
  with self.assertRaises(ValueError):validate_polygon([[0,0],[1,0],[2,0]],(100,100))
 def test_tumour_outside_denominator_does_not_inflate_percent(self):
  m=measurements([{'kind':'tumour','points':rect(0,0,20,20)},{'kind':'tissue','points':rect(0,0,10,10)}],[1,1]);self.assertEqual(m['tumour_area_percent'],100)

class ContextTests(unittest.TestCase):
 def test_conditions_have_exact_requested_views_and_same_center(self):
  s=Slide(demo=True);points=rect(1500,1000,600,600)
  with tempfile.TemporaryDirectory() as tmp:
   for condition,views in CONDITIONS.items():
    folder=Path(tmp)/condition;m=context_manifest(s,points,views,folder)
    self.assertEqual(m['center_pixels'],[1800,1300])
    images=verify_images(folder,m,m['batches'][0]);self.assertEqual(len(images),len(views));self.assertNotIn('overview',[i for i,b in images])
    self.assertEqual([t['view'] for t in m['tiles']],views)
    for t in m['tiles']:
     if t['view']=='native':self.assertEqual(t['content_pixels'],[512,512])
    self.assertNotIn('reference',json.dumps(m))
