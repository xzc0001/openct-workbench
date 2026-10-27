"""Delegated Linux cgroups keep expensive reconstruction outside the web group."""
from pathlib import Path
import json
import math
import os
import sys
import time
import uuid
from contextlib import contextmanager

MIB = 1024**2
WORKER_MAX = 3072*MIB
WORKER_SWAP = 1024*MIB

def worker_budget():
    value = int(os.environ.get('WORKBENCH_WORKER_BUDGET_BYTES',str(WORKER_MAX)))
    if value < 256*MIB:
        raise ValueError('计算内存预算不能小于 256 MiB')
    return value

def atomic(path, value):
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(value), encoding='utf-8')
    temporary.replace(path)

@contextmanager
def runtime_lock(runtime):
    import fcntl
    fd = os.open(Path(runtime)/'resource.lock', os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)

def current_group():
    record = next(line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
    return Path('/sys/fs/cgroup')/record.lstrip('/')

class Resources:
    def __init__(self, root, runtime):
        self.root, self.runtime = root, runtime
        self.compute = root/'compute'
    def configure(self):
        budget = worker_budget()
        required = {'cpu','memory','pids'}
        if not required.issubset((self.root/'cgroup.controllers').read_text().split()):
            raise RuntimeError('计算资源隔离不可用，请检查 systemd Delegate 配置')
        web = self.root/'web'; web.mkdir(exist_ok=True)
        (web/'cgroup.procs').write_text(str(os.getpid()))
        (self.root/'cgroup.subtree_control').write_text('+cpu +memory +pids')
        self.compute.mkdir(exist_ok=True)
        with runtime_lock(self.runtime): pass
        for group, settings in [(web, {'memory.min':128*MIB,'memory.low':256*MIB,'memory.high':'max','memory.max':768*MIB,'memory.swap.max':256*MIB,'pids.max':64,'cpu.weight':100}), (self.compute, {'memory.high':'max','memory.max':budget,'memory.swap.max':WORKER_SWAP,'memory.oom.group':1,'pids.max':96,'cpu.weight':25})]:
            for name, value in settings.items(): (group/name).write_text(str(value))
    def oom_count(self):
        values = dict(line.split() for line in (self.compute/'memory.events').read_text().splitlines())
        return int(values['oom_kill'])
    def command(self, python, worker, command, job):
        return [str(python), str(Path(__file__).resolve()), '--enter', str(self.root), str(worker), command, str(job)]
    def started(self, process, job):
        record = {'pid':process.pid,'job':job.name,'token':uuid.uuid4().hex,'started_at':time.time(),'oom_start':self.oom_count()}
        process.workbench_resource_record = record
        with runtime_lock(self.runtime): atomic(self.runtime/'active-worker.json', record)
    def finished(self, process):
        with runtime_lock(self.runtime): return self._finished(process)
    def _finished(self, process):
        record = process.workbench_resource_record
        reason = None
        try:
            aborted = json.loads((self.runtime/'worker-abort.json').read_text())
            if aborted.get('token') == record['token']: reason = aborted['reason']
        except (OSError, ValueError, KeyError): pass
        if self.oom_count() > record['oom_start']:
            reason = '本次重建超出计算内存预算，已停止计算；输入和上一版结果已保留'
        try:
            if json.loads((self.runtime/'active-worker.json').read_text()).get('token') == record['token']:
                (self.runtime/'active-worker.json').unlink()
        except (OSError, ValueError): pass
        return reason

def setup(runtime):
    if os.environ.get('WORKBENCH_RESOURCE_ISOLATION') != '1': return None
    if sys.platform != 'linux': raise RuntimeError('计算资源隔离需要 Linux cgroup v2')
    resources = Resources(current_group(), Path(runtime))
    resources.configure()
    return resources

def validate_native_workload(selected, geometry, budget=None):
    """Reject an oversized native watershed; never reduce CT resolution silently."""
    budget = int(os.environ.get('WORKBENCH_WORKER_BUDGET_BYTES','0')) if budget is None else budget
    if not budget or 'ribs' not in selected: return
    size = geometry.get('original_geometry',geometry).get('dimensions_xyz')
    if not size: raise ValueError('缺少原始 CT 几何，不能评估骨性胸廓计算内存')
    estimated = math.prod(size)*32
    if estimated > budget:
        raise ValueError(f'原始 CT 骨性胸廓计算预计需要约 {estimated/1024**3:.1f} GiB 工作内存，超过当前 {budget/1024**3:.1f} GiB 计算预算。请增加计算内存后重试；输入和上一版结果已保留')

def enter(root, worker, command, job):
    root = Path(root)
    if current_group() != root/'web' or root.parent != Path('/sys/fs/cgroup/system.slice'):
        raise RuntimeError('工作进程资源组不正确')
    (root/'compute/cgroup.procs').write_text(str(os.getpid()))
    # A live limit increase must also reach admission in subsequently launched workers.
    os.environ['WORKBENCH_WORKER_BUDGET_BYTES'] = str(int((root/'compute/memory.max').read_text()))
    Path('/proc/self/oom_score_adj').write_text('500')
    os.execv(sys.executable, [sys.executable, worker, command, job])

if __name__ == '__main__':
    if len(sys.argv) != 6 or sys.argv[1] != '--enter': raise SystemExit('Invalid resource launcher')
    enter(*sys.argv[2:])
