# Coverage 审计最终补充（2026-09-20）

逐条清单：[coverage-inventory.json](coverage-inventory.json)。SHA256 `a6bef34695eb7323491f1997e2622e2fea073f79d02a02a52bdf57414d163fb7`。

覆盖27票，共1614行，全部 `current_status=NOT RUN`；321行有具体 `test_selectors`，1293行仍缺可确认的精确测试绑定。本审计 **INCOMPLETE**：没有把主题相似测试当作完整义务通过。`test_candidates` 仅辅助定位，`test_mapping.covered_subclauses` 说明具体断言覆盖；复合条款其余子条件仍有缺口。#16 V01–V19逐项给出了现成断言与剩余条件；#36补了真实浏览器入口。#84 AC01–22新增引用主代理矩阵，核对选择器/断言存在，固定候选评审及执行仍 NOT RUN。

本补审只读产品，不是c2评审，没有执行产品测试或模型请求。结构校验已确认：1614个ID唯一、每行原文与源文件行区间/sha256吻合、全部27票存在、#31 R01–16/#16 V01–19无缺号、选择器文件存在。源文件行段/哈希复核记录由各固定候选的 coverage-source-verification.json 保存。

新来源选择：#29仅最终Models决议；#31仅最终R01–16/A01–55；#65仅最终Database resolution及AC01–30；#69仅最终trace-export resolution及AC01–30/CE01–12；#18/#55的最终Q定义取已确认decisions文档，不重复访谈轮次；#24用已发布SKILL-SPEC-r1，不用旧Question。补回#81有序User Stories的R01–10，以及#16 V03/V04/V19、#19 Q10；S0/S5来源别名、D1/D4批准动作及纯crosswalk不作义务。

`active_definition`仅表示当前定义；`active_as_amended`须执行 effective_amendments 指定后续合同；同ID新定义以 supersedes 保留旧位置而不重复计数。#34红线条款按禁止解读，原文另保留。历史交付不反造规范，历史PASS只在 historical_evidence。

剩余 source_gap：#45 A29-01–24批准补正原文、#35 ACCEPTANCE-AMENDMENT-r1原附件、原始standalone brief/30+原文。#1当前地图已封存，但不是原始brief。历史Linux豁免不继承#84。

下面保留此前来源级说明便于回溯；“尚未抓取”的旧时态以本节及最终JSON为准，代表性测试不能当整票覆盖。

---

# Issue #84 — SPEC-08 coverage source audit

本补审只读取 `docs/acceptance/sources/`、`docs/design/` 和既有测试/公共接口；不修改产品，不评审尚未冻结的 c2。结论：清单必须从「正则抓到 R/AC/CE 的文本」改为「现行义务及其证据关系」。下面是可落地的来源选择和映射建议，不是本候选验收 PASS。

**所有以下本候选 execution_status 均为 NOT RUN（本补审未执行测试）**。`test located` 只证明源码中的测试入口存在；历史验收评论只能放 historical_evidence，不能成为本候选运行结果。源码测试断言覆盖程度仍需实施者逐行核对并绑定新候选日志；本审计没有声称完成 27 票所有条款的独立语义审核。

## 1. 来源集合与现行性规则

已有 sources 的 27 票应全部进入 source_inventory，不论是否存在 AC/CE ID：12、13、14、15、16、17、18、19、20、21、24、25、26、27、28、34、35、36、45、46、55、66、70、75、76、81、84。

原 c1 coverage 有 1473 行，但以下八票完全无行：**#12/#13/#14/#15/#28/#34/#36/#55**。#55 并非没有验收编号：正文有 **A01–A36**，被只认 AC/CE/R 的抽取遗漏。#19 的 **T01–T50** 也不能丢；Memory 的 **Axx**、其它票的 Q/D/F/S、正文无编号不变量均须纳入语义盘点。

推荐每条保留：source_issue/source_url/source_sha256/source_section/source_span/original_id（允许 null）/inventory_id/obligation/authority_status/superseded_by/public_path/test_selectors/historical_evidence/current_evidence/applicability/gap/owner。无 ID 的条目用 `I12-MIGRATION-IDEMPOTENCE` 等**盘点 ID**，明确不是用户原始 AC 编号。不要用行号当唯一身份；行号是定位，内容与批准版本才是身份。

