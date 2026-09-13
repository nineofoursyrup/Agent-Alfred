# Issue 21 MCP 实施证据

规范：`MCP-SPEC-r2`，以远端 r1 加本轮用户 D5 批准的 Draft-07 扩展为准；本地规范 hash 见候选 manifest，远端尚未更新。
比较基线与 HEAD：`35a053e5e793257187aea0a66a93fbc8de252dd0`。分支：`codex/21-mcp`。

## 实现

stdlib stdio 会话由 Host 构造与关闭路径持有。配置预检、完整发现、HMAC 能力身份、允许权限退役、三态授权和两账沿现有接口接通。Connections 的 apply/reconnect/cleanup 使用同一 MutationGate，HTTP 提前受理不会释放执行门禁。Schema 与结构化成功结果使用真实可终止校验进程；相同声明仅复用同字节、此前已验证的进程结果，不缓存超时。

未确认资源保持隔离；跨 Host 的旧 PID 只用于查询是否仍存在，从不据此杀进程。有效 env 变化同时停用旧 MCP，显式重连后才启动替代。

## 验证方法与 CE 映射

实施前已从确认 spec 保留 CE 编号、预期和公共接缝；按纵向分片保留 red/green 日志。以下为实际测试定位，不用计数替代完整合同。除模型 ScriptedModel 与外部 MCP stdio 夹具外，Host、Registry、授权存储、SQLite、HTTP、校验器、进程组和浏览器均真实运行。时序使用 FIFO、条件屏障、单调时钟与明确 OS 故障点。

| CE | 验证入口（test_mcp.py） | 证据范围 |
|---|---|---|
| CE-01 | `test_ce01_launch_permission_is_separate`；`test_ce01_unauthorized_real_host_invocation_sends_zero_requests` | enabled 三格；实际未授权调用零 tools/call；not_executed 两账；浏览器 |
| CE-02 | `test_ce02_ce10_replacement_retires_allowance_persistently`；`test_ce02_ce10_restart_reconciles_only_observed_sources` | 替换与重连、跨重启对账、未观察的 A→B→A、alias 路由 |
| CE-03 | `test_ce03_ce05_deadline_after_real_execution_stops_batch`；`test_ce03_late_success_after_cancel_never_restores_old_run` | 真实写入后 deadline、取消、迟到及重复成功不恢复旧 Run，后续调用停止 |
| CE-04 | `test_ce04_ce12_no_partial_directory`；`test_ce04_no_tools_capability_never_lists` | 无 tools capability、成功空目录与坏分页隔离 |
| CE-05 | `test_ce03_ce05_deadline_after_real_execution_stops_batch`；`test_ce05_ce12_stderr_partial_line_is_not_published` | 双向请求拒绝码、文本/非文本安全投影、浏览器脚本不执行、stderr 分片 |
| CE-06 | `test_ce06_real_host_process_shutdown_and_kill_limit`；`test_ce06_ce15_inherited_resource_is_never_killed_or_claimed_clean` | 子进程 Host 的正常/SIGINT/SIGTERM/异常/SIGKILL、孙进程与继承资源 |
| CE-07 | `test_ce07_actual_stdio_write_boundary` | 进入 callable 后写前耗尽 / 真实部分写入；传输未发送与业务 unknown 分账 |
| CE-08 | `test_ce08_schema_declaration_real_validator`；`test_ce08_schema_utf8_exact_limit`；`test_ce08_forbidden_reference_never_requests_network` | 真实可终止 Schema 校验、内部/外部/循环/方言、禁止网络、Schema 字节边界 |
| CE-09 | `test_ce09_invalid_configuration_never_partially_starts`；`test_ce09_stale_preview_and_duplicate_operation`；`test_ce09_config_utf8_exact_limit`；`test_ce09_server_count_exact_limit`；`test_ce09_absolute_python_environment_is_preserved`；`test_ce09_old_host_token_is_not_replayed_after_restart` | 整配置预检、重复 key、字段类型、过期 token、重放、重启、配置/服务器容量、venv |
| CE-10 | `test_ce02_ce10_replacement_retires_allowance_persistently`；`test_ce02_ce10_restart_reconciles_only_observed_sources`；`test_ce10_disabled_and_json_key_order_preserve_allowance`；`test_ce10_retirement_write_failure_does_not_open_new_generation` | allowed 退役与 denied 保留、disabled/reconnect/key-order、启动对账、持久失败 |
| CE-11 | `test_ce11_real_host_result_matrix`；`test_ce11_json_rpc_error_codes` | 成功/空/非文本/isError/坏结构、精确 RPC code、IC-01、费用 unknown |
| CE-12 | `test_ce12_frame_utf8_exact_boundary`；`test_ce04_ce12_no_partial_directory`；`test_ce12_discovery_counts_actual_utf8_frame_bytes`；`test_ce12_tool_count_exact_limit`；`test_ce12_alias_collision_routes_by_identity_not_order`；`test_ce05_ce12_stderr_partial_line_is_not_published`；`test_ce12_page_count_exact_limit`；`test_ce12_total_tool_count_exact_limit`；`test_ce12_stderr_flood_is_drained_and_bounded`；`test_ce12_protocol_depth_exact_boundary` | 分页、400/100/20/帧/深度边界、唯一 alias 正确路由、stderr 排空 |
| CE-13 | `test_ce13_real_validator_process_is_killed_before_replacement`；`test_ce13_discovery_validator_timeout_keeps_tool_visible_unavailable` | 真实验证进程 SIGSTOP 屏障与 kill/reap；发现不可用及调用后 unknown |
| CE-14 | `test_ce14_env_publish_stops_old_capability_without_spawn`；`test_ce14_process_environment_wins_without_restart` | dotenv 真发布、旧能力不可调用、零自动 spawn、进程 env 优先、双浏览器 |
| CE-15 | `test_ce15_incomplete_cleanup_keeps_ownership_and_replay_is_read_only`；`test_ce06_ce15_inherited_resource_is_never_killed_or_claimed_clean` | 真实信号失败、未完成 close、同 id 回读、继续清理、旧归属不扫杀 |

