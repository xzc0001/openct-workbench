# 测试与贡献

在自己的虚拟环境安装依赖并生成本地配置后，从仓库根目录执行：

```bash
python scripts/check_release.py
python -m unittest discover -s workbench -p "test_*.py"
python -m unittest discover -s cluster/tests -t . -p "test_*.py"
```

测试生成临时数组、DICOM 字节、账户和证书，不能改用真实临床检查。
Node.js 启用查看器合约测试；未安装时相应测试可能跳过。
平台限定测试需在对应系统补验，跳过不能当作通过。

## 可选浏览器测试

`workbench/qa/test_guest_ui.cjs` 与打印浏览器测试使用 Playwright，需自行安装
该公开包及浏览器。测试不需要真实影像；打印前用
`workbench/qa/build_print_fixture.py` 生成合成页面。生成物不进入仓库。
浏览器测试与 Node.js 合约测试是不同证据层级。

## 提交前

- 更新 `release-files.json` 仅列出经过审查的通用文件。
- 检查暂存区与提交内容，运行 `check_release.py --staged`。
- 使用独立秘密扫描器检查新内容，推送前检查所有可达历史。
- 保留第三方许可证，不上传模型或许可证密钥。
- 改动推理接口时单独验证数值、几何和内存边界。

Wiki 页面在 `docs/wiki/` 维护。页面文件名使用稳定的英文连字符标题，
`Home.md` 作为首页，`_Sidebar.md` 和 `_Footer.md` 用于 GitHub Wiki 导航。
修改页面时同时检查标题、导航、内部链接与脱敏边界。

[贡献规范](https://github.com/xzc0001/openct-workbench/blob/main/CONTRIBUTING.md) ·
[验证记录](https://github.com/xzc0001/openct-workbench/blob/main/docs/VALIDATION.md)
