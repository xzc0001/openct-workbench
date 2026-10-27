"""Explicit cleanup inside this workbench; model weights and external DICOM stay out."""
from pathlib import Path
import json
import re
import shutil

ACTIVE = {'uploading', 'queued_scan', 'scanning', 'queued', 'running'}
JOB_ID = re.compile(r'[a-f0-9]{32}')


def linked(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def bounded(path, parent):
    path, parent = Path(path).absolute(), Path(parent).resolve()
    if path == parent or not path.is_relative_to(parent):
        raise ValueError('清理路径超出工作台数据范围')
    for part in (path, *path.parents):
        if part == parent:
            break
        if linked(part):
            raise ValueError('清理范围包含链接，已停止')
    if not path.resolve().is_relative_to(parent):
        raise ValueError('清理路径解析后超出工作台数据范围')
    return path


def size(path, seen=None):
    seen = set() if seen is None else seen
    if not path.exists() or linked(path):
        return 0
    if path.is_file():
        stat=path.stat();identity=(stat.st_dev,stat.st_ino)
        if identity in seen:return 0
        seen.add(identity)
        return stat.st_size
    return sum(size(p,seen) for p in path.iterdir()) if path.is_dir() else 0


def reclaimable(paths):
    """Count an inode only if this plan removes all of its hard links."""
    files={}
    def collect(path):
        if linked(path):raise ValueError('清理范围包含链接，已停止')
        if path.is_dir():
            for child in path.iterdir():collect(child)
        elif path.is_file():
            stat=path.stat();key=(stat.st_dev,stat.st_ino)
            row=files.setdefault(key,[stat.st_size,stat.st_nlink,0]);row[2]+=1
    for path in paths:collect(path)
    return sum(length for length,links,count in files.values() if count>=links)


def states(root):
    result = {}
    for path in (root/'jobs').glob('*/status.json'):
        if JOB_ID.fullmatch(path.parent.name):
            bounded(path, root/'jobs')
            result[path.parent.name] = json.loads(path.read_text(encoding='utf-8'))
    return result


def inventory(root, weights=None):
    root = Path(root).resolve()
    rows = []
    for sid, status in states(root).items():
        job = root/'jobs'/sid
        rows.append({'id':sid, 'note_name':status.get('note_name',''), 'state':status['state'], 'created_at':status['created_at'],
                     'bytes':size(job), 'temporary_bytes':reclaimable([p for p in [job/'output/revisions',job/'output/live',job/'cache'] if p.exists()]),
                     'data_pruned':status.get('data_pruned', False),
                     'source_job':status.get('source_job'),
                     'owner_name':status.get('owner_name',''), 'can_clean':status['state'] not in ACTIVE})
    disk = shutil.disk_usage(root)
    return {'disk_total':disk.total, 'disk_used':disk.used, 'disk_free':disk.free,
            'jobs_bytes':sum(r['bytes'] for r in rows), 'cache_bytes':size(root/'cache'),
            'weights_bytes':size(weights) if weights else 0,
            'jobs':sorted(rows, key=lambda row:row['created_at'], reverse=True)}


def plan(root, request, running=()):
    root = Path(root).resolve()
    action, ids = request.get('action'), request.get('job_ids', [])
    if not isinstance(action,str) or action not in {'cache', 'temporary', 'archive', 'delete'}:
        raise ValueError('未知清理操作')
    if not isinstance(ids, list) or len(ids)>100 or any(not isinstance(i,str) or not JOB_ID.fullmatch(i) for i in ids):
        raise ValueError('请选择有效任务')
    ids = list(dict.fromkeys(ids))
    if action != 'cache' and not ids:
        raise ValueError('请先选择需要清理的任务')
    if action == 'cache' and ids:
        raise ValueError('缓存清理不接收任务列表')
    status = states(root)
    busy = set(running) | {sid for sid,s in status.items() if s['state'] in ACTIVE}
    if action == 'cache' and busy:
        raise ValueError('有任务正在上传、排队或计算，请待完成后清理缓存')
    paths, rows = [], []
    if action == 'cache':
        folder = root/'cache'
        if folder.exists():
            paths = [bounded(p, root) for p in folder.iterdir()]
    for sid in ids:
        if sid not in status:
            raise ValueError('任务已不存在，请刷新列表')
        if sid in busy:
            raise ValueError('不能清理正在上传、排队或计算的任务：'+sid[:8])
        job = bounded(root/'jobs'/sid, root/'jobs')
        if action in {'archive','delete'}:
            children = [i for i,s in status.items() if s.get('source_job')==sid and not s.get('data_pruned') and i not in ids]
            if children:
                raise ValueError('任务 '+sid[:8]+' 被后续任务引用，请先处理：'+', '.join(i[:8] for i in children))
        if action == 'temporary':
            chosen = [job/'output/revisions', job/'output/preview.html.tmp',job/'output/live',job/'cache']
        elif action == 'archive':
            if status[sid]['state'] != 'completed' or not (job/'output/preview.html').is_file():
                raise ValueError('仅已完成且有预览的任务可保留预览后清理')
            chosen = [p for p in job.iterdir() if p.name not in {'output','status.json','progress.json'}]
            chosen += [p for p in (job/'output').iterdir() if p.name not in {'preview.html','manifest.json','online'}]
        else:
            chosen = [job]
        chosen = [bounded(p, root/'jobs') for p in chosen if p.exists()]
        count = reclaimable(chosen)
        rows.append({'id':sid,'bytes':count})
        paths.extend(chosen)
    names = {'cache':'清理可再生成的显示缓存与临时计算文件',
             'temporary':'移除所选检查的计算缓存、上一版备份和未完成预览',
             'archive':'仅保留当前离线预览和清单，移除所选检查的上传影像、CT、分割、STL 和中间数据',
             'delete':'删除所选检查及其影像、预览和重建数据'}
    return {'action':action, 'job_ids':ids, 'bytes':reclaimable(paths),
            'description':names[action], 'jobs':rows,
            'removes_source':action in {'archive','delete'}, '_paths':paths}


def execute(root, request, running=()):
    # The HTTP layer requires a one-use preview token plus explicit confirmation.
    root = Path(root).resolve()
    current = plan(root, request, running)
    # Mark before deleting: an interrupted cleanup must not advertise that its
    # remaining, potentially incomplete CT/inference inputs can be reconstructed.
    if current['action']=='archive':
        for sid in current['job_ids']:
            path = root/'jobs'/sid/'status.json'
            status = json.loads(path.read_text(encoding='utf-8'))
            status['data_pruned'] = True
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(path)
    removed = current['bytes']
    for path in current['_paths']:
        bounded(path, root)
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    return {'removed_bytes':removed, 'action':current['action'], 'job_ids':current['job_ids']}
