# S02 startup-r7 修复交接

固定 head `0af962fbc4fc799806ba2a27593102c3df8c9e48`，tree `745d8609cc61d3b171da96aa250c26303fa2227e`；repair parent `fd1c62513eaf8da2111edcfc5974798d1d7bca05`（r6 tree `e76f6ad0e3ec807ae1fa34c8a64cb5168c0fde9d`），integration base `ddefb65905e4792559e8ff4d130dd427b34cd137`。分支 `codex/dashboard-s02`，工作树 `/Users/nineofour/.codex/worktrees/dashboard-s02/Agent-Alfred`，clean。一个本地修复提交；未 push、未合入 integration、未写 tracker。产品在此身份后不再修改；独立两轴复审由 coordinator 调度。

## 修复与边界

双审 r6 各自独立发现同一个 P2：A→B 实际重启，先接受 B revision 0，B 的匹配 entry 回执在 offline 到达；随后 native SSE 收到同一 B/revision 0，去重分支提前返回，凭据和状态虽齐备却一直0中央页面。依据 #87 R01 `SPEC.md:31` 与 I06 `INTERFACES.md:137,145`。两份报告、Spec细节与两个真实probe在 `r6-review-evidence/` 保全。

`app.js` 只在已有相同 revision 的去重分支补首次就绪重试：已通过当前 instance 守卫、`first`、已接受的 `revision>=0`、strict revision equality 和现有 matching csrf 才调用原 `shell.start()`。不重新应用旧 state 内容，不推动 revision/projection，不另造页面挂载旁路。shell 的 `!navigating` 与文档级 started 门均保持；已 ready 的页面普通断连仍不重锁。

回归在首挂恢复后另捕获 **独立焦点 FAIL**：等待时用户已在宽屏 MainBar 编辑新草稿，首次挂页标题夺焦。按 #87 R10 `SPEC.md:180`，仅在 `!mounted` 且当前焦点确在 MainBar 内时保留它；正常已挂页导航聚焦规则不变。原 restoreRunPage owner/generation、S04 producer 和 S03 未来 Inbox actions 接缝均未改。

## 先失败后修复的实际证据

| 阶段 | 结果与确切代码状态 |
| --- | --- |
| `first-fail.log` / artifacts | **1 FAIL**，产品是原固定 r6 `fd1c62513eaf8da2111edcfc5974798d1d7bca05`，仅添加新测试。真实 A→B、实际 browser context offline/online、两次 B/rev0；runtime connected=true，pages=0、reads=[]、posts=[]。A、B 都exit0。 |
| `reconnect-successor.log` / artifacts | **1 FAIL（焦点）**，产品是 r6 加当时一处 app.js equal-revision 补启动，shell 仍原 r6。已 pages=1、Models GET=1、POST=0，但新 MainBar 输入失焦。**不是原 fd1c625 的焦点执行结果**。 |
| `final.log` / artifacts | **18 PASS**，最终代码：startup4 + shell13 + restart1。新回归：0提前中央GET/POST；最终只挂最新Models一次、一个活跃nativeEventSource/一份MainBar；同两次B/rev0；新draft和焦点保持；两个真实child exit0。原entry/state双屏障、native panel Back在途、retired Run恢复、后续offline读取、hidden保护均保留。 |
| `typecheck.log`、`diff-check.log` | **PASS**。ruff/20资产HTTP/其他64回归按 r6 原执行点和未变责任范围复用，没有重复大包或合计为全量通过。 |

新的实际回归复用 `tests/browser/server.py`，临时 SQLite state 目录、离线 ScriptedModel；fixture准备阶段先创建一个真实持久 Session，浏览器观测阶段0 POST。屏障只延迟两份实际 entry response；Host/SSE、业务内容、凭据和 revision 未伪造。native EventSource 包装仅记录实例/修订和实例数。既有 Node22/Python3.14.7/Chromium环境，端口17830/17832；所有新进程正常关闭。

焦点失败阶段的三文件内容和 SHA-256 在 `focus-failure-source/`、`focus-failure-source-manifest.json`；差异 `focus-failure-source.diff`。内容从保全 r6 bytes 和实际执行过的单次 app.js patch 精确恢复。失败 trace 的测试源码逐字节匹配；Playwright 未保存 JS response body，明确不冒称独立 trace-body hash 证据（见 `evidence-tooling-note.txt`）。两项 FAIL 各自经 `candidate-transition-map.json` 绑定最终 successor。

## 交接身份与责任

- `repair.diff` 是 r6→r7 的三个文件修复；`candidate.diff` 是当前integration base→r7，含原r6。`r6-candidate.diff`、原r6三文件源码及8份保全项由 `r6-preservation-manifest.json` 核验；`startup-r6/` 未覆盖。
- `source-coverage.json` 保持完整164个source / 25个AC、冻结hash/owner，旧片段仍绑定原r5/r6日志。增加本轮同修订资格、首挂焦点片段，分别显式保留FAIL与后继。所有whole source/AC/G继续 **NOT RUN**，没有把18个通过提升为最终完整验收。
- `resource-inventory.json` 按当前HTML→JS imports→CSS url闭包校验20资源/42边、ASSETS登记及字节hash。没有新资源/新注册；安装后四组HTTP不在本轮。
- `evidence-validation.json` 校验exacthead/tree/base/repair-parent、两个diff、clean、完整责任身份、各资源hash、证据存在、原r6保全与中间失败身份。

未执行的完整仓库 gates、S03及其他页面组合、S11最终入口和安装矩阵保持原责任；原生200%/真实中文IME仍NOT RUN，实际BFCache仍BLOCKED。新修复只验证明确范围，独立复审结论仍待coordinator记录。
