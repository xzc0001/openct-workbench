"""Auth/ownership/ingestion adversarial tests. Only synthetic images are used."""
from pathlib import Path
import io
import json
import queue
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
import copy
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import generate_uid, ExplicitVRLittleEndian, ImplicitVRLittleEndian, ExplicitVRBigEndian
import auth
import server
import dicom_ingest as ingest
from auth_test_support import opener, TEST_PASSWORD
import secrets


def ct_bytes(syntax=ExplicitVRLittleEndian):
    meta=FileMetaDataset();meta.TransferSyntaxUID=syntax;meta.MediaStorageSOPClassUID=ingest.CT;meta.MediaStorageSOPInstanceUID=generate_uid()
    ds=FileDataset(None,{},file_meta=meta,preamble=b'\0'*128)
    ds.SOPClassUID=ingest.CT;ds.SOPInstanceUID=meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID=generate_uid();ds.SeriesInstanceUID=generate_uid();ds.FrameOfReferenceUID=generate_uid()
    ds.Modality='CT';ds.Rows=8;ds.Columns=8;ds.SamplesPerPixel=1;ds.PhotometricInterpretation='MONOCHROME2'
    ds.BitsAllocated=16;ds.BitsStored=12;ds.HighBit=11;ds.PixelRepresentation=1
    ds.ImagePositionPatient=[0,0,0];ds.ImageOrientationPatient=[1,0,0,0,1,0];ds.PixelSpacing=[.7,.7]
    ds.PixelData=bytes(128)
    stream=io.BytesIO();pydicom.dcmwrite(stream,ds,enforce_file_format=True,little_endian=syntax!=ExplicitVRBigEndian,implicit_vr=syntax==ImplicitVRLittleEndian)
    return stream.getvalue()


