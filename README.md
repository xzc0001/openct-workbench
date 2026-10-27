# OpenCT Workbench

面向内部部署的离线 CT 重建与交互式三维查看基础工程。支持 DICOM 上传、序列几何检查、本地自动分割、表面导出、原始 HU 容积显示与自包含 HTML 离线预览。

仓库只包含通用工程代码、空配置模板和运行时生成的合成测试。**不附带真实影像、患者资料、机构配置、账号数据库、密钥或模型权重。** Fork / clone 后需独立安装依赖、配置模型和创建管理员。

> 研究与工程验证用途。自动分割、候选区域、骨骼编号和几何外扩参考均需人工复核，不能直接用于诊断、手术决策或替代获批的医疗软件。无专用肺段或骨折定位模型。

## 项目亮点

- **从上传到离线交付**：目录或 ZIP 上传、任务队列、进度展示、预览与离线下载组成完整流程；原始输入与派生结果分开管理。
- **保留空间与灰度依据**：保存源 CT 的尺寸、间距、方向矩阵和 HU 数据；三平面切片、WebGL2 容积与灰度等值面共享物理坐标。完整参考表面与浏览 LOD 分开，柔和光照只改变显示。
- **按需计算与渐进展示**：按选择的结构规划推理任务，逐步显示已完成结果；分块压缩、按需解码和在线索引降低重复读取开销，完整 HTML 可独立离线浏览。
- **可审查的几何链路**：记录文件哈希与处理参数，提供标签边界、源体积、网格及预览一致性检查；工程一致性与解剖正确性分别验收。
- **内部多用户工作流**：管理员、上传用户与只读用户，任务所有权校验、PBKDF2 密码哈希、可撤销会话、CSRF 检查，以及绑定用户与会话的限时清理令牌。
- **输入与资源边界**：DICOM 格式、像素与几何校验；ZIP 路径穿越、符号链接、嵌套归档与膨胀限制；有界 HTTP 并发、可选 Linux cgroup v2 隔离及内存压力恢复。
- **默认不联网推理**：工作进程使用本地权重并阻止 Python 外连；缺失模型明确失败，不在处理影像时自动下载。系统级网络隔离仍由部署方配置。

## 结构

```text
workbench/         Python HTTP 服务、重建管线和合成回归测试
  assets/          Three.js、查看器模板、MPR 与容积渲染代码
  web/             上传、任务、账号、存储管理界面
  offline_guard/   计算进程的 Python 网络保护
  qa/              无病例载荷的 JavaScript 单元测试
portal/            可选的离线 HTML 版本化静态发布工具
scripts/           本地初始化与发布前检查
docs/              部署、隐私、依赖与验证说明
```

## 快速开始

建议 Python 3.11 / 3.12、64 位操作系统。CPU 推理可运行但耗时，内存需求与体积和结构有关；先用合成数据完成安装验证。Linux 可选资源隔离；Windows 不启用 cgroup 功能。浏览器建议支持 WebGL2 与浮点线性采样，受显卡纹理尺寸与内存限制，不支持时可回退到表面显示。

```bash
git clone https://github.com/xzc0001/openct-workbench.git
cd openct-workbench
python -m venv .venv
```

激活虚拟环境，Linux/macOS 使用 `source .venv/bin/activate`，Windows PowerShell 使用 `.\.venv\Scripts\Activate.ps1`。然后执行：

```bash
python -m pip install --upgrade pip
python -m pip install torch==2.14.1 torchvision==0.29.1 --index-url https://download.pytorch.org/whl/cpu
python -m pip install -r requirements.txt
python -m pip check
python scripts/configure.py
python workbench/auth.py --database auth/accounts.sqlite3 --credentials-file auth/initial-admin.txt
python workbench/server.py
```

在本机打开 <http://127.0.0.1:8769>。初始管理员用户名是 `admin`，随机密码只写入本地 `auth/initial-admin.txt`，请限制权限并在登录后修改密码、妥善移除初始凭据文件。无任何内置固定密码。`config.json` 和 `auth/` 均被 Git 忽略。

