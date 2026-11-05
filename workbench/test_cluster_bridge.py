"""Synthetic cluster/workbench admission, DAG and ownership regressions."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from http.server import ThreadingHTTPServer
import hashlib,json,tempfile,threading,unittest,urllib.error,urllib.request
import numpy as np
import SimpleITK as sitk
import server
from auth_test_support import fixture,opener
from cluster_bridge import ClusterBridge,logical
from structures import inference_plan
from progressive import structure_dependencies

class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.job=self.root/'jobs'/('a'*32);self.out=self.job/'output';self.out.mkdir(parents=True)
        self.volume=sitk.GetImageFromArray(np.arange(16**3,dtype=np.int16).reshape(16,16,16)-1024);self.volume.SetSpacing((.7,.8,1.2));self.volume.SetOrigin((-15,-20,30))
        for name in ['CT.nii.gz','CT_original.nii.gz']:sitk.WriteImage(self.volume,str(self.out/name))
        self.geometry={'dimensions_xyz':[16]*3,'spacing_xyz_mm':[.7,.8,1.2],'origin_lps_mm':[-15,-20,30],'direction':list(self.volume.GetDirection())};self.geometry['original_geometry']=dict(self.geometry)
        server.write(self.out/'geometry.json',self.geometry);server.write(self.out/'native_instances.json',list(range(16)))
        models=[{'id':hashlib.sha256(f'totalsegmentator-model-v1:Dataset{d}_SyntheticFixture'.encode()).hexdigest()[:32],'name':f'Dataset{d}_SyntheticFixture','sha256':'2'*64,'files':[f'models/Dataset{d}_SyntheticFixture/checkpoint_final.pth']} for d in [117,291,292,293,294,295,297,298,913]]
        normalized=sorted([{k:m[k] for k in ('id','sha256','files')} for m in models],key=lambda m:m['id'])
        self.manifest={'engine_id':'openct.reconstruction.cpu.windows-x64','models':sorted(models,key=lambda m:m['id']),'model_set_sha256':hashlib.sha256(json.dumps(normalized,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
        server.write(self.root/'engine.json',self.manifest)
        self.bridge=object.__new__(ClusterBridge);self.bridge.root=self.root;self.bridge.config={'engine_manifest':'engine.json'};self.bridge.app=SimpleNamespace(read=server.read)
    def tearDown(self):self.tmp.cleanup()
    def test_shared_model_dag_full_rib_native_and_margin_group(self):
        status={'mode':'full','structures':['ribs','airways','arteries','veins','nodules','margin','suspects'],'margin_mm':5}
        data=self.bridge.plan(self.job,status,1);stages={s['stage_key']:s for s in data['stages']}
        self.assertEqual(sum(s['stage_key']=='lung_vessels' for s in data['stages']),1)
        self.assertEqual(set(stages['mesh_ribs']['dependencies']),{'ribs_highres','spine_highres','scapulae_highres'})
        self.assertEqual(stages['mesh_nodules']['structures'],['nodules','margin']);self.assertNotIn('mesh_margin',stages)
        self.assertTrue({s for s in stages if s.startswith('mesh_')}.issubset(stages['candidate_suspects']['dependencies']))
        self.assertEqual(stages['lung_vessels']['parameters']['thread_policy'],'node_budget')
        self.assertTrue(all(s['resources']['threads']==1 for s in stages.values()))
        roles={r['role']:r for r in data['input_manifest']};self.assertEqual(roles['native_ct']['geometry'],self.geometry['original_geometry'])
        self.assertEqual(roles['ct']['sha256'],hashlib.sha256((self.out/'CT.nii.gz').read_bytes()).hexdigest())
        wire=json.dumps([{k:v for k,v in x.items() if k!='source_path'} for x in data['input_manifest']]);self.assertNotIn(str(self.root),wire)
    def test_revision_binds_input_ids_without_changing_hu_or_geometry(self):
        a=self.bridge.plan(self.job,{'mode':'quick','structures':['heart']},1);b=self.bridge.plan(self.job,{'mode':'quick','structures':['heart']},2)
        self.assertNotEqual(a['input_sha256'],b['input_sha256']);self.assertEqual(a['geometry_sha256'],b['geometry_sha256'])
        self.assertEqual([x['sha256'] for x in a['input_manifest']],[x['sha256'] for x in b['input_manifest']])
        np.testing.assert_array_equal(sitk.GetArrayFromImage(sitk.ReadImage(str(self.out/'CT_original.nii.gz'))),sitk.GetArrayFromImage(self.volume))
    def test_real_coordinator_accepts_canonical_plan_and_minimal_stage_inputs(self):
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
        from cluster.api import Coordinator
        data=self.bridge.plan(self.job,{'mode':'full','structures':['heart','ribs','nodules','margin','suspects'],'margin_mm':4},1)
        c=Coordinator(self.root/'cluster/cluster.sqlite3',self.root/'cluster/artifacts')
        try:
            c.enqueue_job(self.job.name,1,**data)
            result=c.job_snapshot(self.job.name)
            self.assertEqual(result['state'],'active');self.assertEqual(len(result['stages']),len(data['stages']))
            roles={r['id']:r['role'] for r in data['input_manifest']}
            for stage in data['stages']:
                actual={roles[i] for i in stage['input_ids']}
                self.assertEqual(actual,{'ct','geometry','native_ct'} if stage['stage_key']=='mesh_ribs' else {'ct','geometry'})
        finally:c.close()
    def test_retry_revision_and_persisted_settings_survive_bridge_restart(self):
        def update(job,**values):
            status=server.read(job/'status.json');status.update(values);server.write(job/'status.json',status)
        app=SimpleNamespace(ROOT=self.root,JOBS=self.root/'jobs',LOCK=threading.RLock(),read=server.read,write=server.write,update=update)
        config={'engine_manifest':'engine.json','database':'cluster/cluster.sqlite3','artifacts':'cluster/artifacts','enabled':False,'local_fallback':False}
        bridge=ClusterBridge(app,config)
        try:
            self.assertEqual(bridge.settings,{'enabled':False,'local_fallback':False})
            bridge.configure({'enabled':True,'local_fallback':True})
            server.write(self.job/'status.json',{'state':'running','mode':'quick','structures':['heart']})
            bridge.submit(self.job);first=bridge.snapshot(self.job)
            bridge.submit(self.job);second=bridge.snapshot(self.job)
            self.assertEqual((first['job_revision'],second['job_revision']),(1,2))
            self.assertEqual(server.read(self.job/'status.json')['cluster_revision'],2)
            self.assertEqual(second['state'],'active')
            unknown=self.root/'jobs'/('f'*32)
            bridge.cancel(unknown);bridge.deleted([unknown.name])
        finally:bridge.close()
        restarted=ClusterBridge(app,config)
        try:
            self.assertEqual(restarted.settings,{'enabled':True,'local_fallback':True})
            self.assertEqual(restarted.snapshot(self.job)['job_revision'],2)
        finally:restarted.close()
    def test_submission_intent_fences_crashes_before_and_after_queue_commit(self):
        def update(job,**values):
            status=server.read(job/'status.json');status.update(values);server.write(job/'status.json',status)
        app=SimpleNamespace(ROOT=self.root,JOBS=self.root/'jobs',LOCK=threading.RLock(),read=server.read,write=server.write,update=update)
        bridge=ClusterBridge(app,{'engine_manifest':'engine.json','database':'cluster/cluster.sqlite3','artifacts':'cluster/artifacts'})
        try:
            server.write(self.job/'status.json',{'state':'running','mode':'quick','structures':['heart']})
            with patch.object(bridge.coordinator,'enqueue_job',side_effect=SystemExit('synthetic process crash')):
                with self.assertRaises(SystemExit):bridge.submit(self.job)
            self.assertFalse(bridge.recoverable(self.job));self.assertIsNone(bridge.snapshot(self.job))
            # Model death immediately after the DB commit, before progress I/O.
            with patch.object(app,'write',side_effect=SystemExit('synthetic process crash')):
                with self.assertRaises(SystemExit):bridge.submit(self.job)
            self.assertTrue(bridge.recoverable(self.job))
            previous=bridge.snapshot(self.job)['job_revision']
            with patch.object(app,'write',side_effect=SystemExit('synthetic process crash')):
                with self.assertRaises(SystemExit):bridge.submit(self.job)
            self.assertEqual(bridge.snapshot(self.job)['job_revision'],previous+1)
            self.assertTrue(bridge.recoverable(self.job))
            # A newer persisted intent must never adopt an older active queue.
            with patch.object(bridge.coordinator,'revise_job',side_effect=SystemExit('synthetic process crash')):
                with self.assertRaises(SystemExit):bridge.submit(self.job)
            self.assertFalse(bridge.recoverable(self.job))
            bridge.cancel(self.job);self.assertNotIn(self.job.name,bridge.active())
        finally:bridge.close()
    def test_late_event_after_deletion_never_recreates_job(self):
        import shutil
        shutil.rmtree(self.job)
        event={'event_id':'f'*32,'job_id':self.job.name,'job_revision':1}
        import time
        self.bridge.last_sync=time.monotonic();self.bridge.publisher_id='test-publisher'
        calls=[];self.bridge.coordinator=SimpleNamespace(claim_publish_event=lambda **kwargs:event,tombstone_job=lambda sid:calls.append(sid),release_publish_event=lambda *args,**kwargs:self.fail('must not retry deleted job'))
        self.bridge.app=SimpleNamespace(JOBS=self.root/'jobs',LOCK=threading.RLock())
        self.bridge.tick();self.assertEqual(calls,[self.job.name]);self.assertFalse(self.job.exists())

class BrowserBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.job=self.root/'jobs'/('d'*32);self.job.mkdir(parents=True)
        server.write(self.job/'status.json',{'state':'completed','id':self.job.name,'kind':'folder','created_at':1,'owner_id':'owner'})
        self.accounts,self.auth_headers=fixture(self.root);self.cluster=SimpleNamespace(active=lambda:{self.job.name},admin_get=lambda:{'settings':{'enabled':False,'local_fallback':False},'nodes':[]})
        self.p=patch.multiple(server,ROOT=self.root,JOBS=self.root/'jobs',AUTH=self.accounts,CLUSTER=self.cluster,CONFIG={'weights':'../models','minimum_free_gb':0},PROCESSES={});self.p.start()
        self.http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);self.thread=threading.Thread(target=self.http.serve_forever);self.thread.start();self.base=f'http://127.0.0.1:{self.http.server_port}';self.client=opener(self.auth_headers)
    def tearDown(self):self.http.shutdown();self.http.server_close();self.thread.join();self.p.stop();self.tmp.cleanup()
    def test_remote_lease_blocks_cache_cleanup_even_after_local_process_exits(self):
        request=urllib.request.Request(self.base+'/api/storage/plan',json.dumps({'action':'cache','job_ids':[]}).encode(),{'Content-Type':'application/json','X-Local-App':'1'})
        with self.assertRaises(urllib.error.HTTPError) as caught:self.client.open(request)
        self.assertEqual(caught.exception.code,400)
    def test_no_node_route_on_human_session_listener(self):
        request=urllib.request.Request(self.base+'/api/cluster/v1/node/claim',b'{}',{'Content-Type':'application/json','X-Local-App':'1','X-Node-ID':'forged'})
        with self.assertRaises(urllib.error.HTTPError) as caught:self.client.open(request)
        self.assertEqual(caught.exception.code,404)

class LocalComputeAdmissionTests(unittest.TestCase):
    def test_preprocess_and_publication_serialize_and_shutdown_rejects_waiter(self):
        entered=threading.Event();release=threading.Event();calls=[];results={}
        def compute(job,command,extra_env=None):
            calls.append(command);entered.set();self.assertTrue(release.wait(3));return 0,None
        stopping=threading.Event()
        def run(name):results[name]=server.execute_worker(Path('synthetic'),name)
        with patch.multiple(server,LOCAL_COMPUTE_LOCK=threading.Lock(),SERVER_STOPPING=stopping),patch.object(server,'_execute_worker',side_effect=compute):
            first=threading.Thread(target=run,args=('cluster_prepare',));first.start();self.assertTrue(entered.wait(3))
            second=threading.Thread(target=run,args=('cluster_apply',));second.start()
            # Wait until the real admission lock has refused the second worker.
            self.assertTrue(second.is_alive());self.assertEqual(calls,['cluster_prepare'])
            stopping.set();second.join(2);self.assertFalse(second.is_alive())
            release.set();first.join(2);self.assertFalse(first.is_alive())
        self.assertEqual(results['cluster_prepare'],(0,None));self.assertEqual(results['cluster_apply'][0],1)
        self.assertEqual(calls,['cluster_prepare'])

class CapturePublicationTests(unittest.TestCase):
    def test_exact_mask_and_geometry_survive_capture_and_trusted_publication(self):
        import worker,progressive,cluster_publication,surface_geometry
        with tempfile.TemporaryDirectory() as tmp:
            job=Path(tmp)/('b'*32);out=job/'output';out.mkdir(parents=True)
            data=np.full((16,18,20),-850,np.int16);data[3:12,4:14,5:16]=30
            ct=sitk.GetImageFromArray(data);ct.SetSpacing((.7,.8,1.2));ct.SetOrigin((-20,-30,40))
            geometry={'dimensions_xyz':list(ct.GetSize()),'spacing_xyz_mm':list(ct.GetSpacing()),'origin_lps_mm':list(ct.GetOrigin()),'direction':list(ct.GetDirection())};geometry['original_geometry']=dict(geometry)
            for name in ['CT.nii.gz','CT_original.nii.gz']:sitk.WriteImage(ct,str(out/name),True)
            mask=np.zeros(data.shape,np.uint8);mask[4:11,5:13,6:15]=1
            image=sitk.GetImageFromArray(mask);image.CopyInformation(ct);(out/'ai/total').mkdir(parents=True);sitk.WriteImage(image,str(out/'ai/total/heart.nii.gz'),True)
            status={'id':job.name,'state':'running','cluster_revision':1,'mode':'quick','structures':['heart'],'created_at':1,'started_at':2,'series_id':'synthetic','kind':'folder'}
            server.write(job/'status.json',status);server.write(job/'inventory.json',{'series':[{'id':'synthetic','files':[]}]});server.write(out/'geometry.json',geometry);server.write(out/'native_instances.json',[])
            with patch.object(worker,'JOB',job):record=worker.export(ct,geometry,out,'quick',partial=True,capture_only=True)
            self.assertFalse((out/'preview.html').exists());self.assertEqual(record['selected'],['heart'])
            record['models'][0].pop('mask_file')
            payload=Path(tmp)/'validated-mesh.json';server.write(payload,record)
            progressive.initialize(job,ct,geometry)
            eid='e'*32;event={'event_id':eid,'job_id':job.name,'job_revision':1,'structures':['heart'],'job_complete':True,'artifacts':[{'id':'c'*32,'type':'mesh_json','role':'surface_export','path':str(payload),'bytes':payload.stat().st_size,'sha256':hashlib.sha256(payload.read_bytes()).hexdigest()}]}
            (job/'cache/cluster-events').mkdir(parents=True);server.write(job/'cache/cluster-events'/(eid+'.json'),event)
            with patch.object(worker,'JOB',job),patch.object(surface_geometry,'build_surface',side_effect=AssertionError('must reuse exact node mesh')):
                cluster_publication.apply_event(job,eid)
            actual=sitk.ReadImage(str(out/'masks/heart.nii.gz'));np.testing.assert_array_equal(sitk.GetArrayFromImage(actual),mask)
            saved_ct=sitk.ReadImage(str(out/'CT.nii.gz'))
            self.assertEqual(actual.GetSpacing(),saved_ct.GetSpacing());self.assertEqual(actual.GetOrigin(),saved_ct.GetOrigin())
            np.testing.assert_array_equal(sitk.GetArrayFromImage(sitk.ReadImage(str(out/'CT_original.nii.gz'))),data)
            self.assertTrue((out/'models/heart.stl').is_file());self.assertTrue((out/'preview.html').is_file())
            scene=server.read(out/'scene.json');self.assertEqual(scene['models'][0]['mask_file'],'heart.nii.gz')

if __name__=='__main__':unittest.main()
