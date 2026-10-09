# S01 CI repair r6

**实现性质：测试夹具及获准增量字段的兼容断言更新。没有生产收尾缺陷修复，也没有放松坏收尾保护。**

## 固定候选

- worktree：`/Users/nineofour/.codex/worktrees/dashboard-s01/Agent-Alfred`
- branch：`codex/dashboard-s01`
- base / 同步 integration：`e40f006b54817380807fe62372374c0013ce2398`
- head：`7f4bce39e452762dcbbb1e97cce566a85ab029b9`
- tree：`5597c29efc6d1abf934c7bb4757b9ce7a061882f`
- 相对 base 仅 3 个测试文件，44 additions / 7 deletions；`candidate-files.json` 固定文件 hash。
- r5 原 `../HANDOFF.md`、`../source-coverage.json`、`../candidate-files.json` 原样保留；本轮 REPORT 记录其 hash。合入 integration 为 clean fast-forward，没有冲突，见 `merge.log`。

## 首次失败与诊断

首轮 PR CI `37841021392`：6400 PASS / 8 FAIL，22 skipped / 1 deselected，原日志 `../../ci/initial/37841021392-first-fail.log` 保留。合入 S02 后首次本地复现仍为 **8 FAIL / 1 PASS**，`local-first.log`。

1. `test_failed_run_keeps_recorded_accounting_and_string_terminal_error`：实际本地 SQLite 证据 `failed-fixture-telemetry.json` 显示两次模型调用共用 `failed-attempt` 身份。共享收尾判据因此正确拒绝确认记录，S02 的前端 recording guard 不会改变这一后端输入。修复夹具逐次分配 `failed-attempt-1/2`，明确核对两次调用、两个不同 Attempt、两个 aborted 和各自 7 output tokens；recorded / trace / terminal-error 原断言保留。`runtime/telemetry.py` 与所有生产文件均未改。
2. `test_routing_baseline.py`：disabled / explicit / automatic / command / recovery / http 六模式只因已获准 `message_anchor` 新增字段不同。测试读取当前 fixture 的实际 SQLite 行，逐项核对 anchor 的版本、kind、Session、分段和持久行 ID，并核对消息 Run ID / role；仅在这些语义证明成立后移除这个字段，剩余全部旧结果、请求、事件、消息及引用仍沿原精确比较。没有通用忽略新增字段、更新整个黄金样本或放松业务比较。probe 零消息路径仍保留。
3. `test_the_mainbar_targets_the_requested_session`：内存 facade 的旧 historic DTO 没有持久行锚点，严格期望中增量明确 `message_anchor: null`；所有旧字段比较保持。

## 验证

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 首次本地重现 | FAIL | `local-first.log`：8 failed / 1 passed，9.82s |
| 原失败集合修复 | PASS | `targeted-successor.log`：9 passed，10.60s |
| 三个完整受影响文件＋坏摘要保护 | PASS | `scoped-checks.log`：92 passed，15.24s |
| Ruff | PASS | `ruff-first.log` |
| git diff --check | PASS | 固定候选提交前执行 |
| 完整 Python / PR CI | NOT RUN | 由协调器集成后重新绑定 CI；不覆盖首次失败 |
| 独立 Standards / Spec | NOT RUN | 协调器安排，不以实施者判断代替 |

Scoped 命令：

```sh
uv run pytest src/agent_alfred/evals/deterministic/test_dashboard_evidence.py src/agent_alfred/evals/deterministic/test_routing_baseline.py src/agent_alfred/evals/deterministic/test_web_api.py src/agent_alfred/evals/deterministic/test_dashboard_shared_reads.py::test_run_summaries_gate_input_and_preserve_source_filter -q
uv run ruff check
git diff --check
```

Python 3.14.7、锁定的现有 dev / mcp 环境；隔离 pytest 状态与真实 loopback HTTP，模型仅 fixture。未改依赖、JS / CSS / HTML、HTTP 生产实现、合同或 schema；没有新增资源清单。原 72 源项 / 19 AC 的组合责任保持，本次局部通过不提升整体 AC / G 状态。

等待双轴评审、merger 串行整合及 CI successor；没有 push、tracker 修改或自启动 review。
