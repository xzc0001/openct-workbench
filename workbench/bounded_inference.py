"""Worker-only memory adapters for the pinned TotalSegmentator implementation.

Keep the model, crop, interpolation order and numerical CT calculations intact.
Integer labels need no float64 copies. Input voxels are converted to float64
only inside CT resampling, rather than retained throughout neural inference.
No installed package is modified. Unknown upstream source fails closed.
"""
import ast
import inspect
import textwrap
import hashlib
import json
from pathlib import Path


def native(image):
    import numpy as np
    return np.asanyarray(image.dataobj)


def spacing_data(image, order):
    import numpy as np
    # Nearest-neighbour selection of byte labels is exact in either dtype.
    if order == 0 and image.get_data_dtype() == np.dtype('uint8'):
        return native(image)
    return image.get_fdata(caching='unchanged')


def compact_undo_crop(image, reference, bbox):
    import numpy as np
    import nibabel as nib
    data = native(image)
    # Preserve the upstream float64 path for any non-byte input.
    result = np.zeros(reference.shape, dtype=np.uint8 if data.dtype == np.uint8 else np.float64)
    result[tuple(slice(a, b) for a, b in bbox)] = data
    return nib.Nifti1Image(result, reference.affine)


def adapted(function, receivers=(), spacing=False, threads=None):
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    counts = {name: 0 for name in receivers}
    special = 0

    class Adapter(ast.NodeTransformer):
        def visit_Call(self, node):
            nonlocal special
            if isinstance(node.func, ast.Attribute) and node.func.attr == 'get_fdata' and not node.args and not node.keywords:
                receiver = node.func.value
                if isinstance(receiver, ast.Name) and receiver.id in counts:
                    counts[receiver.id] += 1
                    replacement = ast.Call(ast.Name('_workbench_native', ast.Load()), [receiver], [])
                    return ast.copy_location(replacement, node)
                if spacing and isinstance(receiver, ast.Name) and receiver.id == 'img_in':
                    special += 1
                    return ast.copy_location(ast.Call(ast.Name('_workbench_spacing_data', ast.Load()), [receiver, ast.Name('order', ast.Load())], []), node)
            if threads is not None and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == 'torch' and node.func.attr == 'set_num_threads':
                special += 1
                node.args = [ast.Call(ast.Name('min', ast.Load()), [node.args[0], ast.Constant(threads)], [])]
            return self.generic_visit(node)

    tree = Adapter().visit(tree)
    if any(n == 0 for n in counts.values()) or ((spacing or threads is not None) and not special):
        raise RuntimeError('本地推理适配器与模型运行库不兼容，请核对固定版本')
    ast.fix_missing_locations(tree)
    namespace = function.__globals__
    namespace.update(_workbench_native=native, _workbench_spacing_data=spacing_data)
    scope = {}
    exec(compile(tree, inspect.getsourcefile(function), 'exec'), namespace, scope)
    return scope[function.__name__]


def cached_localizer(predict, folder, input_sha256, weights):
    """Reuse identical whole-CT localization, independently of selected organs."""
    import nibabel as nib
    checkpoint = next((Path(weights)).glob('Dataset298_*/**/checkpoint_final.pth'))
    with checkpoint.open('rb') as source:
        model_sha256 = hashlib.file_digest(source, 'sha256').hexdigest()
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)

    def predict_cached(*args, **kwargs):
        task_id = args[2] if len(args)>2 else kwargs.get('task_id')
        output = args[1] if len(args)>1 else kwargs.get('file_out')
        source = args[0] if args else kwargs.get('file_in')
        if task_id != 298 or output is not None or not isinstance(source, (str, Path)):
            return predict(*args, **kwargs)
        identity = {'ct':input_sha256,'model':model_sha256,'runtime':'2.18.0','args':args[2:],'kwargs':kwargs}
        key = hashlib.sha256(json.dumps(identity,sort_keys=True,default=str).encode()).hexdigest()
        path = folder/(key+'.nii.gz')
        if path.is_file():
            print('复用同一 CT 的器官定位标签',flush=True)
            return nib.load(path), nib.load(source), None
        result = predict(*args, **kwargs)
        pending = folder/(key+'.pending.nii.gz')
        nib.save(result[0], pending)
        pending.replace(path)
        return result
    return predict_cached


def install(threads, localizer_directory=None, input_sha256=None, weights=None):
    from importlib.metadata import version
    if version('TotalSegmentator') != '2.18.0':
        raise RuntimeError('内存优化需固定 TotalSegmentator 2.18.0，未对未知版本启用')
    import totalsegmentator.cropping as cropping
    import totalsegmentator.resampling as resampling
    import totalsegmentator.nnunet as nnunet
    import totalsegmentator.python_api as api
    import totalsegmentator.nifti_ext_header as header
    if getattr(api, '_workbench_bounded', False):
        return api
    cropping.crop_to_bbox_nifti = adapted(cropping.crop_to_bbox_nifti, ['image'])
    cropping.crop_to_mask = adapted(cropping.crop_to_mask, ['mask_img'])
    cropping.undo_crop = compact_undo_crop
    nnunet.crop_to_mask = cropping.crop_to_mask
    nnunet.undo_crop = compact_undo_crop
    resampling.change_spacing = adapted(resampling.change_spacing, spacing=True)
    nnunet.change_spacing = resampling.change_spacing
    header.add_label_map_to_nifti = adapted(header.add_label_map_to_nifti, ['img_in'])
    nnunet.add_label_map_to_nifti = header.add_label_map_to_nifti
    nnunet.nnUNetv2_predict = adapted(nnunet.nnUNetv2_predict, threads=max(1, int(threads)))
    nnunet.nnUNet_predict_image = adapted(nnunet.nnUNet_predict_image, ['img_in_orig', 'crop_mask_img', 'img_pred'])
    if localizer_directory:
        nnunet.nnUNet_predict_image = cached_localizer(nnunet.nnUNet_predict_image,localizer_directory,input_sha256,weights)
    api.nnUNet_predict_image = nnunet.nnUNet_predict_image
    api.totalsegmentator = adapted(api.totalsegmentator, ['organ_seg','seg_img'])
    api._workbench_bounded = True
    return api
