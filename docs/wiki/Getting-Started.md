# 安装与首次启动

## 环境

需要 64 位 Python 3.12/3.13 或经过依赖验证的兼容版本，以及支持 WebGL2
的浏览器。CPU 推理耗时和内存随 CT 体积、结构选择与模式而变化。
Node.js 用于可选查看器测试，不是服务运行的前置要求。

```bash
git clone https://github.com/xzc0001/openct-workbench.git
cd openct-workbench
python -m venv .venv
```

Linux/macOS 激活：`source .venv/bin/activate`。
Windows PowerShell 激活：`.\.venv\Scripts\Activate.ps1`。

```bash
python -m pip install --upgrade pip
python -m pip install torch==2.14.1 torchvision==0.29.1 --index-url https://download.pytorch.org/whl/cpu
python -m pip install -r requirements.txt
python -m pip check
python scripts/configure.py
python workbench/auth.py --database auth/accounts.sqlite3 --credentials-file auth/initial-admin.txt
python workbench/server.py
```

打开 <http://127.0.0.1:8769>。初始用户名为 `admin`，随机密码写入本地私有
文件 `auth/initial-admin.txt`。登录后修改密码并妥善移除初始凭据文件。
无预设固定密码。

此时可以验证登录与界面；进行推理前还需准备离线权重。
[模型与配置说明](https://github.com/xzc0001/openct-workbench/wiki/Configuration)
说明了相关要求。

## 首次使用检查

- 先运行合成测试，确认依赖和数据读写链路。
- 确认服务仅监听 loopback，账户目录与缓存、任务目录分离。
- 配置自己有权使用的权重，先验证缺失模型时的失败行为。
- 完成目标环境验收后，再使用经过授权的数据。

直接依赖版本已固定，但不等于所有平台的完整安装锁定。
不要为解决版本问题随意替换推理内部接口。
