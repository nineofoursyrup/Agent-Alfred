# 阶段 A 公共路径与验收映射

合同：V1-ACCEPTANCE-PHASE-A-SPEC-r1。此文件固定验证入口，实际候选身份、逐项状态和日志由候选旁的 acceptance-results.json / checks/ 记录；它不预先宣称未来候选通过。所有此处模型案例是显式 simulation；后续真实质量/通用阈值/正式金标/调用额度仍需批准。

测试简称：T=`src/agent_alfred/evals/deterministic/test_acceptance.py`；R=`src/agent_alfred/evals/deterministic/test_acceptance_review.py`；C=`src/agent_alfred/evals/deterministic/test_acceptance_c3.py`；D=`src/agent_alfred/evals/deterministic/test_acceptance_c4.py`；E=`src/agent_alfred/evals/deterministic/test_acceptance_c5.py`。未写 `test_` 的简称须加此前缀；已写 `test_` 的保持原名。对应测试实际调用 acceptance CLI/服务、Host/Session/SQLite/Registry、受控 transport 或真实文件；不是凭最终状态 mock 得出结论。

| AC | 公共验证入口（文件::test_名称） | 证据与限制 |
|---|---|---|
| 01 | T::empty_report_is_blocked_without_network；T::cli_prepare_dry_run_verify_and_rebuild_do_not_resample | CLI JSON、零在线调用、缺项 BLOCKED |
| 02 | T::group_failures_missing_judge_and_disputes_are_not_averaged | 分组 PASS/FAIL/BLOCKED 与 simulation 标签，失败和缺口并存 |
| 03 | T::candidate_capture_detects_uncommitted_bytes；T::import_is_immutable_and_report_detects_corruption；R::execute_checks_candidate_and_claims_batch_before_construction；C::actual_loaded_package_must_match_declared_candidate_before_clients；D::calibration_outer_label_cannot_override_internal_evidence | 真实候选字节/包字节变化、工厂构造前拒绝 |
| 04 | T::schema_rejects_drift_duplicates_and_calibration_leakage；T::schema_is_closed_and_nonboolean_applicability_is_rejected；C::grade_raw_and_evidence_references_must_match_recorded_result | 未知版本、重复与断裂来源拒绝 |
| 05 | T::schema_rejects_drift_duplicates_and_calibration_leakage；R::complete_calibration_link_and_real_formal_denominator；C::calibration_requires_complete_effective_scores；D::calibration_keeps_product_failure_and_human_resolved_judge_error | 30/120 的各组分母，实质输入隔离，模拟材料不算正式质量 |
| 06 | T::fixture_runs_real_host_and_reopens_saved_replies | 六组真实 Host 与重开 SQLite；Memory/Skill/路由/聚合元数据 |
| 07 | R::failed_tool_claim_does_not_replace_execution_fact；C::result_summary_cannot_overrule_failed_or_missing_recording | 工具 invalid_input/not_started、无 outbox 文件，模型声称成功不能造执行事实 |
| 08 | T::freshness_uses_original_sample_and_specialist_empty_denominator；R::specialist_public_report_preserves_counts_failure_staleness_and_na；C::specialist_failed_or_unrecorded_result_cannot_become_pass；D::specialist_empty_output_is_completion_failure_even_with_high_aggregate | 通用无默认门槛，独立提炼公式/空分母/禁止项/时效 |
| 09 | T::group_failures_missing_judge_and_disputes_are_not_averaged；T::mixed_profiles_and_future_samples_cannot_release | 不跨组平均、不跨 profile 择优 |
| 10 | T::failed_product_and_missing_judge_remain_separate；R::real_host_empty_illegal_and_timeout_keep_case_and_unknown_usage；T::authorization_precedes_factory_and_request_budget_counts_retries；E::judge_control_interruption_stops_batch_and_preserves_first_attempt | 产品失败与判分错误分轴，未运行案和 unknown 用量保留 |
| 11 | T::judge_has_no_tools_and_human_ruling_does_not_overwrite_grade；R::human_confirmed_failure_is_a_failure_not_unknown；C::error_grade_can_be_explicitly_adjudicated_without_erasing_original；C::calibration_dispute_requires_case_ruling_before_formal_reuse；D::calibration_keeps_product_failure_and_human_resolved_judge_error | 显式人工记录、原判分保留、确定失败为 FAIL |
| 12 | T::judge_has_no_tools_and_human_ruling_does_not_overwrite_grade；T::schema_rejects_drift_duplicates_and_calibration_leakage | tool_choice=none，无产品工具/Host 能力；不证明真实 judge 语义抗诱导 |
| 13 | T::online_runner_preflight_zero_construction_and_real_host_injection；R::test_execution_without_source_rejects_credentials_before_factory；`test_acceptance_admission.py` 公共模拟授权回归 | 生产缺可信来源在凭据/工厂前拒绝；仅 `SimulationSession` + `MockTransport` 合成正向；未来付费需可验证硬金额上限 |
| 14 | T::transport_retry_obeys_shared_budget_without_hidden_sdk_retries；R::public_judge_persists_shared_budget_and_refuses_parent_replay；D::judge_preserves_actual_attempt_links_for_calibration；E::judge_control_interruption_stops_batch_and_preserves_first_attempt | 真实 adapter+受控传输重试，产品与 judge 共享账、deadline 和停止事实 |
| 15 | T::regrade_and_rerun_cannot_replace_first_product_results；R::regrade_configuration_preserves_original_product_and_requires_calibration；R::explicit_new_regrade_budget_preserves_old_spend_without_old_deadline | 原样本/时间/已花请求保留，显式新重评预算域无自动授权 |
| 16 | T::freshness_uses_original_sample_and_specialist_empty_denominator；T::mixed_profiles_and_future_samples_cannot_release；C::formal_freshness_does_not_refresh_referenced_calibration；E::calibration_request_time_must_match_product_and_judgment_windows | 明确时区七天两侧、未来/混合配置，供应商版本 unknown 明示 |
| 17 | T::mechanical_evidence_requires_current_candidate_complete_collection；R::collector_rejects_ambient_test_selection_before_spawn；C::raw_failed_pytest_phase_overrides_successful_summary | 未筛选计划→requires_key 排除→实际三阶段；平台独立，不把旧 CI 计为当前通过 |
| 18 | T::installation_source_pollution_is_blocked；scripts/check_mcp_installations.py | 原始日志/四组合/产物哈希/实际安装源文件、解释器、sys.path；真实安装结果另记 |
| 19 | T::partial_package_never_becomes_a_valid_report；R::report_publication_failure_keeps_batch_and_explicit_latest_identity | 批次与报告历史排他写入、明确 latest 身份，半写不代表新批次 PASS |
| 20 | T::unknown_reference_and_deleted_parent_cannot_be_reused；T::package_secrets_interrupted_latest_and_deletion | 完整性重读、删除标记和依赖失效；不随产品 trace 清理 |
| 21 | R::test_execution_without_source_rejects_credentials_before_factory；R::production_bait_is_not_read_and_judge_does_not_receive_it | 显式/ambient 密钥保护、私人状态诱饵、仅专用状态 |
| 22 | T::cli_prepare_dry_run_verify_and_rebuild_do_not_resample；R::interrupted_judge_publish_recovers_without_new_calls | 可复制命令、原包重建、部分 judge 恢复零新调用 |
| 23 | coverage-inventory.json；coverage.assess；源快照与逐行映射 | 当前/历史证据严格分开；明确 source_gap 与缺少断言覆盖；不是需求覆盖100%证明 |

