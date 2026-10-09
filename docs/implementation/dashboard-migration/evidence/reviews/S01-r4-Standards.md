# S01 r4 — 独立 Standards review

**结论：PASS；0项硬违规，1项未变的可选P3启发式。** 本结论限S01固定候选，不代表Spec、跨片组合或最终产品验收。

- base：`7d87243ff715eaecc42159f35aa9f223c4ef8ce7`
- head：`5672fbb9e9aa13224bc1d9f07dcd736c033ae0df`
- tree：`fa4e825ca0895048626bc300fa584b0e9440abf7`

完整范围18文件；复用[r2评审](S01-r2-Standards.md)未变部分，新增审阅r2→r4的3文件差异：`replies.py`、`source_locations.py`及`test_dashboard_shared_reads.py`。18文件manifest与固定head全部吻合。

## Documented hard violations

无剩余项。原P2已修复：[source_locations.py:64](/Users/nineofour/.codex/worktrees/dashboard-s01/Agent-Alfred/src/agent_alfred/runtime/source_locations.py:64) 复用 `parse_cursor_position_int`，在数据库绑定前拒绝超域、bool和零，并将失败映射为 `InvalidAnchor`，符合 [I04:99](/Users/nineofour/.codex/worktrees/dashboard-s01/Agent-Alfred/docs/design/dashboard-implementation/INTERFACES.md:99)。真实HTTP回归保留最大合法整数为404；原500首次FAIL和[独立复现](S01-r2-anchor-repro.json)均保留。

聚合定位新增字段来自同身份Host投影或既有持久收尾元数据，复用 `aggregation_metadata`；输出经过中央Redactor，异常转为安全不可用，chat为null。pending／recorded仍独立，没有由正文或Graph结果推导保存成功，符合ADR-0003／0024／0029。

## Optional heuristic smell

**possible Duplicated Code（P3，非阻塞）**仍在 [runs.py:1176](/Users/nineofour/.codex/worktrees/dashboard-s01/Agent-Alfred/src/agent_alfred/runtime/runs.py:1176) 与 [api.py:1047](/Users/nineofour/.codex/worktrees/dashboard-s01/Agent-Alfred/src/agent_alfred/gateway/web/api.py:1047)：`for key in ("purpose", "filter", "purpose_known", …)` 与 `key: _run_json(run)[key] for key in ("purpose", "purpose_known", "filter", …)` 重复元数据白名单。可用共同命名投影收敛；不要求本次修复。

## 检查适用性

读取并复用聚合修复425 PASS、锚点修复306 PASS及各自首次FAIL；未变backend678／旧browser39与适用Ruff/typecheck继续有效，不累计重叠数量。未重跑广测。完整Python此前中止，仍NOT RUN；最终门禁归S11。未修改产品，未读取Spec结论。

汇总：Standards **0项硬违规；1项可选P3**。Spec未评判。
