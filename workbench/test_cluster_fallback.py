import sys,time,unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parent))
from cluster_bridge import ClusterBridge
class FallbackCheck(unittest.TestCase):
 def setUp(self):
  self.bridge=object.__new__(ClusterBridge)
  self.plan={'engine_id':'e','engine_sha256':'a'*64,'stages':[{'model_ids':[{'id':'m','sha256':'b'*64}],'resources':{'memory_bytes':3000}}]}
  self.bridge.plan=lambda *_:self.plan
  self.node={'node_id':'n','state':'ready','last_seen':time.time(),'capabilities':{'engine':{'id':'e','sha256':'a'*64},'models':[{'id':'m','sha256':'b'*64}],'resources':{'memory_bytes':3000,'threads':1,'cpu_percent':50}}}
  self.bridge.coordinator=SimpleNamespace(get_settings=lambda:{'node_allowances':{}},list_nodes=lambda:[self.node])
 def test_matching_node(self):self.assertTrue(self.bridge.can_schedule(Path('.'),{}))
 def test_insufficient_memory(self):self.node['capabilities']['resources']['memory_bytes']=2999;self.assertFalse(self.bridge.can_schedule(Path('.'),{}))
 def test_wrong_engine(self):self.node['capabilities']['engine']['sha256']='c'*64;self.assertFalse(self.bridge.can_schedule(Path('.'),{}))
 def test_missing_model(self):self.node['capabilities']['models']=[];self.assertFalse(self.bridge.can_schedule(Path('.'),{}))
 def test_stale_node(self):self.node['last_seen']=time.time()-60;self.assertFalse(self.bridge.can_schedule(Path('.'),{}))
 def test_stage_set_requires_a_match_for_every_stage(self):self.plan['stages'].append({'model_ids':[{'id':'other','sha256':'c'*64}],'resources':{'memory_bytes':3000}});self.assertFalse(self.bridge.can_schedule(Path('.'),{}))
 def test_administrator_allowance(self):self.bridge.coordinator.get_settings=lambda:{'node_allowances':{'n':{'memory_bytes':1000}}};self.assertFalse(self.bridge.can_schedule(Path('.'),{}))
if __name__=='__main__':unittest.main()