- 按主题应用明确批准的补正，不是简单选最后一条评论。完成评论通常是历史证据；仅其中明确批准的边界修订才能改变原义务。
- 已作废旧要求保留为 superseded，不计活动分母；多处引用同一 A/CE 不能变成多条义务；`R01–R08` 范围引用不应只产生 R01/R08 两条原义务。
- 来源的 User Stories/Implementation Decisions/Testing Decisions 是义务来源，不能仅抽表格。一个测试可关联多个义务，一个义务也可能需要后端/HTTP/browser多条证据。
- 「测试源码已存在」「历史候选通过」「当前候选实际运行」「真实模型质量」分开。允许留真实缺口，不能把全部现成映射改成泛化的“待核对”。

## 2. 八个完全漏掉的来源：活动义务与具体映射

以下 `D/` 表示 `src/agent_alfred/evals/deterministic/`；`B/` 表示 `tests/browser/`。表内模块/测试均经本轮路径搜索确认存在，若给出具体 test 名也经搜索确认；未列的其它义务仍需扩展，不得以代表性映射宣称整票覆盖。

### #12 SQLite/schema

权威来源：`sources/issue-12.md` 正文 9–37 行，加评论 `5436093062`（trace_prunes）、`5437625839`（IANA/提炼列）、`5442832518`（最终版本1冻结与修复迁移）。后续结构重排由 #35 承接，不能因此删掉原行为合同。状态目录进程锁已明确移交 #28，不应在 #12 重复计为缺实现。

| 建议盘点 ID / 活动义务 | 公共路径及已定位测试 |
|---|---|
| I12-INJECTED-SCHEMA：注入连接、busy_timeout、单用户无user_id、Session隔离 | `schema.migrate(conn)`；D/test_schema.py::test_migrate_sets_busy_timeout_on_injected_connection、test_migrate_creates_required_tables_without_user_id、test_agent_log_isolates_sessions_by_session_id |
| I12-IDEMPOTENT-UPGRADE：版本账连续、已执行版本不重写、幂等、未知旧形状拒绝 | `schema.migrate`；D/test_schema.py::test_migrate_twice_leaves_identical_schema、test_migrate_writes_one_contiguous_ledger_row_per_version、test_migrate_rejects_a_ledger_with_a_gap、test_migrate_refuses_a_version_1_database_whose_shape_it_cannot_place；D/test_schema_baseline.py |
| I12-CALLER-TRANSACTION：迁移不得越权commit/破坏调用方事务 | D/test_schema.py::test_migrate_does_not_commit_the_callers_transaction、test_first_migrate_joins_an_open_caller_transaction、test_a_failed_migration_in_a_caller_transaction_undoes_only_itself |
| I12-FTS：facts/episodes插改删同步；FTS不可用明确错误 | D/test_schema.py::test_facts_fts_syncs_on_insert_update_delete、test_episodes_fts_syncs_on_insert_update_delete、test_python_sqlite_has_fts5、test_migrate_raises_when_fts5_is_missing |
| I12-TIME：未来日程保留IANA名，aware时刻与DST语义 | D/test_schema.py::test_calendar_entry_keeps_each_instant_paired_with_its_zone_name、test_parse_instant_rejects_naive_values |
| I12-LEDGERS：trace裁剪账/工具账/提炼状态闭集与幂等身份 | D/test_schema.py::test_trace_prunes_reject_a_null_timestamp、test_record_trace_prune_stores_the_highest_priority_reason、test_consolidation_op_row_accepts_an_update_to_each_terminal_state；事务最终落点还需关联 #13 Runtime测试 |

历史：#12 `5442832518` 记录旧候选 0cfe017/82 passed；不可沿用“schema_migrations只有一行”旧断言到现在多版本迁移，现行含义是每个已执行版本恰有一行。

### #13 公共循环/CLI/记录

权威来源：正文及 #29 回写把直接 `ModelClient` 注入改为 **ModelClientFactory**；后续 recording/Graph 回写按正文评论链保留。真实 OpenCode 演示义务仍是在线历史/质量轴，默认离线不能改成 PASS。

