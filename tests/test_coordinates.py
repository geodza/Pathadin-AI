import unittest
from zsendo.protocol import geojson

class CoordinateExportTests(unittest.TestCase):
    def test_nonzero_bounds_do_not_shift_exported_geometry(self):
        manifest = {'slide': {'dimensions':[10000,20000], 'mpp':[.25,.25],
                    'scanner_bounds':[1200,3400,8000,15000],
                    'bounds_relative_offset':[1200,3400]},
                    'tiles':[{'id':'a','rect':[1536,3584,512,512]}]}
        read = {'regions':[{'tile_ids':['a'],'role':'normal','morphology':'test'}]}
        result = geojson([('final',read)],manifest,'test','hash')
        self.assertEqual(result['features'][0]['geometry']['coordinates'][0][0],[1536,3584])
        self.assertEqual(result['coordinate_system']['bounds_relative_offset'],[1200,3400])
        self.assertFalse(result['coordinate_system']['crop_applied'])

    def test_legacy_manifest_does_not_invent_offset(self):
        result = geojson([],{'slide':{'dimensions':[10,10],'mpp':[1,1]},'tiles':[]},'test','hash')
        self.assertIsNone(result['coordinate_system']['bounds_relative_offset'])
