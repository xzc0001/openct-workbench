import json,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import server
class OneTask:
 def __init__(self,job):self.job=job;self.count=0
 def get(self):
  self.count+=1
  if self.count>1:raise SystemExit()
  return self.job,'run'
 def task_done(self):pass
class QueueFallback(unittest.TestCase):
 def run_case(self,nodes,expected):
  with tempfile.TemporaryDirectory() as tmp:
   job=Path(tmp)/('a'*32);(job/'output').mkdir(parents=True);server.write(job/'status.json',{'state':'queued'});calls=[]
   cluster=SimpleNamespace(settings={'enabled':True,'local_fallback':True},coordinator=SimpleNamespace(list_nodes=lambda:nodes),can_schedule=lambda *args:False,submit=lambda *_:self.fail('incompatible task submitted'))
   def worker(job,command,*args,**kwargs):
    calls.append(command)
    if command=='run':(job/'output/preview.html').write_text('synthetic')
    return 0,None
   with patch.multiple(server,QUEUE=OneTask(job),CLUSTER=cluster),patch.object(server,'execute_worker',side_effect=worker),patch.object(server,'finish_update',return_value=False):
    with self.assertRaises(SystemExit):server.work()
   self.assertEqual(calls,expected);self.assertEqual(server.read(job/'status.json')['state'],'completed')
 def test_no_node_runs_locally(self):self.run_case([],['run'])
 def test_online_incompatible_node_runs_locally_after_preparation(self):self.run_case([{'state':'ready','last_seen':time.time()}],['cluster_prepare','run'])
if __name__=='__main__':unittest.main()