| 建议盘点 ID / 活动义务 | 公共路径及已定位测试 |
|---|---|
| I13-FACTORY-CHAT：同一业务循环由可注入工厂驱动 | `build_default_host`→Host.create_session/submit/wait；D/test_runtime.py::test_scripted_chat_run_records_one_message_pair_and_run_telemetry；D/test_endpoint_factory.py |
| I13-RESTART：重启历史可读并用于后续窗口 | D/test_runtime.py::test_a_new_host_reuses_session_record_as_working_memory、test_file_database_survives_closing_the_host_and_connection |
| I13-CLI：one-shot与Rich正文渲染 | `cli.main`；D/test_cli.py::test_cli_one_shot_prints_the_scripted_reply、test_cli_markdown_reply_goes_through_the_rich_renderer；B/cli-flow.spec.js |
| I13-BUDGET：Step/Attempt、总deadline、回退/重试与可控max_steps | D/test_runtime.py::test_max_steps_run_records_the_controlled_message；D/test_attempt_io_deadline.py、test_budget.py、test_interrupted_attempt_accounting.py；不要用Step数替代请求数 |
| I13-RECORDING：屏障/原子记录，pending占租约，failed阻塞新Run | D/test_runtime.py::test_barrier_failure_marks_trace_incomplete_but_still_records、test_pending_to_recorded_has_no_idle_unrecorded_window、test_recording_failed_snapshot_is_authoritative_before_503 |
| I13-PROBE：独立purpose推理probe记Attempt但不写消息 | D/test_runtime.py::test_inference_probe_persists_telemetry_without_messages；D/test_inference_probe_target.py |
| I13-REAL-MODEL：指定端点真实请求/日志 | D/test_requires_key_opencode.py；**本候选 NOT RUN，无付费授权，不属于离线默认集的缺席失败** |

### #14 endpoints/adapters

权威来源：正文+`2026-09-06 用户批准的 Issue #14 实施边界修订`（39–47 行）+完成评论保留限制。新修订明确生产形状规则0条、生产失败不触发翻转；不能把原“真实供应商翻转正例”继续作为本票已通过，也不能自动解除 #84 新合同的平台义务。

| 活动义务 | 公共路径及已定位测试 |
|---|---|
| I14-ENDPOINTS：六只读端点，两个OpenCode目的地独立，model级wire | `list_endpoints`/工厂；D/test_endpoints.py::test_six_read_only_endpoints_keep_the_two_opencode_destinations_distinct、test_opencode_rows_read_destination_and_key_from_one_source |
| I14-WIRE：两adapter同一内部消息，无业务供应商分支 | D/test_adapter_tools.py::test_openai_encodes_tool_batch_and_decodes_documentation_blocks、test_anthropic_encodes_one_user_result_batch_and_the_same_blocks；D/test_endpoint_factory.py；结构“无供应商分支”仍需独立静态断言定位，不应以wire测试替代 |
| I14-AUTH-PROBE：五字段齐全/未声明零探针；正确单套请求头 | D/test_endpoints.py::test_probe_requires_all_five_fields_and_production_declares_none、test_opencode_exposes_source_and_one_auth_scheme_per_route；D/test_opencode_headers.py、test_auth_probe.py |
| I14-SUPPORT：support/style来源分离、覆盖按三元键、不改指派、重启清空 | D/test_endpoints.py::test_effective_support_never_borrows_evidence_from_another_shape；D/test_support_overrides.py、test_model_settings_host.py |
| I14-CREDENTIALS：短凭据全掩码、完整值不外发 | D/test_connections.py；D/test_runtime.py::test_prompt_preview_redacts_loaded_secrets；保留历史完成评论所述端到端保密证据限制 |

### #15 catalog/cost

权威来源：正文+ #29 六项改名/逐维判别修订；特别原“拉目录失败退回默认模型”不能覆盖 #29 已冻结的“不能改现有指派/不能暗换模型”。