| CE | 对应 AC / 实际反例及恢复 |
|---|---|
| 01 | 01/02：无证据 → BLOCKED；增加 simulation 判分只验证机制 |
| 02 | 17/23：skip/漏收集/环境选集 → BLOCKED；恢复完整同候选门禁 |
| 03 | 08/09：某组/专项/禁止项失败仍 FAIL，空分母 N/A；保留旧批次 |
| 04 | 07/10：真实 Host 超时/空输出/非法响应及工具失败；余案不移分母 |
| 05 | 11/12：同模型拒绝、judge 非 JSON/诱导文本记错误；无工具；人工裁决追加 |
| 06 | 05/15：重复样本/替换首次结果/混批/校准混用拒绝；新批保留父链 |
| 07 | 13/14：无授权零请求，受控重试实际记账，到限停止；新预算域单独授权 |
| 08 | 03/12/16：当前候选先核验，judge 改动需新校准；哈希不宣称外部真实性 |
| 09 | 15/16：重评分不刷新采样时间，七天两侧判定；过期仍须另行采样 |
| 10 | 18：源码污染/产物不符不可通过，真实四组合恢复证据 |
| 11 | 19/20/21：半写/丢文件/显式删除/敏感标记，原始日志可重建但不重跑模型 |
| 12 | 02/17/22/23：手填 PASS/旧候选/旧豁免不成为当前有效机械与覆盖证据 |

