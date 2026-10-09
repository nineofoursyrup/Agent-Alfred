# S02 r8 独立 Spec 窄增量评审

**PASS；0 findings。新增回归符合冻结要求，没有发现本轮缺失、错误实现或越界行为。**

- base：`bba439d7faeedde71970a74cea146a5e896e5345`
- head：`552ab48fc1d757ea881107827e583d592a5403b1`
- tree：`e4fa378f6e161834d528769865ab97ee1bc4517f`
- 工作树：`/Users/nineofour/.codex/worktrees/dashboard-s02/Agent-Alfred`，clean。
- 唯一差异：`tests/browser/shell-startup.spec.js:9–37`，+30 行；[固定 diff](/Users/nineofour/.codex/dashboard-migration-20261009/S02/ci-breakpoint-r8/candidate.diff) SHA256 `ca08f1b12be2643e737f5bb53f0b1a822820c57d55acc4ba9587f2f16d63b5f1`。

规范 #87 `SPEC.md:101` 要求“在主对话编辑时保持主对话可见”，`:183` 要求不得“清空输入、丢失光标选择或把焦点留在隐藏节点”；实现规格 `VALIDATION.md:7` 允许控制“HTTP／SSE 到达顺序”。新增用例仅暂停真实 SSE 请求，显式新建后填写草稿和选区，等待真实首态触发 Inbox 挂载，再用真实 viewport 触发原生 matchMedia；未替换 Host、状态分类、焦点处理或生产 DOM。它断言输入、焦点、选区和宽屏偏好保持。

独立核验三组 trace：CI 实际 checkout `f86c579…` 与单独撤去 r7 首挂保护的副本均 **FAIL**（`message → H1 → hidden`）；原样 r7 **PASS**。最终同生产代码、同测试文件的 startup 5＋shell 13 共 **18 PASS**，覆盖这条独立交错；原测试未放宽。R01／I06 的入口凭据及首次权威状态屏障、I07–I09 和资源字节仍沿已审 r7。

1210 个导出文件、30 保全副本、17 原 CI artifact、冻结 R02／R10 身份及 20 资源核验通过。原 Linux CI **FAIL** 保留；本地因果对照不是云端 activeElement 实测或稳定性证明。最终 CI、whole source／AC／G、原生 200%／真实中文 IME及安装矩阵仍 **NOT RUN**；实际 BFCache **BLOCKED**。

复用仍适用检查，未另跑产品测试，未读取本轮 Standards 结论、修改产品或发布。详见 [证据与适用性](/Users/nineofour/.codex/dashboard-migration-20261009/reviews/S02-r8-Spec-details.md)、[独立核验结果](/Users/nineofour/.codex/dashboard-migration-20261009/reviews/S02-r8-Spec-verification.json)。