class Ingestion(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.path=self.root/'upload';self.input=self.root/'input';self.input.mkdir()
    def tearDown(self):self.temp.cleanup()
    def test_real_ct_and_extension_independence_preserve_bytes(self):
        for syntax in [ExplicitVRLittleEndian,ImplicitVRLittleEndian,ExplicitVRBigEndian]:
            original=ct_bytes(syntax);self.path.write_bytes(original)
            result=ingest.accept(self.path,self.input)
            self.assertEqual(result.read_bytes(),original);self.assertEqual(result.suffix,'.dcm')
    def test_renamed_scripts_magic_only_polyglot_truncated_and_appended_rejected(self):
        good=ct_bytes()
        for data in [b'MZ'+bytes(500),b'<script>alert(1)</script>',b'\0'*128+b'DICM'+b'fake ct',b'MZ'+good[2:],good[:-2],good+b'#!/bin/sh\necho BAD']:
            self.path.write_bytes(data)
            with self.assertRaises(ingest.Rejected):ingest.accept(self.path,self.input)
            self.assertFalse(self.path.exists())
        self.assertEqual(list(self.input.iterdir()),[])
    def test_invalid_metadata_and_deflated_dataset_rejected_before_decompression(self):
        good=ct_bytes();ds=pydicom.dcmread(io.BytesIO(good))
        for key,value in [('Modality','MR'),('Columns',4097),('PixelSpacing',[0,1]),('NumberOfFrames',20)]:
            changed=copy.deepcopy(ds);setattr(changed,key,value)
            stream=io.BytesIO();pydicom.dcmwrite(stream,changed,enforce_file_format=True)
            self.path.write_bytes(stream.getvalue())
            with self.assertRaises(ingest.Rejected):ingest.validate(self.path)
        uid=str(ds.SOPInstanceUID).encode();replacement=uid[:-1]+(b'1' if uid[-1:]!=b'1' else b'2')
        self.path.write_bytes(good.replace(uid,replacement,1))
        with self.assertRaises(ingest.Rejected):ingest.validate(self.path)
        ds.file_meta.TransferSyntaxUID=pydicom.uid.DeflatedExplicitVRLittleEndian
        stream=io.BytesIO();pydicom.dcmwrite(stream,ds,enforce_file_format=True)
        self.path.write_bytes(stream.getvalue())
        with self.assertRaises(ingest.Rejected):ingest.validate(self.path)
    def test_zip_filters_mixed_content_paths_symlinks_and_nested_archives(self):
        good=ct_bytes();archive=self.root/'study.zip'
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
            z.writestr('nested/CT0001',good);z.writestr('renamed.exe',good)
            z.writestr('fake.dcm',b'MZ'+bytes(500));z.writestr('notes.txt','hello');z.writestr('../outside.dcm',good)
            z.writestr('/absolute.dcm',good);z.writestr('C:/escape.dcm',good);z.writestr('nested.zip',b'PK'+good)
            link=zipfile.ZipInfo('link.dcm');link.external_attr=0o120777<<16;z.writestr(link,good)
        stats=ingest.extract(archive,self.input)
        self.assertEqual(stats,{'accepted':2,'rejected':7});self.assertFalse(archive.exists())
        self.assertEqual(len(list(self.input.iterdir())),2);self.assertFalse((self.root/'outside.dcm').exists())
        self.assertFalse(list(self.root.glob('*.ingest')))
    def test_archive_limits_and_corruption_remove_staging(self):
        for name in ['too-many.zip','broken.zip']:
            archive=self.root/name
            if name.startswith('too'):
                with zipfile.ZipFile(archive,'w') as z:z.writestr('a',ct_bytes());z.writestr('b',ct_bytes())
                with patch.object(ingest,'MAX_FILES',1),self.assertRaises(ingest.Rejected):ingest.extract(archive,self.input)
            else:
                archive.write_bytes(b'not a ZIP')
                with self.assertRaises((zipfile.BadZipFile,ingest.Rejected)):ingest.extract(archive,self.input)
            self.assertFalse(archive.exists())


class SecurityHTTP(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);(self.root/'jobs').mkdir();(self.root/'cache').mkdir()
        (self.root/'cache/shared').write_bytes(b'shared')
        self.store=auth.Accounts(self.root/'auth/accounts.sqlite3')
        self.ids={role:self.store.create(name,TEST_PASSWORD,role) for role,name in [('admin','admin'),('user','owner'),('viewer','reader')]}
        self.ids['other']=self.store.create('other',TEST_PASSWORD,'user')
        self.sid='a'*32;self.job=self.root/'jobs'/self.sid;(self.job/'input').mkdir(parents=True);(self.job/'output/revisions').mkdir(parents=True)
        server.write(self.job/'status.json',{'id':self.sid,'state':'completed','kind':'folder','created_at':1,'owner_id':self.ids['user']})
        (self.job/'output/revisions/old').write_bytes(b'backup');(self.job/'output/preview.html').write_bytes(b'preview')
        self.queue=queue.Queue();self.patch=patch.multiple(server,ROOT=self.root,JOBS=self.root/'jobs',AUTH=self.store,CONFIG={'weights':'weights','minimum_free_gb':0},CLEANUP_PLANS={},PROCESSES={},QUEUE=self.queue)
        self.patch.start();self.http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        self.base=f'http://127.0.0.1:{self.http.server_port}';self.clients={};self.headers={}
        for role,name in [('admin','admin'),('user','owner'),('viewer','reader'),('other','other')]:
            token=self.store.login(name,TEST_PASSWORD,'fixture');cookie=auth.COOKIE+'='+token
            self.headers[role]={'Cookie':cookie,'X-CSRF-Token':self.store.session(cookie)['csrf']}
            self.clients[role]=opener(self.headers[role])
    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.thread.join();self.patch.stop();self.temp.cleanup()
    def post(self,path,data,role='user',headers=None):
        req=urllib.request.Request(self.base+path,json.dumps(data).encode(),headers={'Content-Type':'application/json','X-Local-App':'1','Origin':self.base,**(headers or {})})
        return json.load(self.clients[role].open(req))
    def denied(self,code,func):
        with self.assertRaises(urllib.error.HTTPError) as error:func()
        self.assertEqual(error.exception.code,code)
    def test_anonymous_cannot_enumerate_upload_cleanup_or_fetch_models(self):
        client=opener({})
        for path in ['/api/jobs','/api/storage','/api/auth/check','/results/'+self.sid+'/view/'+'b'*32+'/plane']:
            self.denied(401,lambda:client.open(self.base+path))
        for path in ['/api/uploads/folder','/api/storage/plan']:
            req=urllib.request.Request(self.base+path,b'{}',{'Content-Type':'application/json','X-Local-App':'1'})
            self.denied(401,lambda:client.open(req))
    def test_login_cookie_flags_logout_and_csrf(self):
        client=opener({});req=urllib.request.Request(self.base+'/api/auth/login',json.dumps({'username':'owner','password':TEST_PASSWORD}).encode(),{'Content-Type':'application/json','X-Local-App':'1'})
        with client.open(req) as response:
            cookie=response.headers['Set-Cookie'];self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=Strict',cookie);self.assertNotIn(TEST_PASSWORD,response.read().decode())
        self.denied(403,lambda:self.post('/api/uploads/folder',{},headers={'X-CSRF-Token':'wrong'}))
        self.denied(403,lambda:self.post('/api/uploads/folder',{},headers={'Origin':'http://untrusted.example'}))
        self.post('/api/auth/logout',{})
        self.denied(401,lambda:self.clients['user'].open(self.base+'/api/jobs'))
    def test_owner_and_admin_cleanup_but_other_users_cannot(self):
        request={'action':'temporary','job_ids':[self.sid]}
        for role in ['other','viewer']:self.denied(403,lambda:self.post('/api/storage/plan',request,role))
        plan=self.post('/api/storage/plan',request)
        self.denied(403,lambda:self.post('/api/storage/execute',{'token':plan['token'],'confirmed':True},'other'))
        self.assertTrue((self.job/'output/revisions/old').exists())
        self.post('/api/storage/execute',{'token':plan['token'],'confirmed':True})
        self.assertFalse((self.job/'output/revisions').exists());self.assertTrue((self.job/'output/preview.html').exists())
        for role in ['user','other','viewer']:self.denied(403,lambda:self.post('/api/storage/plan',{'action':'cache'},role))
        self.assertTrue((self.root/'cache/shared').exists())
        plan=self.post('/api/storage/plan',{'action':'cache'},'admin');self.post('/api/storage/execute',{'token':plan['token'],'confirmed':True},'admin')
        self.assertFalse((self.root/'cache/shared').exists());self.assertTrue(self.store.path.exists())
    def test_upload_ownership_and_mixed_folder_filter(self):
        self.denied(400,lambda:self.post('/api/uploads/zip',{}))
        self.assertEqual(len(list((self.root/'jobs').iterdir())),1)
        self.denied(403,lambda:self.post('/api/uploads/folder',{},'viewer'))
        sid=self.post('/api/uploads/folder',{})['id'];job=self.root/'jobs'/sid
        self.assertEqual(server.read(job/'status.json')['owner_id'],self.ids['user'])
        self.denied(403,lambda:self.post('/api/jobs/'+sid+'/note',{'note_name':'intruder'},'other'))
        for content,expected in [(ct_bytes(),True),(b'MZ'+bytes(600),False)]:
            req=urllib.request.Request(self.base+'/api/jobs/'+sid+'/file',content,{'X-Local-App':'1','Content-Type':'application/octet-stream'})
            self.assertEqual(json.load(self.clients['user'].open(req))['uploaded'],expected)
        self.assertEqual(len(list((job/'input').iterdir())),1)
        self.assertEqual(server.read(job/'status.json')['upload_stats'],{'accepted':1,'rejected':1})
        self.assertFalse(list(job.glob('*.ingest')))
        self.post('/api/jobs/'+sid+'/commit',{});self.assertEqual(self.queue.get_nowait()[0],job)
    def test_read_only_permissions_and_legacy_ownerless_data(self):
        for role,allowed in [('admin',True),('user',True),('other',False),('viewer',False)]:
            data=json.load(self.clients[role].open(self.base+'/api/jobs'))['jobs'][0]
            self.assertEqual(data['can_manage'],allowed)
        status=server.read(self.job/'status.json');status.pop('owner_id');server.write(self.job/'status.json',status)
        self.denied(403,lambda:self.post('/api/storage/plan',{'action':'delete','job_ids':[self.sid]}))
    def test_account_admin_and_session_revocation(self):
        self.post('/api/auth/users',{'username':'newreader','password':secrets.token_urlsafe(24),'role':'viewer'},'admin')
        self.assertEqual(next(u for u in self.store.users() if u['username']=='newreader')['role'],'viewer')
        self.denied(403,lambda:self.post('/api/auth/users',{'username':'hacker','password':'x'*12},'user'))
        self.denied(403,lambda:self.clients['user'].open(self.base+'/api/auth/users'))
        self.post('/api/auth/users',{'id':self.ids['other'],'disabled':True},'admin')
        self.denied(401,lambda:self.clients['other'].open(self.base+'/api/jobs'))
        self.denied(400,lambda:self.post('/api/auth/users',{'id':self.ids['admin'],'disabled':True},'admin'))
        self.post('/api/auth/password',{'old_password':TEST_PASSWORD,'password':secrets.token_urlsafe(24)})
        self.denied(401,lambda:self.clients['user'].open(self.base+'/api/jobs'))
    def test_cleanup_token_cannot_be_used_by_another_session_of_same_user(self):
        plan=self.post('/api/storage/plan',{'action':'temporary','job_ids':[self.sid]})
        token=self.store.login('owner',TEST_PASSWORD,'new-session')
        cookie=auth.COOKIE+'='+token;csrf=self.store.session(cookie)['csrf']
        self.denied(403,lambda:self.post('/api/storage/execute',{'token':plan['token'],'confirmed':True},headers={'Cookie':cookie,'X-CSRF-Token':csrf}))
        self.assertTrue((self.job/'output/revisions/old').exists())
        self.post('/api/storage/execute',{'token':plan['token'],'confirmed':True})
    def test_rate_limit_and_expired_sessions(self):
        for _ in range(10):self.assertIsNone(self.store.login('missing','wrong','bad-client'))
        with self.assertRaises(auth.Forbidden):self.store.login('missing','wrong','bad-client')
        with self.store.connect() as db:db.execute('UPDATE sessions SET touched=?',(time.time()-4000,))
        self.denied(401,lambda:self.clients['user'].open(self.base+'/api/jobs'))


if __name__=='__main__':unittest.main()
