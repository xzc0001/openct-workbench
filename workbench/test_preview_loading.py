"""Indexed/offline native CT fidelity, bounded access and study lifecycle."""
import base64
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest
from auth_test_support import fixture,opener
import numpy as np
import threading
import urllib.request
from urllib.parse import urlencode
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from preview_package import write_preview,TILE,upgrade,refresh_indexed
from preview_access import Preview,region


class PreviewLoadingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.job=Path(self.temp.name)/'jobs'/('b'*32);self.out=self.job/'output';self.out.mkdir(parents=True)
        self.accounts,self.auth_headers=fixture(Path(self.temp.name))
        z,y,x=np.indices((69,75,130));self.data=((x*17+y*23+z*31)%5000-1024).astype('<i2')
        a=.31;direction=np.array([[np.cos(a),-np.sin(a),0],[np.sin(a),np.cos(a),0],[0,0,1]])
        matrix=direction@np.diag([.71,.83,1.21]);origin=np.array([-31.,41.,-80.]);size=list(self.data.shape[::-1]);corners=np.array([origin+matrix@np.array([x,y,z]) for x in [0,size[0]-1] for y in [0,size[1]-1] for z in [0,size[2]-1]])
        self.ct={'dtype':'int16','size':size,'origin':origin.tolist(),'spacing':[.71,.83,1.21],'direction':direction.tolist(),'matrix':matrix.tolist(),'inverse':np.linalg.inv(matrix).tolist(),'bounds':[corners.min(axis=0).tolist(),corners.max(axis=0).tolist()],'original':True,'instances':list(range(69)),'masks':[],'maskGeometry':{'origin':[-31,41,-80],'size':size,'spacing':[.71,.83,1.21]}}
        self.positions=np.array([[0.,0,0],[1,0,0],[0,1,0]],dtype='<f4')
        enc=lambda a:base64.b64encode(gzip.compress(a.tobytes(),mtime=0)).decode()
        self.model={'id':'heart','name':'test','group':'heart','encoding':'gzip','vertices':3,'triangles':1,'positions':enc(self.positions),'indices':enc(np.array([0,1,2],'<u4')),'normals':enc(np.array([[0.,0,1]]*3,'<f4')),'color':[.8,.2,.2],'opacity':1,'bounds_ras':[0,1,0,1,0,0]}
        self.scene={'models':[self.model]};self.rois={'rois':[]}
        self.receipt=write_preview(self.out,self.scene,self.ct,self.rois,(self.data[z:z+TILE[2]] for z in range(0,len(self.data),TILE[2])))
        (self.out/'manifest.json').write_text(json.dumps(self.receipt));self.preview=Preview(self.out/'online'/self.receipt['online_generation'])
    def tearDown(self):self.temp.cleanup()
    def test_bricks_recover_every_native_voxel_and_geometry_byte(self):
        restored=np.empty_like(self.data)
        for i,brick in enumerate(self.preview.ct['tiles']):
            x,y,z=brick['start'];w,h,d=brick['size'];restored[z:z+d,y:y+h,x:x+w]=self.preview.tile(i)
        np.testing.assert_array_equal(restored,self.data)
        self.assertEqual(self.receipt['ct_raw_sha256'],hashlib.sha256(self.data.tobytes()).hexdigest())
        m=self.preview.index['scene']['models'][0];raw=gzip.decompress(self.preview.compressed(m['positions']['asset']))
        self.assertEqual(raw,self.positions.tobytes())
        self.assertTrue((self.out/'preview.html').samefile(self.preview.directory/'source.html'))
        self.assertLess((self.preview.directory/'index.html').stat().st_size,(self.out/'preview.html').stat().st_size)
    def request(self):
        origin=np.array(self.ct['origin']);matrix=np.array(self.ct['matrix'])
        point=lambda q:(origin+matrix@np.array(q)).tolist()
        return {'name':'axial','width':130,'height':75,'p00':point([0,0,33]),'p10':point([129,0,33]),'p01':point([0,74,33])}
    def test_native_plane_across_brick_boundaries_is_exact(self):
        np.testing.assert_array_equal(self.preview.plane(self.request()),self.data[33])
        with self.assertRaises(ValueError):self.preview.plane({**self.request(),'width':100000})
    def test_identical_air_bricks_with_different_edge_shapes_share_bytes_only(self):
        shape=(69,96,96);values=np.full(shape,-1000,dtype='<i2');ct={**self.ct,'size':list(shape[::-1])}
        output=self.out/'air';output.mkdir()
        receipt=write_preview(output,self.scene,ct,self.rois,(values[z:z+32] for z in range(0,69,32)))
        preview=Preview(output/'online'/receipt['online_generation'])
        for index,brick in enumerate(preview.ct['tiles']):
            self.assertEqual(preview.tile(index).shape,tuple(brick['size'][::-1]))
        q=[np.array([48,80,48,80]),np.array([80,48,48,80]),np.array([33,33,65,65])]
        np.testing.assert_array_equal(preview.sample(q),np.full(4,-1000))
    @unittest.skipUnless(shutil.which('node'),'Node runtime unavailable on this host')
    def test_client_offline_sampler_matches_server_without_network(self):
        assets={key:base64.b64encode(self.preview.compressed(key)).decode() for key in self.preview.index['assets']}
        script=Path(__file__).parent/'qa/test_viewer_source.cjs'
        request={'ct':self.preview.ct,'rois':self.rois,'assets':assets,'plane':self.request()}
        result=subprocess.run(['node',str(script)],input=json.dumps(request),text=True,capture_output=True,check=True)
        reply=json.loads(result.stdout)
        self.assertEqual(reply['plane_sha256'],hashlib.sha256(self.data[33].tobytes()).hexdigest())
        self.assertEqual(reply['volume_sha256'],hashlib.sha256(self.data.tobytes()).hexdigest())
        self.assertLess(reply['cached_bytes'],64*1024**2)
    def test_volume_budget_rejects_before_allocating_and_exact_full_native(self):
        with self.assertRaises(ValueError):self.preview.volume('',100)
        values,start,size=self.preview.volume('',128*1024**2)
        np.testing.assert_array_equal(values,self.data);self.assertEqual(start.tolist(),[0,0,0]);self.assertEqual(size.tolist(),self.ct['size'])
    @unittest.skipUnless(shutil.which('node'),'Node runtime unavailable on this host')
    def test_online_native_planes_volume_and_mesh_without_decompression_stream(self):
        import server
        with patch.multiple(server,AUTH=self.accounts,JOBS=self.job.parent,PROCESSES={}):
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
            thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
            try:
                endpoint=f'http://127.0.0.1:{http.server_port}/results/{self.job.name}/view/{self.receipt["online_generation"]}'
                request={'ct':self.preview.ct,'rois':self.rois,'plane':self.request(),'onlineBase':endpoint,'model':self.preview.index['scene']['models'][0],'failFirstRequests':2,'requestHeaders':self.auth_headers}
                script=Path(__file__).parent/'qa/test_viewer_source.cjs'
                result=subprocess.run(['node',str(script)],input=json.dumps(request),text=True,capture_output=True,check=True,timeout=30)
                reply=json.loads(result.stdout)
                self.assertEqual(reply['plane_sha256'],hashlib.sha256(self.data[33].tobytes()).hexdigest())
                self.assertEqual(reply['volume_sha256'],hashlib.sha256(self.data.tobytes()).hexdigest())
                self.assertEqual(reply['model_sha256'],hashlib.sha256(self.positions.tobytes()).hexdigest())
                self.assertEqual(reply['failed_requests'],2)
                self.assertLess(reply['cached_bytes'],64*1024**2)
            finally:http.shutdown();http.server_close();thread.join()
    def test_asset_refresh_preserves_native_hu_and_reference_geometry(self):
        before=self.preview.ct['raw_sha256'];old_model=self.preview.index['scene']['models'][0]
        receipt=refresh_indexed(self.out/'preview.html');current=Preview(self.out/'online'/receipt['online_generation'])
        self.assertEqual(current.ct['raw_sha256'],before)
        self.assertEqual(current.index['scene']['models'][0]['positions'],old_model['positions'])
        np.testing.assert_array_equal(current.plane(self.request()),self.data[33])
    def test_http_shell_download_plane_and_previous_generation(self):
        import server
        from study_updates import begin,restore
        from storage import execute
        (self.job/'status.json').write_text(json.dumps({'id':self.job.name,'state':'completed','created_at':1,'mode':'full'}))
        with patch.multiple(server,AUTH=self.accounts,JOBS=self.job.parent,PROCESSES={}):
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
            thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
            op=opener(self.auth_headers);base=f'http://127.0.0.1:{http.server_port}/results/{self.job.name}'
            try:
                with op.open(base+'/preview.html') as r:shell=r.read().decode()
                self.assertIn("connect-src 'self'",shell);self.assertNotIn('type="application/octet-stream"',shell)
                with op.open(base+'/preview.html?download=1') as r:
                    self.assertIn('attachment',r.headers['Content-Disposition']);self.assertEqual(r.read(),(self.out/'preview.html').read_bytes())
                self.assertIn("connect-src 'none'",(self.out/'preview.html').read_text(encoding='utf-8'))
                before=(self.job/'status.json').read_bytes()
                request=urllib.request.Request(base.replace('/results/','/api/jobs/')+'/preview-error',data=json.dumps({'message':'Failed to fetch','resource':'/results/test/view/asset','browser':'test','attempts':3}).encode(),headers={'Content-Type':'application/json','X-Local-App':'1'})
                with patch('builtins.print') as output,op.open(request) as r:
                    self.assertTrue(json.loads(r.read())['recorded']);self.assertIn('Failed to fetch',output.call_args[0][0])
                self.assertEqual((self.job/'status.json').read_bytes(),before)
                generation=self.receipt['online_generation'];endpoint=base+'/view/'+generation
                with op.open(endpoint+'/plane?'+urlencode({'request':json.dumps(self.request())})) as r:plane=np.frombuffer(gzip.decompress(r.read()),dtype='<i2').reshape(75,130)
                np.testing.assert_array_equal(plane,self.data[33])
                with op.open(endpoint+'/plane?'+urlencode({'request':json.dumps(self.request()),'decoded':'1'})) as r:
                    self.assertEqual(r.headers['Content-Encoding'],'gzip')
                    self.assertEqual(gzip.decompress(r.read()),self.data[33].tobytes())
                begin(self.job,{'mode':'full'})
                current=refresh_indexed(self.out/'preview.html')
                self.assertNotEqual(current['online_generation'],generation)
                key=self.preview.index['scene']['models'][0]['positions']['asset']
                with op.open(endpoint+'/asset/'+key+'.gz') as r:self.assertEqual(gzip.decompress(r.read()),self.positions.tobytes())
                restore(self.job)
                execute(self.job.parent.parent,{'action':'archive','job_ids':[self.job.name]})
                self.assertTrue((self.out/'online/current.json').is_file())
                with op.open(base+'/preview.html') as r:self.assertIn('transport',r.read().decode())
            finally:http.shutdown();http.server_close();thread.join()
    def test_legacy_upgrade_streams_ct_without_changing_source_voxels(self):
        legacy=self.out/'legacy';legacy.mkdir()
        ct={**self.ct,'codec':'gzip','data':base64.b64encode(gzip.compress(self.data.tobytes(),mtime=0)).decode()}
        page=''.join(f'<script id="{name}" type="application/json">{json.dumps(value)}</script>' for name,value in [('sceneData',self.scene),('ctData',ct),('volumeRoiData',self.rois)])
        (legacy/'preview.html').write_text(page);(legacy/'manifest.json').write_text('{"viewer_version":3}')
        receipt=upgrade(legacy/'preview.html',lambda message:None);updated=Preview(legacy/'online'/receipt['online_generation'])
        np.testing.assert_array_equal(updated.plane(self.request()),self.data[33])
        self.assertEqual(receipt['ct_raw_sha256'],hashlib.sha256(self.data.tobytes()).hexdigest())


if __name__=='__main__':unittest.main()
