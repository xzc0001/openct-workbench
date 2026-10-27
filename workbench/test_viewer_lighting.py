"""Upper-left light follows the observer while retaining surface contrast."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

class ViewerLightingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node runtime unavailable')
    def test_orbit_and_pivot_keep_upper_left_direction_and_shape(self):
        script = Path(__file__).parent/'qa/test_viewer_lighting.cjs'
        reply = subprocess.run(['node', str(script)], capture_output=True, text=True, check=True, timeout=15)
        result = json.loads(reply.stdout)
        self.assertTrue(result['camera_relative'])
        self.assertTrue(result['upper_left'])
        self.assertFalse(result['shadow_casting'])
        self.assertEqual(result['poses'], 12)
        self.assertGreater(result['min_contrast'], 1.5)
        self.assertLess(result['max_gain']/result['min_gain'], 3)

if __name__ == '__main__': unittest.main()