| 活动义务 | 公共路径及已定位测试 |
|---|---|
| I15-CATALOG：优先catalog_url、10秒、无key零请求、5分钟成功/约1分钟失败缓存 | `catalog.list_models`；D/test_catalog.py::test_missing_key_does_not_send_a_catalog_request、test_catalog_url_wins_over_base_models_and_uses_ten_second_timeout、test_success_cache_lasts_five_minutes_on_injected_clock、test_unavailable_failure_is_cached_for_about_one_minute |
| I15-CACHE-STATE：stale保留上次成功/失败，不驱动连接态 | D/test_catalog.py::test_failed_refresh_keeps_last_success_as_stale、test_catalog_success_does_not_write_connection_observation、test_new_pool_has_no_catalog_cache |
| I15-PRICE-CHAIN：endpoint+model键，逐维覆盖缺省与0区分 | `pricing.PriceChain/project_cost`；D/test_cost_projection.py::test_omitted_override_looks_down_the_chain、test_explicit_zero_override_does_not_invent_other_dimension_prices；D/test_static_prices.py |
| I15-UNKNOWN：endpoint_reported独占exact；缺token/价unknown不补零 | D/test_cost_projection.py::test_endpoint_reported_cost_is_exact_and_skips_price_components、test_missing_price_for_positive_tokens_is_unknown_without_default、test_openai_total_without_cache_details_is_unknown_even_when_prices_exist |
| I15-EXPORT：派生金额记录单价与计算时刻 | D/test_derived_usage.py::test_derived_snapshot_keeps_unit_prices_and_computation_time、test_derived_exact_amount_still_records_computation_time；不能另发明CLI命令，已有 test_cli_has_no_usage_export_command |

### #28 HTTP/SSE

权威来源：正文；评论 `5443666167`；`5444211896` 明确pending=409/failed=503、两段旧消息游标；`5464852097` 明确`:0`是传输启动边界和可重试close进度。

| 活动义务 | 公共路径及已定位测试 |
|---|---|
| I28-START：锁→bind回环→描述符，失败清理、不换端口 | `build_dashboard/start`；D/test_web_startup_order.py::test_a_lock_conflict_touches_nothing_at_all、test_a_failed_bind_releases_the_lock_and_never_starts_the_host、test_the_successful_path_runs_the_steps_in_one_order、test_the_cli_and_serve_paths_share_one_start_up_order |
| I28-HTTP-GUARD：Host/Origin/CSRF/正文及ContentType、无CORS | 真HTTP；D/test_web_guard.py、test_web_http.py、test_web_loopback_boundary.py |
| I28-REPLAY：环/双队列限额、慢连接隔离、原子注册快照、完整事件checkpoint | `SSEBroker/ReplayRing` + FakeConnection；D/test_web_broker.py、test_web_connection.py、test_web_frames.py、test_web_replay_budget.py |
| I28-RESEED：四种gap及:0语义 | D/test_web_replay_reseed.py::test_the_reserved_startup_checkpoint_is_a_real_cursor、test_the_replay_order_is_retry_reseed_gap_patch_then_events、test_a_forged_positive_seq_is_still_refused |
| I28-ADMISSION-STATE：202持久准入、pending409、failed503、绝对patch单调 | 真HTTP→Host；D/test_web_admission.py、test_web_api.py、test_recording_states.py；D/test_runtime.py::test_pending_to_recorded_has_no_idle_unrecorded_window |
| I28-PAGING-LEGACY：Run/Session/消息keyset及无Run旧消息第二段 | D/test_web_reads.py、test_runtime_run_queries.py；B/pagination.spec.js、upgrade-flow.spec.js |
| I28-CLOSE：关闭确认后才能释放宿主资源、可重试 | D/test_web_close_progress.py、test_web_rollback_close_progress.py、test_web_serving_close_timeout.py |

### #34 Models/Connections

权威来源：正文9–37行与明确引用的 #29；继承 #14 获批范围修订。来源 #29 本身不在现有 sources，若正文不能完整解释某个规则，应标原裁决源待补，不得仅凭“本票completed”补出事实。

| 活动义务 | 公共路径及已定位测试 |
|---|---|
| I34-ORTHOGONAL：支持/连接/目录健康分别渲染，未配置supported仍可指派 | `project_connections/models page`；D/test_models_page.py::test_connected_unsupported_keeps_dimensions_apart、test_unconfigured_supported_stays_assignable、test_group_header_carries_connection_observation_separately |
| I34-NO-AUTO-REQUEST：懒加载、缺key无请求、无声明不探针 | D/test_catalog.py::test_missing_key_does_not_send_a_catalog_request；D/test_auth_probe.py、test_models_page.py::test_probe_enablement_follows_key_not_connection_state |
| I34-PERSIST：revision/disk冲突保持原字节，先持久再发布 | `ModelSettingsStore`/Host mutation gate；D/test_model_settings_store.py、test_model_settings_host.py、test_settings_http.py |
| I34-PINS-OVERRIDES：唯一钉选持久化，手选style不改support，四维价 | D/test_models_page.py::test_user_declared_unknown_label_does_not_say_supported；D/test_cost_projection.py；B/settings.spec.js |
| I34-RELOAD-PROBE：显式重载credential、探针purpose及目标、不改assignments | D/test_connections.py、test_inference_probe_target.py、test_runtime.py::test_inference_probe_persists_telemetry_without_messages |

