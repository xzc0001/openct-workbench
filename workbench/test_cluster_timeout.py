import json,queue,sys,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parent))
from cluster_bridge import ClusterBridge
class TimeoutFallback(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.job=self.root/('a'*32);self.job.mkdir();self.status={'state':'running','cluster_managed':True,'cluster_revision':1};self.events=[];self.q=queue.Queue();self.snap={'state':'active','job_revision':1,'stages':[{'status':'ready'}],'leases':[]}
  (self.job/'status.json').write_text(json.dumps(self.status))
  def read(path):return json.loads(path.read_text())
  def write(path,val):path.write_text(json.dumps(val))
  def update(job,**values):
   self.events.append('update');d=read(job/'status.json');d.update(values);write(job/'status.json',d)
  self.bridge=object.__new__(ClusterBridge);self.bridge.unavailable_since={self.job.name:time.monotonic()-61};self.bridge.snapshot=lambda _:self.snap;self.bridge.can_schedule=lambda *_:False
  self.bridge.coordinator=SimpleNamespace(reap_expired=lambda:None,get_settings=lambda:{'enabled':True,'local_fallback':True},list_nodes=lambda:[],cancel_job=lambda _:self.events.append('cancel'))
  self.bridge.app=SimpleNamespace(JOBS=self.root,LOCK=threading.RLock(),read=read,write=write,update=update,QUEUE=self.q)
 def tearDown(self):self.temp.cleanup()
 def test_no_available_node_invalidates_then_requeues_locally(self):
  self.bridge.sync_progress();self.assertEqual(self.events[:2],['cancel','update']);self.assertEqual(self.q.get_nowait(),(self.job,'run'))
  status=json.loads((self.job/'status.json').read_text());self.assertEqual(status['state'],'queued');self.assertTrue(status['cluster_force_local']);self.assertFalse(status['cluster_managed'])
 def test_active_lease_is_not_cancelled(self):
  self.snap['leases']=[{'state':'active','node_id':'n'}];self.bridge.sync_progress();self.assertTrue(self.q.empty());self.assertNotIn('cancel',self.events)
 def test_compatible_waiting_node_is_not_cancelled(self):
  self.bridge.can_schedule=lambda *_:True;self.bridge.sync_progress();self.assertTrue(self.q.empty());self.assertNotIn('cancel',self.events)
 def test_completed_stages_do_not_switch_during_publication(self):
  self.snap['stages']=[{'status':'completed'}];self.bridge.sync_progress();self.assertTrue(self.q.empty());self.assertNotIn('cancel',self.events)
if __name__=='__main__':unittest.main()