服务始终绑定 loopback；配置里的 `host` 是兼容字段，不能用它直接开放监听。共享部署应使用经过配置的反向代理，见 [部署说明](docs/DEPLOYMENT.md)。不要把开发 HTTP 服务直接暴露到互联网。

### 准备离线模型

安装 Python 包不会包含全部模型。请在**不含影像数据**的准备环境中，按照 [TotalSegmentator 官方说明](https://github.com/wasserth/TotalSegmentator) 下载有权使用的模型，将 nnU-Net 权重结果目录配置到本地 `workbench/config.json` 的 `weights` 字段，默认 `../weights`。不同结构依赖不同模型；完整目录必须保留上游要求的名称与 checkpoint 文件。缺失时任务会明确报错。

目前推理适配器依赖 TotalSegmentator / nnU-Net 的内部接口，`requirements.txt` 固定直接依赖基线，但不代表所有平台、传递依赖与模型组合都已验证。部署前应在目标系统复验并生成完整依赖锁定记录；参阅 [依赖说明](docs/DEPENDENCIES.md)。部分任务或权重另有授权要求，不能因本仓库可用就推定全部模型可自由再分发。

本工程不需要云端 API key。若后续扩展外部服务，应通过部署方的本地环境或密钥管理系统配置，禁止写入源码。

## 验证

```bash
python scripts/check_release.py
python -m unittest discover -s workbench -p "test_*.py"
```

测试使用临时目录、生成的数组与 DICOM 字节，不读取真实病例。Node.js 18+ 可启用 JavaScript 合约测试，未安装时对应测试会跳过。运行全套测试需要上述医学图像依赖；无需下载模型权重。测试结果及首版检查范围见 [验证记录](docs/VALIDATION.md)。

## 开源项目与许可

感谢下列项目。详细用途、许可边界与链接见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

| 项目 | 本工程中的作用 |
| --- | --- |
| [TotalSegmentator](https://github.com/wasserth/TotalSegmentator) | 本地解剖结构自动分割 |
| [nnU-Net](https://github.com/MIC-DKFZ/nnUNet)、[PyTorch](https://pytorch.org/) / [torchvision](https://github.com/pytorch/vision) | 推理后端、模型与 CPU 张量计算 |
| [SimpleITK](https://simpleitk.org/)、[pydicom](https://pydicom.github.io/)、[NiBabel](https://nipy.org/nibabel/) | 医学影像读写、几何与 NIfTI 处理 |
| [VTK](https://vtk.org/) | 等值面、网格、STL 与几何检查 |
| [NumPy](https://numpy.org/)、[SciPy](https://scipy.org/)、[scikit-image](https://scikit-image.org/)、[Pillow](https://python-pillow.org/) | 数组、区域、图像与显示处理 |
| [Three.js / OrbitControls](https://threejs.org/) | 浏览器三维交互与 WebGL 渲染，随附 MIT 声明 |
| [Radix Colors](https://www.radix-ui.com/colors) | 颜色设计来源，随附 MIT 声明 |
| [NGINX](https://nginx.org/) | 可选内部反向代理 |
| [3D Slicer](https://www.slicer.org/) | 可选独立复核工具；未随附，不是运行服务的必需依赖 |

原创工程部分按 [MIT](LICENSE) 提供；第三方代码和模型权重分别遵循各自许可。无任何机构、上游项目或医疗产品的背书。

## 隐私与贡献

本仓库的发布检查用于保护**源码仓库**，不是临床数据匿名化工具。源 CT、派生网格、离线 HTML 和日志在实际使用中仍可能包含或推导个人医学信息。内部部署方负责合法授权、TLS、访问审计、备份、保留期限与导出权限。

提交前运行发布检查，阅读 [隐私说明](docs/PRIVACY.md) 和 [贡献说明](CONTRIBUTING.md)。Issue、PR、截图和测试附件也不得上传真实患者、机构内部标识、内部地址、凭证或真实数据派生结果。
