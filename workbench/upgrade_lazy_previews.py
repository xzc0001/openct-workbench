"""Repackage completed previews only; never rerun segmentation or touch CT/STL."""
from pathlib import Path
import hashlib
import json
import sys
import time
from preview_package import upgrade

ROOT=Path(__file__).resolve().parent


def run(sid):
    if len(sid)!=32 or any(c not in '0123456789abcdef' for c in sid):raise ValueError('Invalid study ID')
    job=ROOT/'jobs'/sid
    status=json.loads((job/'status.json').read_text(encoding='utf-8'))
    if status['state']!='completed':raise ValueError('Study is active')
    started=time.monotonic()
    protected=[job/'output/scene.json',job/'output/CT_original.nii.gz',job/'output/CT.nii.gz']
    before={str(path):[path.stat().st_size,path.stat().st_mtime_ns] for path in protected if path.is_file()}
    print('UPGRADE',sid,flush=True)
    receipt=upgrade(job/'output/preview.html',lambda message:print(sid[:8],message,flush=True))
    for path,stat in before.items():
        current=Path(path).stat()
        if [current.st_size,current.st_mtime_ns]!=stat:raise ValueError('Source data changed during preview upgrade')
    print(json.dumps({'study':sid,'seconds':round(time.monotonic()-started,2),**receipt},ensure_ascii=False),flush=True)


if __name__=='__main__':
    for sid in sys.argv[1:]:run(sid)
