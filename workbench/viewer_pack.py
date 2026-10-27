"""Pack raw source CT, geometry and cropped segmentation bitsets for offline MPR."""
from pathlib import Path
import base64
import gzip
import hashlib
import json
import numpy as np
import SimpleITK as sitk

ROOT = Path(__file__).resolve().parent


def zipped(array):
    # gzip accepts the buffer directly; avoid a full-volume bytes duplicate.
    return base64.b64encode(gzip.compress(memoryview(np.ascontiguousarray(array)).cast('B'), compresslevel=3, mtime=0)).decode("ascii")


def original_volume(job, canonical):
    path = job / "output" / "CT_original.nii.gz"
    if path.exists():
        return sitk.ReadImage(str(path)), True
    inventory = job / "inventory.json"
    status = job / "status.json"
    if inventory.exists() and status.exists():
        current = json.loads(status.read_text(encoding="utf-8"))
        selected = next(s for s in json.loads(inventory.read_text(encoding="utf-8"))["series"] if s["id"] == current["series_id"])
        reader = sitk.ImageSeriesReader(); reader.SetFileNames(selected["files"])
        image = reader.Execute()
        sitk.WriteImage(image, str(path), True)
        return image, True
    return canonical, False


def encode_mask(item, mask):
    ranges = [np.flatnonzero(mask.any(axis=axes)) for axes in ((1, 2), (0, 2), (0, 1))]
    if any(not len(indices) for indices in ranges):
        return None
    lo = [int(indices[0]) for indices in ranges]
    hi = [int(indices[-1])+1 for indices in ranges]
    crop = np.ascontiguousarray(mask[tuple(slice(a, b) for a, b in zip(lo, hi))])
    return {"id": item["id"], "start": lo[::-1], "size": list(crop.shape[::-1]), "data": zipped(np.packbits(crop.reshape(-1), bitorder="little"))}


def pack(output, canonical, scene, model_masks, native=None, native_is_original=False, instance_numbers=None, progress_callback=None, online_base=None):
    from preview_package import write_preview, TILE
    notify = progress_callback or (lambda message:None)
    notify('压缩原始 CT 影像')
    native = native if native is not None else canonical
    data = sitk.GetArrayViewFromImage(native)
    integer_hu = np.issubdtype(data.dtype, np.integer) and data.min() >= -32768 and data.max() <= 32767
    target_dtype = '<i2' if integer_hu else ('<i4' if np.issubdtype(data.dtype,np.integer) and data.dtype.itemsize<=4 else ('<f4' if data.dtype==np.float32 else '<f8'))
    direction=np.array(native.GetDirection()).reshape(3,3);matrix=direction@np.diag(native.GetSpacing())
    corners=np.array([native.TransformIndexToPhysicalPoint((x,y,z)) for x in (0,native.GetWidth()-1) for y in (0,native.GetHeight()-1) for z in (0,native.GetDepth()-1)])
    ct={'dtype':{'<i2':'int16','<i4':'int32','<f4':'float32','<f8':'float64'}[target_dtype], 'size':list(native.GetSize()),'spacing':list(native.GetSpacing()),'origin':list(native.GetOrigin()),'direction':direction.tolist(),'matrix':matrix.tolist(),'inverse':np.linalg.inv(matrix).tolist(),'bounds':[corners.min(axis=0).tolist(),corners.max(axis=0).tolist()],'original':native_is_original,'instances':instance_numbers or [],'masks':[m for m in model_masks if m is not None],'maskGeometry':{'origin':list(canonical.GetOrigin()),'spacing':list(canonical.GetSpacing()),'size':list(canonical.GetSize())}}
    if scene['models']:
        from mesh_lod import display_lods
        scene=display_lods(scene,progress_callback=notify)
    from volume_rois import context_rois
    notify('生成容积显示范围')
    rois=context_rois(scene,ct)
    slabs=(data[z:z+TILE[2]].astype(target_dtype,copy=False) for z in range(0,len(data),TILE[2]))
    return write_preview(output,scene,ct,rois,slabs,notify,online_base=online_base)
