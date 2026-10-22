# PR #147 V03 code-review

候选：`a8bebff38c3a99e0c2bc5662a8b4b0ee66535bb8`；基线：`e982bfa83e72938b3e5cbaeb4f469290cc3538ad`。评审范围为该固定三点差异，以及本轮追加的 V03 门禁证据。产品／测试候选在全量检查期间保持不变。

本轮按 `code-review` 技能执行一次本地 Standards／Spec 聚焦评审；不是独立多代理评审。

## Standards

依据：仓库 `CLAUDE.md`、`docs/agents/issue-tracker.md`、`docs/design/issue-122/RECIPES.md`、ADR-0049、Dashboard 实施规范第 5 节、用户的本票执行说明和随后批准的三个既有测试差异例外；另检查技能的命名、重复、职责归属和无必要抽象等 smell baseline。

- 新增 CSS 选择器均以 `#mainbar` 开头；去除 MainBar 自有规则后，CSS 与基线逐字相同。`:root`、公共 `sy-` 类、其他视图规则及延迟会话回执保护没有变化。
- HTML 的差异完全位于 `#mainbar`；JS 除 MainBar 渲染函数、气泡辅助函数与 MainBar 状态绑定外均与基线相同。没有新增静态资源、HTTP/SSE 读取、计时器或业务状态机。
- 消息使用既有 `node()`／`textContent` 生成；Run ID 经过 `encodeURIComponent`，空字符串身份沿用现有 `/runs/` 详情路径处理，不能用 truthy 判断误删合法空身份。
- 三个测试文件的稳定 patch-id 在 rebase 前后均为 `1471ea13a58853477cc110593fe5527b9cc95a2d`；保留精确 `toBe` 断言、既有状态断言、超时与 retries=0。shell 测试同时继承 main 的 V11 Inbox 定位器更新，该上游变化不计入本票差异。
- 未发现需修正的规范违反或有实际影响的代码异味。

Standards：0 项发现。

## Spec

依据：GitHub #125 验收条件、父票 #122 MainBar 配方、冻结范围及用户授权。

- 用户气泡靠右且上限 88%，助手靠左且上限 92%；使用已批准 token 得到指定颜色、1px 边框、12/12/4/12 与 12/12/12/4 圆角、13px 字号；`pre-wrap` 与 `overflow-wrap:anywhere` 保留换行和长路径。
- `article[data-run-id]`、正文恢复、阅读／定位文字、状态 `small`、保存／结局事实及旧消息身份保留。Run 入口只使用已有身份；临时流式内容以虚线提示。
- MainBar 状态点直接复用 V02 的 `dotClass` 和已持有事实；V02 原用例增加的连接、运行、断连、恢复和 reducedMotion 覆盖仍保留。
- 原「消息」label 只做视觉隐藏；textarea、rows、占位符、输入法处理、草稿、发送原因与按钮语义未改。
- 已核对旧候选四视口证据、1440／320 MainBar 图片和 1440 原稿图片，MainBar 产品文件与旧候选字节相同。既有窄屏面板、多行输入、真实状态与原稿差异均已明确记录；不把旧截图声称为本轮新截图，不宣称原生 IME／200%／真实 provider 验收已完成。
- 本轮完整 Python 为 6445 passed / 1 deselected / 14 warnings；完整浏览器为 545 passed / 0 failed，包含 MainBar 相关用例、V02 状态点与两处定位器适配；其余门禁及 wheel／sdist × base／mcp 四种安装组合均通过。运行前后 719 个源码、测试、脚本、依赖和配置文件 SHA-256 一致；本轮无失败、重试或 flaky 复现。

Spec：0 项发现；全部本地门禁通过。

总计：Standards 0 项（无阻塞项）、Spec 0 项（无阻塞项）；本地 code-review 通过。远端 CI 须绑定推送后的最终 HEAD 独立回读，本结论不代替该检查。
