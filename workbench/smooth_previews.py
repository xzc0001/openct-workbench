"""Restore faithful reference surfaces from unchanged masks, with recoverable revisions."""
from pathlib import Path
import hashlib,json,shutil,sys,time
import SimpleITK as sitk
import vtk
from surface_geometry import build_surface,surface_arrays,SMOOTH_GROUPS,SURFACE_VERSION
from refresh_viewer_assets import refresh

ROOT=Path(__file__).resolve().parent
sha=lambda p:hashlib.file_digest(p.open('rb'),'sha256').hexdigest()

def upgrade(job):
    out=job/'output';scene_path=out/'scene.json';scene=json.loads(scene_path.read_text(encoding='utf-8'))
    targets=[m for m in scene['models'] if m['group'] in SMOOTH_GROUPS]
    if not targets:return
    if all(m.get('surface_processing',{}).get('version')==SURFACE_VERSION for m in targets):return
    revision=out/'revisions'/('surfaces-'+time.strftime('%Y%m%d-%H%M%S'))
    revision.mkdir(parents=True)
    for name in ['scene.json','manifest.json']:shutil.copy2(out/name,revision/name)
    (revision/'models').mkdir();stage=revision/'updated_models';stage.mkdir()
    hashes={}
    for m in targets:
        sid=m['id'];mask_path=out/'masks'/m.get('mask_file',sid+'.nii.gz');hashes[sid]=sha(mask_path)
        image=sitk.ReadImage(str(mask_path));previous=m.get('surface_processing',{});resolution=previous.get('source_resolution_mm',3.);poly,meta=build_surface(sitk.GetArrayFromImage(image)>0,image,m['group'],source_resolution_mm=resolution)
        if 'source_task' in previous:meta['source_task']=previous['source_task']
        m.update(surface_arrays(poly));m['surface_processing']=meta;m.pop('lod',None)
        path=out/'models'/(sid+'.stl')
        if path.exists():shutil.copy2(path,revision/'models'/path.name)
        writer=vtk.vtkSTLWriter();writer.SetFileName(str(stage/path.name));writer.SetInputData(poly);writer.SetFileTypeToBinary()
        if not writer.Write():raise ValueError('STL write failed')
        print(job.name,sid,m['triangles'],flush=True)
    # refresh validates/copies CT payload verbatim and backs up the previous HTML.
    receipt=refresh(out/'preview.html',scene_override=scene)
    for m in targets:
        sid=m['id'];assert hashes[sid]==sha(out/'masks'/m.get('mask_file',sid+'.nii.gz'))
        shutil.copy2(stage/(sid+'.stl'),out/'models'/(sid+'.stl'))
    temporary=scene_path.with_suffix('.json.tmp');temporary.write_text(json.dumps(scene,ensure_ascii=False),encoding='utf-8');temporary.replace(scene_path)
    manifest_path=out/'manifest.json';manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['surface_processing_version']=SURFACE_VERSION
    manifest['surfaces']=[{k:m[k] for k in ('id','group','vertices','triangles','voxels')} for m in scene['models']]
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    (revision/'upgrade_receipt.json').write_text(json.dumps({'source_mask_sha256':hashes,'surface_processing_version':SURFACE_VERSION,**receipt},indent=2),encoding='utf-8')
    print('UPGRADED',job.name,flush=True)

if __name__=='__main__':
    jobs=[ROOT/'jobs'/name for name in sys.argv[1:]] if sys.argv[1:] else sorted((ROOT/'jobs').iterdir())
    for job in jobs:
        status_path=job/'status.json'
        if status_path.exists() and json.loads(status_path.read_text(encoding='utf-8')).get('state')=='completed':upgrade(job)
