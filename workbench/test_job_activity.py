import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from job_activity import model_blocks, process_usage, activity


class JobActivityTests(unittest.TestCase):
    def test_tiles_reset_between_actual_model_phases(self):
        log='Predicting...\r 8%|xx | 1/12 [00:01]\r100%|xxxxxxxx| 12/12 [01:00]'
        self.assertEqual(model_blocks(log), {'current':12,'total':12})
        self.assertIsNone(model_blocks(log+'\nResampling...\n'))
        self.assertEqual(model_blocks(log+'\nResampling...\nPredicting...\r 0%| | 0/5 [00:00]'), {'current':0,'total':5})
        self.assertIsNone(model_blocks('100%|xx| 13/12 [00:01]'))
    def test_usage_includes_live_and_reaped_children_and_io_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for pid, state, counters, children in [(101,'S',[10,20,30,40],'102'),(102,'D',[2,3,4,5],'')]:
                path=root/str(pid);(path/'task'/str(pid)).mkdir(parents=True)
                fields=[state]+['0']*21
                fields[11:15]=map(str,counters)
                (path/'stat').write_text(f'{pid} (worker (name)) '+' '.join(fields))
                (path/'task'/str(pid)/'children').write_text(children)
            with patch('os.sysconf',return_value=100,create=True):
                result=process_usage(101,root)
            self.assertEqual(result,{'cpu_seconds':1.14,'io_waiting':True})
            self.assertIsNone(process_usage(999,root))
    def test_activity_uses_current_log_range_and_elapsed_not_fake_eta(self):
        with tempfile.TemporaryDirectory() as tmp:
            job=Path(tmp);old=b'100%|xxxxxxxx| 10/10 [01:00]\n';new=b' 20%|xx| 2/10 [00:10]'
            (job/'worker.log').write_bytes(old+new)
            process=Mock(pid=101);process.poll.return_value=None
            with patch('job_activity.process_usage',return_value={'cpu_seconds':5.,'io_waiting':False}):
                result=activity(job,{'started_at':10.,'updated_at':1.},{'stage':'segmentation','log_start_bytes':len(old)},process,{'cpu_seconds':4.},now=70.)
            self.assertEqual(result['elapsed_seconds'],60)
            if os.name != 'nt':self.assertTrue(result['cpu_active'])
            self.assertEqual(result['blocks'],{'current':2,'total':10})
            self.assertNotIn('eta',result)


if __name__=='__main__':unittest.main()