### #36 conversation UI

权威来源：正文11–47行、评论 `5444212208` 的pending busy与历史会话补正。注意后续#81增加Run path UI并不删掉基础聊天恢复合同。

| 活动义务 | 公共路径及已定位测试 |
|---|---|
| I36-SEND：真实202→Step/Attempt/delta→唯一正式回复 | B/send-flow.spec.js 的 `one real UI flow reaches 202, Step, Attempt, deltas and one reply across pages and reload` |
| I36-DRAFT-SESSION：跨页草稿/抽屉、tab Session隔离、显式继续 | B/mainbar.spec.js、tabs.spec.js、inbox.spec.js |
| I36-BUSY-RECORDING：CLI忙/409保草稿、pending保存中/failed未保存、无假Run | B/busy.spec.js、recording.spec.js、cli-flow.spec.js |
| I36-ATTEMPT-RECOVERY：aborted原位置/账目、丢delta清临时、gap/restart | B/streaming.spec.js、notices.spec.js、restart.spec.js、runs.spec.js |
| I36-LEGACY-PAGES：旧Session可见无假Run，两段不重不漏 | B/upgrade-flow.spec.js、pagination.spec.js |
| I36-A11Y：键盘/IME/焦点/节流播报/非颜色语义 | B/accessibility.spec.js；不得由Chromium自动断言推断人工读屏器已验收 |

### #55 Tools/Ops（A01–A36 必须逐条保留）

权威来源：`sources/issue-55.md` 的 D01–D07、60条User Stories、明确36条验收；本地相同领域发布稿 `docs/design/issue-32-published-spec.md`、`issue-32-tools-ops-spec.md`、验收表 `issue-32-tools-ops-acceptance.md`。原 issue #32 是设计来源，不能把设计票号当实现 owner。

| 原AC范围（落清单时逐条拆开） | 公共路径及已定位测试 |
|---|---|
| A01–A03：真实工具目录/隐藏/六格授权 | Host.tools_catalog/submit/wait；D/test_ops_metering.py::test_public_run_records_read_write_and_rejected_requests_without_bodies、test_catalog_includes_hidden_external_with_stable_identity；B/tools.spec.js、accounting.spec.js |
| A04–A09：授权保存busy/conflict/回执丢失/损坏/生效 | Host.save_tool_authorization与HTTP；D/test_ops_acceptance.py::test_disk_conflict_preserves_bytes_and_applied_policy；D/test_ops_failures.py；B/accounting.spec.js A04/A05/A06/A08 场景 |
| A10–A16：能力身份、请求/真实启动、同一操作回执、计量失败/恢复 | D/test_ops_acceptance.py::test_stable_capability_rename_and_different_source_do_not_reassign_history、test_restart_keeps_only_provable_start_facts、test_duplicate_call_identity_stops_before_dispatch_and_new_identity_is_new_action；D/test_ops_failures.py |
| A17–A22：Attempt全账、服务单位分开、unknown/旧记录/trace降级 | Host Ops读取；D/test_ops_acceptance.py::test_two_services_credits_unknown_and_local_free_are_separate、test_active_run_exposes_coverage_without_guessing_attempt_count、test_database_read_failure_is_not_empty_accounting；D/test_ops_snapshots.py::test_legacy_bad_row_is_isolated_and_exact_cost_without_model_survives |
| A23–A27：固定账目/价格快照、资源/自然日时区、过滤不切Session | D/test_ops_snapshots.py::test_mixed_attempt_costs_and_prices_stay_frozen、test_quota_expiry_and_returned_objects_cannot_mutate_snapshot、test_custom_calendar_boundaries_cover_dst、test_global_filters_do_not_change_session_and_include_system_runs；B/accounting.spec.js |
| A28–A31：投影不等于已入模、参数精确配对、分段安全历史 | Host.read_tool_history；D/test_tool_projections.py、test_ops_history.py；D/test_ops_acceptance.py::test_history_rejects_unmatched_parameters_and_untrusted_files |
| A32–A35：离页/关闭/重连释放、遗忘复用、unknown不能隐式重试 | D/test_ops_acceptance.py::test_http_close_drains_inflight_history_before_host_resources、test_manual_predelete_trace_survives_but_deleted_memory_is_not_reused；B/accounting.spec.js A24/A32/A33场景 |
| A36：跨功能默认离线回归 | 完整预定pytest/browser清单与candidate gate记录；不是取上述文件小计当全量 |

