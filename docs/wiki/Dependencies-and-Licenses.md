# 开源依赖与许可

| 项目 | 作用 |
| --- | --- |
| [TotalSegmentator](https://github.com/wasserth/TotalSegmentator) | 本地结构分割和任务规划 |
| [nnU-Net](https://github.com/MIC-DKFZ/nnUNet) | 神经网络分割运行时 |
| [PyTorch](https://pytorch.org/) / [torchvision](https://github.com/pytorch/vision) | 张量计算与推理依赖 |
| [SimpleITK](https://simpleitk.org/) / [pydicom](https://pydicom.github.io/) / [NiBabel](https://nipy.org/nibabel/) | 医学影像读写与物理几何 |
| [VTK](https://vtk.org/) | 等值面、网格与 STL |
| [NumPy](https://numpy.org/) / [SciPy](https://scipy.org/) / [scikit-image](https://scikit-image.org/) / [Pillow](https://python-pillow.org/) | 数组、区域与图像处理 |
| [Three.js](https://threejs.org/) / OrbitControls | 浏览器三维渲染与交互 |
| [Radix Colors](https://www.radix-ui.com/colors) | 颜色设计来源 |
| [cryptography](https://cryptography.io/) | 证书、签名和密码学验证 |
| [NGINX](https://nginx.org/) | 可选反向代理 |
| [3D Slicer](https://www.slicer.org/) | 可选独立复核，不是服务运行必需组件 |
| [Playwright](https://playwright.dev/) | 可选合成浏览器测试，不随附浏览器 |

原创工程使用 MIT；随附 Three.js 和 Radix 许可声明需保留。
第三方代码、模型、数据与工具分别遵守各自许可，不能将本项目 MIT 解释为
允许重新分发所有模型。完整说明见
[THIRD_PARTY_NOTICES.md](https://github.com/xzc0001/openct-workbench/blob/main/THIRD_PARTY_NOTICES.md)。

直接依赖固定版本不构成完整跨平台锁定；部署方应保留实际验收的依赖、资源
哈希和许可清单。本 Wiki 不宣称上游项目对本工程提供背书。
