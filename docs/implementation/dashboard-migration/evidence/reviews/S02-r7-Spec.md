# S02 startup-r7 独立 Spec 增量复审

**PASS（本轮启动／首次焦点修复范围）；无新增 finding。原 S02-r6-Spec F1 在 r7 关闭，原 FAIL 保留。**

- base：`ddefb65905e4792559e8ff4d130dd427b34cd137`
- repair parent：`fd1c62513eaf8da2111edcfc5974798d1d7bca05`
- head：`0af962fbc4fc799806ba2a27593102c3df8c9e48`
- tree：`745d8609cc61d3b171da96aa250c26303fa2227e`

复核完整三文件增量及相关调用链。#87 `SPEC.md:31` 要求“首次加载仍先取得现有入口凭据和权威状态”，I06 `INTERFACES.md:145` 要求“首次壳层同步取得入口凭据及 Host 状态后再读页面”。`app.js:436,450–454` 在当前实例、重连首状态、非负已接受 revision、严格相等、匹配凭据成立后补调原 `start()`；不重新应用旧 state，`shell.js:269` 的 `!navigating`／单次挂载仍有效。

#87 `SPEC.md:180` 要求异步结束保持用户已转移的焦点；`shell.js:172–178` 仅对首次挂载保护 MainBar 已有焦点，正常已挂页导航不变。现有真实回归验证等待期间新草稿与焦点保留。

已核验两份 FAIL→最终 **18 PASS**（startup 4／shell 13／restart 1）、typecheck 和差异检查；未把旧 64 项／20 资产检查叠加成全量 PASS。首次失败对应固定 r6；焦点失败对应 r6＋中间 app 补丁，其测试源码与失败 trace 字节一致，但无独立 JS response-body hash。

20 份原规范字节一致；164 source／25 AC 身份、hash、owner 与 20 资源／42 边闭包核验通过。复用未变责任，不重跑无新疑点的确定性检查。未读取本轮 Standards 结论，未修改产品。

整体 source／AC／G、S11 组合及安装矩阵仍 **NOT RUN**；原生 200%／真实中文 IME **NOT RUN**，实际 BFCache **BLOCKED**。详见 [证据与适用性](/Users/nineofour/.codex/dashboard-migration-20261009/reviews/S02-r7-Spec-details.md) 和 [独立核验结果](/Users/nineofour/.codex/dashboard-migration-20261009/reviews/S02-r7-Spec-verification.json)。