## 3. 其余19票应选的权威来源及映射起点

下列是来源级建议，不能代替逐条清单。

| 来源 | 活动合同/现行性 | 可用具体公共路径与测试文件 |
|---|---|---|
| #16 | 当前正文 Implementation/Testing Decisions；不能仅保留末尾R07/R09/R15引用三行 | Host Session/工作记忆/Runtime读取；D/test_sessions.py、test_runtime_working_memory.py、test_session_validity.py；B/inbox.spec.js、tabs.spec.js |
| #17 | Memory权威补充 A01/A05…A53；已完成后端与跨票集成责任分开 | MemoryService.execute、真实FTS、Host检索/实际请求；D/test_memory_commands.py、test_memory_stores.py、test_runtime_memory_gate.py、test_memory_input_attempts.py、test_memory_statistics.py。来源完成评论已有逐A→文件表，应导入为 historical_evidence |
| #18 | `issue-18-published-spec.md` D01–D09及确定性矩阵；本地consolidation-spec.md为接缝说明 | Host提炼调度/审批、SQLite/镜像；D/test_memory_consolidation_runtime.py、test_memory_consolidation_service.py、test_memory_mirrors.py、test_consolidation_final_revision.py；历史32案质量材料另轴保留原采样时间/失败 |
| #19 | 正文 D01–D15、**T01–T50**；Memory A编号是继承引用 | Host→Registry→内置工具/事务/文件候选；D/test_tool_acceptance.py、test_tool_registry.py、test_runtime_tools.py、test_file_tools.py、test_candidates.py、test_tool_recovery_edges.py；B/tools.spec.js |
| #20 | `issue-20-integrations-spec.md` r3，Q1–Q12/CE01–12，正文发布同步源 | Host web_search/Connections/真实可控HTTP；D/test_integrations.py；B/integrations.spec.js；真实Tavily不从离线fixture推导 |
| #21 | local `issue-21-mcp-spec.md`声明MCP-SPEC-r2、远端r1；需保存D5批准的Q8/Q15/CE08修订链，不默认为两个独立合同 | Host+真实本地stdio子进程/Registry；D/test_mcp.py、test_mcp_dialects.py、test_mcp_review.py；B/mcp.spec.js；scripts/check_mcp_installations.py |
| #24 | `issue-24-skill-spec.md` SKILL-SPEC-r1/Q1–Q28/CE01–30 | Host CLI/HTTP显式和自动Skill→实际请求；D/test_runtime_skills.py、test_skill_input_attempts.py、test_skill_acceptance_combinations.py；B/skills.spec.js |
| #25 | `issue-25-graph-spec.md` GRAPH-SPEC-r1，最终Q/AC/CE及回退合同 | Host→Graph/普通循环统一Run；D/test_graph.py、test_graph_recording.py、test_graph_review_regressions.py、test_runtime_skill_graph.py |
| #26 | `issue-26-message-routing-spec.md` ROUTING-SPEC-r1；不要抽设计多轮重复编号 | Host submit→分类器/Graph路径/回退；D/test_message_routing.py、test_routing_behaviour.py、test_routing_input_safety.py、test_routing_baseline.py；B/routing.spec.js |
| #27 | `issue-27-manual-aggregation-spec.md` Q1–Q15/F01…/AC/CE；source正文自身完整 | Host.aggregate→真实三类Store/草稿/持久回复；D/test_aggregation.py、test_aggregation_safety.py、test_aggregation_repair.py；B/aggregation.spec.js |
| #35 | source SCHEMA-SPEC-r1 + 明确验收修订；旧结构目标的最终处置，不能恢复已放弃项 | schema公共兼容/调用方事务；D/test_schema_structure.py、test_schema_boundaries.py、test_schema_baseline.py；历史Linux NOT RUN豁免不继承到#84新平台合同 |
| #45 | source A28/A29/A30/A34/A54及已批准A29-01–24补充；#31源引用需可解析 | MemoryService删除/来源闭包/恢复/真实trace屏障；D/test_forgetting.py、test_forgetting_trace.py、test_mirror_confirmation_recovery.py |
| #46 | Memory页面施工正文及#31对应A集合；不将后端通过等同browser集成 | HTTP+SSE+真实MemoryService；D/test_memory_page_http.py、test_memory_http.py、test_memory_sse.py；B/memory.spec.js、memory-fixture.spec.js |
| #66 | source AC01–30、保护SQL合同；所引用#65/ADR完整性另记 | DatabaseController真实SQLite+HTTP+browser；D/test_database_console_sql.py、test_database_boundaries.py、test_database_http.py、test_database_lifecycle.py；B/database*.spec.js与native lifecycle脚本 |
| #70 | source TRACE-EXPORT-IMPL-SPEC-r1/D01–D12/全部AC/CE，承接#69 | Host trace-export→实际文件/下载/遗忘/租约；D/test_trace_export.py、test_trace_export_http.py、test_trace_bundle_integrity.py、test_trace_export_failures.py；B/trace_export.spec.js |
| #75 | source BEHAVIOUR-TOPOLOGY-SPEC-r1；Q/R/AC/CE均取正文当前表 | Host只读拓扑+Behaviour页面；D/test_behaviour_topology.py；B/topology.spec.js；拓扑可见不证明可执行 |
| #76 | source ROUTING-STATS-SPEC-r1；保留固定公式例和跨版本分区 | 持久routing summary→统计读取→HTTP/browser；D/test_routing_statistics.py、test_routing_statistics_lifecycle.py；B/routing-statistics.spec.js |
| #81 | source GRAPH-RUN-EVIDENCE-SPEC-r1；当前/历史证据、手动刷新、trace保留 | Host.read_run_path/read_run_evidence；D/test_run_path.py；B/run-path.spec.js |
| #84 | 当前V1-ACCEPTANCE-PHASE-A-SPEC-r1 R01–R10/AC01–23/CE01–12，来源必须指向新完整候选 | acceptance CLI/Host/judge/预算/文件/门禁；D/test_acceptance.py及后继修复测试。本补审不对未冻结c2给任何PASS |

