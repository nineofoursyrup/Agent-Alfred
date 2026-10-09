# S01 r4 Spec review — 证据详情

## 候选与审查范围

- worktree：`/Users/nineofour/.codex/worktrees/dashboard-s01/Agent-Alfred`
- base：`7d87243ff715eaecc42159f35aa9f223c4ef8ce7`
- head：`5672fbb9e9aa13224bc1d9f07dcd736c033ae0df`
- tree：`fa4e825ca0895048626bc300fa584b0e9440abf7`
- 固定候选 18 个改动文件均与 `S01/candidate-files.json` 一致；按 `git show`／固定 diff 审查，未混入未提交文件。
- 完整来源入口和初次来源核验见 `SPEC-SOURCES.md`。本轮重新核对 S01 的 72 项来源 hash、19 项 AC 映射及 I00–I05；未读取 Standards／port-check 报告。HANDOFF 引述的另一轴发现仅作为已声明历史，不替代本轴独立判断。
- 审查包括期间完整集合／Ops 槽隔离、Memory revision／计数、Run 与聚合元数据、四类来源定位、MainBar 完整正文与用户 preview、既有 DTO 兼容性、读侧身份和失败边界。除下述问题，本轮未发现另外可证实的片内 Spec 缺口；这不等于全部消费者组合或最终门禁通过。

## F1 — [P2] 会话 Run 定位漏掉具有相同 revision 的目标

规范原文：

- `docs/design/dashboard-implementation/INTERFACES.md:95`：**“返回定位页包含目标及最多 limit−1 条最近的较新邻项，按该列表原顺序输出，并签发与普通读取兼容的下一段 cursor。”**
- 同文件 `:86`：新增定位与现有分页共享查询／脱敏／排序。
- `docs/design/issue-89/DESIGN.md:54`：**“同文档返回时重新核验来源，按其锚点恢复原条目及可用阅读位置”**。
- 同文件 `:239`：**“目标身份缺失、损坏或矛盾时明确不可用，不回退到其他 Run。”**

固定代码路径：

`GET /api/sessions/runs/locate` → `DashboardApi.shared_read` → Host 定位 → `src/agent_alfred/runtime/source_locations.py:144–191`。第 165–171 行正确按 `(activity_revision, run_id)` 选择目标之后至多 `limit−1` 个最近较新对象，第 172 行也保留了完整上界；但第 **173 行**生成 cursor 时改成 `(upper[0] + 1, "")`。随后第 174–181 行调用原 `runs.list_session_chat_runs`，其原降序 keyset 会把与上界相同 revision 的更大 ID 一并纳入，足以挤出目标。第 186–190 行仍然返回原目标身份和 `placement=page`，没有发现页内缺少目标。

相同 `activity_revision` 是既有排序合同覆盖的输入，不可凭单列唯一性排除：base 的 `src/agent_alfred/evals/deterministic/test_runtime_run_queries.py:88–106` 显式用 `(2,run-a)`、`(2,run-b)` 验证 ID 决胜与连续分页；普通查询本身也将 Run ID 作为第二排序键。

复现使用 [S01-r4-spec-repro.py](S01-r4-spec-repro.py)：`git archive` 固定 head 到临时目录，再导入该候选；使用项目既有 `ops_host` 和 ScriptedModel 创建隔离真实 Host／SQLite，添加三条同一 Session、`activity_revision=100` 的已完成 Run `tie-a`、`tie-b`、`tie-c`。通过真实 `DashboardApi.shared_read` 调用该端点，带正确实例与 Session。此复现是实际后端 API／数据库路径，未声称是 TCP HTTP 或 UI 组合测试。

命令：

```sh
/Users/nineofour/.codex/worktrees/dashboard-s01/Agent-Alfred/.venv/bin/python /Users/nineofour/.codex/dashboard-migration-20261009/reviews/S01-r4-spec-repro.py
```

原始执行结果保留在 [S01-r4-spec-repro.log](S01-r4-spec-repro.log)：

| 目标 | limit | HTTP 风格状态 | target.placement | 实际 runs | target_in_page |
| --- | --- | --- | --- | --- | --- |
| tie-a | 1 | 200 | page | tie-c | false |
| tie-a | 2 | 200 | page | tie-c, tie-b | false |

期望分别包含 `tie-a`，以及按原降序排列的 `tie-b,tie-a`；用户从来源返回时必须恢复原对象。复现另核对定位前后的 Host snapshot 和 model request 数量相同。没有产品改动、真实模型调用、服务切换或用户业务数据访问。

修复应保留完整排序上界的包含语义，并验证相同 revision 下的小 limit、目标包含、原降序以及 next_cursor 接普通页的无重复／遗漏行为。活动槽使用同一路径，也应让目标本身保持正确身份。无需重跑输入与环境仍适用的全部确定性检查。

## 检查与剩余责任

已阅读 `S01/HANDOFF.md`、`candidate-files.json`、`source-coverage.json` 及以下日志：`backend-r2.log` 678 passed、`browser-r1.log` 39 passed、`aggregation-locate-r3-successor.log` 425 passed、`anchor-domain-r4-successor.log` 306 passed、`ruff-r4.log`、`typecheck-r3.log`。继承的未变化行为检查与新增受影响检查分开记录，未累计为总验收测试数。r4 第一次真实 HTTP 锚点边界失败仍在 `anchor-domain-r4-first.log`；全 Python 记录为 824 passed、1 deselected 后中断，不是全量 PASS。

`source-coverage.json` 的全部实施者片内 PASS 是待独立评审的证据索引。本轮 F1 直接否定 I04 中会话 Run 定位的目标包含责任；应新增失败／修复证据链并绑定后继候选，不覆盖 r4 本报告或原始复现。S02／S03 的实际来源返回和 MainBar 消费者、S04 Overview，以及 S11 最终十页／309 项来源、AC／MCE／G、全门禁、wheel／sdist × base／mcp 安装后 HTTP、正常关闭／升级／支持目标回退、原生 200%／真实中文 IME 和旧 BFCache BLOCKED，不由 S01 本轮评审提升为 PASS。
