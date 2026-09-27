# #95 公共路径覆盖映射

规范：[V1-ACCEPTANCE-OFFLINE-SUPPLEMENT-SPEC-r1](https://github.com/nineofoursyrup/Agent-Alfred/issues/95)。下面列出证据入口；每次实际执行的固定候选、命令、退出码和日志另存于全新的输出目录，不能用此表代替执行结果。

测试文件简称：A 为 `src/agent_alfred/evals/deterministic/test_acceptance_supplement.py`，B 为同目录 `test_acceptance_supplement_boundaries.py`。

| AC / 反例 | 可复核公共证据 |
|---|---|
| AC-01、AC-06、AC-13 / CE-07 | A `test_schema4_roundtrip_distribution_and_legacy_bytes`：schema4 6/30/120 清单经 import/read；旧 schema3 字节不改；仅换版本、分布、重复 ID、未知字段拒绝。旧1/2/3行为另由原测试保留。 |
| AC-02、AC-03 / CE-01 | A `test_critical_failure_and_unknown_cannot_be_diluted`：import/revise/read/report 展示关键失败与禁止项失败，同时列出支持不足和未决裁决。 |
| AC-04 / CE-01、CE-03 | A `test_denominator_includes_unrun_and_extra_obligations_do_not_add_votes`：额外次要项不增加分母；未运行仍计分母。摘要测试拒绝删案。 |
| AC-05 / CE-08 | B `test_business_refusal_is_distinct_from_required_execution_and_run_failure`：真实 Host/Registry persona 版本拒绝、要求成功执行的任务、文件出版失败和独立恢复分别保留。 |
| AC-07 / CE-05 | A `test_judge_checks_are_blind_separate_and_original_errors_retained`：三个独立失效模式、ScriptedModel 原文/重复 JSON 错误、漏判 FAIL、独立计数、构造答案不得进入产品列表。 |
| AC-08 / CE-04 | A `test_blind_review_cannot_fake_independence`：同实例、提前泄漏标签、封存字节修改、复核早于输出均拒绝；其他正常复核使用同模型不同实例。 |
| AC-09、AC-10、AC-11 / CE-02、CE-03 | A `test_summary_scope_user_source_and_rulings_survive_restart`：摘要覆盖、独立来源、普通项批准不消争议、重读 pending、追加最终裁决、原 grade/results 保持。 |
| AC-12 / CE-06 | A `test_material_chronology_on_import_read_and_regrade`，B `test_formal_source_approval_order_and_original_cutoff`：import/read、正式引用及原预算起点，等时/时区等价合法、迟到批准拒绝。 |
| AC-13 / CE-01、CE-07 | B `test_material_revision_invalidates_approvals_without_rewriting_samples`：改义务后批准/判分失效；原包字节和首次样本不改，刷新预算拒绝。 |
| AC-14 / CE-02 | B `test_material_approval_cannot_authorize_online_or_invoke_credentials`、`test_schema4_dispatch_only_uses_bound_mock_transport`：真实调用工厂及凭据零触达；正向仅使用合成 Authority 和 MockTransport，停机后分母保留。 |
| AC-15 | A `test_cli_prepare_validate_import_summary_and_readonly_report`：CLI/API 合同一致，纯 read/report 的前后树 hash 不变；原存储故障/恢复测试覆盖报告发布及 latest 的共享机制。 |
| AC-16 | 本映射、SCHEMA4.md、`examples_v4.demonstrate` 和当前候选验证记录。 |

复用的共享回归包括原 `test_acceptance_*` 下身份、来源、批准截止、七天时效、regrade、预算、历史隔离、报告故障与 Host 清理测试；并执行项目完整离线 CI 命令。Ubuntu 与 macOS 的执行事实分别记录，未运行环境不能由另一平台替代。

第二轮边界回归：A `test_confirmed_judge_miss_remains_fail_alongside_support_gap` 验证盲评封存后才比较原 raw/实际引用，已确认漏判 FAIL 与支持缺口同时保留；A `test_new_judge_invalidates_qualification_but_keeps_check_history` 保留旧 judge 检验历史。B `test_formal_execute_rejects_empty_approved_policy_before_mock_call` 在 dispatch 前阻止空政策；`test_formal_cannot_reuse_registered_ancestor_or_judge_material_family` 拒绝谱系和裁判材料重叠；`test_formal_requires_reviewed_calibration_and_reuses_qualified_judge` 通过 import/revise/read/report/preflight 验证校准产品复核缺口阻塞、补齐后的合成正对照、先批准阈值再冻结材料及原采样七天时效。该测试的 Host/ScriptedModel 数据仅为合成机制证据。

c3 评审修复：B `test_formal_requires_reviewed_calibration_and_reuses_qualified_judge` 扩展至校准源及多代祖先授权隔离，验证源包可审计而正式 import/read/report/preflight 全部拒绝。A `test_blind_support_gap_survives_supported_comparison` 覆盖材料、产品判分、裁判检验的盲评缺口在对照支持后仍保留，且只有用户裁决可以解除。A `test_summary_scope_user_source_and_rulings_survive_restart` 扩展至结果/校准摘要展示已裁定关键失败、未来裁决不生效、失败不可删、批准不得早于裁决、重启无来源时历史摘要可读但批准失效。对应 R-07/R-08、AC-09/10/11/12。

修复候选的独立复核补充：A `test_judge_checks_are_blind_separate_and_original_errors_retained` 同时核对摘要保留材料已批准时确认的漏判 FAIL；`test_c3_summary_bytes_remain_auditable_with_current_disputes` 使用旧 c3 候选实际生成的 `fixtures/schema4_c3_summary.json`，验证历史 version 1 材料/结果摘要可导入、重读且字节不变，当前报告保留新争议而不继承旧批准。

正式摘要校准继承回归：`test_acceptance_supplement_calibration_summary.py` 经 import/revise/read/report/CLI 验证结果及校准摘要继承 `judge_missed_prohibition` 和 `FAIL`，完整展示校准评估；可信来源签发的不完整摘要批准不能生效。覆盖校准决定失联、校准包缺失及授权隔离、摘要隐藏失败/错源/删除决定快照、早于校准决定的批准；历史快照可审计，但不能恢复当前权限。对应 R-07/R-08、AC-09/10/11/12/15。
