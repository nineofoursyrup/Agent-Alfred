# S01 r5 — Spec 增量复核

**结论：PASS（S01 后端片内 Spec）；r4 的 P2 F1 已修复，本轮无新增发现。整体 source／AC／G 仍未验收。**

固定 base `7d87243ff715eaecc42159f35aa9f223c4ef8ce7` → head `3c0cd57edfb9f67e78130ca8ed94c6dbb24fda5a`，tree `0472b276c7d7520c006ebb2d3f4249a4df66a7a3`。核对 tree、18 文件 SHA-256 manifest；r4→r5 只有 `runtime/source_locations.py` 与 `evals/deterministic/test_dashboard_shared_reads.py`，68 增／4 删。72 项来源定义／hash／归属和 19 项 AC 映射与已独立审查的 r4 一致，沿用未变审查，不读取 Standards 结论。

**F1 修复核验：** `src/agent_alfred/runtime/source_locations.py:165–188` 多取一条真实较新复合 key 作为排他边界，保留完整 `(activity_revision, run_id)`，消除 revision 加一对同 revision 大 ID 的重新纳入。邻项不足时 `page_limit=min(limit,len(newer)+1)` 使定位窗口止于目标。与原 `runtime/runs.py:1111–1164` 的相同过滤、降序复合 key、目标后普通 cursor 合同相符；活动槽仍只返回目标并清除历史 cursor。满足冻结 `INTERFACES.md:95` 的“包含目标及最多 limit−1 条最近的较新邻项”，以及 `issue-89/DESIGN.md:54,239` 的原条目恢复要求。

已核对新增行为测试：真实隔离 Host／SQLite、同／不同 revision、三个目标各 `limit=1/2`、目标身份与顺序、普通 cursor 全部后续记录无重复／遗漏、Host snapshot／模型请求数量不变。`composite-location-r5-first.log` 保留 **2 FAIL**，`composite-location-r5-successor.log` 为 **264 PASS**；`ruff-r5.log` PASS，独立固定差异 `git diff --check` PASS。本轮复用这些已通过且适用的确定性检查，未再广测。

本结论关闭 r5 对应缺口，不改写 [r4 FAIL](S01-r4-Spec.md) 或[原始复现](S01-r4-spec-repro.log)。原后端／浏览器等未变证据继续适用；全 Python 中断记录不升级为 PASS。S02／S03／S04 实际消费者组合、S11 十页／309 源项、最终全门禁、安装后 HTTP、原生 200%／IME、正常关闭／升级／回退与历史 BFCache BLOCKED 继续保留。未修改产品、未执行合并或远端写入。
