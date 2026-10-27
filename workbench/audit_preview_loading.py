"""Read-only full native-voxel and reference-buffer audit of viewer packaging."""
from pathlib import Path
import base64
import gzip
import hashlib
import json
import sys
import numpy as np
import SimpleITK as sitk
from preview_access import Preview

ROOT=Path(__file__).resolve().parent


def audit(sid):
    output=ROOT/'jobs'/sid/'output'
    pointer=json.loads((output/'online/current.json').read_text())
    preview=Preview(output/'online'/pointer['generation']);ct=preview.ct
    native=sitk.ReadImage(str(output/'CT_original.nii.gz'));reference=sitk.GetArrayViewFromImage(native)
    assert native.GetSize()==tuple(ct['size'])
    assert np.allclose(native.GetOrigin(),ct['origin']) and np.allclose(native.GetSpacing(),ct['spacing'])
    assert np.allclose(np.array(native.GetDirection()).reshape(3,3),ct['direction'])
    tx,ty,tz=ct['tile_size'];nx=(ct['size'][0]+tx-1)//tx;ny=(ct['size'][1]+ty-1)//ty
    digest=hashlib.sha256();voxels=0
    for z in range(0,ct['size'][2],tz):
        slab=np.empty((min(tz,ct['size'][2]-z),ct['size'][1],ct['size'][0]),dtype=preview.dtype)
        for y in range(ny):
            for x in range(nx):
                brick=preview.tile(x+nx*(y+ny*(z//tz)))
                slab[:,y*ty:y*ty+brick.shape[1],x*tx:x*tx+brick.shape[2]]=brick
        assert np.array_equal(slab,reference[z:z+len(slab)])
        digest.update(memoryview(slab).cast('B'));voxels+=slab.size
    assert digest.hexdigest()==ct['raw_sha256']
    source=json.loads((output/'scene.json').read_text(encoding='utf-8'))
    models={m['id']:m for m in preview.index['scene']['models']};checked=0
    for model in source['models']:
        for name in ('positions','indices','normals'):
            expected=base64.b64decode(model[name])
            if model.get('encoding')=='gzip':expected=gzip.decompress(expected)
            actual=gzip.decompress(preview.compressed(models[model['id']][name]['asset']))
            assert expected==actual
            checked+=len(actual)
    result={'study':sid,'native_voxels_compared':voxels,'native_hu_sha256':digest.hexdigest(),'reference_models':len(models),'reference_buffer_bytes_compared':checked,'online_html_bytes':(preview.directory/'index.html').stat().st_size,'offline_html_bytes':(output/'preview.html').stat().st_size,'shared_html_inode':(output/'preview.html').samefile(preview.directory/'source.html')}
    print(json.dumps(result),flush=True)
    return result


if __name__=='__main__':
    for sid in sys.argv[1:]:audit(sid)
