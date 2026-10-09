# POST-120-01 后继证据适用性（冻结候选，待独立采用）

实际 main `63fe1a6ac026084118cbc9585cedb30e7f03a0a2` 的首次合并后 CI 37935569616 为 FAIL，不能由原 PR/push 两条 PASS 覆盖。Python 6423 PASS / 22 skipped / 1 deselected；browser 540 PASS / 1 FAIL。原始 checkout、日志、artifact 哈希及受控 red 独立保留。

本缺陷直接涉及 S03 / #111 AC06：Graph 图文同源、缩放/点选/Step 锚点可用；Dashboard AC09；源项 issue-89:M18 / P14 / CE-18 / D10。issue-89:M14 当前 Memory 引用与 Dashboard AC10 trace 导出是相邻未变回归边界，不是此缺陷的直接失败责任。共享 topology 的 Behaviour 调用涉及 S06 / #114 AC02/AC06 及 issue-90:P11。S11 / #119 AC03、AC06、AC07、AC08 需绑定后继代码、真实 CI、包内资源与支持回退。

修复目标是现有路径快照的引用可用性随独立 Run 过程 DOM 到达/失效同步，不改变 Run-path endpoint、快照身份、拓扑、Step/Attempt 事实、读取边界、录制状态或后台读取策略。冻结 diff 与受控 red→green 已就绪，最终采用须有独立报告。

原生中文 IME、200%实测、MainBar单次发送/202竞态、4项前次修复证据保留为历史。若冻结 diff 仅为引用同步且不改变 MainBar / CSS / 输入事件、几何布局、设置持久化或通用会话协议，则按相同字节和不相交交互范围复用；这是适用性论证，不能将历史截图冒充新候选重跑。

后继本地验证需覆盖受控晚到证据、现有路径键盘/拓扑/Run阅读以及新增失效路径。新版 wheel/sdist × base/mcp 四项隔离安装需新参考；由于两个前端模块将变化，旧包资源 SHA 不能直接冒当前身份。当前源码/远端 CI 与每安装 HTTP 闭包分别核验，不声称四套完整安装浏览器测试。

支持目标需复制同一产品修复，保留 Inbox 默认入口差异，再以新包运行 old22c→current→supported 同状态正常关闭链（G08）。旧支持 SHA 3f0588d 只保留为历史，不用来宣称修复后完整支持目标。

实际门禁：修复 PR CI → 预期 head 约束合并 → 实际修复 merge SHA CI；工作流只对 main 与原 integration 分支触发 push，因此修复分支的合并前适用门禁为 pull_request CI（不凭空增补不适用的 push 门禁）。每条 CI 必须完成项目完整步骤，新失败先查因，不盲目重试。

最终采用前需独立 Standards / Spec 对同一冻结候选 PASS、首败关闭证据完整。原309 sources / 674 fragments / 26 AC / 12 MCE / 8 G的原始矩阵不重写历史；将后继适用性、差异验证与 CI 作为采用依据。当前 issues 仍 OPEN，本文不构成完成。

## 当前新增验证

- 主 tree `123caa591cf8053a9cfbafa7fdbe2e9865e77686`，src tree `209f93a5401dd4f00ea954dbd28eb5a820b36fba`；支持 tree `19be93d8a59b0cb463f7fb2fbcbc2d0d371699b0`。三变更文件逐字相同；620产品文件仅原 index.html / shell.js 默认入口差异。
- 当前1项完整引用生命周期与137项相关回归PASS；typecheck与diff check PASS。最终完整test list为542项，远端完整CI尚待。详见 verification.json / candidate.json；首FAIL、两版red与较早green保留，不将旧测试源码冒最终测试身份。
- wheel `d06e3d61e6bf33ff98b775451f05285f69387ba0159b27b2bc63bc7c554c780d`、sdist `b6623acb4da0506dc28f456431a5b373877d41eed6e599ee18ad1eee1d527c95`；四安装每组620包文件、34资源、16路由、100HTTP全通过。产品包内文件与HTTP闭包由后继CI逐字比对此 installation-results.json 参考。
- 新G08 PASS：旧598文件包→当前620文件包→支持620文件包，在同一全新隔离状态与保留标签上完成；3次正常close0与所有权释放、消息/记忆/遗忘/账目/设置/草稿/深链保持、刷新零自动POST，端口释放复查PASS。wheel/source/installed三方身份核对见 lifecycle-source-binding.json；可公开结果 lifecycle-portable.json 仅删除3处进程CSRF，原件hash和删除路径见 lifecycle-summary.json。

本文与三文件修复不声称实际merge CI已通过；修复PR、实际merge、其CI及逐票验收写回仍是剩余交付门禁。
