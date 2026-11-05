"""Trusted, offline single-writer publication of already validated node data.

Called only in the resource-controlled worker. The HTTP process never imports
Torch/VTK. Event files are server-owned, revision-bound and outside node control.
"""
from pathlib import Path
import base64,gzip,hashlib,json,os,re,shutil

HEX=re.compile('[0-9a-f]{32}')
LABEL=re.compile('[a-zA-Z0-9_]{1,80}')
def read(p):return json.loads(p.read_text(encoding='utf-8'))
def save(p,value):
    from progressive import save as atomic
    atomic(p,value)

def copy_verified(artifact,destination):
    source=Path(artifact['path'])
    if not source.is_file() or source.is_symlink():raise ValueError('Validated artifact is unavailable')
    with source.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
    if digest!=artifact['sha256'] or source.stat().st_size!=artifact['bytes']:raise ValueError('Validated artifact changed before publication')
    destination.parent.mkdir(parents=True,exist_ok=True)
    tmp=destination.with_suffix(destination.suffix+'.cluster-pending')
    shutil.copyfile(source,tmp);os.replace(tmp,destination)

def prepare_candidate_inputs(job):
    """Keep only reconstruction candidate state, never DICOM or human notes."""
    import SimpleITK as sitk
    source=job/'output/candidate_review.json';folder=job/'cache';folder.mkdir(exist_ok=True)
    rows=[]
    if source.exists():
        review=read(source)
        identity=read(job/'output/candidate_identity.json') if (job/'output/candidate_identity.json').exists() else {}
        review['identity']=identity
        for entry in review.get('candidates',[]):
            if not re.fullmatch('suspect_[0-9]{3,6}',entry['id']):raise ValueError('Invalid prior candidate identity')
            filename=entry.pop('mask_file')
            if not re.fullmatch(r'(?:suspect_[0-9]{3,6}|[0-9a-f]{32})\.nii\.gz',filename):raise ValueError('Invalid prior candidate filename')
            path=job/'output/candidate_regions'/filename
            if path.is_symlink() or not path.resolve().is_relative_to(job.resolve()):raise ValueError('Invalid prior candidate path')
            role='candidate_mask.'+hashlib.sha256(('prior-candidate:'+entry['id']).encode()).hexdigest()[:32]
            entry['mask_role']=role;entry.pop('thumbnail_file',None)
            image=sitk.ReadImage(str(path))
            geometry={'dimensions_xyz':list(image.GetSize()),'spacing_xyz_mm':list(image.GetSpacing()),'origin_lps_mm':list(image.GetOrigin()),'direction':list(image.GetDirection())}
            rows.append({'role':role,'type':'nifti_mask','relative_path':path.relative_to(job).as_posix(),'geometry':geometry})
        payload=folder/'cluster-prior-review.json';save(payload,review)
        rows.append({'role':'candidate_review','type':'candidates_json','relative_path':payload.relative_to(job).as_posix()})
    save(folder/'cluster-prior-inputs.json',rows)

def apply_event(job,event_id):
    import worker
    from structures import inference_plan,resolve
    from progressive import publish,cache_export
    if not HEX.fullmatch(event_id):raise ValueError('Invalid event')
    status=read(job/'status.json');event=read(job/'cache/cluster-events'/(event_id+'.json'))
    if status.get('state')!='running' or status.get('cluster_revision')!=event['job_revision'] or event['job_id']!=job.name:
        raise ValueError('Publication revision is no longer current')
    output=job/'output';selected,_=resolve(status)
    keys={row[1] for row in inference_plan(selected,status['mode'])}
    for a in event['artifacts']:
        role=a['role'];kind=a['type']
        if role.startswith('prediction.') and kind=='nifti_mask':
            parts=role.split('.')
            if len(parts)!=3 or parts[1] not in keys or not LABEL.fullmatch(parts[2]):raise ValueError('Unexpected prediction role')
            copy_verified(a,output/'ai'/parts[1]/(parts[2]+'.nii.gz'))
        elif role.startswith('inference.') and kind=='inference_json':
            key=role[len('inference.'):]
            if key not in keys:raise ValueError('Unexpected inference role')
            record=read(Path(a['path']))
            receipt=read(output/'inference.json') if (output/'inference.json').exists() else {'device':'cpu','offline_guard':True,'tasks':[]}
            receipt['tasks']=[x for x in receipt['tasks'] if x['key']!=key]+record.get('tasks',[])
            save(output/'inference.json',receipt)
        elif role=='surface_export' and kind=='mesh_json':
            record=read(Path(a['path']))
            if set(record['selected'])!=set(event['structures']):raise ValueError('Surface selection mismatch')
            restore_surface_files(output,record)
            publish(output,record['models'],record['masks'],record['nodules'])
            cache_export(output,record['selected'],record['models'],record['masks'],record['nodules'],record['missing'])
        elif role.startswith('candidate_mask.') and kind=='nifti_mask':
            if not HEX.fullmatch(role[len('candidate_mask.'):]):raise ValueError('Invalid candidate mask role')
            copy_verified(a,output/'candidate_regions'/(a['id']+'.nii.gz'))
        elif role=='candidate_review' and kind=='candidates_json':
            # Domain candidate IDs stay stable. File names are assigned by the
            # publisher; remote file/path/thumbnail fields are never trusted.
            record=read(Path(a['path']))
            identity=record.pop('identity',None)
            if identity is not None:save(output/'candidate_identity.json',identity)
            masks={x['role']:x for x in event['artifacts'] if x['type']=='nifti_mask'}
            for row in record.get('candidates',[]):
                if not re.fullmatch('suspect_[0-9]{3,6}',row['id']):raise ValueError('Invalid candidate domain ID')
                ref=masks.get(row.pop('mask_role',None))
                if not ref:raise ValueError('Missing candidate mask')
                row['mask_file']=ref['id']+'.nii.gz'
                row['thumbnail_file']=row['id']+'.png'
            save(output/'candidate_review.json',record)
            candidate_thumbnails(output,record)
    # Final trusted template generation reuses cached exact node surfaces. It
    # does not rerun neural inference or replace original CT pixels/geometry.
    if event.get('job_complete'):
        import SimpleITK as sitk
        worker.JOB=job
        volume=sitk.ReadImage(str(output/'CT.nii.gz'))
        worker.export(volume,read(output/'geometry.json'),output,status['mode'])


