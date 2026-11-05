# Wiki 维护与同步

本项目 Wiki 使用 GitHub Flavored Markdown，首页为 `Home.md`，页面文件名
使用稳定英文与连字符，`_Sidebar.md`、`_Footer.md` 提供统一导航与页脚。
正文使用中文，并链接到对应源码、依赖项目和许可说明。

`docs/wiki/` 是仓库内可审查的页面源文件。主仓库与 Wiki 是独立 Git 仓库，
更新代码不会自动更新 Wiki。Fork 后也应自行初始化和同步 Wiki。

## 首次初始化

GitHub 要求先在网页创建一个 Wiki 页面，随后可克隆 `.wiki.git`。
如果 `git ls-remote` 提示不存在，先确认账号权限和 Wiki 是否已创建首页。
参阅 [GitHub 官方页面管理说明](https://docs.github.com/en/communities/documenting-your-project-with-wikis/adding-or-editing-wiki-pages)。

## 同步流程

1. 在主仓库中编辑 `docs/wiki/` 并检查正文、导航、源码链接与脱敏边界。
2. 完成主仓库发布检查后，克隆对应的 Wiki Git 仓库到独立目录。
3. 仅复制已审查的 Markdown 页面，检查 Wiki 暂存区与全部历史。
4. 在 Wiki 的默认分支提交并推送，核对远端页面文件与已审查源文件相同。

禁止将运维日志、私有配置、截图、数据样例、真实机构或访问令牌夹带进 Wiki。
页面删除或脱敏更新不能清除已发布的 Git 历史，因此每次推送前都要检查。

侧边栏与页脚遵循
[GitHub 官方命名约定](https://docs.github.com/en/communities/documenting-your-project-with-wikis/creating-a-footer-or-sidebar-for-your-wiki)。
