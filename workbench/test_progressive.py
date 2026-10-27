"""Progressive geometry, native-HU fidelity, dependency ordering and failure state."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from http.server import ThreadingHTTPServer
import base64,gzip,hashlib,json,tempfile,threading,unittest,urllib.request
import numpy as np
import SimpleITK as sitk
from auth_test_support import fixture,opener
import progressive
import server
import worker
from preview_access import Preview

class ProgressiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.accounts,self.auth_headers=fixture(self.root)
        self.job=self.root/'jobs'/('c'*32);self.out=self.job/'output';self.out.mkdir(parents=True)
        self.data=(np.arange(16**3).reshape(16,16,16)-1024).astype(np.int16)
        self.volume=sitk.GetImageFromArray(self.data);self.volume.SetSpacing((.7,.8,1.2));self.volume.SetOrigin((-20,-15,-10))
        for name in ['CT.nii.gz','CT_original.nii.gz']:sitk.WriteImage(self.volume,str(self.out/name),True)
        self.geometry={'dimensions_xyz':[16]*3}
        self.status={'id':self.job.name,'state':'running','mode':'full','structures':['heart','aorta'],'started_at':100,'created_at':1,'kind':'folder','series_id':'s'}
        progressive.save(self.job/'status.json',self.status);progressive.save(self.job/'inventory.json',{'series':[{'id':'s','files':[]}]})
        progressive.save(self.job/'progress.json',{'stage':'segmentation','percent':20})
        progressive.save(self.out/'geometry.json',self.geometry);progressive.save(self.out/'native_instances.json',[])
        self.folder=progressive.initialize(self.job,self.volume,self.geometry)
        pointer=json.loads((self.folder/'online/current.json').read_text(encoding='utf-8'));self.native=Preview(self.folder/'online'/pointer['generation'])
        enc=lambda a:base64.b64encode(gzip.compress(a.tobytes(),mtime=0)).decode()
        self.model={'id':'heart','name':'心脏','group':'heart','encoding':'gzip','vertices':3,'triangles':1,'positions':enc(np.array([[0,0,0],[1,0,0],[0,1,0]],'<f4')),'normals':enc(np.array([[0,0,1]]*3,'<f4')),'indices':enc(np.array([0,1,2],'<u4')),'color':[1,.2,.2],'opacity':.5}
    def tearDown(self):self.temp.cleanup()
    def test_ct_readable_before_any_model_and_never_repacked_on_publish(self):
        self.assertEqual(self.native.index['scene']['models'],[])
        self.assertTrue(self.native.ct['original'])
        p=lambda q:self.volume.TransformContinuousIndexToPhysicalPoint(q)
        request={'width':16,'height':16,'p00':p((0.,0.,8.)),'p10':p((15.,0.,8.)),'p01':p((0.,15.,8.))}
        np.testing.assert_array_equal(self.native.plane(request),self.data[8])
        source=self.native.directory/'source.html';digest=hashlib.sha256(source.read_bytes()).hexdigest()
        progressive.publish(self.out,[self.model],[])
        first=list((self.folder/'mesh').iterdir())
        progressive.publish(self.out,[{**self.model,'target_id':'candidate_1'},{**self.model,'id':'aorta','group':'aorta'}],[])
        self.assertEqual(len(list((self.folder/'mesh').iterdir())),len(first))
        record=json.loads((self.folder/'snapshot.json').read_text(encoding='utf-8'));self.assertEqual(len(record['models']),2)
        self.assertEqual(record['models'][0]['target_id'],'candidate_1')
        positions=record['models'][0]['positions'];self.assertEqual(gzip.decompress((self.folder/'mesh'/positions['asset']).read_bytes()),gzip.decompress(base64.b64decode(self.model['positions'])))
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),digest)
        self.assertFalse(record['complete']);progressive.finish(self.out)
        self.assertTrue(json.loads((self.folder/'snapshot.json').read_text(encoding='utf-8'))['complete'])
    def test_dependencies_do_not_publish_partial_airways_or_separated_ribs(self):
        self.assertEqual(progressive.ready_structures(['airways','arteries'],'full',{'trachea_highres'}),[])
        self.assertEqual(progressive.ready_structures(['airways','arteries'],'full',{'trachea_highres','lung_vessels'}),['airways','arteries'])
        self.assertEqual(progressive.ready_structures(['ribs'],'full',{'ribs_highres','spine_highres'}),[])
        self.assertEqual(progressive.ready_structures(['ribs'],'full',{'ribs_highres','spine_highres','scapulae_highres'}),['ribs'])
    def test_placeholders_follow_real_stages_and_terminal_states(self):
        read=lambda:json.loads((self.folder/'snapshot.json').read_text(encoding='utf-8'))
        self.assertEqual([p['phase'] for p in read()['plan']],['waiting','waiting'])
        progressive.set_phase(self.out,'segmenting',task='heart_highres',mode='full',label='心脏分割')
        self.assertEqual([p['phase'] for p in read()['plan']],['segmenting','waiting'])
        progressive.set_phase(self.out,'meshing',structures=['heart'])
        self.assertEqual(read()['plan'][0]['phase'],'meshing')
        progressive.publish(self.out,[self.model],[])
        progressive.cache_export(self.out,['heart'],[self.model],[],[],[])
        record=read();self.assertEqual(record['plan'][0]['phase'],'ready');self.assertEqual(record['plan'][0]['model_ids'],['heart'])
        progressive.set_phase(self.out,'segmenting',task='aorta_highres',mode='full')
        failed=progressive.presentation_plan(read(),{'state':'completed','error':'rollback'})
        self.assertEqual([p['phase'] for p in failed],['ready','interrupted'])
        cancelled=progressive.presentation_plan(read(),{'state':'cancelled'})
        self.assertEqual([p['phase'] for p in cancelled],['ready','stopped'])
        progressive.finish(self.out)
        self.assertEqual([p['phase'] for p in read()['plan']],['ready','empty'])
    def test_empty_predictions_end_placeholder_without_fabricating_geometry(self):
        progressive.cache_export(self.out,['heart'],[],[],[],['心脏'])
        record=json.loads((self.folder/'snapshot.json').read_text(encoding='utf-8'))
        self.assertEqual(record['models'],[]);self.assertEqual(record['plan'][0]['phase'],'empty')
        self.assertEqual(record['plan'][0]['model_ids'],[])
    def test_http_live_entry_assets_and_failure_snapshot(self):
        progressive.publish(self.out,[self.model],[])
        with patch.multiple(server,AUTH=self.accounts,JOBS=self.root/'jobs'):
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
            client=opener(self.auth_headers);base=f'http://127.0.0.1:{http.server_port}'
            path=f'/results/{self.job.name}/live/{self.folder.name}'
            try:
                self.assertEqual(server.public(self.job)['live_preview'],path+'/preview.html')
                page=client.open(base+path+'/preview.html').read().decode();self.assertIn(path+'/view/',page)
                record=json.load(client.open(base+path+'/snapshot'));self.assertFalse(record['terminal'])
                data=client.open(base+record['models'][0]['positions']['url']).read();self.assertEqual(gzip.decompress(data),gzip.decompress(base64.b64decode(self.model['positions'])))
                progressive.save(self.job/'status.json',{**self.status,'state':'failed','error':'test memory limit'})
                record=json.load(client.open(base+path+'/snapshot'));self.assertTrue(record['terminal']);self.assertFalse(record['complete']);self.assertIsNone(record['preview'])
                self.assertEqual(client.open(base+path+'/preview.html').status,200)
            finally:http.shutdown();http.server_close();thread.join()
    def test_pipeline_interleaves_inference_and_publication_with_released_children(self):
        digest=hashlib.sha256((self.out/'CT.nii.gz').read_bytes()).hexdigest()
        progressive.save(self.job/'status.json',{**self.status,'resume_ct_sha256':digest})
        calls=[]
        def child(args,**kwargs):calls.append((args[2],args[4:],int(kwargs['env']['WORKBENCH_PHASE_PERCENT'])));return SimpleNamespace(returncode=0)
        with patch.object(worker,'JOB',self.job),patch.object(progressive,'initialize') as initialize,patch.object(worker.subprocess,'run',side_effect=child),patch.object(worker,'export') as export:
            worker.run()
        initialize.assert_called_once();export.assert_called_once()
        self.assertEqual([row[0] for row in calls],['infer_one','publish','infer_one','publish'])
        self.assertEqual([calls[0][1],calls[2][1]],[['heart_highres'],['aorta_highres']])
        self.assertEqual(json.loads(calls[1][1][0]),['heart']);self.assertEqual(json.loads(calls[3][1][0]),['aorta'])
        self.assertEqual(sorted(row[2] for row in calls),[row[2] for row in calls])
    def test_partial_and_final_exports_keep_identical_reference_geometry(self):
        mask=np.zeros(self.data.shape,np.uint8);mask[3:12,4:11,5:12]=1
        image=sitk.GetImageFromArray(mask);image.CopyInformation(self.volume)
        labels=self.out/'ai/heart_highres';labels.mkdir(parents=True);sitk.WriteImage(image,str(labels/'heart.nii.gz'),True)
        progressive.save(self.job/'status.json',{**self.status,'structures':['heart']})
        with patch.object(worker,'JOB',self.job):
            worker.export(self.volume,self.geometry,self.out,'full',selected_override=['heart'],partial=True)
            self.assertFalse((self.out/'preview.html').exists())
            before=hashlib.sha256((self.out/'models/heart.stl').read_bytes()).hexdigest()
            modified=(self.out/'models/heart.stl').stat().st_mtime_ns
            worker.export(self.volume,self.geometry,self.out,'full')
        self.assertEqual(hashlib.sha256((self.out/'models/heart.stl').read_bytes()).hexdigest(),before)
        self.assertEqual((self.out/'models/heart.stl').stat().st_mtime_ns,modified)
        self.assertTrue((self.out/'preview.html').is_file())
        record=json.loads((self.folder/'snapshot.json').read_text(encoding='utf-8'));self.assertTrue(record['complete']);self.assertEqual(len(record['models']),1)
    def test_interrupted_update_restores_previous_preview_and_keeps_live_ct(self):
        from study_updates import begin
        (self.out/'preview.html').write_bytes(b'previous verified preview')
        progressive.save(self.out/'scene.json',{'models':[]});progressive.save(self.out/'manifest.json',{})
        begin(self.job,{**self.status,'state':'completed'})
        progressive.publish(self.out,[self.model],[])
        progressive.save(self.job/'status.json',{**self.status,'inplace_operation':'run'})
        with patch.multiple(server,AUTH=self.accounts,JOBS=self.root/'jobs'):
            server.finish_update(self.job,False,'test interruption')
        self.assertEqual((self.out/'preview.html').read_bytes(),b'previous verified preview')
        self.assertTrue((self.folder/'snapshot.json').is_file())
        self.assertTrue((self.native.directory/'source.html').is_file())

if __name__=='__main__':unittest.main()
