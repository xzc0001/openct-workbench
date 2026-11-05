import json,os,subprocess,sys,unittest
from pathlib import Path
import test_preview_loading as preview_tests
class RefreshPrint(unittest.TestCase):
 def test_metadata_and_old_generation(self):
  x=preview_tests.PreviewLoadingTests('test_bricks_recover_every_native_voxel_and_geometry_byte');x.setUp()
  try:
   (x.job/'status.json').write_text(json.dumps({'state':'completed'}));root=x.job.parent.parent;env=os.environ.copy();env['PYTHONPATH']=str(Path(__file__).resolve().parent)
   subprocess.run([sys.executable,str(Path(__file__).resolve().parent/'qa/refresh_print.py'),str(root)],env=env,check=True,capture_output=True,text=True)
   self.assertIn('print-open',(x.out/'online'/json.loads((x.out/'online/current.json').read_text())['generation']/'index.html').read_text(encoding='utf-8'))
   self.assertTrue((x.out/'online'/x.receipt['online_generation']/'index.json').exists())
  finally:x.tearDown()
if __name__=='__main__':unittest.main()
