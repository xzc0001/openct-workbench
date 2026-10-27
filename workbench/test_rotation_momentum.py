"""Regression for the renderer's optional momentum and demand-driven idle."""
from pathlib import Path
import json
import shutil
import subprocess
import unittest


class RotationMomentumTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'),'Node runtime unavailable on this host')
    def test_pointer_momentum_settles_and_stops_drawing(self):
        script=Path(__file__).parent/'qa/test_rotation_momentum.cjs'
        result=subprocess.run(['node',str(script)],capture_output=True,text=True,check=True,timeout=20)
        reply=json.loads(result.stdout)
        self.assertTrue(all(reply[key] for key in ('default_off','coasts_and_stops','idle_without_redraw','no_jump_when_disabled','quality_opt_out','autoplay_pause','visibility_pause','persisted')))
        self.assertLess(reply['slow_frames'],50)


if __name__=='__main__':unittest.main()
