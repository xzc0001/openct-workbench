"""Replace only user selected density regions, preserving every base mesh and CT."""
import hashlib
import json
import SimpleITK as sitk
import numpy as np
import vtk
from surface_geometry import build_surface, surface_arrays
from viewer_pack import pack, encode_mask, original_volume


def save(path, value):
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    temporary.replace(path)


def rebuild(job, status, progress=lambda message:None):
    output=job/'output'; review=json.loads((output/'candidate_review.json').read_text(encoding='utf-8'))
    with (output/'CT.nii.gz').open('rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=review['source_ct_sha256']:
            raise ValueError('候选来源 CT 校验不一致')
    rows={entry['id']:entry for entry in review['candidates']}
    selected=status.get('candidate_ids',[])
    if any(sid not in rows for sid in selected):raise ValueError('候选已变更，请重新选择')
    scene=json.loads((output/'scene.json').read_text(encoding='utf-8'))
    old={m['id'] for m in scene['models'] if m['group']=='reviewed_regions'}
    scene['models']=[m for m in scene['models'] if m['group']!='reviewed_regions']
    scene['nodules']=[r for r in scene.get('nodules',[]) if r.get('id') not in old and not r.get('user_selected')]
    volume=sitk.ReadImage(str(output/'CT.nii.gz')); masks=[]
    for model in scene['models']:
        mask=sitk.GetArrayFromImage(sitk.ReadImage(str(output/'masks'/model['mask_file'])))>0
        masks.append(encode_mask(model,mask))
    for sid in selected:
        entry=rows[sid]; image=sitk.ReadImage(str(output/'candidate_regions'/entry['mask_file']))
        image=sitk.Resample(image,volume,sitk.Transform(),sitk.sitkNearestNeighbor,0,sitk.sitkUInt8)
        mask=sitk.GetArrayFromImage(image)>0
        if not mask.any():raise ValueError('候选区域为空')
        poly,processing=build_surface(mask,volume,'reviewed_regions')
        model={'id':sid,'name':f'候选 {int(sid.rsplit("_",1)[1]):02d}','group':'reviewed_regions','color':[.85,.65,.97],'opacity':1.,**surface_arrays(poly),'voxels':int(mask.sum()),'mask_file':sid+'.nii.gz','review_status':'user_selected_density_region_unvalidated','surface_processing':processing}
        mask_path=output/'masks'/(sid+'.nii.gz');mask_path.unlink(missing_ok=True)
        sitk.WriteImage(image,str(mask_path),True)
        mesh_path=output/'models'/(sid+'.stl');mesh_path.unlink(missing_ok=True)
        writer=vtk.vtkSTLWriter();writer.SetFileName(str(mesh_path));writer.SetFileTypeToBinary();writer.SetInputData(poly)
        if not writer.Write():raise ValueError('候选 STL 写入失败')
        scene['models'].append(model);masks.append(encode_mask(model,mask));scene['nodules'].append({**entry,'user_selected':True})
    scene['review_candidates']=[entry for entry in review['candidates'] if entry['id'] not in selected]
    progress('更新当前检查的三维预览')
    native,original=original_volume(job,volume)
    instances=json.loads((output/'native_instances.json').read_text(encoding='utf-8')) if (output/'native_instances.json').exists() else []
    receipt=pack(output,volume,scene,masks,native,original,instances,progress_callback=progress)
    save(output/'scene.json',scene)
    manifest=json.loads((output/'manifest.json').read_text(encoding='utf-8'))
    manifest.update(receipt,surfaces=[{k:m[k] for k in ('id','group','vertices','triangles','voxels')} for m in scene['models']],candidate_ids=selected)
    save(output/'manifest.json',manifest)
    for sid in old-set(selected):
        (output/'masks'/(sid+'.nii.gz')).unlink(missing_ok=True)
        (output/'models'/(sid+'.stl')).unlink(missing_ok=True)
