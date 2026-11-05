from pathlib import Path
import json,tempfile,unittest,zipfile
import numpy as np
from preview_package import write_preview,TILE
from preview_access import Preview
from preview_transfer import export_preview,validate_archive,import_preview
from unittest.mock import patch
import server
import auth,threading
import http.client as http_client
from http.server import ThreadingHTTPServer
from transfer_client import send_preview

class PreviewTransfer(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.out=self.root/'a'*0 if False else self.root/'jobs'/('a'*32)/'output';self.out.mkdir(parents=True)
  self.data=np.arange(24,dtype='<i2').reshape(2,3,4);self.ct={'dtype':'int16','size':[4,3,2],'spacing':[1,1,1],'origin':[0,0,0],'direction':[1,0,0,0,1,0,0,0,1],'matrix':[[1,0,0],[0,1,0],[0,0,1]],'inverse':[[1,0,0],[0,1,0],[0,0,1]],'bounds':[[0,0,0],[3,2,1]],'masks':[],'original':True,'instances':[]}
  write_preview(self.out,{'models':[]},self.ct,{'rois':[]},[self.data]);self.package=self.root/'transfer.zip';export_preview(self.out,self.package,{'source_id':'a'*32})
 def tearDown(self):self.tmp.cleanup()
 def stage(self):p=self.root/('stage'+str(len(list(self.root.glob('stage*')))));p.mkdir();return p
 def test_transfer_rebuilds_with_server_template_and_preserves_data(self):
  preview,_=validate_archive(self.package,self.stage());out=self.root/'jobs'/('b'*32)/'output';out.mkdir(parents=True)
  receipt=write_preview(out,preview.index['scene'],preview.ct,preview.index['rois'],resolver=preview.compressed)
  restored=Preview(out/'online'/receipt['online_generation']);np.testing.assert_array_equal(restored.tile(0),self.data)
  self.assertEqual(restored.ct['raw_sha256'],preview.ct['raw_sha256']);self.assertEqual(restored.ct['size'],preview.ct['size'])
 def test_reupload_updates_same_study_and_preserves_previous_generation(self):
  dest=self.root/'destination';dest.mkdir();actor={'id':'u1','username':'tester','role':'user'}
  with patch.object(server,'JOBS',dest):
   first=import_preview(server,self.package,actor,self.stage());job=dest/first['id'];old=json.loads((job/'output/online/current.json').read_text())['generation']
   same=import_preview(server,self.package,actor,self.stage());self.assertEqual(same,{'id':first['id'],'unchanged':True})
   self.data=self.data+1;write_preview(self.out,{'models':[]},self.ct,{'rois':[]},[self.data]);export_preview(self.out,self.package,{'source_id':'a'*32,'note_name':'Test'})
   updated=import_preview(server,self.package,actor,self.stage());self.assertEqual(updated['id'],first['id']);self.assertFalse(updated['unchanged']);self.assertTrue((job/'output/online'/old/'index.html').exists())
   pointer=json.loads((job/'output/online/current.json').read_text());preview=Preview(job/'output/online'/pointer['generation']);np.testing.assert_array_equal(preview.tile(0),self.data)
   self.assertEqual(len(list(dest.glob('*/status.json'))),1)
 def test_source_html_is_never_executed_on_receiver(self):
  with zipfile.ZipFile(self.package) as z:index=z.read('index.json');source=z.read('source.html')
  with zipfile.ZipFile(self.package,'w') as z:z.writestr('index.json',index);z.writestr('source.html',source+b'<script>INJECTED_SCRIPT</script>');z.writestr('metadata.json',json.dumps({'source_id':'a'*32}))
  dest=self.root/'dest';dest.mkdir()
  with patch.object(server,'JOBS',dest):
   result=import_preview(server,self.package,{'id':'u','username':'a','role':'admin'},self.stage());content=(dest/result['id']/'output/preview.html').read_bytes();self.assertNotIn(b'INJECTED_SCRIPT',content)
 def test_authenticated_http_transfer(self):
  dest=self.root/'remote';dest.mkdir();accounts=auth.Accounts(self.root/'auth/accounts.sqlite');accounts.create('transferuser','synthetic-password-123','user')
  (self.out.parent/'cache').mkdir();(self.out.parent/'status.json').write_text(json.dumps({'state':'completed','note_name':'synthetic'}))
  with patch.object(server,'JOBS',dest),patch.object(server,'AUTH',accounts):
   http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
   try:
    events=[];result=send_preview(self.out.parent,('127.0.0.1',http.server_port),'transferuser','synthetic-password-123',lambda **kw:events.append(kw))
    self.assertTrue((dest/result['id']/'output/preview.html').is_file());self.assertIn('uploading',[e['stage'] for e in events])
    token=accounts.login('transferuser','synthetic-password-123','127.0.0.1');connection=http_client.HTTPConnection('127.0.0.1',http.server_port,timeout=20);connection.request('GET','/api/jobs/'+result['id']+'/export-preview',headers={'Cookie':'ct_session='+token});response=connection.getresponse();blob=response.read();self.assertEqual(response.status,200);self.assertTrue(blob.startswith(b'PK'));self.assertIn('.ctpreview.zip',response.getheader('Content-Disposition'));connection.close()
    csrf=accounts.session('ct_session='+token)['csrf'];connection=http_client.HTTPConnection('127.0.0.1',http.server_port,timeout=20);connection.request('POST','/api/import-preview',blob,{'Cookie':'ct_session='+token,'X-Local-App':'1','X-CSRF-Token':csrf,'Content-Type':'application/octet-stream'});response=connection.getresponse();returned=json.loads(response.read());self.assertEqual(response.status,200);self.assertEqual(returned['id'],result['id']);connection.close()
    result2=send_preview(self.out.parent,('127.0.0.1',http.server_port),'transferuser','synthetic-password-123');self.assertEqual(result['id'],result2['id'])
    with self.assertRaises(ValueError):send_preview(self.out.parent,('127.0.0.1',http.server_port),'transferuser','wrong-password')
    busy=dest/('c'*32);busy.mkdir();(busy/'status.json').write_text(json.dumps({'id':'c'*32,'state':'running','created_at':1,'updated_at':1}))
    with self.assertRaisesRegex(ValueError,'正在处理'):send_preview(self.out.parent,('127.0.0.1',http.server_port),'transferuser','synthetic-password-123')
   finally:http.shutdown();http.server_close();thread.join()
 def test_import_of_original_examination_never_duplicates_or_overwrites_source(self):
  actor={'id':'u','username':'a','role':'admin'};job=self.out.parent;(job/'status.json').write_text(json.dumps({'id':job.name,'state':'completed','owner_id':'u','created_at':1,'updated_at':1}))
  with patch.object(server,'JOBS',job.parent):
   result=import_preview(server,self.package,actor,self.stage());self.assertEqual(result,{'id':job.name,'unchanged':True})
   other=self.root/'other'/('a'*32)/'output';other.mkdir(parents=True);write_preview(other,{'models':[]},self.ct,{'rois':[]},[self.data+1]);export_preview(other,self.package,{'source_id':'a'*32})
   with self.assertRaisesRegex(ValueError,'不同版本'):import_preview(server,self.package,actor,self.stage())
   self.assertEqual(len(list(job.parent.glob('*/status.json'))),1)
 def test_unexpected_archive_member_rejected(self):
  package=self.root/'bad.zip'
  with zipfile.ZipFile(package,'w') as z:z.writestr('../evil.py','x');z.writestr('source.html','x');z.writestr('metadata.json',json.dumps({'source_id':'a'*32}))
  with self.assertRaises(ValueError):validate_archive(package,self.stage())
 def test_tampered_asset_and_out_of_range_offset_rejected(self):
  with zipfile.ZipFile(self.package) as z:index=json.loads(z.read('index.json'));source=z.read('source.html')
  index['assets'][next(iter(index['assets']))]['offset']=len(source)+1
  package=self.root/'bad.zip'
  with zipfile.ZipFile(package,'w') as z:z.writestr('index.json',json.dumps(index));z.writestr('source.html',source);z.writestr('metadata.json',json.dumps({'source_id':'a'*32}))
  with self.assertRaises(ValueError):validate_archive(package,self.stage())
if __name__=='__main__':unittest.main()
