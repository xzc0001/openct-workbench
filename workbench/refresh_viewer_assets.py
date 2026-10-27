"""Refresh viewer assets, retaining native HU and reference geometry."""
from pathlib import Path
import sys,time
ROOT=Path(__file__).resolve().parent


def refresh(target, scene_override=None, rebuild_lods=True, backup=True):
    from preview_package import refresh_indexed,upgrade
    target=Path(target).resolve()
    if backup:
        from study_updates import link_or_copy
        revision=target.parent/'revisions'/('preview-'+time.strftime('%Y%m%d-%H%M%S')+'-before-viewer.html')
        if not revision.exists():link_or_copy(target,revision)
    if (target.parent/'online/current.json').is_file():
        return refresh_indexed(target,scene_override,rebuild_lods)
    return upgrade(target,scene_override=scene_override,rebuild_lods=rebuild_lods)


if __name__=='__main__':
    for arg in sys.argv[1:]:
        if arg not in {'--assets-only','--no-backup'}:
            print(refresh(arg,rebuild_lods='--assets-only' not in sys.argv,backup='--no-backup' not in sys.argv),flush=True)