IC-01：`test_ic01_error_result_precedes_success_output_schema`；有效工具错误不套成功 Schema，成功缺失或不符则 unknown。

浏览器：`tests/browser/mcp.spec.js`，覆盖真实 MainBar/Tools/Connections/Ops 与双标签页 env/reconnect。进程夹具仅在隔离测试状态目录运行。

## 独立评审后的补充证据

c1 两轴确认缺陷已进入修复；当前候选须由相同两轴重新核验，不能沿用 c1 结论。所有补充用例位于 `src/agent_alfred/evals/deterministic/test_mcp_review.py`，以下保留固定发现编号：

| 发现 / CE | 回归入口 | 实际验证范围 |
|---|---|---|
| STD-01 / CE-06/13/15 | `test_std01_real_popen_return_interruption_is_owned` | 真实服务器与校验进程的 Popen 返回边界，KeyboardInterrupt/SystemExit；PID、pipes、持久清理记录 |
| STD-02 / CE-09/15 | `test_std02_native_control_thread_holds_gate_before_ident`；`test_std02_start_failure_without_native_effect_releases_gate` | native start-before-ident；Run/mutation 拒绝；同 id 回读；close 不假完成；未启动失败释放 gate |
| SPEC-01 / CE-09 | `test_spec01_ce09_invalid_startup_has_explicit_recovery`；浏览器 SPEC-01 | 坏 JSON/缺环境引用/错误命令修正后显式恢复；真实 HTTP stale token 零启动，刷新后成功 |
| SPEC-02 / CE-14 | `test_spec02_ce14_reconnect_only_selected_server` | 双服务器共享引用、不同引用、另一引用缺失；只启动选中的服务器 |
| SPEC-03 / CE-03/11 | `test_spec03_ce03_unknown_hides_from_next_real_model_request` | unknown 后新 Run 的模型目录隐藏，强行选择在 callable 前 not_executed；显式重连恢复 |
| SPEC-04 / CE-08 | `test_spec04_ce08_vocabulary_exact_support` | 标准 vocabulary、同前缀未知 URI、自定义 URI，required true/false；健康同服工具仍可用 |
| SPEC-05 / CE-02/10 | `test_ce10_complete_observation_retires_but_failure_preserves`；`test_ce10_key_change_never_inherits_allowance_and_recovers` | description/input/output 改变、完整缺项/删除与失败目录的退役区别、密钥失效/轮换及恢复 |
| SPEC-05 / CE-12 | `test_ce12_short_hash_collision_alias_shift_and_list_changed` | 强制真实 alias 短 hash 碰撞；新增碰撞者和反转顺序；身份授权保持、Registry 路由、list_changed 不自动发现 |
| SPEC-05 / CE-04/12 | `test_ce04_partial_server_with_descendant_does_not_disable_healthy_server`；`test_ce04_ce12_startup_deadlines_isolate_and_mark_unattempted` | B 坏分页/孙进程回收，A 继续真实调用；每服10秒/全体30秒及 not_attempted |
| SPEC-05 / CE-08/13 | `test_ce08_real_validator_file_reference_performs_zero_reads`；`test_ce13_unreaped_validator_prevents_replacement_job` | 实际校验器进程的文件读取审计（含正对照）；真实 SIGSTOP + kill 拒绝，未回收时禁止替代 |
| SPEC-05 / CE-09/14 | `test_ce14_environment_publication_failure_keeps_consistent_mcp`；浏览器迟到 HTTP 用例 | publish 后失败的完整回滚/回滚失败暂停；旧 HTTP 响应不得恢复页面能力 |
| SPEC-05 / CE-09/15 | `test_ce15_continue_cleanup_releases_gate_and_permits_core`；`test_ce09_ce15_operation_expiry_and_capacity_do_not_replay`；浏览器 pending HTTP 用例 | 清理失败后核心 Run、继续清理及重连；10分钟/256条淘汰；FIFO挂起 initialize、受理中同 id/不同 id |