def restore_surface_files(output,record):
    """Restore exact validated arrays for local selection/export compatibility."""
    import numpy as np
    import SimpleITK as sitk
    import vtk
    from vtk.util.numpy_support import numpy_to_vtk,numpy_to_vtkIdTypeArray
    from structures import LOBE_NAMES,SCAPULA_NAMES
    allowed={}
    for selected in record['selected']:
        mapping={'heart':{'heart':'heart'},'aorta':{'aorta':'aorta'},'lungs':{'lungs':'lungs'},'airways':{'airways':'airways'},'arteries':{'pulmonary_arteries':'arteries'},'veins':{'pulmonary_veins':'veins'},'nodules':{'lung_nodule_candidates':'nodules'},'ribs':{'ribcage':'ribcage'},'lobes':{name:'lobes' for name in LOBE_NAMES},'scapulae':{name:'scapulae' for name in SCAPULA_NAMES},'margin':{f'margin_{i}':'margin' for i in range(1,21)}}
        allowed.update(mapping.get(selected,{}))
    reference=sitk.ReadImage(str(output/'CT.nii.gz'))
    shape=tuple(reversed(reference.GetSize()));masks={m['id']:m for m in record['masks']}
    if len(masks)!=len(record['masks']) or set(masks)!={m['id'] for m in record['models']}:raise ValueError('Surface masks do not match models')
    (output/'masks').mkdir(exist_ok=True);(output/'models').mkdir(exist_ok=True)
    for model in record['models']:
        sid=model['id']
        if not LABEL.fullmatch(sid) or sid not in masks or allowed.get(sid)!=model['group']:raise ValueError('Invalid surface identity')
        model['review_status']='automatic_unreviewed'
        if model['group']=='margin':
            model['visible']=False;model['requires_target_confirmation']=True;model['target_id']='candidate_'+sid.split('_')[1]
        crop=masks[sid];count=math_prod(crop['size'])
        raw=gzip.decompress(base64.b64decode(crop['data'],validate=True))
        if len(raw)!=(count+7)//8:raise ValueError('Invalid surface bitset')
        values=np.unpackbits(np.frombuffer(raw,dtype=np.uint8),bitorder='little',count=count).reshape(tuple(reversed(crop['size'])))
        mask=np.zeros(shape,np.uint8);start=crop['start'][::-1];size=crop['size'][::-1]
        mask[tuple(slice(a,a+b) for a,b in zip(start,size))]=values
        image=sitk.GetImageFromArray(mask);image.CopyInformation(reference)
        model['mask_file']=sid+'.nii.gz';sitk.WriteImage(image,str(output/'masks'/model['mask_file']),True)
        def array(key,dtype):
            data=base64.b64decode(model[key],validate=True)
            if model.get('encoding')=='gzip':data=gzip.decompress(data)
            return np.frombuffer(data,dtype=dtype)
        points=array('positions','<f4').reshape(-1,3);indices=array('indices','<u4').astype(np.int64)
        poly=vtk.vtkPolyData();p=vtk.vtkPoints();p.SetData(numpy_to_vtk(points,deep=True));poly.SetPoints(p)
        cells=vtk.vtkCellArray();cells.SetData(numpy_to_vtkIdTypeArray(np.arange(0,len(indices)+1,3,dtype=np.int64),deep=True),numpy_to_vtkIdTypeArray(indices,deep=True));poly.SetPolys(cells)
        writer=vtk.vtkSTLWriter();writer.SetFileName(str(output/'models'/(sid+'.stl')));writer.SetFileTypeToBinary();writer.SetInputData(poly)
        if not writer.Write():raise ValueError('Surface STL could not be saved')

def math_prod(values):
    import math
    return math.prod(values)


def candidate_thumbnails(output,record):
    """Trusted display assets use the original candidate thumbnail renderer."""
    import math,numpy as np,SimpleITK as sitk
    from candidate_regions import render_thumbnail,PARAMETERS
    volume=sitk.ReadImage(str(output/'CT.nii.gz'));spacing=PARAMETERS['analysis_spacing_mm']
    size=[max(1,int(math.ceil((n-1)*s/spacing))+1) for n,s in zip(volume.GetSize(),volume.GetSpacing())]
    ref=sitk.Image(size,sitk.sitkFloat32);ref.SetSpacing([spacing]*3);ref.SetOrigin(volume.GetOrigin());ref.SetDirection(volume.GetDirection())
    image=sitk.Resample(volume,ref,sitk.Transform(),sitk.sitkLinear,-1024,sitk.sitkFloat32)
    array=sitk.GetArrayViewFromImage(image);folder=output/'candidate_regions';folder.mkdir(exist_ok=True)
    for row in record.get('candidates',[]):
        path=folder/(row['id']+'.png')
        if not path.exists():
            seed=np.rint(image.TransformPhysicalPointToContinuousIndex(tuple(row['center_lps_mm'])))[::-1].astype(int)
            render_thumbnail(array,seed,path)
