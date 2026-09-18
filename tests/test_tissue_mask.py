import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image, ImageDraw
from zsendo.tissue_mask import TissueMask, MaskStore, detect, encode
from zsendo.bundle import prepare
from zsendo.slide import Slide

SID='a'*32

class TissueMaskTests(unittest.TestCase):
    def test_white_glass_and_tissue_margin(self):
        im=Image.new('RGB',(32,32),'white')
        self.assertIsNone(detect(im).getbbox())
        im.putpixel((16,16),(230,175,214))
        mask=detect(im)
        self.assertEqual(mask.getbbox(),(13,13,20,20))

    def test_overlap_includes_boundary_pixels_on_both_axes(self):
        im=Image.new('L',(10,20),0);im.putpixel((3,7),255)
        mask=TissueMask(im,{'slide_dimensions':[1000,4000]})
        self.assertTrue(mask.intersects([399,1599,1,1]))
        self.assertTrue(mask.intersects([299,1399,2,2]))
        self.assertFalse(mask.intersects([400,1600,100,200]))
        self.assertFalse(mask.intersects([0,0,300,1400]))

    def test_reviewed_revision_is_immutable_and_edits_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=MaskStore(tmp);im=Image.new('L',(20,20),0);im.putpixel((5,5),255)
            original=store.save(SID,im,[1000,1000])
            edited=Image.open(__import__('io').BytesIO(original.display_png())).copy()
            edited.putpixel((12,12),(42,171,127,255))
            reviewed=store.review(SID,original.metadata['revision'],base64.b64encode(encode(edited)).decode())
            self.assertTrue(reviewed.metadata['reviewed']);self.assertTrue(reviewed.metadata['human_edited'])
            self.assertNotEqual(original.metadata['sha256'],reviewed.metadata['sha256'])
            self.assertEqual(store.load(SID,original.metadata['revision']).image.getpixel((12,12)),0)
            self.assertEqual(store.load(SID).metadata['revision'],reviewed.metadata['revision'])

    def test_empty_review_and_wrong_dimensions_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=MaskStore(tmp);m=store.save(SID,Image.new('L',(10,10)),[100,100])
            for im in [Image.new('RGBA',(10,10)),Image.new('RGBA',(11,10))]:
                with self.assertRaises(ValueError):store.review(SID,m.metadata['revision'],base64.b64encode(encode(im)).decode())

    def test_painted_white_area_is_authoritative_and_other_tiles_not_decoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=MaskStore(Path(tmp)/'masks')
            image=Image.new('L',(3,1),0);image.putpixel((0,0),255)
            mask=store.save(SID,image,[1536,512],reviewed=True)
            slide=Slide(demo=True);slide.dimensions=(1536,512);slide.image=Image.new('RGB',slide.dimensions,'white')
            slide.scan_bounds=[512,0,512,512]
            with patch.object(slide,'tile',wraps=slide.tile) as reader:
                manifest=prepare(slide,Path(tmp)/'bundle','native',8,True,lambda *a:None,lambda:False,tissue_mask=mask)
            self.assertEqual(reader.call_count,1)
            self.assertEqual(manifest['tiles'][0]['rect'],[0,0,512,512])
            self.assertEqual(manifest['coverage']['mask_excluded_tiles'],2)
            self.assertFalse(manifest['filter']['enabled'])
            self.assertEqual(manifest['tissue_mask']['sha256'],mask.metadata['sha256'])
            self.assertTrue((Path(tmp)/'bundle/tissue-mask.png').exists())

    def test_mask_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=MaskStore(tmp);m=store.save(SID,Image.new('L',(2,2),255),[200,200],True)
            (Path(tmp)/SID/(m.metadata['revision']+'.png')).write_bytes(b'changed')
            with self.assertRaises(ValueError):store.load(SID)

if __name__=='__main__':unittest.main()
