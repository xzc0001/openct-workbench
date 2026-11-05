import secrets as _test_secrets
_TEST_PASSWORD_0 = _test_secrets.token_urlsafe(24)
_TEST_PASSWORD_1 = _test_secrets.token_urlsafe(24)
import http.client,json,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
import auth,server

class GuestLinks(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.jobs=self.root/'jobs';self.jobs.mkdir();self.a='a'*32;self.b='b'*32;self.c='c'*32
  for sid in [self.a,self.b,self.c]:
   output=self.jobs/sid/'output';output.mkdir(parents=True);(output/'preview.html').write_text('preview');(output.parent/'status.json').write_text(json.dumps({'id':sid,'state':'completed','note_name':'Case '+sid[0]}))
  self.store=auth.Accounts(self.root/'auth/db.sqlite');self.adminid=self.store.create('administrator',_TEST_PASSWORD_0,'admin');self.uid=self.store.create('regular',_TEST_PASSWORD_1,'user')
  self.admin=self.store.login('administrator',_TEST_PASSWORD_0,'127.0.0.1');self.regular=self.store.login('regular',_TEST_PASSWORD_1,'127.0.0.1')
  self.old_jobs,self.old_auth=server.JOBS,server.AUTH;server.JOBS=self.jobs;server.AUTH=self.store
  self.http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
 def tearDown(self):
  self.http.shutdown();self.http.server_close();self.thread.join();server.JOBS=self.old_jobs;server.AUTH=self.old_auth;self.temp.cleanup()
 def req(self,method,path,body=None,cookie=None,csrf=None):
  h={};
  if cookie:h['Cookie']=cookie
  if method=='POST':h.update({'X-Local-App':'1','Content-Type':'application/json'});body=json.dumps(body).encode()
  if csrf:h['X-CSRF-Token']=csrf
  c=http.client.HTTPConnection('127.0.0.1',self.http.server_port,timeout=5);c.request(method,path,body,h);r=c.getresponse();data=r.read();result=(r.status,dict(r.getheaders()),data);c.close();return result
 def credential(self,token):
  cookie='ct_session='+token;return cookie,self.store.session(cookie)['csrf']
 def create(self,ids=None):
  cookie,csrf=self.credential(self.admin);status,_,body=self.req('POST','/api/guest/links',{'study_ids':ids or [self.a,self.b]},cookie,csrf);self.assertEqual(status,201);return json.loads(body)
 def enter(self,token):
  code,headers,_=self.req('POST','/api/guest/enter',{'token':token});self.assertEqual(code,200);return headers['Set-Cookie'].split(';')[0]
 def test_full_lifecycle_and_scope(self):
  start=time.time();link=self.create();self.assertAlmostEqual(link['expires']-start,7*86400,delta=5);guest=self.enter(link['token'])
  code,_,data=self.req('GET','/api/guest/studies',cookie=guest);self.assertEqual(code,200);self.assertEqual([x['id'] for x in json.loads(data)['studies']],[self.a,self.b])
  for sid in [self.a,self.b]:self.assertEqual(self.req('GET','/results/'+sid+'/preview.html',cookie=guest)[0],200)
  self.assertEqual(self.req('GET','/results/'+self.c+'/preview.html',cookie=guest)[0],302)
  self.assertEqual(self.req('GET','/results/'+self.a+'/preview.html?download=1',cookie=guest)[0],302)
  self.assertEqual(self.req('GET','/api/jobs',cookie=guest)[0],401)
  self.assertEqual(self.req('POST','/api/uploads/folder',{},guest)[0],401)
  cookie,csrf=self.credential(self.admin);self.assertEqual(self.req('POST','/api/guest/revoke',{'id':link['id']},cookie,csrf)[0],200)
  self.assertEqual(self.req('GET','/api/guest/studies',cookie=guest)[0],401);self.assertEqual(self.req('GET','/results/'+self.a+'/preview.html',cookie=guest)[0],302)
 def test_expiry_and_token_storage(self):
  link=self.create();guest=self.enter(link['token']);db=self.store.connect()
  with db as con:
   row=con.execute('SELECT * FROM guest_links WHERE id=?',(link['id'],)).fetchone();self.assertNotIn(link['token'],str(dict(row)));con.execute('UPDATE guest_links SET expires=? WHERE id=?',(time.time()-1,link['id']))
  self.assertEqual(self.req('GET','/api/guest/studies',cookie=guest)[0],401);self.assertEqual(self.req('POST','/api/guest/enter',{'token':link['token']})[0],401)
 def test_only_admin_and_validation(self):
  cookie,csrf=self.credential(self.regular)
  self.assertEqual(self.req('POST','/api/guest/links',{'study_ids':[self.a]},cookie,csrf)[0],403)
  self.assertEqual(self.req('GET','/api/guest/links',cookie=cookie)[0],403)
  cookie,csrf=self.credential(self.admin)
  for ids,days in [([],7),([self.a,self.a],7),([self.a],0),([self.a],True),(['z'*32],7)]:self.assertEqual(self.req('POST','/api/guest/links',{'study_ids':ids,'days':days},cookie,csrf)[0],400)
  self.assertEqual(self.req('POST','/api/guest/links',{'study_ids':[self.a]},cookie)[0],403)
 def test_authorized_asset_route(self):
  link=self.create();guest=self.enter(link['token']);generation='d'*32;asset='e'*64
  folder=self.jobs/self.a/'output'/'online'/generation;folder.mkdir(parents=True);(folder/'index.json').write_text('{}')
  class FakePreview:
   def __init__(self,directory):self.directory=directory
   def compressed(self,key):return b'abc'
  with patch('preview_access.Preview',FakePreview):
   self.assertEqual(self.req('GET',f'/results/{self.a}/view/{generation}/asset/{asset}.gz',cookie=guest)[0],200)
   self.assertEqual(self.req('GET',f'/results/{self.c}/view/{generation}/asset/{asset}.gz',cookie=guest)[0],401)
if __name__=='__main__':unittest.main()
