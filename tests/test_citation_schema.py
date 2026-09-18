import unittest
from zsendo.adapters import response_schema
from zsendo.protocol import canonical
class CitationSchemaTests(unittest.TestCase):
 def test_batch_enum_is_shared_by_all_citations(self):
  s=response_schema(canonical({'stage':'batch','tile_ids':['t2','t1']}))
  self.assertEqual(s['$defs']['EvidenceTileId']['enum'],['t1','t2'])
  for f in [s['properties']['assessed_tile_ids'],s['$defs']['Region']['properties']['tile_ids'],s['$defs']['Followup']['properties']['target_tile_ids']]:
   self.assertEqual(f['items'],{'$ref':'#/$defs/EvidenceTileId'})
 def test_synthesis_uses_report_evidence(self):
  s=response_schema(canonical({'stage':'synthesis','reports':[{'regions':[{'tile_ids':['t7']}]}]}))
  self.assertEqual(s['$defs']['EvidenceTileId']['enum'],['t7'])
 def test_empty_evidence_forbids_regions(self):
  s=response_schema(canonical({'stage':'synthesis','reports':[{'regions':[]}]}))
  self.assertEqual(s['properties']['regions']['maxItems'],0)
