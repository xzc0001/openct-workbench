"""Study identity, atomic recovery and candidate-only reconstruction contracts."""
import hashlib
import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from auth_test_support import fixture,opener
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import numpy as np
import SimpleITK as sitk
import server
from study_updates import begin, restore


class StudyWorkflow(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.sid='a'*32;self.job=self.root/'jobs'/self.sid;out=self.job/'output';out.mkdir(parents=True)
        self.status={'id':self.sid,'kind':'zip','state':'completed','created_at':1,'updated_at':1,'mode':'quick','series_id':'series1','structures':['airways'],'bytes':10,'files':1,'note_name':'原备注'}
        server.write(self.job/'status.json',self.status);server.write(self.job/'inventory.json',{'series':[]})
        (out/'preview.html').write_bytes(b'previous self-contained CT preview')
        server.write(out/'scene.json',{'models':[]});server.write(out/'manifest.json',{})
        (out/'masks').mkdir();(out/'models').mkdir()
        (out/'masks/base.nii.gz').write_bytes(b'base mask')
        (out/'models/base.stl').write_bytes(b'base geometry')
        server.write(out/'candidate_review.json',{'source_ct_sha256':'fixture','candidates':[{'id':'suspect_001','name':'candidate','mask_file':'suspect_001.nii.gz','thumbnail_file':'suspect_001.png'}]})
        self.accounts,self.auth_headers=fixture(self.root)
        self.queue=queue.Queue();self.patch=patch.multiple(server,ROOT=self.root,JOBS=self.root/'jobs',CONFIG={'weights':'../weights','minimum_free_gb':0,'port':0},AUTH=self.accounts,PROCESSES={},QUEUE=self.queue)
        self.patch.start();self.http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        self.origin=f'http://127.0.0.1:{self.http.server_port}';self.op=opener(self.auth_headers)
    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.thread.join();self.patch.stop();self.temp.cleanup()
    def post(self,action,data):
        req=urllib.request.Request(self.origin+'/api/jobs/'+self.sid+'/'+action,json.dumps(data).encode(),{'Content-Type':'application/json','X-Local-App':'1','Origin':self.origin})
        return json.load(self.op.open(req))
    def test_note_edit_validation_and_public_render(self):
        self.post('note',{'note_name':' 胸部复查 <test> '})
        record=server.public(self.job);self.assertEqual(record['note_name'],'胸部复查 <test>')
        for name in ['x'*81,'bad\nname',None]:
            with self.assertRaises(urllib.error.HTTPError):self.post('note',{'note_name':name})
        self.post('note',{'note_name':''});self.assertEqual(server.public(self.job)['note_name'],'')
    def test_screen_selected_and_remove_keep_same_id_and_previous_preview(self):
        for action,data in [('screen',{}),('selected',{'candidate_ids':['suspect_001'],'confirmed':True}),('selected',{'candidate_ids':[],'confirmed':True}),('derive',{'mode':'full','structures':['aorta']})]:
            reply=self.post(action,data);self.assertEqual(reply['id'],self.sid)
            self.assertEqual(len(list((self.root/'jobs').iterdir())),1)
            self.assertEqual(self.op.open(self.origin+'/results/'+self.sid+'/preview.html').read(),b'previous self-contained CT preview')
            self.assertEqual(self.queue.get_nowait()[0],self.job)
            self.assertEqual(server.read(self.job/'status.json')['note_name'],'原备注')
            self.post('cancel',{});self.assertEqual(server.read(self.job/'status.json')['state'],'completed')
    def test_selected_requires_explicit_submission_and_valid_ids(self):
        for data in [{'candidate_ids':['suspect_001']},{'candidate_ids':['suspect_999'],'confirmed':True},{'candidate_ids':'suspect_001','confirmed':True}]:
            with self.assertRaises(urllib.error.HTTPError):self.post('selected',data)
        self.assertTrue(self.queue.empty())
    def test_failure_restores_files_configuration_and_preserves_new_note(self):
        self.post('selected',{'candidate_ids':['suspect_001'],'confirmed':True})
        self.post('note',{'note_name':'计算时修改备注'})
        server.write(self.job/'output/scene.json',{'models':['broken']})
        server.write(self.job/'output/manifest.json',{'broken':True})
        (self.job/'output/preview.html.tmp').write_bytes(b'incomplete')
        server.finish_update(self.job,False,'fixture failure')
        self.assertEqual(server.read(self.job/'output/scene.json'),{'models':[]})
        current=server.read(self.job/'status.json');self.assertEqual(current['note_name'],'计算时修改备注');self.assertNotIn('candidate_ids',current)
        self.assertEqual(current['state'],'completed');self.assertNotIn('inplace_operation',current)


class CandidateGeometry(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.job=Path(self.temp.name)/'study';self.out=self.job/'output'
        for folder in ['masks','models','candidate_regions']:(self.out/folder).mkdir(parents=True,exist_ok=True)
        a=np.zeros((20,20,20),np.uint8);a[3:8,3:8,3:8]=1
        self.volume=sitk.GetImageFromArray(np.full(a.shape,-700,np.int16));self.volume.SetSpacing((.7,.7,1.2))
        for name in ['CT.nii.gz','CT_original.nii.gz']:sitk.WriteImage(self.volume,str(self.out/name),True)
        mask=sitk.GetImageFromArray(a);mask.CopyInformation(self.volume);sitk.WriteImage(mask,str(self.out/'masks/base.nii.gz'),True)
        from surface_geometry import build_surface,surface_arrays
        poly,processing=build_surface(a>0,self.volume,'airways')
        self.base={'id':'base','name':'base','group':'airways','color':[1,1,1],'opacity':1,**surface_arrays(poly),'voxels':int(a.sum()),'mask_file':'base.nii.gz','surface_processing':processing}
        self.scene={'models':[self.base],'nodules':[],'source_geometry':{},'method_summary':'fixture','coordinate_system':'RAS','units':'mm'}
        server.write(self.out/'scene.json',self.scene);server.write(self.out/'manifest.json',{});server.write(self.out/'native_instances.json',[])
        b=np.zeros(a.shape,np.uint8);b[12:15,12:15,12:15]=1;candidate=sitk.GetImageFromArray(b);candidate.CopyInformation(self.volume)
        sitk.WriteImage(candidate,str(self.out/'candidate_regions/suspect_001.nii.gz'),True)
        self.sha=hashlib.sha256((self.out/'CT.nii.gz').read_bytes()).hexdigest()
        server.write(self.out/'candidate_review.json',{'source_ct_sha256':self.sha,'candidates':[{'id':'suspect_001','name':'候选 01','mask_file':'suspect_001.nii.gz','thumbnail_file':'suspect_001.png','center_ras_mm':[0,0,0]}]})
    def tearDown(self):self.temp.cleanup()
    def test_native_ct_buffer_compression_is_byte_identical(self):
        import base64,gzip
        from viewer_pack import zipped
        original=np.arange(20*30*40,dtype='<i2').reshape(20,30,40)
        for values in [original,original[:,::2,::-1]]:
            expected=base64.b64encode(gzip.compress(values.tobytes(),compresslevel=3,mtime=0)).decode('ascii')
            self.assertEqual(zipped(values),expected)
    def test_select_remove_preserves_ct_and_all_base_geometry(self):
        from selected_regions import rebuild
        for ids in [['suspect_001'],[]]:
            messages=[]
            rebuild(self.job,{'candidate_ids':ids},messages.append)
            self.assertIn('压缩原始 CT 影像',messages)
            self.assertIn('写入三维预览',messages)
            scene=server.read(self.out/'scene.json');base=next(m for m in scene['models'] if m['id']=='base')
            for key in ['positions','indices','normals','vertices','triangles']:self.assertEqual(base[key],self.base[key])
            self.assertEqual([m['id'] for m in scene['models']],['base']+ids)
            self.assertEqual(hashlib.sha256((self.out/'CT.nii.gz').read_bytes()).hexdigest(),self.sha)
            self.assertEqual(hashlib.sha256((self.out/'CT_original.nii.gz').read_bytes()).hexdigest(),self.sha)
            self.assertTrue((self.out/'preview.html').stat().st_size>100000)
    def test_candidate_source_ct_mismatch_rejected(self):
        from selected_regions import rebuild
        review=server.read(self.out/'candidate_review.json');review['source_ct_sha256']='wrong';server.write(self.out/'candidate_review.json',review)
        with self.assertRaisesRegex(ValueError,'CT'):rebuild(self.job,{'candidate_ids':['suspect_001']})
    def test_rescreen_identity_and_selected_regions_survive_new_proposals(self):
        from candidate_regions import screen
        lung=sitk.GetImageFromArray(np.ones((20,20,20),np.uint8));lung.CopyInformation(self.volume);sitk.WriteImage(lung,str(self.out/'masks/lungs.nii.gz'))
        (self.out/'candidate_review.json').unlink()
        def region(lo):return {'mask':np.ones((3,3,3),bool),'start_zyx':np.array(lo),'center_zyx':np.array(lo)+1,'volume_mm3':91.,'mean_hu':-600.,'density_range_hu':[-700.,-500.],'local_response_hu':20.,'flags':[],'manual_seed':False}
        first,second=region([2,2,2]),region([8,8,8])
        with patch('candidate_regions.propose',return_value=[first,second]):one=screen(self.out)
        ids=[r['id'] for r in one['candidates']]
        server.write(self.job/'status.json',{'candidate_ids':[ids[0]]})
        with patch('candidate_regions.propose',return_value=[second]):two=screen(self.out)
        self.assertEqual([r['id'] for r in two['candidates']],ids)
        with patch('candidate_regions.propose',return_value=[second,first]):three=screen(self.out)
        self.assertEqual([r['id'] for r in three['candidates']],ids)
        self.assertEqual(len(server.read(self.out/'candidate_identity.json')),2)


if __name__=='__main__':unittest.main()
