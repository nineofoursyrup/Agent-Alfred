# S05 r5 — repeated reload failure repaired

已定位并修复 `S05-r4-static-reload-repeated-failure`：原 listener 继承 backlog5，reload 新连接突发在 accept 前被重置，app.js未加载。真实 NetLog 将失败定位到全新 TCP_CONNECT；仅改变 backlog 的对照、受控实际 listener 和回归共同确认原因。生产改为平台 `socket.SOMAXCONN`（本机128），不改准入/guard/重试/timeout/资源关闭所有权。详见DIAGNOSIS.md。

## 固定候选

- Worktree：`/Users/nineofour/.codex/worktrees/dashboard-s05/Agent-Alfred`；branch `codex/dashboard-s05`，clean。
- Accepted base：`5650d23dcf648285aacecc099bb2538fa92089d1`；tree `f9774fff7d404d02453eadb046ea9e1082ed8d9a`。
- Head：`3d2382c71574695838d3b29b36cde8f47d64a541`；tree `20821980c0bb7977c157416a52db5ac80327b307`。
- 窄修提交 `a476f37`；此前固定r4 `a2594b6`及全部证据保留。

相对accepted base的完整8文件见candidate-files.json/candidate.diff：原S05六文件＋lifecycle.py六行＋真实listener burst回归68行。本轮Memory产品与用例保持r4原bytes；新shared补丁由root明确指定S05独占实施。已接受S10的10文件同步与本轮2文件分开列于r4-delta.json。CSS插入冲突原文留存，Database+Memory links/ASSETS联合完整。

## 证据

- 固定r4隔离诊断原测试：第一批5 PASS；第二批9 PASS/1 FAIL。失败发生于新TCP连接、errno54，不是旧keep-alive读取。只改backlog128后10/10 PASS。
- 受控真实accept队列：backlog5仅5/13取得实际app.js200，8连接断裂；128则13/13真实200。正式回归真RED **1 FAIL**→GREEN **1 PASS**。
- 最终candidate无诊断hook：原R10/R14 **20/20 PASS**；listener/启动/关闭/loopback六套 **230 PASS**；启动、Memory、Behaviour/Run、SSE组合 **16 PASS**。20次是一个场景的有界样本，总共17个不同浏览器场景。
- **32资源/77引用**独立闭包；**36实际HTTP GET PASS**覆盖完整资源和4Memory路由，无HTTP继承。typecheck/Ruff/diff check PASS。
- 正常关闭最终fixture，17940–17949全部bind+close释放；生产src/tests无诊断hook。

原r4首次FAIL、单次PASS、root重复4/5、仪器化FAIL、回归RED全部保存。首版一次性probe漏捕BrokenPipe而跳过自身close，已留存脚本和stderr并仅终止其PID；改正后的probe均normal-close。这项工具错误与生产缺陷分别记录，不隐藏。checks.json绑定命令、候选和日志SHA；check-applicability.md列出复用条件，没有全量重跑80/326或宣称全局稳定。

## 交接边界

source-coverage.json完整保留54 source/24AC identity、owner、hash、全原文精确引用及新增shared补丁责任，whole一律NOT RUN。原r1/r2/r3/r4和review证据字节经manifest核验，完整性PASS不替代评审。

请独立Standards/Spec增量同时覆盖历史S05修复与新listener补丁。原ST-01、Spec-F1和重复reload失败未由实施者关闭。最终G01–G08、完整CI/4安装、native200%/realIME仍属root/S11；BFCache历史BLOCKED、真实iPhone/iOS/移动键盘排除边界保留。未push、改tracker/ledger/GitHub、碰integration工作树/main或调用真实provider。
