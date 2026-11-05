import secrets as _test_secrets
_TEST_PASSWORD_0 = _test_secrets.token_urlsafe(24)
_TEST_PASSWORD_1 = _test_secrets.token_urlsafe(24)
import json,tempfile,threading,unittest,urllib.request,urllib.error
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
import server
from auth import Accounts,COOKIE
class AccessTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name);cls.jobs=cls.root/'jobs';cls.jobs.mkdir();cls.auth=Accounts(cls.root/'auth.sqlite3')
  cls.ids={name:cls.auth.create(name,_TEST_PASSWORD_0,role) for name,role in [('admin1','admin'),('owner1','user'),('other1','user'),('viewer1','viewer'),('viewer2','viewer')]}
  cls.clients={}
  for name in cls.ids:
   token=cls.auth.login(name,_TEST_PASSWORD_0,'127.0.0.1');cookie=COOKIE+'='+token;principal=cls.auth.session(cookie);op=urllib.request.build_opener(urllib.request.ProxyHandler({}));op.addheaders=[('Cookie',cookie),('X-CSRF-Token',principal['csrf']),('X-Local-App','1')];cls.clients[name]=op
  cls.sid='a'*32;cls.other='b'*32
  for sid,owner in [(cls.sid,'owner1'),(cls.other,'other1')]:
   job=cls.jobs/sid;(job/'output').mkdir(parents=True);server.write(job/'status.json',{'id':sid,'kind':'folder','state':'completed','created_at':1,'owner_id':cls.ids[owner]});(job/'output/preview.html').write_text('synthetic preview')
  cls.patch=patch.multiple(server,AUTH=cls.auth,JOBS=cls.jobs,CLUSTER=None);cls.patch.start();cls.http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);cls.thread=threading.Thread(target=cls.http.serve_forever);cls.thread.start();cls.base='http://127.0.0.1:'+str(cls.http.server_port)
 @classmethod
 def tearDownClass(cls):cls.http.shutdown();cls.http.server_close();cls.thread.join();cls.patch.stop();cls.temp.cleanup()
 def req(self,user,path,data=None):
  req=urllib.request.Request(self.base+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'} if data is not None else {})
  try:
   with self.clients[user].open(req,timeout=10) as r:return r.status,r.read()
  except urllib.error.HTTPError as e:return e.code,e.read()
 def test_01_default_privacy_and_owner_control(self):
  for user,expected in [('admin1',{self.sid,self.other}),('owner1',{self.sid}),('other1',{self.other}),('viewer1',set())]:
   code,data=self.req(user,'/api/jobs');self.assertEqual(code,200);self.assertEqual({j['id'] for j in json.loads(data)['jobs']},expected)
  self.assertEqual(self.req('owner1','/api/jobs/'+self.sid+'/note',{'note_name':'own'})[0],200)
  self.assertEqual(self.req('other1','/api/jobs/'+self.sid+'/note',{'note_name':'x'})[0],403)
  self.assertEqual(self.req('viewer1','/results/'+self.sid+'/preview.html')[0],403)
  self.assertEqual(self.req('viewer1','/results/'+self.sid+'/view/'+'c'*32+'/plane')[0],403)
 def test_02_grant_and_revoke(self):
  path='/api/jobs/'+self.sid+'/viewers'
  self.assertEqual(self.req('other1',path,{'user_ids':[self.ids['viewer1']]})[0],403)
  self.assertEqual(self.req('owner1',path,{'user_ids':[self.ids['viewer1']]})[0],200)
  self.assertEqual(self.req('viewer1','/results/'+self.sid+'/preview.html')[0],200)
  self.assertEqual(self.req('viewer2','/results/'+self.sid+'/preview.html')[0],403)
  self.assertEqual(self.req('viewer1','/results/'+self.sid+'/preview.html?download=1')[0],403)
  self.assertEqual(self.req('viewer1','/api/jobs/'+self.sid+'/note',{'note_name':'x'})[0],403)
  self.assertEqual(self.req('viewer1','/api/jobs/'+self.sid+'/export-preview')[0],403)
  self.assertEqual(self.req('admin1',path,{'user_ids':[]})[0],200)
  self.assertEqual(self.req('viewer1','/results/'+self.sid+'/preview.html')[0],403)
 def test_03_non_admin_has_no_account_or_storage_control(self):
  for user in ('owner1','viewer1'):
   self.assertEqual(self.req(user,'/api/auth/password',{'old_password':_TEST_PASSWORD_0,'password':_TEST_PASSWORD_1})[0],403)
   self.assertEqual(self.req(user,'/api/auth/users')[0],403)
   self.assertEqual(self.req(user,'/api/cluster/v1/admin/nodes')[0],403)
  self.assertEqual(self.req('viewer1','/api/storage')[0],403)
  self.assertEqual(self.req('owner1','/api/auth/admin-check')[0],403)
  self.assertEqual(self.req('admin1','/api/auth/admin-check')[0],204)
 def test_04_cannot_grant_upload_or_admin_accounts(self):
  self.assertEqual(self.req('owner1','/api/jobs/'+self.sid+'/viewers',{'user_ids':[self.ids['other1']]})[0],400)
 def test_05_existing_upload_ownership_grants_derive(self):
  with patch.object(server,'queue_update') as q:
   code,_=self.req('owner1','/api/jobs/'+self.sid+'/derive',{'mode':'quick','structures':['heart']})
   self.assertEqual(code,200);q.assert_called_once()
 def test_06_admin_can_transfer_owner_but_upload_user_cannot(self):
  path='/api/jobs/'+self.sid+'/owner'
  self.assertEqual(self.req('owner1',path,{'user_id':self.ids['other1']})[0],403)
  self.assertEqual(self.req('admin1',path,{'user_id':self.ids['viewer1']})[0],400)
  self.assertEqual(self.req('admin1',path,{'user_id':self.ids['other1']})[0],200)
  self.assertEqual(self.req('owner1','/api/jobs/'+self.sid+'/note',{'note_name':'no longer owner'})[0],403)
  self.assertEqual(self.req('other1','/api/jobs/'+self.sid+'/note',{'note_name':'new owner'})[0],200)
  self.assertEqual(self.req('admin1',path,{'user_id':self.ids['owner1']})[0],200)
 def test_07_owner_can_start_reconstruction(self):
  import queue
  path=self.jobs/self.sid;status=server.read(path/'status.json');original=dict(status);status['state']='ready';server.write(path/'status.json',status)
  server.write(path/'inventory.json',{'series':[{'id':'synthetic','eligible':True}]})
  try:
   q=queue.Queue()
   with patch.object(server,'QUEUE',q):
    code,_=self.req('owner1','/api/jobs/'+self.sid+'/start',{'mode':'quick','structures':['heart'],'series_id':'synthetic'})
   self.assertEqual(code,200);self.assertEqual(q.get_nowait(),(path,'run'))
  finally:server.write(path/'status.json',original)
 def test_08_transferred_owner_can_reconstruct_without_mocking_queue_update(self):
  import queue
  path=self.jobs/self.sid;original=server.read(path/'status.json')
  self.assertEqual(self.req('admin1','/api/jobs/'+self.sid+'/owner',{'user_id':self.ids['other1']})[0],200)
  q=queue.Queue()
  try:
   with patch.object(server,'QUEUE',q):
    code,body=self.req('other1','/api/jobs/'+self.sid+'/derive',{'mode':'quick','structures':['heart']})
   self.assertEqual(code,200,body);self.assertEqual(q.get_nowait(),(path,'run'))
   status=server.read(path/'status.json');self.assertEqual(status['state'],'queued');self.assertEqual(status['owner_id'],self.ids['other1'])
  finally:server.write(path/'status.json',original)
if __name__=='__main__':unittest.main()
