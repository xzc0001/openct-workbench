# 配置与离线模型

`scripts/configure.py` 从 `workbench/config.example.json` 生成本地
`workbench/config.json`，并创建私有数据目录；已有配置不会被静默覆盖。

| 字段 | 作用 | 示例含义 |
| --- | --- | --- |
| `port` | 本地 HTTP 服务端口 | 默认 8769 |
| `python` | 工作进程解释器 | 配置脚本记录当前虚拟环境 |
| `weights` | 本地权重结果目录 | 默认相对路径 `../weights` |
| `threads` | 计算线程配置 | 按机器资源设定 |
| `minimum_free_gb` | 磁盘余量门槛 | 默认 4 GiB |
| `public_origins` | 反向代理使用的确切浏览器来源 | 本地默认空列表 |

相对路径以 `workbench` 为基准。`host` 是兼容字段，修改它不会开放服务监听；
后端始终使用 loopback。`WORKBENCH_AUTH_DATABASE` 可指定账户数据库的绝对路径。

## 模型准备

在不含影像的准备环境中，按照
[TotalSegmentator 官方说明](https://github.com/wasserth/TotalSegmentator)
下载有权使用的模型，再放到自己配置的权重目录。保留上游目录层级、
checkpoint 名称及任务依赖，不要只复制单个权重文件。

推理使用已有本地权重；缺失模型直接失败。部分任务和模型另有授权要求，
本仓库的 MIT 许可不授予第三方模型权限。

## 可选功能

未设置 `cluster` 时不启动集群桥接。未设置 `transfer_target` 时不开放
跨工作台发送入口。它们的实际目标与私有配置应只写到本地。

跨工作台目标格式为 `{"host":"YOUR_INTERNAL_HOST","port":8769}`；
`transfer_public_url` 是使用者访问接收端的地址。示例中的主机名必须在本地替换。
当前发送器使用 HTTP，必须限定在受信任的加密网络边界内，详见传送说明。

不要把 API key、密码、私钥、许可证、真实地址或本地配置提交回仓库。
本地基础重建无需云 API key。
