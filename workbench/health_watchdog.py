"""Host-side end-to-end health recovery, separate from reconstruction limits."""
from pathlib import Path
import http.client
import json
import os
import socket
import subprocess
import time
import uuid
from contextlib import contextmanager

MIB = 1024**2
WORKER_MAX = 3072*MIB

def atomic(path, value):
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(value),encoding='utf-8')
    temporary.replace(path)

@contextmanager
def runtime_lock(runtime):
    import fcntl
    directory = os.open(runtime,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try: fd = os.open('resource.lock',os.O_RDWR|os.O_NOFOLLOW,dir_fd=directory)
    finally: os.close(directory)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        yield
    finally: os.close(fd)

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT.parent/'run'
CGROUP = Path('/sys/fs/cgroup/system.slice/reconstruction-workbench.service/compute')
UNIT = 'reconstruction-workbench.service'
CONTAINER = 'reconstruction-preview'
STATE = Path('/var/lib/reconstruction-health/state.json')

def gateway_commands(action):
    if os.environ.get('WORKBENCH_GATEWAY_MODE')=='native':
        return [['/usr/sbin/nginx','-c','/etc/reconstruction-nginx.conf','-t'],['systemctl','reload','reconstruction-gateway.service']] if action=='reload_gateway' else [['systemctl','restart','reconstruction-gateway.service']]
    return [['docker','exec',CONTAINER,'nginx','-t'],['docker','exec',CONTAINER,'nginx','-s','reload']] if action=='reload_gateway' else [['docker','restart',CONTAINER]]

def policy(state, backend, gateway, now, service='active'):
    if service in {'inactive','activating','deactivating'}:
        state.update(web_failures=0,gateway_failures=0)
        return None
    if not backend:
        state['web_failures'] = state.get('web_failures',0)+1
        state['gateway_failures'] = 0
        key, action = 'last_web_repair', 'restart_web'
        ready = state['web_failures']>=3
    elif not gateway:
        state['web_failures'] = 0
        state['gateway_failures'] = state.get('gateway_failures',0)+1
        key = 'last_gateway_repair'
        action = 'restart_gateway' if state.get('gateway_repairs',0) else 'reload_gateway'
        ready = state['gateway_failures']>=3
    else:
        state.update(web_failures=0,gateway_failures=0,gateway_repairs=0)
        return None
    if ready and (key not in state or now-state[key]>=120):
        state[key] = now
        if backend: state['gateway_repairs'] = state.get('gateway_repairs',0)+1
        return action
    return None

def pressured(state, memory, swap, full, now, active, limit=WORKER_MAX):
    if not active or limit <= 0 or memory < limit*.9 or swap < 512*MIB or full < 40:
        state.pop('pressure_since',None)
        return False
    state.setdefault('pressure_since',now)
    return now-state['pressure_since']>=60

class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(RUNTIME/'workbench.sock'))

def probe(connection):
    try:
        connection.request('GET','/api/healthz',headers={'Host':'127.0.0.1:8769','Connection':'close'})
        response = connection.getresponse()
        if response.status==503 and response.getheader('Retry-After')=='3':
            response.read(4096)
            return True  # Intentional concurrency backpressure is not a crash.
        return response.status==200 and json.loads(response.read(4096)).get('ok') is True
    except (OSError,ValueError,http.client.HTTPException): return False
    finally: connection.close()

def runtime_read(name):
    directory = os.open(RUNTIME,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        fd = os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
        with os.fdopen(fd,'rb') as stream:
            data = stream.read(4097)
            if len(data)>4096: raise ValueError('Invalid runtime record')
            return json.loads(data)
    finally: os.close(directory)

def runtime_abort(record):
    directory = os.open(RUNTIME,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    temporary = 'abort-'+uuid.uuid4().hex+'.tmp'
    try:
        fd = os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
        with os.fdopen(fd,'w') as stream:
            owner = os.fstat(directory)
            os.fchown(stream.fileno(),owner.st_uid,owner.st_gid)
            json.dump(record,stream)
        os.replace(temporary,'worker-abort.json',src_dir_fd=directory,dst_dir_fd=directory)
    finally: os.close(directory)

def protect_compute(state, now):
    try:
        record = runtime_read('active-worker.json')
        pid = int(record['pid'])
        active = str(pid) in (CGROUP/'cgroup.procs').read_text().split()
        memory = int((CGROUP/'memory.current').read_text())
        limit = int((CGROUP/'memory.max').read_text())
        swap = int((CGROUP/'memory.swap.current').read_text())
        full_line = next(line for line in (CGROUP/'memory.pressure').read_text().splitlines() if line.startswith('full '))
        full = float(dict(pair.split('=') for pair in full_line.split()[1:])['avg10'])
        if not pressured(state,memory,swap,full,now,active,limit): return
        with runtime_lock(RUNTIME):
            current = runtime_read('active-worker.json')
            if current.get('token')!=record.get('token'): return
            if str(pid) not in (CGROUP/'cgroup.procs').read_text().split(): return
            runtime_abort({'token':record['token'],'reason':'本次计算持续发生严重内存交换，已自动停止；输入和上一版结果已保留'})
            (CGROUP/'cgroup.kill').write_text('1')
        state.pop('pressure_since',None)
        print('Stopped reconstruction after sustained memory pressure',flush=True)
    except (OSError,ValueError,KeyError,StopIteration): state.pop('pressure_since',None)

def run(args, timeout=40):
    return subprocess.run(args,check=True,capture_output=True,text=True,timeout=timeout)

def main():
    try: state = json.loads(STATE.read_text())
    except (OSError,ValueError): state = {}
    now = time.time()
    service = run(['systemctl','show',UNIT,'-p','ActiveState','--value'],5).stdout.strip()
    if service=='active': protect_compute(state,now)
    backend = probe(UnixConnection('127.0.0.1',timeout=3)) if service=='active' else False
    gateway = probe(http.client.HTTPConnection(os.environ.get('WORKBENCH_GATEWAY_HOST','127.0.0.1'),int(os.environ.get('WORKBENCH_GATEWAY_PORT','8080')),timeout=3)) if backend else False
    previous_failures = state.get('web_failures',0)+state.get('gateway_failures',0)
    action = policy(state,backend,gateway,now,service)
    atomic(STATE,state)
    if action:
        print('Health recovery: '+action,flush=True)
        if action=='restart_web':
            run(['systemctl','reset-failed',UNIT],5)
            run(['systemctl','restart',UNIT])
        else:
            for command in gateway_commands(action): run(command,30)
    elif backend and gateway and previous_failures:
        print('Workbench and gateway healthy again',flush=True)

if __name__=='__main__': main()
