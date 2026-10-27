"""Regression checks for anatomy selection and conservative airway display scope."""
import base64
import copy
import gzip
from pathlib import Path
import tempfile
import unittest

import numpy as np
from airway_cleanup import connected_airways
from structures import PRESETS, SPINE_NAMES, SCAPULA_NAMES, inference_plan, resolve
from viewer_pack import encode_mask
import volume_rois


class AnatomySelectionTests(unittest.TestCase):
    def test_presets_are_defaults_and_extra_anatomy_is_allowed(self):
        self.assertEqual(set(PRESETS['nodule']), {'airways', 'arteries', 'veins', 'nodules'})
        self.assertEqual(PRESETS['ribs'], ['ribs'])
        for mode in ['quick', 'full']:
            selected, _ = resolve({'mode': mode, 'preset': 'ribs', 'structures': ['ribs', 'scapulae', 'heart']})
            self.assertEqual(selected, ['ribs', 'scapulae', 'heart'])
            with self.assertRaises(ValueError): resolve({'mode': mode, 'structures': []})

    def test_bone_only_plan_does_not_compute_lung_heart_or_airway_models(self):
        plan = inference_plan(['ribs'], 'full')
        self.assertEqual({t[1] for t in plan}, {'ribs_highres', 'spine_highres', 'scapulae_highres'})
        self.assertEqual(next(t[2]['roi_subset'] for t in plan if t[1]=='spine_highres'), SPINE_NAMES)
        extra = inference_plan(['ribs', 'scapulae'], 'full')
        self.assertEqual({t[1] for t in extra}, {'ribs_highres', 'spine_highres', 'scapulae_highres'})
        self.assertEqual(next(t[2]['roi_subset'] for t in extra if t[1]=='scapulae_highres'), SCAPULA_NAMES)
        fast = inference_plan(['scapulae'], 'quick')
        self.assertEqual(fast[0][2]['roi_subset'], SCAPULA_NAMES)
        for mode in ['quick', 'full']:
            self.assertIn('lung_vessels', [t[1] for t in inference_plan(['airways'], mode)])

    def test_airway_cleanup_keeps_thin_and_diagonal_connected_branches(self):
        trachea = np.zeros((20, 20, 20), bool); trachea[2:10, 10, 10] = True
        bronchi = np.zeros_like(trachea)
        for i in range(6): bronchi[9+i, 10+i, 10] = True
        bronchi[2:4, 2:4, 2:4] = True
        source_t, source_b = trachea.copy(), bronchi.copy()
        clean, raw, report = connected_airways(trachea, bronchi)
        self.assertTrue(all(clean[9+i, 10+i, 10] for i in range(6)))
        self.assertFalse(clean[2:4, 2:4, 2:4].any())
        self.assertEqual(report['excluded_voxels'], 8)
        self.assertEqual(report['source_components'], 2)
        self.assertTrue(np.array_equal(raw, source_t|source_b))
        self.assertTrue(np.array_equal(trachea, source_t) and np.array_equal(bronchi, source_b))
        with self.assertRaises(ValueError): connected_airways(np.zeros_like(trachea), bronchi)

    def test_airway_display_context_does_not_expand_into_neighboring_air(self):
        airway = np.zeros((18, 18, 18), bool); airway[4:14, 9, 9] = True
        bone = np.zeros_like(airway); bone[4:14, 2, 2] = True
        model_a = {'id':'airways', 'group':'airways'}; model_b = {'id':'spine', 'group':'spine'}
        ct = {'maskGeometry':{'size':[18]*3, 'spacing':[.7,.8,1.1], 'origin':[4.,-8.,2.]}, 'masks':[encode_mask(model_a,airway),encode_mask(model_b,bone)]}
        before = copy.deepcopy(ct)
        with tempfile.TemporaryDirectory() as tmp:
            original = volume_rois.ROOT
            try:
                volume_rois.ROOT = Path(tmp)
                result = volume_rois.context_rois({'models':[model_a,model_b]},ct)
            finally:volume_rois.ROOT = original
        roi = next(r for r in result['rois'] if r['group']=='airways')
        bits=np.unpackbits(np.frombuffer(gzip.decompress(base64.b64decode(roi['data'])),np.uint8),bitorder='little')[:np.prod(roi['size'])]
        self.assertEqual(int(bits.sum()),int(airway.sum()))
        self.assertEqual(roi['context_margin_axis_mm'],[0.,0.,0.])
        mixed=result['rois'][0];labels=np.frombuffer(gzip.decompress(base64.b64decode(mixed['data'])),np.uint8)
        self.assertEqual(int((labels==mixed['model_ids'].index('airways')+1).sum()),int(airway.sum()))
        self.assertEqual(ct,before)


if __name__=='__main__':unittest.main()