## 4. 明确的来源缺口与实施验收建议

1. **#1/原brief**没有在这批sources中完整封存；不能声称27票足以证明全v1来源集合完整。“8大类30+断言”原始清单仍按原规格缺证据处理。地图及依赖只能辅助核对来源范围。
2. **#31 Memory权威全文及A29补充**由 #16/#17/#45/#46多处指向，但不在当前sources。只把它的URL记下来还不够复核完整共同行为；正文承接不完整部分列 `source_gap`，可由执行者补抓固定评论并校验。该事实不能伪装成新产品缺陷。
3. #23/#29/#30/#65/#69等父设计不必无条件全量复制；若实现票明确自包含且条款齐全，可将其作为实施权威并保留设计链接。若正文写“分歧以决策为准”或引用关键缺失补充，应封存决定性内容/修订身份。
4. #18真实质量历史保留30completed/2failed与分母，不把其报告日期当采样日期；#14供应商识别、#20真实Tavily、#21真实服务/安装组合、#13真实模型均分别记录已验证范围，不从普通单元测试推导线上证据。
5. 最小可审阅完成形态：27个source记录均有义务或明确无义务的人工依据；所有原A/T/AC/CE定义表逐条展开；所有无编号强制验收给稳定盘点ID；每条至少有确切公共路径和具体测试/明确未找到原因；历史证据可含来源原comment+旧candidate，当前证据只能绑定新candidate与实际日志。清单要验证不存在未知/重复义务、空行替代覆盖、全部gap字段删除就放行等旁路。
6. 本文件给出了映射起点和八张漏票的实质补盘，但**不是逐条全覆盖认证**。尤其其它19票表内各文件里的具体断言与全部条款对应，需要执行者展开后再由冻结候选Spec审查核验。避免把本补审建议标为各产品条款PASS。
