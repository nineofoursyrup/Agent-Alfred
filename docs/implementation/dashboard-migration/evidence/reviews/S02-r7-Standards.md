结论：**PASS（本次受影响增量）**。Documented violations：**0**；optional heuristic smells：**0**。

固定 head `0af962fbc4fc799806ba2a27593102c3df8c9e48`，tree `745d8609cc61d3b171da96aa250c26303fa2227e`；repair parent `fd1c62513eaf8da2111edcfc5974798d1d7bca05`，integration base `ddefb65905e4792559e8ff4d130dd427b34cd137`。

原 **S02-r6-STD-01 已闭合**。`app.js:450–454` 在当前 instance、first、已接受的非负 revision、strict equality 和 matching csrf 下补调既有 `start()`，随即返回，不重放 state/projection。较旧修订、其他实例和缺凭据不获得该资格。`shell.js:172–178` 仅首次挂页且焦点已在 MainBar 时保留焦点；已挂页导航仍聚焦标题。符合 #87 `SPEC.md:31,171,180` 与 I06 `INTERFACES.md:137,145`。

完整三文件 delta 与相关 app/shell 上下文已审；startup 门、`!navigating`、后续合法断连读取、restoreRunPage owner/generation、S04 producer 均保持。最终 **18 PASS** 覆盖真实 A→B／offline-entry／同 revision 重连、一次挂页、0浏览器阶段POST、单活跃 EventSource、新草稿与焦点，以及原 shell/restart 边界。typecheck／diff PASS；适用的 r5/r6 检查复用，不重跑或相加为全量通过。

原启动 FAIL 与 app-only 中间焦点 FAIL 分别保留；中间源码由保全 r6＋记录 patch 还原，失败 trace 仅独立核验测试源码，未声称有 JS response-body hash。20资源／42边及164 source／25 AC身份核验通过。12项启发式无新增 actionable smell。

[候选与证据核验](S02-r7-candidate-check.json)｜[适用性细节](S02-r7-standards-details.json)。whole source/AC/G、S11组合／安装及原生200%／IME仍NOT RUN，实际BFCache仍BLOCKED；未改产品、未读另一评审轴。