环境发布补充回归先实际复现 row/env 未随 rollback 恢复，再补环境 checkpoint/restore；red 为 `env-rollback-red.log`。其他确认缺陷的 red/green 保存在 `standards-c1-red.log`、`standards-repair-green.log`、`spec-c1-red.log`、`spec-repair-green.log`、`spec-04-red.log` / `spec-04-green.log`。这些复现脚本原先用于诊断，退出码不能替代新增 pytest 断言。

## 第二轮交互修复

c2 全量 `4076 passed / 1 deselected`、浏览器177及四制品通过；两轴关闭 STD-01/02、SPEC-01–05，仍发现以下边界。它们保留独立编号并补实际 red/green：`c3-red.log` 的7个失败 → `c3-green.log` 的7个通过。

| 发现 | 回归入口（test_mcp_review.py） | 验证 |
|---|---|---|
| STD-03 | `test_std03_real_emfile_before_or_after_pipe_creation_recovers` | 真实 RLIMIT_NOFILE：首次管道前/第一对管道之后，服务器/校验器四格；确认无child的失败可清理，核心继续并可重连；保留STD-01不确定启动回归 |
| SPEC-07 | `test_spec07_ce09_invalid_then_identical_apply_keeps_healthy_pid` | 无效配置恢复原样再apply，健康PID/身份/授权保持 |
| SPEC-08 | `test_spec08_ce14_cleanup_completion_clears_residual_flag` | env发布后的真实清理失败，放行信号后继续清理；PID消失与公开残留标记同步 |
| SPEC-09 | `test_ce13_unreaped_validator_prevents_replacement_job` | kill持续受拒时真实Host.close返回false；解除故障后实际reap且close完成 |

## 门禁与证据位置

证据位于 `tmp/agent-work/issue-21/`；完整候选 manifest/diff 和两轴报告与门禁日志绑定，活动 LOOP 不纳入产品候选。

- 旧 c1：`full-final.log` 4040 passed / 1 deselected，`browser-final.log` 173 passed，制品四格通过；修复后只作历史，不能代替新候选门禁。
- 修复分片：`review-regressions.log`、`mcp-repair.log`、`browser-repair.log`、`final-additional.log`。
- 后继候选：`full-c3.log`、`browser-c3.log`、`ruff-c3.log`、`typecheck-c3.log`、`check-skills-c3.log`、`check-env-c3.log`；以日志实际终态为准。
- `artifacts-c3.json` / `artifacts-c3.log`：重新构建 wheel/sdist × base/mcp，四格隔离安装、真实核心 Run、配置与未配置路径；制品哈希单独记录。
- 本地 lint 显式排除非产品 `tmp` 评审/诊断脚本；CI 的干净 checkout 没有这些临时文件。

## 当前限制

Everything 已获用户“授权执行”。固定包 `2026.8.31` / SDK `1.30.0` / Node `25.8.2`、107 项完整 lock，保持第三方包原文。c3 实测先因 Draft-07 全部不可用；用户 D5 批准扩展后真实重验 13/13 可用，未授权 not_authorized / not_started，仅允许 echo 后实际一次 tools/call、文本匹配、费用 unknown，SQLite/Ops 与关闭资源均留证于 `everything-c4/`。

当前主机为 macOS。Linux 生命周期门禁已加入既有 Linux CI，但本地执行不能冒充 Linux CI 结果；提交/推送/远端 CI 尚未获授权。

本票不承诺 Windows MCP 生命周期，也不承诺 Host SIGKILL、掉电或进程组逃逸后无条件清理。Schema 首版只支持规范已确认的受限 Draft-07 / 2020-12 引用范围。

主工作树原有 `.gitignore` 修改未带入；本工作树尚未提交、推送、合并或关闭 Issue。技术验收仍需同候选双轴结果及全部必需证据。

## D5 方言扩展及互操作修复

`schema_worker.py` 按原始 `$schema` 分派 metaschema、实例校验器与引用资源语义，缺省仍为 2020-12；不改写声明或业务参数。嵌套不同方言仍明确不可用；既有禁外部读取/循环/动态引用和可终止进程边界保持。

CE-08 / CE-11 / IC-01 新证据：`test_mcp_dialects.py` 的 26 格覆盖两方言 URI、tuple items、$ref sibling、definitions/$defs、原文模型透传、未满足 input required 仍实际发送、成功输出 mismatch 隔离、正常 isError 优先；两方言分别扩展网络零请求及文件读取正对照的真实校验器观测。修正测试对现有不可变 Mapping 的比较后，旧实现 `dialect-corrected-red.log` 为 6 failed / 20 passed（仅 Draft-07 缺失），修复后 `dialect-green.log` 26 passed。初始 `dialect-red.log` 包含测试预期容器类型错误，不作为纯产品 red。

c4 门禁日志：`full-c4.log`、`browser-c4.log`、`ruff-c4.log`、`typecheck-c4.log`、`check-skills-c4.log`、`check-env-c4.log`、`artifacts-c4.json`。实际结果及双方评审由同目录 `gate-status-c4.json` 绑定最终 manifest；本文不预先声称进行中的门禁通过。Linux 仍缺执行环境和实测证据。
