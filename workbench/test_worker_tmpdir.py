"""Regression coverage for Linux multiprocessing socket paths."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import server


@unittest.skipIf(os.name == 'nt', 'AF_UNIX path limit applies to the Linux worker')
class WorkerScratchTests(unittest.TestCase):
    def test_long_job_scratch_uses_short_alias_and_stays_on_data_volume(self):
        with tempfile.TemporaryDirectory() as parent:
            scratch = Path(parent) / ('long-job-directory-' + 'x' * 75) / 'cache'
            scratch.mkdir(parents=True)
            program = (
                "import multiprocessing as m, pathlib, tempfile; "
                "p=pathlib.Path(tempfile.gettempdir())/'scratch-test'; p.write_text('ok'); "
                "manager=m.Manager(); d=manager.dict(); d['ok']=1; "
                "assert d['ok']==1; manager.shutdown()"
            )
            env = os.environ.copy()
            env['TMPDIR'] = str(scratch)
            baseline = subprocess.run([sys.executable, '-c', program], env=env,
                                      capture_output=True, text=True, timeout=30)
            self.assertNotEqual(baseline.returncode, 0)
            self.assertIn('AF_UNIX path too long', baseline.stderr)
            alias, temporary = server.worker_tmpdir(scratch)
            try:
                env['TMPDIR'] = alias
                result = subprocess.run([sys.executable, '-c', program], env=env,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((scratch / 'scratch-test').read_text(), 'ok')
                self.assertEqual(Path(alias).resolve(), scratch.resolve())
            finally:
                temporary.cleanup()
            self.assertFalse(Path(alias).exists())
            self.assertTrue((scratch / 'scratch-test').exists())


if __name__ == '__main__':
    unittest.main()