| 要求 | 实现边界及验收 |
|---|---|
| R01 | report.py/gates.py/coverage.py；AC01/02/09/17 |
| R02 | candidate.py/schema.py/safety.py；AC03/12/21 |
| R03 | schema.py/store.py/coverage-inventory.json；AC04/05/15/23 |
| R04 | runner.py/judge.py/online_judge.py，真实既有 Host；AC06/07/12 |
| R05 | report.py/specialist.py/裁决；AC08–11 |
| R06 | budget.py/ExecutionJournal/显式执行预占；AC13/14/15 |
| R07 | collect.py/pytest_evidence.py/已有安装脚本；AC17/18；Ubuntu 未跑不能标 PASS |
| R08 | 原始样本时间、候选字节、校准引用；AC03/15/16 |
| R09 | EvidenceStore 不可变包/追加报告/日志恢复/删除；AC19–21 |
| R10 | README.md 全部命令与后续边界；AC22/23；无软件发布或关票动作 |

### r2 引用协议增量

入口文件 `src/agent_alfred/evals/deterministic/test_acceptance_r2_citations.py`：

| 公共路径 | 覆盖 | 限制 |
|---|---|---|
| judge_result + ScriptedModel | 六份实际 r2 raw 回放；实际目录、别案与不存在路径拒绝；转义/数组/空引用边界；动态案例 ID；安全/容量 preflight | 不产生替代 r2 判分，不证明真实模型改善 |
| EvidenceStore.import_batch | 模型/协议/目录/正文漂移、未知协议；错误记录保留及缺 rubric；旧校准不可用于新协议 | 内容哈希不是外部签名 |
| grade_batch 受控工厂 | 未声明新协议时在工厂构造前拒绝；既有真实本地 HTTP transport 试验使用新协议 | 零供应商调用 |
| report + 精确格式反例 | 引用可解析但结论错误仍保留 FAIL/裁决 blocker | 非机械语义仍需后续校准 |

### c4 引用协议修订增量

`judge-citations-v2` 保留 v1 与 r1/r2 历史定义和原始字节；新增公共拒绝路径覆盖：

| 公共路径 | 覆盖 | 限制 |
|---|---|---|
| `judge_result` canonical digest guard | 同 ID 的 JSON 数字/布尔类型漂移、普通内容漂移、跨案例错配均在 client 前拒绝；未改变的深拷贝仍可用 | 不自动纠错、归一化或重试 |
| `validate_grade` / `validate_references` | `format_check.evidence` 必须与 canonical 重算结果一致且全部在实际目录中；伪造、缺失或越界引用拒绝 | 机械可解析性不等于语义支持 |
| `judge_protocol.current_profile` | v2 version/id 与模型、提示词、schema、目录策略、材料 hash 一起绑定 | v1/r1/r2 旧结果不重判、不冒充 v2 |

### c5 引用协议修订增量

`judge-citations-v3` 在 c4/v2 之后新增两条公共 fail-closed 路径：

| 公共路径 | 覆盖 | 限制 |
|---|---|---|
| `judge_result` result identity guard | 同 `result.id` 的 JSON 类型或内容漂移、重复/未知结果身份均在 client 前拒绝；未改变的深拷贝保持可用 | 不自动纠错、归一化、模糊匹配、裁剪或重试 |
| `validate_grade` legacy binding | 新 grade 绑定 `case_hash`、`case_material_id` 与 `result_hash`；无 protocol 且缺历史绑定的 grade 拒绝，案例/金标/结果漂移拒绝 | 历史 r1/r2 原始包不写回、不补字段；专用 calibration/regrade 链仍由其父链与包完整性检查 |

v3 的身份进入 `judge_profile.protocol`、grade 字段和 r3 材料摘要。v1/v2、r1/r2 原始
结果、错误、报告与文件保持原样；c5 离线测试只是协议和接线证据，不能替代真实模型
引用率、语义校准、通用阈值、30 案校准或 120 案正式验收。

### c6 引用协议绑定收紧

| 公共路径 | 覆盖 | 限制 |
|---|---|---|
| `validate_grade` legacy 分支 | `format_check` 事实漂移、缺失 case/result 绑定、同 ID 案例或结果漂移 | 只验证保存材料身份，不证明语义支持 |
| `validate_grade` protocol 分支 | calibration、regrade、formal/package-linked grade 统一核对 case/result/material/rubric；伪造绑定 fail-closed | 不改变父链专用错误顺序，不重判历史包 |
| c6 red/green 公共测试 | 不存在的绑定和格式事实反例先失败、修复后拒绝；旧 fixture 兼容性保持 | 无真实模型请求，不能宣称引用可靠性改善 |
