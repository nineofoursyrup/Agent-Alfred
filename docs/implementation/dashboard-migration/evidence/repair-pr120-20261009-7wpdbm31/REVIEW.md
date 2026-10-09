# PR #120 四项 P2 修复独立复核

基线：`11dcc66f18ceb264ade1c1bcc1b48287dc4f1516`。最终候选为同目录 `final.diff`、`final-manifest.json` 和 `final-candidate/` 中的 7 个文件；已逐个核对 SHA256。

- final-manifest.json SHA256：`274775393b6f209350e45f2920b8abb5daa814f31160246421fbe84d1c822984`
- final.diff SHA256：`08b192690ab7017fa00b0bfa69a2bf3d1ca56ba0f8950a7a8dbc0e16af330665`

范围仅为用户指出的 4 项 P2 修复及其直接回归。由独立审阅代理在一次聚焦审阅内分别检查 Standards 与 Spec；不是整个 PR 的重新验收。依据已读取的 code-review SKILL.md 全部流程与 smell baseline、docs/agents/domain.md、CONTEXT.md 相关领域定义、ADR-0045/0046、dashboard-implementation/INTERFACES.md I00/I05/I07、issue-89/DESIGN.md D08、issue-91/DESIGN.md R03，以及原 Standards/Spec 报告。

## Standards

**0 项可操作问题；0 项需记录的可选 smell。**

Models 将取消钉选的在途/待核验锁与动作回执分离。未知回执后的读取须仍属于相同 receipt/comparison 对象、当前实例与 generation、最新读取序号，并且成功接收非倒退的当前 revision；只有连接已同步、settings status=ok、目标仍已钉选才解除编辑锁。旧读取、离线读取、失败读取不能解锁；原草稿与未知回执保留，不自动重送。正常 pending unpin 仍锁住编辑控件。

Run fields 与 Overview 的用途 key 已统一为真实闭集值 inference_probe，四个已知 purpose 与 _schema/contracts.py 一致；真正未知用途的显式标签保留。共享映射抽取可作为未来维护选择，本次不构成可操作缺陷。

## Spec

**0 项可操作问题。**

定位来源逐 Run 保存；只有当前定位目标显示定位专有片段。回到最新、再次定位、切 Session 后，未获普通来源的旧片段继续被过滤。匹配 Session/Run 的普通历史页、正式 SSE 或当前终态投影可独立将记录纳入普通呈现；正文恢复不改写来源资格，实例变化清理全部回复。原普通历史 cursor 与去重逻辑保持。

SSE error、offline 和相关 pageshow 重新核验入口均退休定位请求并递增代次。迟到响应必须继续通过代次、页面所有者、实例和 Session 检查，重连不能复活断连前的请求。此结论针对已退休的请求，不宣称禁止一切 offline 期间显式只读动作。

初版修复额外加入的两个 blanket connected 判断会阻断初始/跨 Session 同步期间的合法定位。审阅时用原样抽取的候选 locateReply/resume/continueSession 做隔离 Node VM 执行，确认跨 Session HTTP 先返回时返回 retired 且残留 loading。最终候选已移除这两个判断，保留真实断连事件驱动的退休；新增测试覆盖 HTTP 先于新 Stream 首快照返回，已复核其修复路径。初版快照与失败事实保留，没有改写成最终通过证据。

## 验证与局限

审阅未启动 browser、未修改仓库、GitHub 或提交状态。复用执行代理报告的有效检查：Models/MainBar 定向测试 28 项、MainBar reading 2 项；最终定位相关 mainbar-location、empty-identity、mainbar-integration 共 16 项与 typecheck 通过。最终用途测试的最新运行结果由执行代理另行记录；本报告不把尚未取得的运行结果写为 PASS。独立执行仅包括上述隔离 Node VM 诊断和最终候选文件摘要核对。

真实中文 IME 仍为 BLOCKED，未被本次修复或合成事件测试解除。该结论不授权 PR 转 Ready、合并、关票或其他发布动作。

**Standards：0 项，无最高级别；Spec：0 项，无最高级别；既知 IME 验收阻塞保持。**
