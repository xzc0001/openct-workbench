"""One examination, one current preview, one recoverable previous revision."""
from pathlib import Path
import json
import os
import shutil

FILES = ('preview.html', 'scene.json', 'manifest.json', 'candidate_review.json', 'candidate_identity.json')
FOLDERS = ('masks', 'models', 'candidate_regions', 'online')
CONFIG_KEYS = ('mode', 'structures', 'margin_mm', 'candidate_ids', 'candidate_parameters', 'manual_points_lps')


def link_or_copy(source, target):
    source, target = Path(source), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def begin(job, status):
    output = job/'output'
    if not (output/'preview.html').is_file():
        raise ValueError('当前检查没有可更新的预览')
    revision = output/'revisions/study-previous'
    staging = output/'revisions/study-pending'
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for name in FILES:
        if (output/name).is_file():
            link_or_copy(output/name, staging/name)
    for name in FOLDERS:
        if (output/name).is_dir():
            shutil.copytree(output/name, staging/name, copy_function=link_or_copy)
    (staging/'configuration.json').write_text(json.dumps({k:status[k] for k in CONFIG_KEYS if k in status}), encoding='utf-8')
    if revision.exists():
        shutil.rmtree(revision)
    staging.replace(revision)


def restore(job):
    output = job/'output'; revision = output/'revisions/study-previous'
    if not (revision/'preview.html').is_file():
        raise ValueError('上一版预览不可恢复')
    for name in FILES:
        source, target = revision/name, output/name
        if source.exists():
            temporary = target.with_suffix(target.suffix+'.restore')
            if temporary.exists():temporary.unlink()
            link_or_copy(source, temporary)
            temporary.replace(target)
        elif target.exists():
            target.unlink()
    for name in FOLDERS:
        target = output/name
        if target.exists():shutil.rmtree(target)
        if (revision/name).exists():
            shutil.copytree(revision/name, target, copy_function=link_or_copy)
    return json.loads((revision/'configuration.json').read_text(encoding='utf-8'))
