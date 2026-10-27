"""Offline generic CT pipeline. No case-specific coordinates or report matching."""
from pathlib import Path, PurePosixPath
import base64
import gc
import hashlib
import io
import itertools
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import zipfile

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
JOB = None


def save(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(.05)


def progress(stage, percent, message, **extra):
    if stage!='failed' and os.environ.get('WORKBENCH_PHASE_PERCENT'):
        percent=int(os.environ['WORKBENCH_PHASE_PERCENT'])
    log = JOB / 'worker.log'
    save(JOB / "progress.json", {"stage": stage, "percent": percent, "message": message, "updated_at": time.time(), "log_start_bytes":log.stat().st_size if log.exists() else 0, **extra})


def scan():
    import pydicom
    from pydicom.errors import InvalidDicomError
    import numpy as np
    status = json.loads((JOB / "status.json").read_text(encoding="utf-8"))
    progress("inventory", 2, "正在本地检查 DICOM 序列")
    if status["kind"] == "zip":
        from dicom_ingest import extract
        stats=extract(JOB/'upload.zip',JOB/'input',lambda i,n:progress('inventory',2,'校验 DICOM '+str(i)+' / '+str(n)))
        save(JOB/'upload-validation.json',stats)
    # Defense in depth: do not let a stale/unvalidated input reach the CT reader.
    from dicom_ingest import validate, Rejected
    for path in (JOB/'input').iterdir():
        try:validate(path)
        except Rejected:path.unlink()
    groups = {}
    tags = ["Modality", "StudyInstanceUID", "SeriesInstanceUID", "FrameOfReferenceUID", "SOPInstanceUID", "SeriesNumber", "Rows", "Columns", "NumberOfFrames", "ImageType", "ImagePositionPatient", "ImageOrientationPatient", "PixelSpacing", "SliceThickness", "ConvolutionKernel"]
    skipped = 0
    for path in sorted((JOB / "input").iterdir()):
        try:
            header = pydicom.dcmread(path, stop_before_pixels=True, specific_tags=tags)
        except (InvalidDicomError, OSError, ValueError):
            skipped += 1
            continue
        if str(getattr(header, "Modality", "")) != "CT" or int(getattr(header, "NumberOfFrames", 1)) != 1 or any(x in str(getattr(header, "ImageType", "")) for x in ("LOCALIZER", "SCOUT")):
            skipped += 1
            continue
        if any(not hasattr(header, tag) for tag in ("StudyInstanceUID", "SeriesInstanceUID", "FrameOfReferenceUID", "SOPInstanceUID", "ImagePositionPatient", "ImageOrientationPatient", "PixelSpacing", "Rows", "Columns")):
            skipped += 1
            continue
        identity = "|".join(str(getattr(header, k)) for k in ("StudyInstanceUID", "SeriesInstanceUID", "FrameOfReferenceUID"))
        key = hashlib.sha256(identity.encode()).hexdigest()[:24]
        group = groups.setdefault(key, {"id": key, "number": str(getattr(header, "SeriesNumber", "?"))[:20], "rows": int(header.Rows), "columns": int(header.Columns), "files": [], "positions": [], "uids": {}, "orientation": list(map(float, header.ImageOrientationPatient)), "pixel_spacing": list(map(float, header.PixelSpacing)), "thickness_mm": float(getattr(header, "SliceThickness", 0) or 0)})
        uid = str(header.SOPInstanceUID)
        if uid in group["uids"]:
            if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(Path(group["uids"][uid]).read_bytes()).digest():
                raise ValueError("发现内容不同但 SOPInstanceUID 相同的 CT 文件，请检查导出数据")
            continue
        if [group["rows"], group["columns"]] != [int(header.Rows), int(header.Columns)] or not np.allclose(group["orientation"], header.ImageOrientationPatient, atol=1e-4) or not np.allclose(group["pixel_spacing"], header.PixelSpacing, atol=1e-5):
            raise ValueError("同一序列内尺寸、方向或像素间距不一致，请重新导出单一 CT 序列")
        group["uids"][uid] = str(path)
        group["files"].append(str(path))
        group["positions"].append(list(map(float, header.ImagePositionPatient)))
    series = []
    for group in groups.values():
        if len(group["files"]) < 16:
            continue
        orientation = np.array(group["orientation"])
        normal = np.cross(orientation[:3], orientation[3:])
        if not np.allclose([np.linalg.norm(orientation[:3]), np.linalg.norm(orientation[3:]), np.dot(orientation[:3], orientation[3:])], [1, 1, 0], atol=1e-3):
            raise ValueError("CT 方向余弦不合法")
        projections = np.array(group["positions"]) @ normal
        order = np.argsort(projections)
        steps = np.diff(projections[order])
        median = float(np.median(steps))
        valid = median > .05 and np.allclose(steps, median, atol=max(.02, median * .02), rtol=0)
        displacement = np.diff(np.array(group["positions"])[order], axis=0)
        valid = valid and np.allclose(displacement, steps[:, None] * normal, atol=.05, rtol=0)
        group["files"] = [group["files"][int(i)] for i in order]
        group.update(count=len(order), spacing_mm=round(median, 5), eligible=bool(valid), reason="" if valid else "层位置重复、间距不连续或存在层间横向漂移，不能直接重建")
        for field in ("uids", "positions", "orientation"):
            group.pop(field)
        series.append(group)
    if not series:
        raise ValueError("没有找到可组成体积的常规 CT 序列（至少 16 层）；暂不支持增强多帧 DICOM")
    series.sort(key=lambda s: (not s["eligible"], -s["count"], s["id"]))
    save(JOB / "inventory.json", {"series": series, "skipped_non_volume_files": skipped})
    progress("ready", 5, f"找到 {len(series)} 个 CT 序列，请确认要处理的序列和模式")


def prepare(series, output):
    import numpy as np
    import SimpleITK as sitk
    if not series["eligible"]:
        raise ValueError(series["reason"])
    progress("volume", 8, "读取像素并检查空间几何，生成统一 LPS 坐标体积")
    reader = sitk.ImageSeriesReader()
    reader.SetFileNames(series["files"])
    image = reader.Execute()
    sitk.WriteImage(image, str(output / "CT_original.nii.gz"), True)
    original = {"dimensions_xyz": list(image.GetSize()), "spacing_xyz_mm": list(image.GetSpacing()), "origin_lps_mm": list(image.GetOrigin()), "direction": list(image.GetDirection())}
    image = sitk.DICOMOrient(image, "LPS")
    corners = np.array([image.TransformIndexToPhysicalPoint(tuple(map(int, index))) for index in itertools.product(*[(0, n-1) for n in image.GetSize()])])
    lower, upper = corners.min(axis=0), corners.max(axis=0)
    fov = float(max((upper-lower)[:2]))
    if not 80 < fov < 1000:
        raise ValueError("CT 视野大小不适用于本试用版")
    dz = float(image.GetSpacing()[2])
    count = int(np.ceil((upper[2]-lower[2]) / dz)) + 1
    if not 16 <= count <= 1600:
        raise ValueError("重采样层数超出本机试用范围（16–1600 层）")
    origin = [float((lower[i]+upper[i]-fov)/2) for i in (0, 1)] + [float(lower[2])]
    spacing = [fov/511, fov/511, dz]
    canonical = sitk.Resample(image, [512, 512, count], sitk.Transform(), sitk.sitkLinear, origin, spacing, list(np.eye(3).ravel()), -1024, sitk.sitkInt16)
    sitk.WriteImage(canonical, str(output / "CT.nii.gz"), True)
    geometry = {"dimensions_xyz": [512, 512, count], "spacing_xyz_mm": spacing, "origin_lps_mm": origin, "direction": list(np.eye(3).ravel()), "slice_count": count, "original_geometry": original, "resampled_for_preview": True}
    save(output / "geometry.json", geometry)
    return canonical, geometry


def infer(output, mode, selected_override=None, task_key=None):
    import torch
    import socket
    if not getattr(socket, "RECONSTRUCTION_OFFLINE", False):
        raise RuntimeError("离线网络保护未启用，请使用 Start-LocalReconstruction.ps1 启动")
    weights = (ROOT / CONFIG["weights"]).resolve()
    settings = JOB / "model_config"
    settings.mkdir(exist_ok=True)
    save(settings / "config.json", {"totalseg_id": "local_offline", "send_usage_stats": False, "prediction_counter": 0, "statistics_disclaimer_shown": True})
    os.environ["TOTALSEG_HOME_DIR"] = str(settings)
    os.environ["TOTALSEG_WEIGHTS_PATH"] = str(weights)
    torch.set_num_threads(CONFIG["threads"])
    torch.set_num_interop_threads(1)
    from bounded_inference import install
    with (output/'CT.nii.gz').open('rb') as source:
        ct_hash = hashlib.file_digest(source, 'sha256').hexdigest()
    api = install(CONFIG['threads'], output/'ai/localizer', ct_hash, weights)
    from totalsegmentator.map_tasks_config import TASK_CONFIGS, TASK_ID_WEIGHTS_CONFIGS
    def cached_only(task_id):
        info = TASK_ID_WEIGHTS_CONFIGS[task_id]
        folder = weights / info.get("rel_path", "") / info["foldername"]
        if not folder.is_dir() or not any(folder.rglob("checkpoint_final.pth")):
            raise ValueError(f"离线模型 {task_id} 不完整；本试用版不会联网下载")
    api.download_pretrained_weights = cached_only
    TASK_CONFIGS["total"]["sub_modes"]["default"]["task_id"] = [293]
    from structures import resolve, inference_plan
    options = json.loads((JOB / "status.json").read_text(encoding="utf-8"))
    selected, margin = resolve(options)
    if selected_override is not None:selected=selected_override
    tasks = inference_plan(selected, mode)
    if task_key is not None:
        tasks=[task for task in tasks if task[1]==task_key]
        if not tasks:raise ValueError('未知分割阶段')
    record_file=output/'inference.json'
    records = json.loads(record_file.read_text(encoding='utf-8')).get('tasks',[]) if task_key and record_file.exists() else []
    if task_key:records=[record for record in records if record['key']!=task_key]
    for task, key, arguments, name, percent in tasks:
        progress("segmentation", percent, name + "：本地 CPU 正在计算，此步骤可能持续数分钟")
        required = arguments.get("roi_subset") or {"lung_vessels": ["lung_arteries", "lung_veins", "lung_airways"], "lung_nodules": ["lung_nodules"]}[key]
        folder = output/"ai"/key
        cache_file = folder/"prediction_receipt.json"
        signature = {"input_sha256":ct_hash,"task":task,"arguments":arguments}
        sampling_mm = 3.0 if arguments.get("fast") else TASK_CONFIGS[task].get("resample", 1.5)
        cached = all((folder/(name+".nii.gz")).is_file() for name in required)
        if cached and cache_file.exists():
            cached = json.loads(cache_file.read_text(encoding="utf-8")) == signature
        elif cached:
            cached = bool(options.get("source_job")) and all((folder/(name+".nii.gz")).stat().st_mtime >= (output/"CT.nii.gz").stat().st_mtime for name in required)
        if cached:
            import SimpleITK as sitk
            reference = sitk.ReadImage(str(output/"CT.nii.gz"))
            for name in required:
                image = sitk.ReadImage(str(folder/(name+".nii.gz")))
                if image.GetSize()!=reference.GetSize() or image.GetOrigin()!=reference.GetOrigin() or image.GetDirection()!=reference.GetDirection() or image.GetSpacing()!=reference.GetSpacing():
                    cached = False
                    break
            del image, reference
        if cached:
            save(cache_file,signature)
            records.append({"task":task,"key":key,"seconds":0,"cache":"same-input-sha256-and-geometry","resolution_mm":sampling_mm})
            continue
        begin = time.monotonic()
        if task == "total" and not arguments.get("fast"):
            TASK_CONFIGS["total"]["sub_modes"]["default"]["task_id"] = [{"lung_highres": 291, "trachea_highres": 291, "ribs_highres": 295, "spine_highres": 292, "scapulae_highres": 294}.get(key, 293)]
        api.totalsegmentator(input=output / "CT.nii.gz", output=output / "ai" / key, task=task, device="cpu", nr_thr_resamp=1, nr_thr_saving=1, quiet=False, **arguments)
        save(cache_file,signature)
        records.append({"task": task, "key": key, "seconds": round(time.monotonic()-begin, 2), "resolution_mm": sampling_mm})
        gc.collect()
    save(output / "inference.json", {"device": "cpu", "offline_guard": True, "tasks": records})


def export(volume, geometry, output, mode, selected_override=None, partial=False):
    import numpy as np
    import SimpleITK as sitk
    import vtk
    from vtk.util.numpy_support import numpy_to_vtk, vtk_to_numpy
    from scipy import ndimage as ndi
    from viewer_pack import pack, encode_mask, original_volume
    from structures import resolve, LOBE_NAMES, RIB_NAMES, SPINE_NAMES, SCAPULA_NAMES
    import importlib.metadata
    progress("surfaces", 80, "提取未经平滑和简化的原分割表面")
    masks = output / "masks"
    meshes = output / "models"
    masks.mkdir(exist_ok=True)
    meshes.mkdir(exist_ok=True)
    shape = tuple(reversed(volume.GetSize()))
    def load(task, name):
        image = sitk.ReadImage(str(output / "ai" / task / (name+".nii.gz")))
        image = sitk.Resample(image, volume, sitk.Transform(), sitk.sitkNearestNeighbor, 0, sitk.sitkUInt8)
        return sitk.GetArrayFromImage(image) > 0
    selected, margin_mm = resolve(json.loads((JOB / "status.json").read_text(encoding="utf-8")))
    if selected_override is not None:selected=selected_override
    fine_lung = mode == "full" and all((output/"ai/lung_highres"/(name+".nii.gz")).exists() for name in LOBE_NAMES)
    fine_ribs = mode == "full" and all((output/"ai/ribs_highres"/(name+".nii.gz")).exists() for name in RIB_NAMES)
    surfaces = []
    missing_structures = []
    packed_masks = []
    candidates = []
    requested=list(selected)
    if not partial:
        from progressive import cached_export
        cached=cached_export(output)
        selected=[s for s in selected if s not in cached['selected']]
        surfaces.extend(cached['models']);packed_masks.extend(cached['masks']);candidates.extend(cached['nodules']);missing_structures.extend(cached['missing'])
    def add(sid, title, group, mask, color, opacity, label, surface=None):
        progress('surfaces', 80, '提取'+title+'表面', structure=sid, structure_index=len(surfaces)+1)
        count = int(mask.sum())
        if not count:
            missing_structures.append(title)
            if group in {"lungs", "heart", "aorta"}:
                raise ValueError("必要结构未分割成功，请确认上传的是胸部 CT")
            return
        image = sitk.GetImageFromArray(mask.astype(np.uint8)); image.CopyInformation(volume)
        (masks/(sid+'.nii.gz')).unlink(missing_ok=True)
        sitk.WriteImage(image, str(masks / (sid+".nii.gz")), True)
        from surface_geometry import build_surface, surface_arrays
        resolution = 1.5 if (fine_lung and group in {"lungs", "lobes"}) or (fine_ribs and group == "ribs") or (mode == "full" and group in {"spine", "scapulae"}) else 3.0
        poly, smoothing = surface if surface is not None else build_surface(mask, volume, group, source_resolution_mm=resolution)
        if group in {"spine", "scapulae"}:
            smoothing["source_task"] = ("spine_highres" if group == "spine" else "scapulae_highres") if mode == "full" else "total"
        elif group in {"lungs", "lobes", "ribs"}:
            smoothing["source_task"] = ("ribs_highres" if fine_ribs else "total") if group == "ribs" else ("lung_highres" if fine_lung else "total")
        elif group != "ribcage":
            smoothing.pop("source_resolution_mm", None)
        arrays = surface_arrays(poly)
        (meshes/(sid+'.stl')).unlink(missing_ok=True)
        writer = vtk.vtkSTLWriter(); writer.SetFileName(str(meshes / (sid+".stl"))); writer.SetFileTypeToBinary(); writer.SetInputData(poly)
        if not writer.Write():
            raise ValueError("STL 写入失败")
        surfaces.append({"id": sid, "name": title, "group": group, "color": color, "opacity": opacity, **arrays, "voxels": count, "mask_file": sid+".nii.gz", "review_status": "automatic_unreviewed", "surface_processing": smoothing})
        packed_masks.append(encode_mask(surfaces[-1], mask))
        from progressive import publish
        publish(output,[surfaces[-1]],[packed_masks[-1]])
    if "lungs" in selected:
        lung = np.zeros(shape, bool)
        for name in LOBE_NAMES:
            lung |= load("lung_highres" if fine_lung else "total", name)
        add("lungs", "双肺轮廓", "lungs", lung, [.65,.78,.82], .055, 6)
        del lung
    if "lobes" in selected:
        titles = ["左上肺叶", "左下肺叶", "右上肺叶", "右中肺叶", "右下肺叶"]
        colors = [[.85,.6,.24],[.32,.75,.63],[.38,.63,.9],[.9,.47,.46],[.72,.52,.85]]
        for i, name in enumerate(LOBE_NAMES):
            add(name, titles[i], "lobes", load("lung_highres" if fine_lung else "total", name), colors[i], .2, 10+i)
    if "ribs" in selected:
        localizer = np.zeros(shape, bool)
        for name in RIB_NAMES:
            localizer |= load("ribs_highres" if fine_ribs else "total", name)
        for name in SPINE_NAMES:
            localizer |= load("spine_highres" if mode == "full" else "total", name)
        shoulder = np.zeros(shape, bool)
        for name in SCAPULA_NAMES:
            shoulder |= load("scapulae_highres" if mode == "full" else "total", name)
        native_bone, _ = original_volume(JOB, volume)
        from ribcage_geometry import build_ribcage
        progress("surfaces", 82, "从原始 CT 重新提取肋骨与脊柱整体骨表面，不拼接独立标签表面")
        mask, poly, processing = build_ribcage(native_bone, volume, localizer, shoulder)
        processing['source_task'] = 'native_ct_density_with_ai_localizers'
        save(output/'ribcage_reconstruction.json',processing)
        reference=sitk.GetImageFromArray(localizer.astype(np.uint8));reference.CopyInformation(volume)
        sitk.WriteImage(reference,str(output/'ribcage_ai_localizer.nii.gz'),True)
        add("ribcage", "骨性胸廓（肋骨与脊柱整体）", "ribcage", mask, [.9,.87,.73], 1., 48, surface=(poly,processing))
        del localizer, shoulder, native_bone, reference, mask, poly
    if "scapulae" in selected:
        for side, name in zip(["左", "右"], SCAPULA_NAMES):
            add(name, side+"肩胛骨", "scapulae", load("scapulae_highres" if mode == "full" else "total", name), [.87,.84,.69], 1., 49)
    if "heart" in selected:
        add("heart", "心脏", "heart", load("heart_highres" if mode == "full" else "total", "heart"), [.91,.43,.40], .5, 1)
    if "aorta" in selected:
        add("aorta", "主动脉（自动标签）", "aorta", load("aorta_highres" if mode == "full" else "total", "aorta"), [.98,.61,.25], 1., 7)
    if "airways" in selected:
        trachea = load("trachea_highres" if mode == "full" and (output/"ai/trachea_highres/trachea.nii.gz").exists() else "total", "trachea")
        bronchi = np.zeros(shape, bool)
        if (output/"ai/lung_vessels/lung_airways.nii.gz").exists():
            bronchi = load("lung_vessels", "lung_airways")
        from airway_cleanup import connected_airways
        airway, raw_airway, cleanup = connected_airways(trachea, bronchi)
        raw_image = sitk.GetImageFromArray(raw_airway.astype(np.uint8)); raw_image.CopyInformation(volume)
        sitk.WriteImage(raw_image, str(output/"airways_raw.nii.gz"), True)
        save(output/"airway_cleanup.json", cleanup)
        add("airways", "气管与支气管（主气道连通树）", "airways", airway, [189/255,238/255,99/255], 1., 2)
        surfaces[-1]["component_selection"] = cleanup
        del airway, raw_airway, raw_image, trachea, bronchi
    if "arteries" in selected:
        add("pulmonary_arteries", "肺动脉（自动标签）", "arteries", load("lung_vessels", "lung_arteries"), [.25,.61,.97], 1., 3)
    if "veins" in selected:
        add("pulmonary_veins", "肺静脉（自动标签）", "veins", load("lung_vessels", "lung_veins"), [.97,.34,.46], 1., 4)
    if "nodules" in selected:
        mask = load("lung_nodules", "lung_nodules")
        components, number = ndi.label(mask)
        objects = ndi.find_objects(components)
        entries = []
        for label, region in enumerate(objects, 1):
            if region is None:
                continue
            local = components[region] == label
            count = int(local.sum())
            if count < 4:
                continue
            center = np.array(ndi.center_of_mass(local)) + [s.start for s in region]
            xyz = center[::-1]
            ras = np.array(volume.TransformContinuousIndexToPhysicalPoint(tuple(xyz))) * [-1,-1,1]
            entries.append({"component": label, "voxels": count, "center_ijk": xyz.tolist(), "center_ras_mm": ras.tolist(), "slice_k_0based": int(round(center[0])), "bbox_mm": (np.array(local.shape[::-1])*volume.GetSpacing()).tolist(), "volume_mm3": float(count*np.prod(volume.GetSpacing()))})
        entries.sort(key=lambda entry: entry["voxels"], reverse=True)
        for i, entry in enumerate(entries[:20], 1):
            candidates.append({"id": f"candidate_{i}", "name": f"肺结节候选 {i}", **entry})
        save(output / "candidates.json", {"total_components_at_least_4_voxels": len(entries), "listed_largest_candidates": candidates, "policy": "all predicted nodule voxels displayed; largest 20 components listed; no report matching or diagnosis"})
        add("lung_nodule_candidates", "肺结节候选（未确认）", "nodules", mask, [.29,.93,.67], 1., 5)
        if "margin" in selected:
            for i, entry in enumerate(entries[:20], 1):
                component_id = entry["component"]
                region = objects[component_id-1]
                padding = np.ceil(margin_mm / np.array(volume.GetSpacing())[::-1]).astype(int)+2
                raw_lo = np.array([sl.start for sl in region])-padding
                raw_hi = np.array([sl.stop for sl in region])+padding
                lo = np.maximum(0, raw_lo); hi = np.minimum(shape, raw_hi)
                block = tuple(slice(int(a),int(b)) for a,b in zip(lo,hi))
                component = components[block] == component_id
                from margin_geometry import geometric_shell
                expansion = np.zeros(shape, bool)
                expansion[block] = geometric_shell(component, volume.GetSpacing()[::-1], margin_mm)
                if not expansion.any():
                    raise ValueError("所选外扩距离小于当前体素可表示范围，请核对距离与源层厚")
                add(f"margin_{i}", f"候选 {i} 外扩 {margin_mm:g} mm（几何参考）", "margin", expansion, [.85,.45,.91], .3, 60+i)
                surfaces[-1].update(target_id=f"candidate_{i}", margin_mm=margin_mm, visible=False, requires_target_confirmation=True, truncated_by_scan=bool(np.any(raw_lo<0) or np.any(raw_hi>shape)))
                candidates[i-1]["margin_model_id"] = f"margin_{i}"
        save(output / "candidates.json", {"total_components_at_least_4_voxels": len(entries), "listed_largest_candidates": candidates, "policy": "automatic candidates; manual confirmation required; geometric expansion is not surgical safety"})
        del mask, components

    from progressive import publish
    publish(output,surfaces,packed_masks,candidates)
    if partial:
        from progressive import cache_export
        cache_export(output,requested,surfaces,packed_masks,candidates,missing_structures)
        return
    selected=requested
    options = json.loads((JOB/"status.json").read_text(encoding="utf-8"))
    review = None
    review_file = output/"candidate_review.json"
    if "suspects" in selected and not review_file.exists():
        progress("candidate_review", 86, "筛选肺内低密度疑似区域；结果仅供人工核对")
        from candidate_regions import screen
        review = screen(output)
    elif review_file.exists():
        review = json.loads(review_file.read_text(encoding="utf-8"))
    if options.get("candidate_ids"):
        if not review:
            raise ValueError("缺少待核对候选清单")
        with (output/"CT.nii.gz").open("rb") as source:
            if hashlib.file_digest(source,"sha256").hexdigest() != review["source_ct_sha256"]:
                raise ValueError("候选来源 CT 校验不一致")
        rows = {entry["id"]:entry for entry in review["candidates"]}
        for index,sid in enumerate(options["candidate_ids"],1):
            entry = rows[sid]
            image = sitk.ReadImage(str(output/"candidate_regions"/entry["mask_file"]))
            image = sitk.Resample(image,volume,sitk.Transform(),sitk.sitkNearestNeighbor,0,sitk.sitkUInt8)
            add(sid,entry["name"]+"（用户选择，未诊断）","reviewed_regions",sitk.GetArrayFromImage(image)>0,[.85,.65,.97],1.,80+index)
            surfaces[-1]["review_status"]="user_selected_density_region_unvalidated"
            candidates.append({**entry,"user_selected":True})

    progress("preview", 90, "打包原始轴位像素、三平面重建与三维模型")
    method = "按所选结构自动重建。骨性胸廓采用原始 CT 150 HU 等值面，肋骨与脊柱统一处理；AI 仅用于定位和区分其他骨骼。其他结构参考自动标签，快速模式基础结构使用 3 mm，精细模式使用 1.5 mm 模型。完整表面不做几何平滑或简化；自动选择不证明符合真实解剖，边界需逐层结合原始 CT 复核。几何外扩不表示手术安全，无骨折专用模型时不生成骨折结论。"
    method += f" 本次肺标签来源：{'1.5 mm' if fine_lung else '3 mm'}；胸廓定位标签来源：{'1.5 mm' if fine_ribs else '3 mm'}，胸廓几何使用原始 CT 分辨率（仅适用于所选结构）。"
    if "ribs" in selected:method += " 肋骨与脊柱从原始 CT 骨密度等值面统一提取为一个整体，AI 仅作定位；不拼接独立标签表面，不造骨填充关节。"
    if "scapulae" in selected:method += " 左右肩胛骨为单独可选结构。"
    if "airways" in selected:method += " 气道默认保留与气管相连的 26 邻接分支，不腐蚀、不剪除短分支；原始联合标签保存在 airways_raw.nii.gz。游离成分可能包含真实分支，需人工核对。"
    if review:
        method += " 另含实验性低密度区域候选，可能漏检或误报；仅在用户核对选择后生成候选几何，不构成磨玻璃结节诊断。"
    scene = {"missing_structures":missing_structures, "coordinate_system":"RAS", "units":"mm", "engineVersion":importlib.metadata.version("TotalSegmentator"), "models":surfaces, "nodules":candidates, "source_geometry":geometry, "method_summary":method, "selected_structures":selected, "margin_mm":margin_mm, "review_candidates":([] if not review else [entry for entry in review["candidates"] if entry["id"] not in options.get("candidate_ids",[])])}
    if surfaces and all(m['group'] in {'ribcage','scapulae'} for m in surfaces):scene['preferred_render_mode']='surface'
    save(output/"scene.json", scene)
    native, is_original = original_volume(JOB, volume)
    import pydicom
    status = json.loads((JOB/"status.json").read_text(encoding="utf-8"))
    if (output/"native_instances.json").exists():
        instances = json.loads((output/"native_instances.json").read_text(encoding="utf-8"))
    else:
        series = next(item for item in json.loads((JOB/"inventory.json").read_text(encoding="utf-8"))["series"] if item["id"] == status["series_id"])
        instances = [int(getattr(pydicom.dcmread(path,stop_before_pixels=True,specific_tags=["InstanceNumber"]),"InstanceNumber",index+1)) for index,path in enumerate(series["files"])]
        save(output/"native_instances.json",instances)
    receipt = pack(output, volume, scene, packed_masks, native, is_original, instances, progress_callback=lambda message:progress('preview',90,message))
    publish(output,surfaces,packed_masks,candidates)
    # Only this new job's exported models belong to its selected anatomy.
    # Source predictions and the parent job remain untouched.
    exported_masks={m['mask_file'] for m in surfaces};exported_models={m['id']+'.stl' for m in surfaces}
    for folder, pattern, keep in [(masks,'*.nii.gz',exported_masks),(meshes,'*.stl',exported_models)]:
        for path in folder.glob(pattern):
            if path.name not in keep:path.unlink()
    save(output/"manifest.json", {"mode":mode, "offline":True, "review_status":"automatic_unreviewed", "surfaces":[{k:m[k] for k in ("id","group","vertices","triangles","voxels")} for m in surfaces], "candidate_count_listed":len(candidates), "geometry":geometry, "selected_structures":selected, "method":method, **receipt})
    from progressive import finish
    finish(output)
    progress("completed", 100, "重建完成，可打开四宫格预览或下载离线 HTML")



def run():
    status = json.loads((JOB/"status.json").read_text(encoding="utf-8"))
    series = next(s for s in json.loads((JOB/"inventory.json").read_text(encoding="utf-8"))["series"] if s["id"] == status["series_id"])
    output = JOB/"output"
    output.mkdir(exist_ok=True)
    if status.get('inplace_operation'):
        import SimpleITK as sitk
        volume=sitk.ReadImage(str(output/'CT.nii.gz'))
        geometry=json.loads((output/'geometry.json').read_text(encoding='utf-8'))
    elif status.get("source_job"):
        source = ROOT/"jobs"/status["source_job"]/"output"
        shutil.copytree(source,output,dirs_exist_ok=True,ignore=shutil.ignore_patterns("preview.html","preview.html.tmp","revisions","models","geometry_audit.json","live"))
        import SimpleITK as sitk
        volume = sitk.ReadImage(str(output/"CT.nii.gz"))
        geometry = json.loads((output/"geometry.json").read_text(encoding="utf-8"))
    elif status.get('resume_ct_sha256'):
        with (output/'CT.nii.gz').open('rb') as source:
            if hashlib.file_digest(source,'sha256').hexdigest()!=status['resume_ct_sha256']:
                raise ValueError('恢复处理的 CT 校验不一致')
        geometry=json.loads((output/'geometry.json').read_text(encoding='utf-8'))
        volume=None
    else:
        volume, geometry = prepare(series, output)
    from progressive import initialize,ready_structures
    if volume is None:
        import SimpleITK as sitk
        volume=sitk.ReadImage(str(output/'CT.nii.gz'))
    initialize(JOB,volume,geometry,lambda message:progress('volume',10,message))
    # Do not retain the 512 x 512 x N CT in memory during neural inference.
    # A separate process also releases Torch/model allocators before export.
    del volume
    gc.collect()
    from resource_control import validate_native_workload
    validate_native_workload(status.get('structures',[]), geometry)
    from structures import resolve,inference_plan
    selected,_=resolve(status)
    tasks=inference_plan(selected,status['mode']); completed=set(); published=set()
    save(output/'inference.json',{'device':'cpu','offline_guard':True,'tasks':[]})
    def child_run(command,extra,percent):
        env=os.environ.copy();env['WORKBENCH_PHASE_PERCENT']=str(percent)
        child=subprocess.run([sys.executable,str(ROOT/'worker.py'),command,str(JOB),*extra],env=env,check=False)
        if child.returncode:
            report=json.loads((JOB/'progress.json').read_text(encoding='utf-8'))
            raise RuntimeError(report.get('error','本地重建阶段未完成'))
    for index,task in enumerate(tasks):
        from progressive import set_phase
        set_phase(output,'segmenting',task=task[1],mode=status['mode'],label=task[3])
        child_run('infer_one',[task[1]],10+int(index*65/len(tasks)))
        completed.add(task[1])
        ready=[s for s in ready_structures(selected,status['mode'],completed) if s not in published]
        if ready:
            set_phase(output,'meshing',structures=ready)
            child_run('publish',[json.dumps(ready)],10+int((index+1)*65/len(tasks)))
            published.update(ready)
    import SimpleITK as sitk
    volume = sitk.ReadImage(str(output/'CT.nii.gz'))
    export(volume, geometry, output, status["mode"])


def review_job(command):
    status = json.loads((JOB/"status.json").read_text(encoding="utf-8"))
    output = JOB/"output";output.mkdir(exist_ok=True)
    if command == "screen":
        from candidate_regions import screen
        from structures import LOBE_NAMES
        if not (output/'masks/lungs.nii.gz').exists() and not any(all((output/'ai'/task/(name+'.nii.gz')).exists() for name in LOBE_NAMES) for task in ['total','lung_highres']):
            infer(output,status['mode'],selected_override=['suspects'])
        progress("candidate_review",30,"筛选候选结节")
        screen(output,parameters=status.get("candidate_parameters",{}),manual_points_lps=status.get("manual_points_lps",[]))
        from refresh_viewer_assets import refresh
        refresh(output/'preview.html',rebuild_lods=False,backup=False)
    else:
        from selected_regions import rebuild
        rebuild(JOB,status,lambda message:progress('selected',60,message))
    progress('completed',100,'当前检查已更新')


if __name__ == "__main__":
    JOB = Path(sys.argv[2]).resolve()
    if JOB.parent != (ROOT/"jobs").resolve():
        raise ValueError("任务目录不正确")
    try:
        if sys.argv[1] == "scan":
            scan()
        elif sys.argv[1] == "run":
            run()
        elif sys.argv[1] == 'infer':
            status = json.loads((JOB/'status.json').read_text(encoding='utf-8'))
            infer(JOB/'output', status['mode'])
        elif sys.argv[1] == 'infer_one':
            status=json.loads((JOB/'status.json').read_text(encoding='utf-8'))
            infer(JOB/'output',status['mode'],task_key=sys.argv[3])
        elif sys.argv[1] == 'publish':
            import SimpleITK as sitk
            status=json.loads((JOB/'status.json').read_text(encoding='utf-8'))
            output=JOB/'output'
            volume=sitk.ReadImage(str(output/'CT.nii.gz'))
            export(volume,json.loads((output/'geometry.json').read_text(encoding='utf-8')),output,status['mode'],selected_override=json.loads(sys.argv[3]),partial=True)
        elif sys.argv[1] in {"screen","selected"}:
            review_job(sys.argv[1])
        else:
            raise ValueError("未知处理指令")
    except Exception as error:
        progress("failed", 0, "处理失败", error=str(error)[:1200])
        traceback.print_exc()
        sys.exit(1)
