"""Destructive cleanup regressions use disposable data, never real patient jobs."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from auth_test_support import fixture,opener
import urllib.request
import urllib.error
from unittest.mock import patch
from http.server import ThreadingHTTPServer
import storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name).resolve()
        self.root=self.base/'app';(self.root/'jobs').mkdir(parents=True);(self.root/'cache').mkdir()
        self.external=self.base/'original.dcm';self.external.write_bytes(b'original DICOM')
        self.weights=self.base/'weights';self.weights.mkdir();(self.weights/'model.pth').write_bytes(b'cached model')
        (self.root/'cache/display.json').write_bytes(b'regenerable cache')
        self.sid='a'*32;self.job=self.make_job(self.sid)

    def tearDown(self):self.temp.cleanup()

    def make_job(self,sid,state='completed',parent=None):
        job=self.root/'jobs'/sid;(job/'input').mkdir(parents=True);(job/'output/revisions').mkdir(parents=True)
        status={'id':sid,'state':state,'created_at':1,'kind':'folder','bytes':0,'files':1}
        if parent:status['source_job']=parent
        (job/'status.json').write_text(json.dumps(status),encoding='utf-8')
        for name,data in [('input/source.dcm',b'uploaded DICOM'),('upload.zip',b'ZIP'),('inventory.json',json.dumps({'files':[str(self.external)]}).encode()),('output/CT_original.nii.gz',b'original CT'),('output/CT.nii.gz',b'canonical CT'),('output/preview.html',b'complete embedded HTML'),('output/manifest.json',b'{}'),('output/revisions/old.html',b'previous HTML')]:
            (job/name).write_bytes(data)
        return job

    def test_temporary_keeps_current_geometry_pixels_sources_and_weights(self):
        result=storage.execute(self.root,{'action':'temporary','job_ids':[self.sid]})
        self.assertGreater(result['removed_bytes'],0);self.assertFalse((self.job/'output/revisions').exists())
        for name in ['output/CT_original.nii.gz','output/CT.nii.gz','output/preview.html','input/source.dcm']:
            self.assertTrue((self.job/name).is_file())
        self.assertEqual(self.external.read_bytes(),b'original DICOM');self.assertTrue((self.weights/'model.pth').is_file())

    def test_preview_archive_removes_bulk_data_and_marks_no_reconstruction(self):
        before=(self.job/'output/preview.html').read_bytes()
        storage.execute(self.root,{'action':'archive','job_ids':[self.sid]})
        self.assertEqual((self.job/'output/preview.html').read_bytes(),before)
        self.assertEqual({p.name for p in (self.job/'output').iterdir()},{'preview.html','manifest.json'})
        self.assertTrue(json.loads((self.job/'status.json').read_text())['data_pruned'])
        self.assertFalse((self.job/'input').exists());self.assertTrue(self.external.exists())

    def test_delete_never_follows_inventory_paths_or_removes_weights(self):
        storage.execute(self.root,{'action':'delete','job_ids':[self.sid]})
        self.assertFalse(self.job.exists());self.assertTrue(self.external.exists());self.assertTrue((self.weights/'model.pth').exists())

    def test_interrupted_archive_does_not_advertise_rebuildable_inputs(self):
        with patch('storage.shutil.rmtree',side_effect=OSError('fixture permission error')):
            with self.assertRaises(OSError):storage.execute(self.root,{'action':'archive','job_ids':[self.sid]})
        self.assertTrue(json.loads((self.job/'status.json').read_text())['data_pruned'])
        self.assertTrue((self.job/'output/preview.html').exists());self.assertTrue(self.external.exists())

    def test_active_and_terminating_processes_protected(self):
        for state in storage.ACTIVE:
            status=json.loads((self.job/'status.json').read_text());status['state']=state;(self.job/'status.json').write_text(json.dumps(status))
            for action in ['delete','archive','temporary','cache']:
                with self.assertRaises(ValueError):storage.execute(self.root,{'action':action,'job_ids':[] if action=='cache' else [self.sid]})
        with self.assertRaises(ValueError):storage.plan(self.root,{'action':'delete','job_ids':[self.sid]},running=[self.sid])

    def test_parent_protected_until_children_selected_too(self):
        child='b'*32;self.make_job(child,parent=self.sid)
        for action in ['archive','delete']:
            with self.assertRaisesRegex(ValueError,'后续任务'):storage.plan(self.root,{'action':action,'job_ids':[self.sid]})
        storage.execute(self.root,{'action':'delete','job_ids':[self.sid,child]})
        self.assertFalse(self.job.exists());self.assertTrue(self.external.exists())

    def test_path_and_link_escape_rejected(self):
        for sid in ['../../weights',str(self.external),'a'*31,'A'*32]:
            with self.assertRaises(ValueError):storage.plan(self.root,{'action':'delete','job_ids':[sid]})
        target=self.root/'jobs'/('c'*32)
        try:target.symlink_to(self.weights,target_is_directory=True)
        except OSError:return
        with self.assertRaises(ValueError):storage.bounded(target,self.root/'jobs')
        self.assertTrue((self.weights/'model.pth').exists())

    def test_archived_child_preview_survives_parent_removal(self):
        child='b'*32;job=self.make_job(child,parent=self.sid)
        storage.execute(self.root,{'action':'archive','job_ids':[child]})
        expected=(job/'output/preview.html').read_bytes()
        storage.execute(self.root,{'action':'delete','job_ids':[self.sid]})
        self.assertEqual((job/'output/preview.html').read_bytes(),expected)

    def test_cache_only_removes_cache_and_inventory_reports_totals(self):
        before=storage.inventory(self.root,self.weights)
        self.assertGreater(before['jobs_bytes'],before['cache_bytes']);self.assertGreater(before['weights_bytes'],0)
        storage.execute(self.root,{'action':'cache'})
        self.assertEqual(storage.size(self.root/'cache'),0);self.assertTrue(self.job.exists());self.assertTrue(self.external.exists())

    def test_revision_hardlinks_are_not_reported_as_reclaimed_current_data(self):
        import os
        revision=self.job/'output/revisions';(revision/'old.html').unlink()
        os.link(self.job/'output/preview.html',revision/'current.html')
        expected=storage.size(self.job)
        self.assertEqual(storage.plan(self.root,{'action':'temporary','job_ids':[self.sid]})['bytes'],0)
        self.assertEqual(storage.execute(self.root,{'action':'temporary','job_ids':[self.sid]})['removed_bytes'],0)
        self.assertEqual(storage.size(self.job),expected)

    def test_stale_unix_socket_does_not_break_storage_inventory(self):
        import socket
        if not hasattr(socket,'AF_UNIX'):self.skipTest('Unix sockets unavailable')
        sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        try:
            try:sock.bind(str(self.root/'cache/stale.sock'))
            except OSError:self.skipTest('Unix socket binding unavailable')
            self.assertGreater(storage.inventory(self.root,self.weights)['jobs_bytes'],0)
        finally:sock.close()

    def test_http_preview_confirmation_one_use_token_and_origin(self):
        import server
        accounts,headers=fixture(self.root)
        with patch.multiple(server,ROOT=self.root,JOBS=self.root/'jobs',CONFIG={'weights':'../weights','minimum_free_gb':0,'port':0},AUTH=accounts,PROCESSES={},CLEANUP_PLANS={}):
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
            origin=f'http://127.0.0.1:{http.server_port}'
            op=opener(headers)
            def post(path,data,headers=None):
                request=urllib.request.Request(origin+path,json.dumps(data).encode(),headers or {'Content-Type':'application/json','X-Local-App':'1','Origin':origin})
                return json.load(op.open(request))
            try:
                for headers in [{'Content-Type':'application/json'},{'Content-Type':'application/json','X-Local-App':'1','Origin':'https://untrusted.example'}]:
                    with self.assertRaises(urllib.error.HTTPError) as error:post('/api/storage/plan',{'action':'delete','job_ids':[self.sid]},headers)
                    self.assertEqual(error.exception.code,403)
                plan=post('/api/storage/plan',{'action':'archive','job_ids':[self.sid]});self.assertTrue(plan['removes_source']);self.assertNotIn('_paths',plan)
                with self.assertRaises(urllib.error.HTTPError):post('/api/storage/execute',{'token':plan['token']})
                self.assertTrue((self.job/'input/source.dcm').exists())
                post('/api/storage/execute',{'token':plan['token'],'confirmed':True})
                with self.assertRaises(urllib.error.HTTPError):post('/api/storage/execute',{'token':plan['token'],'confirmed':True})
                self.assertTrue((self.job/'output/preview.html').exists());self.assertTrue(self.external.exists())
            finally:http.shutdown();http.server_close();thread.join()


if __name__=='__main__':unittest.main()
