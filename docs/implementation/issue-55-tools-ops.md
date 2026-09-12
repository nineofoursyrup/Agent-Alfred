# Issue 55：调用工具并看账

施工规格为 [Issue #55](https://github.com/nineofoursyrup/Agent-Alfred/issues/55)，
Q1–Q28 沿用已确认设计，完整验收文字见
[A01–A36](../design/issue-32-tools-ops-acceptance.md)。
生产 Host 只注册现有内置本地工具；测试通过 `extra_tools` 注入外部能力，
这些测试不代表 Tavily 或 MCP 的实际交付。

## 实现与所有权

- MainBar 保留唯一自然语言执行入口。Tools 从 Host 的实际 ToolRegistry 展示能力、
  可用性、外部连接证据、授权与模型暴露，管理隐藏能力时不增加直接执行表单。
- `ToolAuthorization` 独立保存 `tool_authorizations.json`。共享原子配置写入组件负责
  文件指纹、唯一临时文件、0600 权限、fsync 和替换；整配置 revision 与磁盘指纹同时
  拒绝冲突。保存和重新应用均经过 Host mutation gate。发布异常会禁用旧 registry
  和可能已发布的候选，保留“已保存、尚未生效”。坏文件保持原字节，新 Run 不沿用
  无法核实的旧外部授权。本地聊天仍可使用。磁盘外部有效变更保留运行时版本，明确
  展示磁盘值并要求核对文件、重启加载，绝不静默覆盖。
  当前目录读取也核验文件；坏文件立即显示不可读和待核验。在途 Run 的 registry 与
  Schema 保持不变，当前目录不宣称外部授权已生效；空闲核对或下一次准入暂停 external。
- 迁移 17 追加 `tool_metering`、`tool_metering_health`、
  `tool_operation_verifications`，不修改已发布迁移 DDL。
  请求以 Run/Step/call_id 唯一登记，来源与能力身份取自宿主注册，计量不保存正文或参数。
  全批请求先持久登记，再执行。持久意图只证明准备启动；确认进入工具体后才可记为
  已确认启动，无法落定的窗口保持启动未确认。登记失败禁止执行，收尾故障阻止后续工具
  和模型并关闭新 Run 准入；恢复会验证登记和各类收尾写入，再解除阻断，不调用工具体。
  原 Run 仍持有 recording_pending 租约时保留 409 run_in_progress；落定后计量未恢复
  才返回 503 recording_unavailable，不能用计量故障覆盖已有忙态。
- 本地 SQL 业务与计量成功记录借用同一调用方事务。文件持久准备同时绑定原请求与
  operation_id；后续核验保存在独立记录中，带相关恢复 Run，不覆写原观测、不重复计费。
  业务恢复继续使用已有 FileTools 协议，不增加通用重试或取消入口。
- `AccountingSnapshots` 在短 SQLite 读事务中固定完整匹配 Run 集合、持久计量与
  trace 裁剪事实；读取后释放事务，以当次覆盖价、目录价和静态价格快照计算模型费用。
  exact/estimated/unknown 分开，工具按服务和单位分组。合法端点报告金额无需猜模型身份。
  token 零与缺失、旧记录缺口、进行中与记录失败分别表达。
  单条时间损坏不阻断其他记录：全部历史保留该行并标时间缺失；时间筛选无法判定的
  Run 单独列为成员覆盖缺口，不计入所示范围汇总，也不冒称该范围完整。
- 快照保存无正文的 JSON 字节，返回值重新解码，不能经调用者修改。默认 15 分钟、
  最多 8 份、总计 64 MiB；超额拒绝整次创建，不截断后声称全量。分页每页 50 个 Run，
  明细和 Run 页面链接携带同一 snapshot_id；到期或重启返回 410。刷新失败保留旧内容。
- `ToolHistory` 只从当前受管 trace 读取当次模型投影、完整脱敏审计或准确配对的已提交
  Attempt 参数。校验目录、meta、Run、事件顺序、工具身份、工件文件名、大小及哈希，
  拒绝符号链接和替换。签名游标绑定当前文件身份及完整校验值；每段最多 256 KiB，
  UTF-8 不拆字符，浏览器只保留当前段并以纯文本显示。超过 64 MiB 的 trace 返回明确
  资源限制，不伪装成完整历史。历史读取不写入记忆、搜索索引或计量正文库。
- 正文读取持有与 prune 事实相同的数据库锁，文件能力同时校验外部替换。清理失败的
  所有权由 Host 保留以便重试；HTTP 请求排空后才能关闭宿主资源。离页取消请求并清理
  正文；断线保留当前段、禁用续读，重连重新核验，裁剪后清空。响应使用 no-store。
  已保存 model_content 始终不被解释为模型实际收到；可证实的停止标为未发送。
  计量故障时同时在 Run 终态 telemetry 留下未发送的 Step，工具行暂时不可写时由
  持久恢复校验补齐；该依据不依赖 trace 正文。

## 验收映射

以下测试名均位于 `src/agent_alfred/evals/deterministic`；浏览器文件位于
`tests/browser/accounting.spec.js`。表中列出定向证据，最终全量结果、候选哈希和独立
评审记录在工作区 `tmp/issue-55/`；该目录不进入发行包。

| 验收 | 实现边界 | 定向验证证据 |
| --- | --- | --- |
| A01 | MainBar / Registry / Tools / Ops | browser：MainBar to real Tools；`test_ops_metering` 的 read/write/rejected |
| A02 | Host 实际目录与隐藏工具 | browser 主路径；`test_catalog_includes_hidden_external_with_stable_identity` |
| A03 | Schema 与执行授权同源 | `test_external_matrix_uses_same_schema_and_execution_policy` 六格 |
| A04 | Host mutation gate | browser 两标签 busy；`test_active_run_exposes_coverage_without_guessing_attempt_count`；`test_metering_failure_preserves_busy_until_recording_settles` 验证 409 → 503 → 恢复准入 |
| A05 | 整配置版本、磁盘指纹、草稿 | browser 两标签；`test_disk_conflict_preserves_bytes_and_applied_policy` |
| A06 | 回执与回读分离、连接代次 | browser lost receipt、late successful authorization 两例 |
| A07 | 不可读授权与本地可用性 | `test_unreadable_authorization_keeps_bytes_and_local_chat`；`test_authorization_corrupted_after_load_cannot_keep_old_permission`；`test_current_catalog_reports_bad_authorization_without_switching_active_run`；browser focus/reconnect 两例 |
| A08 | 保存后应用失败、显式恢复 | browser apply-fail/reapply；`test_publication_receipt_failure_blocks_candidate_too` |
| A09 | 来源加能力稳定身份 | `test_stable_capability_rename_and_different_source_do_not_reassign_history` |
| A10 | 批次登记、各 effect / 拒绝 / 未执行 | `test_public_run_records_read_write_and_rejected_requests_without_bodies`（无 trace）；授权矩阵；既有删除批次停止回归 |
| A11 | 唯一已提交调用身份 | `test_duplicate_call_identity_stops_before_dispatch_and_new_identity_is_new_action`；`test_tool_acceptance` 幂等回读 |
| A12 | 计量故障阻断后续执行 | `test_metering_failure_stops_effects_and_next_model_and_requires_recovery`；`test_atomic_success_only_fault_stops_subsequent_model` |
| A13 | 中断窗口与启动证据 | `test_restart_keeps_only_provable_start_facts` 三窗口；控制异常；intent 写入耗尽预算 |
| A14 | 调用方事务、业务恢复 | atomic calendar rollback；`test_tool_recovery_edges` caller transaction；`test_runtime_tools` memory 单账与回滚 |
| A15 | 计量健康恢复与只读 | metering failure 两阶段测试在故障仍存在时恢复被拒，修复后通过且不调用模型 |
| A16 | 原观测和当前核验分离 | `test_interrupted_file_request_retains_independent_recovery_link` |
| A17 | 模型逐 Attempt / 逐维费用 | `test_mixed_attempt_costs_and_prices_stay_frozen`；既有 `test_pricing` |
| A18 | 工具计量闭合与服务/单位分组 | `test_two_services_credits_unknown_and_local_free_are_separate`；非法布尔计量测试 |
| A19 | trace 与账务独立 | `test_parameters_are_committed_redacted_and_prune_retains_accounting`；伪造工件拒绝 |
| A20 | 进行中、保存与未知覆盖 | active Run 测试；legacy bad row 测试；既有 recording_failed 公共路径回归 |
| A21 | 单行隔离、整体读取失败 | `test_legacy_bad_row_is_isolated_and_exact_cost_without_model_survives`；`test_database_read_failure_is_not_empty_accounting`；upgrade-flow browser |
| A22 | 精确报告与身份缺失 | legacy exact-without-model；混合 Attempt；空覆盖与真实系统命令回归 |
| A23 | 同一不可变账目快照 | prices frozen；snapshot new records；browser refresh/page race；Run 链接携 snapshot 与规范化筛选，失效后显式刷新 |
| A24 | 到期、刷新、重启边界 | quota/expiry 测试；browser expire/offline/prune 与 expired Ops to Run 回归；process 绑定与签名游标 |
| A25 | 有界快照、无正文、释放事务 | `test_snapshot_size_refusal_releases_transaction_and_never_publishes_partial`；8 份配额测试；body-free metering |
| A26 | 自然日与 IANA 时区 | `test_custom_calendar_boundaries_cover_dst` 春秋半开区间、非法时区；既有时钟测试 |
| A27 | 完整 Run、全局与用途筛选 | `test_global_filters_do_not_change_session_and_include_system_runs`；browser Tools 到 Ops 保留同 Run 的 query_events |
| A28 | 当次保存投影与实际发送区分 | long artifact 的截断投影；prune；删除/预算/计量停止回归及 model_delivery 字段 |
| A29 | 唯一 committed 参数与脱敏 | real Adapter + local transport 参数测试；aborted/duplicate/cross_run 参数拒绝 |
| A30 | 纯文本、UTF-8 分段 | `test_long_artifact_segments_exact_utf8_and_replacement_rejected` 包含 HTML 字符串及跨段多字节 |
| A31 | 受管文件身份与 prune 协调 | forged artifact、symlink、metadata、cross_run、同字节文件替换；prune 后读取拒绝 |
| A32 | 请求排空、失败清理、离页 | `test_http_close_drains_inflight_history_before_host_resources`；`test_history_failed_close_remains_owned_until_host_close`；browser 离页 |
| A33 | 离线副本、重连核验、无缓存 | browser offline/reconnect/prune；HTTP history drain 中 no-store 断言 |
| A34 | 人工历史与遗忘自动复用边界 | `test_manual_predelete_trace_survives_but_deleted_memory_is_not_reused`；既有 forgetting/working-memory 全量回归 |
| A35 | 结果未知与未执行说明 | metering failures、control、file verification；Tools 无执行按钮；Ops 无隐式重试 |
| A36 | 既有功能与安装回归 | 全量离线 pytest、全量 Playwright、typecheck、Ruff、skills/env、wheel/sdist 隔离安装 |

## 验证与复核流程

开发中按公共行为先复现失败，再修改实现；首轮评审报告和 red 日志保留，避免以内部
通过代替验收。首轮 Standards 的三个问题（恢复过早解锁、intent 后预算、发布后异常）
和 Spec 的四个问题（原子计量失败后继续模型、文件恢复关联、分页混快照、迟到授权回执）
均有对应修复及回归。
第二轮继续修复控制异常后的授权阻断、统一 503 映射、计量故障后的未发送依据、
损坏时间行隔离和重连后的核验按钮。旧 Run 落定故障测试的 SQL 注入收窄到 `UPDATE RUNS`，
避免新计量自检提前触发不相关故障，同时保留原有落定失败断言。
第三轮补齐预算耗尽、整体期限与删除边界的相同规则：先保留当前待发送批次的停止事实，
再尝试计量写入；即使该写入失败，恢复后也能标明未发送，且不误标已用于前序模型请求的批次。

协调侧独立复审确认 v4 仍有三个 P2，原 ready 结论已撤回。v5 修复计量故障与忙态的
判断顺序、授权损坏的当前读取证据，以及 Ops → Run 的快照失效说明。Run 链接携带
服务端规范化筛选，日期固定为原快照的当地半开区间，避免跨日后扩大或改变范围；
失效不等于 trace 不可读，返回账本后明确等待用户刷新，不偷偷创建新快照，不采用
尚未提交的筛选草稿。三项原始反例保留在独立评审目录，成功断言与 red/green 日志
位于 `tmp/issue-55/repair-001/`，协调侧负责后继候选的最终双轴验收。

最终门禁命令：`uv run ruff check`、`uv run python scripts/check_skills.py`、
`uv run python scripts/check_env_example.py`、`uv run pytest`、`npm run typecheck`、
`npm run test:browser`、`uv build`。另用锁定运行时依赖分别安装 wheel 和 sdist 到新的
虚拟环境，从临时工作目录通过 `python -I scripts/check_installed_tools.py` 检查实际安装包。

固定基线与未提交 HEAD：`c002a963676d5d26526c24be882c1dfc2a0af4ce`。
工作区 `/Users/nineofour/Agent-Alfred-issue-55`，分支 `codex/55-tools-ops`。
每轮 `tmp/issue-55/review-vN/manifest.json` 包含 staged、unstaged 和纳入 untracked
文件的 SHA-256、基线、HEAD、范围、验证日志位置；每轮双评审前后核验相同字节。
日志状态与最终结论单独写入该轮证据，避免验证过程修改冻结候选。

未执行真实在线模型、外部 Tavily/MCP 服务、真实用户数据库迁移，以及 commit、push、PR、
merge、发布或关闭 Issue；这些均在本次授权范围之外。本地迁移、HTTP/SSE 和业务验证
只使用临时真实数据库、受管目录和离线模型/transport。
