"""Loopback-only local DICOM upload and reconstruction job controller."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
import argparse
import math
import json
import os
import queue
import re
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
import time
import uuid
import gzip
import hashlib
import hmac
from auth import Accounts, Forbidden, can_manage, authorize_cleanup, COOKIE
from structures import resolve, capabilities, reconstruction_presets, validate_candidate_parameters

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
# Linux venv launchers are symlinks. Resolving the final symlink launches the
# system interpreter and loses that venv's site-packages.
PYTHON = Path(os.path.abspath(ROOT / CONFIG["python"]))
JOBS = ROOT / "jobs"
JOBS.mkdir(exist_ok=True)
LOCK = threading.RLock()
QUEUE = queue.Queue()
PROCESSES = {}
ACTIVITY = {}
RESOURCES = None
MAX_UPLOAD = 4 * 1024**3
ACTIVE = {"uploading", "queued_scan", "scanning", "queued", "running"}
CLEANUP_PLANS = {}
AUTH = None


def enough_space():
    if shutil.disk_usage(ROOT).free < CONFIG.get('minimum_free_gb', 2) * 1024**3:
        raise ValueError('可用磁盘空间不足，请先在存储与清理中释放空间')


def stop_process(process):
    if process.poll() is not None:return
    if os.name == 'nt':
        subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(20):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(.05)


def update(job, **values):
    with LOCK:
        status = read(job / "status.json")
        if values.get('state') in {'queued', 'queued_scan'}:
            values.update(queued_at=time.time())
        status.update(values, updated_at=time.time())
        write(job / "status.json", status)
        return status


def create(kind, owner):
    with LOCK:
        enough_space()
        if sum(read(p)["state"] in ACTIVE for p in JOBS.glob("*/status.json")) >= 4:
            raise ValueError("已有多个处理中或上传中的任务，请先完成或取消")
        job = JOBS / uuid.uuid4().hex
        (job / "input").mkdir(parents=True)
        write(job / "status.json", {"id": job.name, "kind": kind, "state": "uploading", "created_at": time.time(), "updated_at": time.time(), "bytes": 0, "files": 0, 'owner_id':owner['id'], 'owner_name':owner['username'], 'upload_stats':{'accepted':0,'rejected':0}})
        return job


def public(job):
    status = read(job / "status.json")
    result = {key: status[key] for key in ("id", "kind", "state", "created_at", "updated_at", "started_at", "queued_at", "finished_at", "bytes", "files", "error", "mode", "series_id", "published", "structures", "margin_mm", "source_job", "data_pruned", "note_name", "inplace_operation", 'owner_name','upload_stats') if key in status}
    if (job/'upload-validation.json').is_file():result['upload_stats']=read(job/'upload-validation.json')
    if status["state"] in {"ready", "queued", "running", "completed", "failed", "cancelled"} and (job / "inventory.json").exists():
        result["series"] = [{k: v for k, v in series.items() if k != "files"} for series in read(job / "inventory.json")["series"]]
    if (job / "progress.json").exists():
        result["progress"] = read(job / "progress.json")
    pointer=job/'output/live/current.json'
    if status['state'] in {'running','failed','cancelled'} and pointer.is_file():
        stream=read(pointer)
        if re.fullmatch('[a-f0-9]{32}',stream.get('token','')) and stream.get('started_at')==status.get('started_at'):
            result['live_preview']=f"/results/{job.name}/live/{stream['token']}/preview.html"
    if job.name in PROCESSES and status['state'] in {'scanning', 'running'}:
        from job_activity import activity
        result['activity'] = activity(job, status, result.get('progress', {}), PROCESSES[job.name], ACTIVITY.get(job.name))
        ACTIVITY[job.name] = result['activity']
    if (job/'output/preview.html').is_file():
        result["preview"] = f"/results/{job.name}/preview.html"
        review = job/"output/candidate_review.json"
        if review.exists():
            record = read(review)
            result["candidate_review"] = {"parameters":record.get("parameters",{}),"count":len(record["candidates"]),"candidates":[{**{k:v for k,v in entry.items() if k not in {"mask_file","thumbnail_file"}}, "thumbnail":f"/results/{job.name}/candidate/{entry['id']}.png"} for entry in record["candidates"]]}
        result["candidate_ids"] = status.get("candidate_ids",[])
    return result


def finish_update(job, success, error=None):
    status = read(job/'status.json')
    if not status.get('inplace_operation'):
        return False
    if not success:
        from study_updates import restore, CONFIG_KEYS
        previous = restore(job)
        for key in CONFIG_KEYS:status.pop(key, None)
        status.update(previous)
    status.pop('inplace_operation', None)
    status.update(state='completed', error=error, updated_at=time.time(), finished_at=time.time())
    write(job/'status.json', status)
    write(job/'progress.json', {'stage':'completed','percent':100,'message':'更新完成' if success else '已恢复上一版预览'})
    return True


def queue_update(job, status, command, **values):
    enough_space()
    from study_updates import begin
    begin(job, status)
    update(job, state='queued', error=None, inplace_operation=command, **values)
    write(job/'progress.json', {'stage':'queued','percent':0,'message':'等待更新当前检查'})
    QUEUE.put((job, command))


def work():
    while True:
        job, command = QUEUE.get()
        try:
            with LOCK:
                status = read(job / "status.json")
                expected = "queued_scan" if command == "scan" else "queued"
                if status["state"] != expected:
                    continue
                update(job, state="scanning" if command == "scan" else "running", started_at=time.time())
                scratch = job/'cache'
                scratch.mkdir(parents=True, exist_ok=True)
                env = os.environ.copy()
                env.update(PYTHONPATH=str(ROOT / "offline_guard"), TMPDIR=str(scratch), PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS=str(CONFIG["threads"]), OMP_NUM_THREADS=str(CONFIG["threads"]), MKL_NUM_THREADS=str(CONFIG["threads"]), OPENBLAS_NUM_THREADS=str(CONFIG['threads']), NUMEXPR_NUM_THREADS=str(CONFIG['threads']), MALLOC_ARENA_MAX='2', nnUNet_compile="false", HF_HUB_OFFLINE="1")
                log = (job / "worker.log").open("ab")
                invocation = RESOURCES.command(PYTHON, ROOT/'worker.py', command, job) if RESOURCES else [str(PYTHON), str(ROOT/'worker.py'), command, str(job)]
                process = subprocess.Popen(invocation, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,start_new_session=os.name!='nt')
                try:
                    if RESOURCES: RESOURCES.started(process, job)
                except Exception:
                    stop_process(process)
                    log.close()
                    raise
                PROCESSES[job.name] = process
            code = process.wait()
            log.close()
            resource_error = RESOURCES.finished(process) if RESOURCES else None
            with LOCK:
                PROCESSES.pop(job.name, None)
                ACTIVITY.pop(job.name, None)
                if read(job / "status.json")["state"] == "cancelled":
                    finish_update(job, False, '本次更新已取消，保留上一版结果')
                    continue
                if code == 0 and (command == "scan" or (job / "output" / "preview.html").is_file()):
                    if not finish_update(job, True):update(job, state="ready" if command == "scan" else "completed", error=None, finished_at=time.time())
                else:
                    progress = read(job / "progress.json") if (job / "progress.json").exists() else {}
                    error=resource_error or progress.get("error", "处理进程退出，请检查 worker.log")
                    if not finish_update(job, False, '更新失败，已恢复上一版：'+error):update(job, state="failed", error=error)
        except Exception:
            if not finish_update(job, False, '更新中断，已恢复上一版'):update(job, state="failed", error="启动或执行处理失败，请检查运行环境")
        finally:
            QUEUE.task_done()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def handle_one_request(self):
        try: return super().handle_one_request()
        finally: self.connection.settimeout(20)

    def log_message(self, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            pass

    def headers_out(self, status, kind, length, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        for key,value in (extra or {}).items():self.send_header(key,value)
        self.end_headers()

    def send_json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.headers_out(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def send_file(self, path, kind, download=None):
        with path.open("rb") as stream:
            self.headers_out(200, kind, os.fstat(stream.fileno()).st_size,{'Content-Disposition':f'attachment; filename="{download}"'} if download else None)
            shutil.copyfileobj(stream, self.wfile, 1024 * 1024)

    def valid_host(self):
        return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def principal(self):
        return AUTH.session(self.headers.get('Cookie')) if AUTH else None

    def require_login(self, page=False):
        self.user=self.principal()
        if self.user:return True
        self.close_connection=True
        if page:
            from urllib.parse import quote
            self.headers_out(302,'text/plain',0,{'Location':'/login?next='+quote(self.path,safe='')})
        else:self.send_json({'error':'请先登录'},401)
        return False

    def csrf_ok(self):
        return hmac.compare_digest(self.headers.get('X-CSRF-Token',''),self.user['csrf'])

    def do_GET(self):
        if not self.valid_host():
            return self.send_json({"error": "仅允许本机地址"}, 403)
        url = urlsplit(self.path)
        if url.path in {'/login','/login.js','/login.css'}:
            name={'/login':'login.html','/login.js':'login.js','/login.css':'login.css'}[url.path]
            return self.send_file(ROOT/'web'/name,'text/html; charset=utf-8' if name.endswith('.html') else 'text/javascript; charset=utf-8' if name.endswith('.js') else 'text/css; charset=utf-8')
        if url.path=='/api/healthz':
            return self.send_json({'ok':True,'resource_isolation':RESOURCES is not None})
        if not self.require_login(url.path.endswith(('.html','/'))):return
        if url.path=='/api/auth/check':return self.headers_out(204,'text/plain',0)
        if url.path=='/api/auth/session':
            return self.send_json({k:self.user[k] for k in ('id','username','role','csrf')})
        if url.path=='/api/auth/users':
            return self.send_json({'users':AUTH.users()}) if self.user['role']=='admin' else self.send_json({'error':'仅管理员可管理账号'},403)
        routes = {"/": ("web/index.html", "text/html; charset=utf-8"), "/workbench/": ("web/index.html", "text/html; charset=utf-8"), "/app.js": ("web/app.js", "text/javascript; charset=utf-8"), "/storage.js": ("web/storage.js", "text/javascript; charset=utf-8"), "/account.js": ("web/account.js", "text/javascript; charset=utf-8"), "/app.css": ("web/app.css", "text/css; charset=utf-8")}
        try:
            if url.path == "/favicon.ico":
                return self.headers_out(204, "image/x-icon", 0)
            if url.path == '/api/healthz':
                return self.send_json({'ok':True,'resource_isolation':RESOURCES is not None})
            if url.path in routes:
                file, kind = routes[url.path]
                return self.send_file(ROOT / file, kind)
            if url.path == "/api/environment":
                weights = (ROOT / CONFIG["weights"]).resolve()
                complete = lambda i:any(p.is_dir() and any(p.rglob('checkpoint_final.pth')) for p in weights.glob(f'Dataset{i}_*'))
                return self.send_json({"python_available": PYTHON.is_file(), "model_folders": {str(i): complete(i) for i in (297, 298, 291, 292, 293, 294, 295, 117, 913)}, "capabilities": capabilities(), "presets": reconstruction_presets(), "offline": True, "study_centered":True, "threads": CONFIG["threads"], "free_disk_gb": round(shutil.disk_usage(ROOT).free / 1024**3, 1), "deployment_mode":CONFIG.get('deployment_mode','local'), "portal_url":CONFIG.get('portal_url')})
            if url.path == '/api/storage':
                from storage import inventory
                with LOCK:
                    data=inventory(ROOT, (ROOT/CONFIG['weights']).resolve())
                    for row in data['jobs']:
                        row['can_clean']=row['can_clean'] and can_manage(self.user,read(JOBS/row['id']/'status.json'))
                    data['can_clean_cache']=self.user['role']=='admin'
                    return self.send_json(data)
            if url.path == "/api/jobs":
                with LOCK:
                    jobs = [public(p.parent) for p in JOBS.glob("*/status.json")]
                    for row in jobs:row['can_manage']=can_manage(self.user,read(JOBS/row['id']/'status.json'))
                return self.send_json({"jobs": sorted(jobs, key=lambda job: job["created_at"], reverse=True)})
            live=re.fullmatch(r'/results/([a-f0-9]{32})/live/([a-f0-9]{32})/(preview\.html|snapshot|asset/([a-f0-9]{64})\.gz)',url.path)
            if live:
                job=JOBS/live[1]; folder=job/'output/live'/live[2]
                if folder.is_symlink():raise ValueError('Invalid live preview')
                if live[3]=='snapshot':
                    snapshot=read(folder/'snapshot.json');status=read(job/'status.json')
                    from progressive import presentation_plan
                    snapshot['plan']=presentation_plan(snapshot,status)
                    pointer=read(job/'output/live/current.json')
                    terminal=snapshot['complete'] or status['state'] not in {'queued','running'} or pointer['token']!=live[2]
                    return self.send_json({**snapshot,'state':status['state'],'terminal':terminal,'error':status.get('error'),'progress':read(job/'progress.json') if (job/'progress.json').is_file() else {},'preview':f'/results/{live[1]}/preview.html' if (job/'output/preview.html').is_file() else None})
                if live[4]:
                    path=folder/'mesh'/live[4]
                    if path.is_symlink() or path.stat().st_size>128*1024**2:raise ValueError('Invalid live asset')
                    data=path.read_bytes()
                    if hashlib.sha256(data).hexdigest()!=live[4]:raise ValueError('Live asset checksum failed')
                    self.send_response(200);self.send_header('Content-Type','application/octet-stream');self.send_header('Content-Length',str(len(data)))
                    if parse_qs(url.query).get('decoded')==['1']:self.send_header('Content-Encoding','gzip')
                    self.send_header('Cache-Control','private, max-age=86400, immutable');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(data)
                    return
                generation=read(folder/'online/current.json')['generation']
                if not re.fullmatch('[a-f0-9]{32}',generation):raise ValueError('Invalid live version')
                return self.send_file(folder/'online'/generation/'index.html','text/html; charset=utf-8')
            thumb=re.fullmatch(r"/results/([a-f0-9]{32})/candidate/(suspect_[0-9]{3,6})\.png",url.path)
            if thumb:
                path=JOBS/thumb[1]/"output/candidate_regions"/(thumb[2]+".png")
                if path.is_file():
                    return self.send_file(path,"image/png")
                return self.send_json({"error":"候选预览不存在"},404)
            asset=re.fullmatch(r'/results/([a-f0-9]{32})/view/([a-f0-9]{32})/(asset/([a-f0-9]{64})\.gz|plane|volume)',url.path)
            live_view=re.fullmatch(r'/results/([a-f0-9]{32})/live/([a-f0-9]{32})/view/([a-f0-9]{32})/(asset/[a-f0-9]{64}\.gz|plane|volume)',url.path)
            if live_view:
                asset=re.fullmatch(r'/results/([a-f0-9]{32})/view/([a-f0-9]{32})/(asset/([a-f0-9]{64})\.gz|plane|volume)',f'/results/{live_view[1]}/view/{live_view[3]}/{live_view[4]}')
            if asset:
                output=JOBS/asset[1]/'output'
                directory=output/'live'/live_view[2]/'online'/asset[2] if live_view else output/'online'/asset[2]
                if not live_view and not (directory/'index.json').is_file():directory=output/'revisions/study-previous/online'/asset[2]
                from preview_access import Preview,SLOTS
                preview=Preview(directory)
                headers={}
                with SLOTS:
                    if asset[4]:data=preview.compressed(asset[4])
                    elif asset[3]=='plane':
                        request=json.loads(parse_qs(url.query).get('request',['{}'])[0])
                        data=gzip.compress(memoryview(preview.plane(request)).cast('B'),compresslevel=1,mtime=0)
                    else:
                        query=parse_qs(url.query)
                        values,start,size=preview.volume(query.get('scope',[''])[0],query.get('budget',['0'])[0])
                        data=gzip.compress(memoryview(values).cast('B'),compresslevel=1,mtime=0)
                        del values
                        headers={'X-CT-Start':json.dumps(start.tolist()),'X-CT-Size':json.dumps(size.tolist())}
                self.send_response(200)
                for key,value in headers.items():self.send_header(key,value)
                self.send_header('Content-Type','application/octet-stream')
                self.send_header('Content-Length',str(len(data)))
                if parse_qs(url.query).get('decoded')==['1']:
                    self.send_header('Content-Encoding','gzip')
                self.send_header('Cache-Control','private, no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.end_headers();self.wfile.write(data)
                return
            match = re.fullmatch(r"/results/([a-f0-9]{32})/preview\.html", url.path)
            if match:
                job = JOBS / match[1]
                if (job/'output/preview.html').is_file():
                    pointer=job/'output/online/current.json'
                    if parse_qs(url.query).get('download')!=['1'] and pointer.is_file():
                        generation=read(pointer)['generation']
                        if not re.fullmatch('[a-f0-9]{32}',generation):raise ValueError('Invalid preview version')
                        return self.send_file(job/'output/online'/generation/'index.html','text/html; charset=utf-8')
                    return self.send_file(job / "output" / "preview.html", "text/html; charset=utf-8",download='thorax-'+match[1]+'.html' if parse_qs(url.query).get('download')==['1'] else None)
            return self.send_json({"error": "未找到"}, 404)
        except (OSError, ValueError, KeyError) as error:
            return self.send_json({"error": str(error) if url.path.endswith('/volume') else "结果暂不可读取，请刷新重试"}, 404)

    def body_json(self):
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 8192:
            raise ValueError("请求大小不正确")
        return json.loads(self.rfile.read(size))

    def receive(self, job, path, limit=MAX_UPLOAD):
        size = int(self.headers.get("Content-Length", "0"))
        with LOCK:
            status = read(job / "status.json")
            if status["state"] != "uploading" or not 0 < size <= limit or status["bytes"] + size > MAX_UPLOAD or status["files"] >= 20000:
                raise ValueError("上传状态或大小不正确：单任务最多 4 GiB、20000 个文件")
            if shutil.disk_usage(ROOT).free < size * 3 + 2 * 1024**3:
                raise ValueError("本地可用磁盘空间不足")
            update(job, bytes=status["bytes"] + size, files=status["files"] + 1)
        self.connection.settimeout(180)
        with path.open("xb") as stream:
            remaining = size
            while remaining:
                block = self.rfile.read(min(1024**2, remaining))
                if not block:
                    raise ValueError("上传中断，请重新上传")
                stream.write(block)
                remaining -= len(block)

    def do_POST(self):
        origin = self.headers.get("Origin")
        origins = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"} | set(CONFIG.get('public_origins', []))
        if not self.valid_host() or self.headers.get("X-Local-App") != "1" or (origin and origin not in origins):
            self.close_connection = True
            return self.send_json({"error": "仅允许本机应用请求"}, 403)
        url = urlsplit(self.path)
        if url.path=='/api/auth/login':
            try:
                data=self.body_json()
                if not isinstance(data,dict):raise ValueError('登录参数无效')
                if AUTH is None:return self.send_json({'error':'账号服务未配置'},503)
                token=AUTH.login(data.get('username'),data.get('password'),self.headers.get('X-Real-IP',str(self.client_address)))
                if not token:return self.send_json({'error':'账号或密码不正确'},401)
                secure='; Secure' if self.headers.get('X-Forwarded-Proto')=='https' else ''
                self.headers_out(200,'application/json',2,{'Set-Cookie':f'{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200'+secure})
                self.wfile.write(b'{}');return
            except Forbidden as error:return self.send_json({'error':str(error)},429)
            except (OSError,ValueError,KeyError):
                self.close_connection=True
                return self.send_json({'error':'登录参数无效'},400)
        if not self.require_login():return
        if not self.csrf_ok():
            self.close_connection=True
            return self.send_json({'error':'登录校验已失效，请刷新页面'},403)
        job = None
        try:
            binary=url.path=='/api/uploads/zip' or re.fullmatch(r'/api/jobs/[a-f0-9]{32}/file',url.path)
            content_type=self.headers.get('Content-Type','').split(';')[0]
            if binary and content_type!='application/octet-stream':
                raise ValueError('上传接口仅接收二进制文件内容')
            request_data = self.body_json() if self.headers.get("Content-Type", "").split(";")[0] == "application/json" else None
            if url.path=='/api/auth/logout':
                AUTH.logout(self.user)
                self.headers_out(200,'application/json',2,{'Set-Cookie':f'{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0'})
                self.wfile.write(b'{}');return
            if url.path=='/api/auth/password':
                if not isinstance(request_data,dict):raise ValueError('密码参数无效')
                # Administrators must also prove their old password when changing their own.
                actor={**self.user,'role':'user'}
                AUTH.change(self.user['id'],request_data,actor)
                return self.send_json({'updated':True})
            if url.path=='/api/auth/users':
                if self.user['role']!='admin':raise Forbidden('仅管理员可管理账号')
                if not isinstance(request_data,dict):raise ValueError('账号参数无效')
                if request_data.get('id'):AUTH.change(request_data['id'],request_data,self.user)
                else:AUTH.create(request_data.get('username'),request_data.get('password'),request_data.get('role','user'))
                return self.send_json({'updated':True})
            if url.path in {'/api/storage/plan', '/api/storage/execute'}:
                if not isinstance(request_data, dict):
                    raise ValueError('清理参数格式不正确')
                from storage import plan, execute, states
                with LOCK:
                    now = time.monotonic()
                    for key in list(CLEANUP_PLANS):
                        if CLEANUP_PLANS[key]['expires'] < now:
                            del CLEANUP_PLANS[key]
                    if url.path.endswith('/plan'):
                        authorize_cleanup(self.user,request_data,states(ROOT))
                        if len(CLEANUP_PLANS)>=128:
                            raise ValueError('待确认清理方案过多，请稍后重试')
                        preview = plan(ROOT, request_data, PROCESSES)
                        token = uuid.uuid4().hex
                        CLEANUP_PLANS[token] = {'request':request_data, 'expires':now+600,'user':self.user['id'],'session':self.user['token']}
                        return self.send_json({k:v for k,v in preview.items() if k!='_paths'} | {'token':token})
                    token=request_data.get('token')
                    if not isinstance(token,str):raise ValueError('清理确认令牌无效')
                    entry = CLEANUP_PLANS.get(token)
                    if entry and (entry['user']!=self.user['id'] or entry['session']!=self.user['token']):raise Forbidden('清理确认仅对当前登录会话有效')
                    if request_data.get('confirmed') is not True or not entry:
                        raise ValueError('请先查看清理范围并明确确认；确认方案十分钟后过期')
                    authorize_cleanup(self.user,entry['request'],states(ROOT))
                    CLEANUP_PLANS.pop(request_data['token'])
                    return self.send_json(execute(ROOT, entry['request'], PROCESSES))
            if url.path == "/api/uploads/zip":
                if self.user['role'] not in {'admin','user'}:raise Forbidden('当前账号无上传权限')
                job = create("zip",self.user)
                self.receive(job, job / "upload.zip")
                update(job, state="queued_scan")
                QUEUE.put((job, "scan"))
                return self.send_json({"id": job.name}, 201)
            if url.path == "/api/uploads/folder":
                if self.user['role'] not in {'admin','user'}:raise Forbidden('当前账号无上传权限')
                job = create("folder",self.user)
                return self.send_json({"id": job.name}, 201)
            match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})/(file|commit|start|cancel|derive|screen|selected|note|preview-error)", url.path)
            if not match or not (JOBS / match[1] / "status.json").exists():
                return self.send_json({"error": "任务不存在"}, 404)
            job = JOBS / match[1]
            action = match[2]
            if action == 'preview-error':
                if not isinstance(request_data,dict):raise ValueError('Invalid preview error')
                details={key:str(request_data.get(key,''))[:400] for key in ('message','resource','browser','attempts','status')}
                print('Preview client error '+json.dumps({'study':job.name,**details},ensure_ascii=False),flush=True)
                return self.send_json({'recorded':True})
            if not can_manage(self.user,read(job/'status.json')):raise Forbidden('只能修改您上传的检查')
            if action == "file":
                from dicom_ingest import accept, Rejected, MAX_FILE
                temporary=job/(uuid.uuid4().hex+'.ingest')
                try:
                    self.receive(job,temporary,MAX_FILE)
                    try:accept(temporary,job/'input');accepted=True
                    except Rejected:accepted=False
                    with LOCK:
                        stats=read(job/'status.json').get('upload_stats',{'accepted':0,'rejected':0})
                        stats['accepted' if accepted else 'rejected']+=1
                        update(job,upload_stats=stats)
                    return self.send_json({'uploaded':accepted,'discarded':not accepted})
                finally:temporary.unlink(missing_ok=True)
            with LOCK:
                status = read(job / "status.json")
                if action == 'note':
                    note=request_data.get('note_name') if isinstance(request_data,dict) else None
                    if not isinstance(note,str) or len(note)>80 or any(ord(c)<32 for c in note):
                        raise ValueError('备注名限 80 字，不支持换行或控制字符')
                    update(job,note_name=note.strip())
                    return self.send_json({'note_name':note.strip()})
                if status.get('data_pruned') and action in {'derive','screen','selected'}:
                    raise ValueError('此任务仅保留离线预览；请重新上传 DICOM 后重建')
                if action in {"screen","selected"} and status["state"] == "completed":
                    if not isinstance(request_data,dict):
                        raise ValueError("候选参数无效")
                    candidate_parameters=validate_candidate_parameters(request_data.get("parameters",{}))
                    manual=request_data.get("manual_points_lps",[])
                    if not isinstance(manual,list) or len(manual)>10 or any(not isinstance(point,list) or len(point)!=3 or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in point) for point in manual):
                        raise ValueError("手工定位必须为有限的三维 LPS 毫米坐标")
                    selected_ids=[]
                    if action == "selected":
                        review_path=job/"output/candidate_review.json"
                        if not review_path.exists() or request_data.get("confirmed") is not True:
                            raise ValueError("请先筛选候选，再提交所选结节")
                        entries={entry["id"] for entry in read(review_path)["candidates"]}
                        selected_ids=request_data.get("candidate_ids")
                        if not isinstance(selected_ids,list) or len(selected_ids)>40 or any(not isinstance(sid,str) or sid not in entries for sid in selected_ids):
                            raise ValueError("请选择当前检查中有效的待核对区域")
                        selected_ids=list(dict.fromkeys(selected_ids))
                    changes={'candidate_ids':selected_ids} if action=='selected' else {'manual_points_lps':manual,'candidate_parameters':candidate_parameters}
                    queue_update(job,status,action,**changes)
                    return self.send_json({'id':job.name,'queued':True})
                if action == "derive" and status["state"] == "completed":
                    if not isinstance(request_data, dict) or request_data.get("mode") not in {"quick", "full"}:
                        raise ValueError("请选择有效的重建模式和结构")
                    selected, margin = resolve(request_data)
                    queue_update(job,status,'run',mode=request_data['mode'],structures=selected,margin_mm=margin)
                    return self.send_json({'id':job.name,'queued':True})
                if action == "cancel":
                    if status["state"] not in ACTIVE | {"ready"}:
                        raise ValueError("任务已经结束")
                    update(job, state="cancelled")
                    process = PROCESSES.get(job.name)
                    if process:stop_process(process)
                    else:finish_update(job,False,'本次更新已取消，保留上一版结果')
                    return self.send_json({"cancelled": True})
                if action == "commit" and status["state"] == "uploading":
                    if not status.get('upload_stats',{}).get('accepted'):
                        update(job,state='failed',error='未找到符合要求的 DICOM CT 文件')
                        raise ValueError('未找到符合要求的 DICOM CT 文件，其余文件已过滤')
                    update(job, state="queued_scan")
                    QUEUE.put((job, "scan"))
                    return self.send_json({"queued": True})
                if action == "start" and status["state"] == "ready":
                    enough_space()
                    if not isinstance(request_data, dict):
                        raise ValueError("重建参数格式不正确")
                    request = request_data
                    if request.get("mode") not in {"quick", "full"}:
                        raise ValueError("未知重建模式")
                    if request.get("series_id") not in {s["id"] for s in read(job / "inventory.json")["series"] if s["eligible"]}:
                        raise ValueError("请选择有效 CT 序列")
                    selected, margin = resolve(request)
                    update(job, state="queued", mode=request["mode"], series_id=request["series_id"], structures=selected, margin_mm=margin)
                    QUEUE.put((job, "run"))
                    return self.send_json({"queued": True})
                raise ValueError("任务当前状态不允许此操作")
        except Forbidden as error:
            self.close_connection=True
            return self.send_json({'error':str(error)},403)
        except (OSError, ValueError, KeyError) as error:
            if job and url.path == "/api/uploads/zip":
                update(job, state="failed", error="上传失败，请重新上传")
            self.close_connection = True
            return self.send_json({"error": str(error)}, 400)


class BoundedThreads:
    def __init__(self, *args, **kwargs):
        self.handler_slots = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.handler_slots.acquire(blocking=False):
            try: request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nRetry-After: 3\r\nConnection: close\r\n\r\n')
            except OSError: pass
            self.shutdown_request(request)
            return
        try: super().process_request(request, client_address)
        except Exception:
            self.handler_slots.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, client_address):
        try: super().process_request_thread(request, client_address)
        finally: self.handler_slots.release()


class BoundedHTTPServer(BoundedThreads, ThreadingHTTPServer):
    request_queue_size = 64


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=CONFIG["port"])
    parser.add_argument('--unix-socket')
    args = parser.parse_args()
    AUTH=Accounts(os.environ.get('WORKBENCH_AUTH_DATABASE',ROOT.parent/'auth/accounts.sqlite3'))
    if not AUTH.users():raise RuntimeError('须先使用 auth.py 初始化管理员账号')
    from resource_control import setup
    RESOURCES = setup(ROOT.parent/'run')
    with LOCK:
        for path in JOBS.glob("*/status.json"):
            if read(path)["state"] in ACTIVE:
                if not finish_update(path.parent,False,'服务重启，已恢复上一版结果'):update(path.parent, state="failed", error="上次服务退出时任务中断，输入文件已保留")
    threading.Thread(target=work, daemon=True).start()
    if args.unix_socket:
        class UnixHTTPServer(BoundedThreads, socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
            request_queue_size = 64
            server_name = 'localhost'
            server_port = args.port
        path = Path(args.unix_socket)
        if path.exists():
            if not path.is_socket():raise ValueError('Unix socket 路径已被其他文件占用')
            path.unlink()
        server = UnixHTTPServer(str(path), Handler)
        os.chmod(path, 0o666)
    else:
        server = BoundedHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    print(f"Local reconstruction: http://127.0.0.1:{args.port}/", flush=True)
    try:
        server.serve_forever()
    finally:
        for process in list(PROCESSES.values()):
            stop_process(process)
