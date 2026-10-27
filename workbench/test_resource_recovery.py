"""Health recovery policy, memory admission and bounded request serving."""
from pathlib import Path
import socket
from http.client import HTTPConnection
import tempfile
import threading
import os
from contextlib import nullcontext
import unittest
import urllib.request
from unittest.mock import patch
import server
from resource_control import validate_native_workload, Resources, worker_budget
from health_watchdog import policy, pressured, probe, gateway_commands, WORKER_MAX, MIB

class RecoveryPolicyTests(unittest.TestCase):
    def test_native_gateway_repairs_only_its_service(self):
        with patch.dict(os.environ,{'WORKBENCH_GATEWAY_MODE':'native'}):
            self.assertEqual(gateway_commands('reload_gateway'),[['/usr/sbin/nginx','-c','/etc/reconstruction-nginx.conf','-t'],['systemctl','reload','reconstruction-gateway.service']])
            self.assertEqual(gateway_commands('restart_gateway'),[['systemctl','restart','reconstruction-gateway.service']])

    def test_existing_docker_gateway_recovery_remains_available(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(gateway_commands('restart_gateway'),[['docker','restart','reconstruction-preview']])
            self.assertEqual(gateway_commands('reload_gateway')[0],['docker','exec','reconstruction-preview','nginx','-t'])

    def test_configured_budget_drives_cgroup_and_native_admission(self):
        budget=6*1024**3
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ,{'WORKBENCH_WORKER_BUDGET_BYTES':str(budget)}):
            root=Path(temporary)/'group'; root.mkdir()
            (root/'cgroup.controllers').write_text('cpu memory pids')
            runtime=Path(temporary)/'run'; runtime.mkdir()
            with patch('resource_control.runtime_lock',return_value=nullcontext()):
                Resources(root,runtime).configure()
            self.assertEqual(int((root/'compute/memory.max').read_text()),budget)
            self.assertEqual(worker_budget(),budget)
            validate_native_workload(['ribs'],{'dimensions_xyz':[512,512,473]})
        with patch.dict(os.environ,{'WORKBENCH_WORKER_BUDGET_BYTES':'0'}):
            with self.assertRaises(ValueError): worker_budget()

    def test_pressure_guard_uses_active_limit_after_budget_increase(self):
        state={}
        for now in [100,130,200]:
            self.assertFalse(pressured(state,3*1024**3,800*MIB,80,now,True,limit=4*1024**3))
        self.assertNotIn('pressure_since',state)
        self.assertFalse(pressured(state,4*1024**3,800*MIB,80,220,True,limit=4*1024**3))
        self.assertTrue(pressured(state,4*1024**3,800*MIB,80,280,True,limit=4*1024**3))

    def test_three_failures_then_repair_with_cooldown(self):
        state={}
        self.assertIsNone(policy(state,False,False,100))
        self.assertIsNone(policy(state,False,False,115))
        self.assertEqual(policy(state,False,False,130),'restart_web')
        self.assertIsNone(policy(state,False,False,145))
        self.assertEqual(policy(state,False,False,250),'restart_web')
        self.assertIsNone(policy(state,True,True,265))
        self.assertEqual(state['web_failures'],0)

    def test_gateway_recovers_independently_and_manual_stop_is_respected(self):
        state={}
        for now in [100,115]: self.assertIsNone(policy(state,True,False,now))
        self.assertEqual(policy(state,True,False,130),'reload_gateway')
        self.assertEqual(policy(state,True,False,250),'restart_gateway')
        self.assertIsNone(policy(state,False,False,400,'inactive'))
        self.assertEqual(state['web_failures'],0)

    def test_temporary_pressure_does_not_abort_a_healthy_job(self):
        state={}
        self.assertFalse(pressured(state,WORKER_MAX,800*MIB,80,100,True))
        self.assertFalse(pressured(state,WORKER_MAX,800*MIB,80,130,True))
        self.assertFalse(pressured(state,WORKER_MAX,800*MIB,2,145,True))
        self.assertFalse(pressured(state,WORKER_MAX,800*MIB,80,160,True))
        self.assertTrue(pressured(state,WORKER_MAX,800*MIB,80,220,True))
        self.assertFalse(pressured(state,WORKER_MAX,800*MIB,80,235,False))

    def test_oversize_bone_rejected_without_reducing_source_resolution(self):
        geometry={'original_geometry':{'dimensions_xyz':[512,512,1137]}}
        with self.assertRaisesRegex(ValueError,'工作内存'):
            validate_native_workload(['ribs'],geometry,budget=3*1024**3)
        validate_native_workload(['heart','aorta'],geometry,budget=3*1024**3)
        validate_native_workload(['ribs'],{'dimensions_xyz':[64,64,64]},budget=3*1024**3)
        self.assertEqual(geometry['original_geometry']['dimensions_xyz'],[512,512,1137])

    def test_handler_overload_is_bounded_and_health_recovers(self):
        http=server.BoundedHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            for _ in range(32): self.assertTrue(http.handler_slots.acquire(False))
            with self.assertRaises(urllib.error.HTTPError) as error:
                opener.open(f'http://127.0.0.1:{http.server_port}/api/healthz')
            self.assertEqual(error.exception.code,503)
            self.assertTrue(probe(HTTPConnection('127.0.0.1',http.server_port,timeout=3)))
            for _ in range(32): http.handler_slots.release()
            with opener.open(f'http://127.0.0.1:{http.server_port}/api/healthz') as response:
                self.assertEqual(response.status,200)
        finally:
            http.shutdown();http.server_close();thread.join()

if __name__=='__main__': unittest.main()
