"""Audit label/surface/CT consistency. Never certifies anatomy or clinical accuracy."""
from pathlib import Path
import base64,gzip,hashlib,json,struct,sys
import numpy as np
import SimpleITK as sitk
from surface_geometry import audit_label_boundary

ROOT=Path(__file__).resolve().parent
sha=lambda p:hashlib.file_digest(p.open('rb'),'sha256').hexdigest()

def audit(job):
    out=job/'output';status=json.loads((job/'status.json').read_text(encoding='utf-8'))
    if status['state']!='completed':raise ValueError('Wait for completed reconstruction')
    scene=json.loads((out/'scene.json').read_text(encoding='utf-8'))
    manifest=json.loads((out/'manifest.json').read_text(encoding='utf-8'))
    assert sha(out/'preview.html')==manifest['preview_sha256']
    ct=sitk.ReadImage(str(out/'CT.nii.gz'));models=[]
    for m in scene['models']:
        processing=m['surface_processing']
        assert processing['version']>=5 and not processing['geometry_smoothing'] and not processing['full_mesh_simplified']
        path=out/'masks'/m.get('mask_file',m['id']+'.nii.gz')
        image=sitk.ReadImage(str(path));assert image.GetSize()==ct.GetSize()
        assert np.allclose(image.GetOrigin(),ct.GetOrigin()) and np.allclose(image.GetSpacing(),ct.GetSpacing()) and np.allclose(image.GetDirection(),ct.GetDirection())
        raw=base64.b64decode(m['positions']);raw=gzip.decompress(raw) if m.get('encoding')=='gzip' else raw
        points=np.frombuffer(raw,dtype='<f4').reshape(-1,3)
        if processing.get('method')=='native_ct_hu_isosurface':
            from ribcage_geometry import audit_ct_surface
            checks=audit_ct_surface(points,sitk.ReadImage(str(out/'CT_original.nii.gz')),processing['threshold_hu'])
            assert not processing['artificial_joint_bridges'] and not processing['source_ct_modified']
        else:checks=audit_label_boundary(points,sitk.GetArrayFromImage(image)>0,image)
        with (out/'models'/(m['id']+'.stl')).open('rb') as stl:
            stl.seek(80);assert struct.unpack('<I',stl.read(4))[0]==m['triangles']
        models.append({'id':m['id'],'group':m['group'],'triangles':m['triangles'],'mask_sha256':sha(path),**checks})
        print(m['id'],checks['checked_vertices'],'vertices checked',flush=True)
    native_hash=sha(out/'CT_original.nii.gz')
    if status.get('source_job'):assert native_hash==sha(ROOT/'jobs'/status['source_job']/'output/CT_original.nii.gz')
    report={'job':job.name,'scope':'source_pixel_and_reference_surface_geometry_only','anatomical_accuracy_verified':False,'clinical_validation':False,'native_ct_sha256':native_hash,'preview_sha256':manifest['preview_sha256'],'models':models,'reference_vertices_checked':sum(m['checked_vertices'] for m in models)}
    path=out/'geometry_audit.json';path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('PASS',len(models),'models;',report['reference_vertices_checked'],'reference vertices; native CT unchanged. Anatomy remains unreviewed.',flush=True)
    return report

if __name__=='__main__':
    for name in sys.argv[1:]:audit(ROOT/'jobs'/name)
