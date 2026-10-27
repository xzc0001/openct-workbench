# Third-party software and models

Original application code is covered by the root MIT license. That license does
not replace upstream licenses, notices, model terms or dataset restrictions.
No pretrained weights, Python environments or patient data are distributed here.

## Bundled browser assets

- [Three.js](https://github.com/mrdoob/three.js) and OrbitControls: MIT.
  See `workbench/assets/THREE-LICENSE.txt`; preserve it when redistributing assets.
  The preview packer also embeds the notice in generated HTML.
- [Radix Colors](https://github.com/radix-ui/colors): MIT; palette attribution and
  notice in `workbench/assets/RADIX-COLORS-LICENSE.txt`. The packer embeds this notice.

## Installed dependencies

| Upstream | Use | License / authoritative terms |
| --- | --- | --- |
| [TotalSegmentator](https://github.com/wasserth/TotalSegmentator) | Anatomical segmentation and local model orchestration | [Apache-2.0 code license](https://github.com/wasserth/TotalSegmentator/blob/master/LICENSE); model/task terms are separate |
| [nnU-Net](https://github.com/MIC-DKFZ/nnUNet) | Neural segmentation runtime | [Apache-2.0](https://github.com/MIC-DKFZ/nnUNet/blob/master/LICENSE) |
| [PyTorch](https://github.com/pytorch/pytorch) | Tensor computation and inference | [BSD-style license and additional notices](https://github.com/pytorch/pytorch/blob/main/LICENSE) |
| [torchvision](https://github.com/pytorch/vision) | Upstream inference dependency | [BSD-3-Clause](https://github.com/pytorch/vision/blob/main/LICENSE) |
| [SimpleITK](https://github.com/SimpleITK/SimpleITK) | CT reading, physical geometry and resampling | [Apache-2.0](https://github.com/SimpleITK/SimpleITK/blob/master/LICENSE) |
| [VTK](https://github.com/Kitware/VTK) | Isosurfaces, meshes and STL | [BSD-3-Clause and third-party notices](https://github.com/Kitware/VTK/blob/master/Copyright.txt) |
| [pydicom](https://github.com/pydicom/pydicom) | DICOM parsing and synthetic fixtures | [MIT](https://github.com/pydicom/pydicom/blob/main/LICENSE) |
| [NiBabel](https://github.com/nipy/nibabel) | NIfTI arrays and geometry | [MIT](https://github.com/nipy/nibabel/blob/master/COPYING) |
| [NumPy](https://github.com/numpy/numpy) | Array operations | [BSD-3-Clause and bundled notices](https://github.com/numpy/numpy/blob/main/LICENSE.txt) |
| [SciPy](https://github.com/scipy/scipy) | Spatial and numerical processing | [BSD-3-Clause and bundled notices](https://github.com/scipy/scipy/blob/main/LICENSE.txt) |
| [scikit-image](https://github.com/scikit-image/scikit-image) | Region processing | [License collection](https://github.com/scikit-image/scikit-image/blob/main/LICENSE.txt) |
| [Pillow](https://github.com/python-pillow/Pillow) | Image processing | [HPND and bundled notices](https://github.com/python-pillow/Pillow/blob/main/LICENSE) |

## Optional tools, not bundled

- [NGINX](https://nginx.org/en/): reverse proxy, see [upstream license](https://nginx.org/LICENSE).
- [3D Slicer](https://www.slicer.org/): independent review of exported volumes and
  masks, see [Slicer license](https://github.com/Slicer/Slicer/blob/main/License.txt).
  Neither a Slicer installation nor its Python runtime is redistributed.
- [Python](https://www.python.org/), [Node.js](https://nodejs.org/),
  [systemd](https://systemd.io/) and Git are supplied by the deployment environment;
  their terms remain applicable.

## Weights, tasks and acknowledgements

Download models through upstream-supported channels and retain their license,
version, task identifiers and checksums in your private deployment inventory.
Some TotalSegmentator tasks require additional authorization; do not place license
keys or model files in this repository. Offline operation does not waive licensing.
For academic use, follow the citation guidance in the TotalSegmentator and nnU-Net
repositories. Their authors do not endorse this application.
