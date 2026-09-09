# Issue 45：遗忘协调核心

基线 `193de2dc377a3eaaa674931ee1408e35e38d146a`（#17 / PR #47 合入后的 main）。
权威行为来自 [#45](https://github.com/nineofoursyrup/Agent-Alfred/issues/45)、
[#31 R09–R10/R13–R14](https://github.com/nineofoursyrup/Agent-Alfred/issues/31#issuecomment-5588135839)
及用户批准的 A29 固定历史范围、部分确认与未知历史暂停裁决。

## 生产接线与边界

`RuntimeHost.memory_service.execute` 的 delete 沿用原命令验证、准入、HMAC 重放和 Store 版本检查。
有 trace 端口时先调用 `FanOutSink.checkpoint_barrier`，实际队列顺序确认持久前缀后才进入删除事务。
Run 工具使用已有权限；手动删除的无 Run checkpoint 覆盖全部待写 bundle。
无 Run、无 trace 的离线服务没有待写 trace；有 Run 但未配置持久端口时拒绝删除。
缺少关键 Sink、丢事件、部分写、同步失败或超时均失败关闭。原最终 flush/close 所有权保留。

删除事务硬删行与 FTS，写入无正文来源限制、清理意图和原操作回执；
SQLite 投影参与者失效与主事务共同提交。新 `memory.forgetting` 提供以下公共端口：

| 端口 | 契约 |
| --- | --- |
| `register_group` | 注册 Run/batch/legacy 不透明身份；complete 必须带可信业务证据 ID。不得把客户端参数、没有找到边、selected 或缺失 telemetry 当完整性证明。 |
| `register_sources` | 补正某 Memory 历史版本的来源边和证据；补边不自动清空未知范围。 |
| `register_read` | 登记真实 Attempt 的 gate/answer/consolidation 使用：Memory 版本或历史组；登记与闭包传播、SQLite 失效同事务。 |
| `evaluate_history` | 四用途共用读闸，返回允许/拒绝身份及带 HMAC 的持久版本令牌，不提供正文。 |
| `consume_history` | 在同一 mutation 准入内重验令牌并调用可信消费者，回调只得到允许身份；不持 SQLite 锁做 IO。 |
| `write_projection` | 在共享事务内核验来源令牌和目标 expected_version，再调用只用该连接的写入方；用于 #18 原子提交接缝。 |
| `get_forgetting` / `list_scopes` | 读当前进度、限制与固定成员快照；范围身份、版本、容器、成员和确认时点均可重读。 |
| `offer_scope` | 可信证据适配器把已有未决成员 regroup 为固定 Session/batch 范围，支持跨 Session；变更旧快照版本，不增添客户端任意成员。 |
| `resolve_scope` / `get_action` | 手动确认持久范围；请求只接受 scope_id/expected_revision。action_id 与原 delete operation_id 分开，HMAC 重放原确认事实。 |
| `retry_cleanup` | 仅重试该操作未完成项，先核验真实输出，再决定是否重建。完成项不会重复副作用。 |
| `managed_target_readable` | 输出生成代次尚未核实时拒绝旧预览；真实文件读口必须在同一准入期间遵守该判定。 |
| `reconcile_projections` | 新发现的受管投影在开放前重新失效；可重新打开已 complete 操作的清理义务。 |

`ProjectionParticipant` 与 `CleanupPort` 的签名和所有权见 `memory/forget_ports.py`。
参与者只能同连接执行 SQL，不自行提交/回滚、不做 IO；整批候选 invalidated 并清正文/差异。
未决暂停来源禁止读取或提交，不能用 invalidated 冒充已经确认污染。
文件适配器以 target_id 解析受管目标，从当前有效 Store 重建，原子替换并核验 generation；
不从任务中取旧正文，不保存额外历史镜像。连续删除共享输出时推进同一目标代次，
所有旧任务必须核验覆盖最新删除的代次。清理 IO 期间持准入但不持数据库锁。

`memory_notifier` 是无正文通知端口：schema_version/process_instance_id/memory_revision/change。
范围及进度变更的 change 为 null；删除 change 只有身份、版本 null、操作 ID 与 deleted。
通知在提交后且数据库锁外调用，投递故障不改写回执；#46 适配器负责不可投递就断开。
数据库版本始终是迟到读取与重连的依据，不依赖通知送达。此票没有新增 SSE wire 类型。

## 范围、恢复和状态

v6 只追加新表，不改写 v1–v5 迁移。旧 Run/批次从结构化表登记为 unknown，
缺 Run ID 的 agent_log 行得到明确标为 legacy 的稳定行映射，不伪造 Run。
旧 deleted 回执恢复 unknown 遗忘任务；单纯 already_absent 不捏造“曾经删除”的进度。
来源不完整且无法枚举未知历史时保留 unresolved 义务；缺证身份不能当作可确认的完整历史范围。
提供可信结构化证据后才能映射，不能确认一个空集合就称完成。

已知来源覆盖各内容版本，直接使用者及历史读取后继均隔离。未知来源不能缩小时使用
所有不能证明安全的现存候选组形成更大快照；既有完整无关证据仍有效；同 Session 的新且有安全证明的 Run 不自动进入原范围。
确认提交冻结确切成员。后继传播不修改冻结成员；确认旧版本首次动作返回 scope_stale。
同 action_id 先比较 HMAC/key_id 并回读原回执；参数变化 mismatch，旧 key 不可比 unverifiable。
确认回执与当前进度独立；重启不扩成员、不消除暂停。

状态优先级为：存在未决范围 → needs_scope；否则存在清理失败 → failed；
否则存在待处理/执行中/重启待核验 → cleaning；否则 complete。
needs_scope 可同时显示失败项。重试只把实际开始的项改为 running；文件已替换但确认中断时，
重启仍禁读，先 verify 同代输出再确认完成。晚到关联/投影/未知范围可重开进度，原回执不变。
状态转换的观察时间与证据版本同事务持久记录在 observations；重启后仍可回读先前 complete，
不以删除回执代替清理完成观测，不将事务内中间状态冒充已提交观测。

`register_group/register_sources/register_read` 可显式传入 `transaction=conn`，
参与调用者已有事务：不重取连接锁、不提交、不回滚，异常交回调用者。
生产下须继承有效 Run 权限或由本服务的事务执行器调用；离线注入连接由其调用方持有。
未显式借用时遇到已有事务返回 transaction_required，保留调用者工作。
可在 `write_projection` 回调内借用该连接，使来源、业务写项和失效共同提交。
借用者在最后一项业务写入后调用 `prepare_commit(conn)` 记录本事务最终进度，
再自行提交并发布通知；本服务的事务执行器已执行这两个边界。

范围额外持久化安全时间上下界、成员数、时间证据是否完整与闭合缺证原因。
缺时间证据显示未知，不由 ID 或正文猜补。`get_scope(scope_id, revision)` 可重读旧展示版本；
确认、证据收敛、重新组合范围均保留各自的不可变版本快照。

关联端口是可信业务 API，不能暴露为模型自行宣称 complete 的工具参数。
消费者须在发送前持久登记未确认/unknown 的消费组，再调用读闸，在真实 Attempt 边界登记 actual use；
崩溃或未落定 telemetry 不得升级完整性。`register_read` 不以 selected 推断请求已经发送。
实际历史入口和独立同义记忆保留，禁止文本/HMAC 黑名单。

## 验收对应

公共路径测试位于 `evals/deterministic/test_forgetting.py` 和 `test_forgetting_trace.py`。
以下为测试覆盖映射；实际执行、候选文件哈希与双轴评审证据另随 `.scratch/issue45/` 候选包交付。

| 验收 | 测试函数（省略 test_） |
| --- | --- |
| A28 | known_closure_is_atomic_and_survives_restart；late_memory_use_and_source_registration_isolate_old_versions；successors_before_or_after_confirmation_and_multiple_operations |
| A29-01 | v5_delete_receipt_and_ungrouped_history_recover_unknown；absence_without_delete_evidence_does_not_invent_forgetting；#17 explicit_new_fact_has_known_empty_sources_not_legacy_unknown |
| A29-02–03/05–06/17 | partial_scopes_freeze_exact_history_and_keep_unknown_after_restart |
| A29-04 | scope_priority_and_last_confirmation_follow_cleanup_facts |
| A29-07 | batch_scope_can_cover_fixed_members_from_two_sessions |
| A29-08/22 | successors_before_or_after_confirmation_and_multiple_operations |
| A29-09 / A30 | independent_same_content_and_raw_history_survive_projection_erasure；delete_and_scope_invalidation_share_transaction_with_cleanup_intents |
| A29-10–12 | partial_scopes_freeze_exact_history_and_keep_unknown_after_restart；v5_delete_receipt_and_ungrouped_history_recover_unknown；four_purposes_keep_fresh_input_and_reject_stale_send |
| A29-13 | four_purposes_keep_fresh_input_and_reject_stale_send（离线消费者捕获；不是实际聊天整链验收） |
| A29-14–15 | scope_evidence_shrinks_only_unconfirmed_members_and_invalidates_revision |
| A29-16 | confirmation_busy_is_not_queued_and_receipt_failure_rolls_back |
| A29-18 | confirm_replay_key_rotation_stale_and_invalid_choices |
| A29-19 | candidate_confirmation_failure_rolls_back_and_late_projection_reopens |
| A29-20 | confirmation_interruption_before_commit_recovers_pending_scope；partial_scopes_freeze_exact_history_and_keep_unknown_after_restart；cleanup_failure_and_crash_after_replace_recover_same_generation |
| A29-21 | late_memory_use_and_source_registration_isolate_old_versions；candidate_confirmation_failure_rolls_back_and_late_projection_reopens |
| A29-23 | confirm_replay_key_rotation_stale_and_invalid_choices；missing_source_identity_and_storage_evidence_failure_never_complete |
| A29-24 | progress_invalidates_old_projection_and_publishes_body_free_notice；four_purposes_keep_fresh_input_and_reject_stale_send |
| A34 | cleanup_failure_and_crash_after_replace_recover_same_generation；scope_priority_and_last_confirmation_follow_cleanup_facts |
| A54 | test_forgetting_trace.py 五项：生产前缀后续可写、fsync 失败、真实队列等待/超时后迟到写、部分写/溢出、成功屏障后事务回滚 |
| R13 | projection_writer_rechecks_sources_and_target_in_shared_transaction；candidate_confirmation_failure_rolls_back_and_late_projection_reopens |

首轮 Spec 修复回归另含：source_registration_borrows_transaction_without_commit_or_rollback、
borrowed_registration_inside_projection_owner_never_relocks、
existing_safe_evidence_is_order_independent_for_unknown_sources、
scope_metadata_has_frozen_time_bounds_reason_and_old_revision；
complete_observation_survives_reopening_and_restart。

安装包排除本地 `.scratch/` 证据和虚拟环境；新工作树 `.gitignore` 增补此路径，
没有改动原裁决工作树或原主工作树的忽略配置。

所有测试使用临时真实 SQLite/FTS；trace 使用生产队列与受管文件。
并发以 Event/SQLite trigger 确定顺序，时钟固定；没有模型 API 或计费调用。

## 下游仍需完成

- #16：真实完整 Run 窗口先过滤再选 N 组，门和回答复用相同安全输入；登记实际读取。
- #18：真实提炼批次生命周期、阈值和审批、业务证据认证、Markdown 镜像及启动恢复接线。
- #19：真实 ToolRegistry 权限传递、删除后无下一次模型请求、未执行工具安全配对与正式固定收尾。
- #46：确认 UI、HTTP 错误/幂等恢复、memory_patch wire/断线重连/迟到响应及人工历史入口。

注入端口 PASS 只证明 #45 核心合同；既有 Dashboard 浏览器回归也不证明上述新产品链路已交付。
本票文档仅迁移 #31 的遗忘与事务相关段落，没有接管其 ADR-0008/0031/0032 或整份 CONTEXT。

## v3/v4 独立复审修复

- 独立修改执行器统一在进入 recording 事务、trace 或文件 IO 前检查调用者已有事务，
  返回 `transaction_required`；`retry_cleanup` 不支持借用，绝不提交或回滚调用者事务。
  显式 `transaction=conn` 的来源登记参与者仍保留原借用契约。
- v5 deleted 回执升级时同迁移清除已知隔离组的 `tool_ledger.summary`，并持久登记
  `forget_projection_recovery`。`projection_recovery_pending` 可独立展示，未恢复时
  不得 complete；有未知范围仍优先 needs_scope，否则 cleaning。
  `retry_cleanup` 或 `reconcile_projections` 先在事务内调用当前受管投影参与者，
  只有服务构造期收到可信 `projection_inventory_evidence_id`、完成 SQLite 失效与
  文件意图登记后才把清单证据持久关联到恢复记录。缺证返回
  `projection_inventory_required`，默认空参与者不证明库存为空；这不是客户端或模型
  可自行提交的字段。参与者失败则整次恢复回滚；
  文件仍须原有 verify/rebuild 流程核实。适配器须在开放其输出前接入该恢复端口，
  不把空的已登记文件列表当作旧投影已完成证明。
- 内部 mutation 使用原生 RLock 的线程所有权核验，但公开重入仍立即 busy。
  finally 不依赖 acquire 返回值已写入局部变量。Host 支持调用前分配的可选 owner
  身份：取得 slot 前登记 owner，清理只释放匹配 owner，避免返回值捕获前中断泄漏
  或误释放其他操作的准入。取得/释放 Host 内部锁均由调用前可达的 OwnedLock
  直接捕获原生 acquire/release 结果并提供 close_completed 完成事实，外层 ResumableRollback 保留并恢复中断的清理，再传播原控制异常。
  测试用真实 Host 与 opcode 中断覆盖捕获和释放窗口；既有无 owner 调用方式保持兼容。
- 新来源或实际读取传播出已知隔离时，同事务从未确认范围移除已确定成员并递增 revision；
  空范围标为 excluded。其他未知成员及未解决身份保持不变，不据此改写来源完整性。
  已确认快照不变，旧首次动作 stale，成功动作仍按原 HMAC 回执重放。
  未解决身份被解析时，旧未确认快照递增版本，已识别部分生成可展示的新容器范围；
  补全时间边界也递增版本；已重组为 Session/batch 的 pending 范围同样依据成员
  身份变化递增版本，不能让同一旧版本从不可确认悄然变为可确认。

新增公共路径回归（`test_forgetting.py`）：

| 修复 | 测试函数（省略 test_） |
| --- | --- |
| 调用者事务 | independent_mutations_preserve_caller_transaction |
| 旧库恢复、受管投影、历史保留 | v5_upgrade_restores_projection_cleanup_before_complete |
| mutation 锁中断 | control_exception_after_mutation_acquire_releases_lease |
| Host 准入中断 | control_exception_after_admission_releases_both_leases |
| 来源/实际使用/后继、范围回滚、stale 与重放、重启 | late_sources_and_reads_revise_only_pending_obligations |
| 身份/时间证据变化使旧确认 stale | resolved_identity_and_time_evidence_invalidate_old_scope |
| 已重组范围的成员身份解析 | regrouped_unresolved_member_stales_when_identity_is_resolved |
| Host 清理中断与可恢复锁所有权 | control_exception_during_host_release_completes_cleanup |
| Host 取得锁/owner/slot 期间中断 | control_exception_inside_host_acquisition_keeps_owner_recoverable |
| 原生锁释放后的 Python 包装返回中断 | native_lock_release_return_has_durable_completion |

以上扩展既有 A28/A29-15/A29-18/A29-20/A30/A34 验收，不改变 #16/#18/#19/#46 的交付归属。

## v7 外部复审的兼容入口与失败进度修复

Host 的无 owner 准入使用线程所属的隐式 claim，局部取得/释放异常同样由可恢复
所有者清理；无 owner 的 end 不可清除其他线程或显式 owner 的 claim。
既有 Session 创建、设置修改、环境重读与鉴权探测统一通过 Host.execute_mutation，
在调用 try_begin 前持有所有者，跨调用返回中断仍能完成清理；HTTP 成功/拒绝映射不变。
底层分步 begin/end 保持兼容，直接使用时调用者仍须配对 end；隐式成功 claim 在 Host
可达，异常返回时也可以由该线程 end 恢复。普通业务异常与清理控制异常并存时，
沿项目 dominant_error 规则优先传播控制异常；已有控制异常保持原对象。

迁移投影恢复的 SQLite 事务失败后，先完整回滚投影和文件意图，再在独立的受管事务
记录 `projection_invalidation_failed`、时间与进度版本，不保存 SQL 错误正文。
`retry_cleanup` 与 `reconcile_projections` 均遵守此边界：无未决范围显示 failed；
仍有未决范围显示 needs_scope，同时提供 `projection_recovery_error` 和
`projection_recovery_failed_at`。原删除回执不变，重启可重读同一失败事实。
成功重试在投影失效及文件意图提交时清除当前失败明细，文件仍按原 verify 规则核实。

失败响应保留 storage_write_failed，并给出 `failure_recorded`：true 仅在独立失败
记录事务确认提交后返回；false 表示本请求未确认持久化该失败，不声称数据库中绝无记录。
数据库不可写或已 poisoned 时，不伪造持久状态；已有事务归属与删除原子性不变。

新增公共回归：

- `ownerless_mutation_gate_interruptions_keep_host_usable`：真实 Session 创建，取得/释放清理与返回中断。
- `direct_legacy_host_cleanup_and_foreign_claim_are_safe`：分步兼容入口、局部恢复、不误释放其他 owner。
- `related_dashboard_mutations_recover_host_return_interruptions`：设置/环境/未知 endpoint 本地拒绝路径，不调用外部模型。
- `projection_recovery_failure_is_durable_and_retryable`：retry/reconcile、有/无未决范围、重启、成功重试和只读数据库。

投影参与者抛出的普通执行异常（包括 ValueError/RuntimeError）与 SQLite 执行失败走同一回滚后失败记账路径，不能误映射为调用输入错误。BaseException 控制异常仍传播，不转换为普通失败回执。回归覆盖 SQLite、ValueError、RuntimeError × retry/reconcile × 已解决/未解决范围及重启恢复。


## v9 外部复审：重发现投影的持久禁读义务

`reconcile_projections` 和需要投影恢复的 `retry_cleanup` 在同一 mutation 准入内，
先以独立事务保存无正文的 `forget_projection_fences(operation_id,target_id)`，
提高受影响目标代次并登记 pending 清理义务，再执行可回滚的投影正文事务。
该预先意图属于已提交删除的安全恢复进度；原删除回执和后续批量正文效果不被拆分。
正文、投影证据以及本次成功消除的 fence 仍同事务提交。

普通执行错误随后尝试独立保存安全失败明细；该记账本身不可写时返回
`failure_recorded:false`，已经提交的 fence 和 pending 代次仍在，重启后继续禁读。
控制异常保留原异常并回滚效果，已经登记的意图仍等待恢复，不伪造失败明细。
若连预先意图事务也无法提交，本次投影执行在调用 invalidate 前拒绝，不能发布新投影。
接口不能承诺把事实写进不可写的数据库。

读闸同时核对目标验证代次与所有操作的 fence。共享目标的一个操作恢复不能
解除另一操作的义务；仍有 fence 时不执行该目标的文件清理。成功恢复只撤销
相应操作的 fence，目标仍需当前代次重建与 verify 成功才重新放行。无未决范围时
失败明细优先 failed；未成功记录错误但存在 pending 清理则 cleaning；needs_scope
仍优先于两者。不会改写原删除回执或曾经完成的观测。

可选可信端口 `ProjectionReconciliationScope.reconciliation_targets` 在执行前只读
声明本次每组 Memory/隔离输入可能影响的完整目标集合，包含共享目标及 invalidate
抛错前未能返回的目标。声明与执行共享同一次 mutation 准入，不得写库、提交或做 IO；
空集合必须是完整的无影响证据。未提供该能力的旧端口不能证明既有目标无关，因此
保守登记所有已知目标；提供完整声明时，集合外目标维持原可用性。
这只是 #18 受管投影适配契约，没有交付其真实清单或文件实现。

新增公共回归：`completed_target_reconcile_rollback_stays_unreadable_after_restart`；
`reconcile_fences_shared_targets_across_failure_and_restart`（普通/控制/失败记账不可写
× 有/无未决范围）。覆盖整批正文回滚、无关目标继续可读、共享义务不越权解除、
重启、文件失败、最终验证及原回执稳定。

声明方法抛错或其迭代器中途失败，同样不能作为无关证据：先将该操作退回全部已知
目标的保守禁读范围并提交，再报告安全失败。声明控制异常也在禁读意图提交后原样
传播，不转换为普通失败；已取得的部分目标不会冒充完整范围。

整轮声明先收集再登记：声明缺失或不完整的操作覆盖既存目标与同轮新发现目标的
全集，处理顺序不影响共享目标禁读。保存意图、提交或回滚再次失败时，按
`dominant_error` 保留先前控制异常；数据库不可写不被描述为已持久保存。

缺少完整目标声明还持久登记 `forget_projection_unknown(operation_id)`，防止旧端口
首次在 invalidate/后续恢复才返回的新共享目标绕过另一未完成操作。该未知义务解决前
受管目标读口和文件清理保守暂停；它不是“已确认污染”或失败明细，不改写删除回执。
可信完整声明可证明无关的正常路径没有该全局暂停。操作自身投影恢复成功后才移除
对应未知义务；其他操作的未知义务仍有效。控制中断后可能新增的是这类真实持久意图
和进度版本，而非伪造执行失败记录。

`reconcile_projections` 与 `retry_cleanup` 统一核对旧迁移恢复记录、目标 fence 和未知
输出义务：任一仍待恢复时必须提供可信完整受管清单证据。重启后的空配置不是空清单
证明，返回 `projection_inventory_required` 并保留所有义务及原回执。


v13 Standards 修复：重新传播声明的主控制异常时，不用显式 `from` 覆盖已有
`IncompleteRollback`。原清理句柄保留在 cause，次生数据库故障通过 context 可达；
调用方只遍历公开异常链即可使用标准 retry，恢复成功后重复 retry 不重复释放。
回归同时验证原控制对象、无效果事务执行、SQLite 回滚、既存禁读及重启后公共修改。

首次声明失败后不再执行其余声明，未求值部分保守归为未知输出义务；避免后续
声明取得新资源后产生另一个不可达清理 owner。原主异常及其恢复链保持。

普通异常也可能携带未完成 owner。相关错误归一化入口先调用标准
`raise_if_rollback_pending`：若公开异常图仍有未完成清理，传播原异常和恢复句柄，
不能转换成丢失 owner 的错误字典。清理完成、不含未完成 owner 的错误仍按原安全码
返回；未解决资源的调用者通过标准 handle.retry 恢复后再重试业务。

归一化检查只保留未完成恢复链，不在内外两层分别自动重试资源。投影执行失败先在
已回滚后的独立事务中记录安全失败事实，再传播仍携 owner 的原异常；调用者使用
公开 handle.retry 恢复资源。资源恢复不会使执行错误落入外层 invalid_input 映射。
回归另覆盖释放第1、2次拒绝、第3次成功，确认重试次数由公开恢复调用推进，
重启仍可观察 failed 与原回执，持续禁读。

文件清理端口的 verify/rebuild 普通异常即使携带未完成清理 owner，也先在独立
事务中保存 `cleanup_unconfirmed`，再传播原异常。范围未解决时仍为 needs_scope；
资源句柄 retry 成功只释放资源，不将 running/failed 任务当作已验证完成。
失败记账自身出错时保留原异常、公开恢复 owner 与次生错误，已有 running 意图
继续禁读并跨重启保持。控制异常不转换为普通失败。
`test_file_failure_is_recorded_before_returning_cleanup_owner` 的32个参数实例覆盖
verify/rebuild、已解决/未知范围、COMMIT可写/拒绝及三类普通异常/控制异常，
并验证标准句柄恢复不重复释放、重启状态、最终重试验证及原删除回执。

无未完成 owner 的普通文件错误若遇到失败状态记账故障，沿用安全存储错误
`storage_write_failed`，不得误分类为 invalid_input 或回显原普通异常。
`test_plain_file_failure_with_unwritable_result_is_storage_failure` 覆盖 ValueError/
RuntimeError × UPDATE/COMMIT 拒绝；未落定的任务保留 cleaning 与禁读事实。

失败结果记账在原文件异常的 except 上下文内执行。因此即使记账抛控制异常，
原文件失败及其 owner 仍由标准 context 链可达，控制异常对象保持为主异常。
`test_file_cleanup_owner_survives_failure_bookkeeping_control` 验证记账时钟中断、
公开恢复句柄、重复恢复、事务回滚及后续修改可继续。

投影声明失败被延后传播时，之后的记账或通知控制异常可能取得主异常地位。
`retain_failure_context` 通过无正文的context桥保留先前错误与主异常既有cause/
context，复用标准公开异常图恢复协议，不接管或推进资源清理。
`test_declaration_owners_survive_later_recovery_failure` 的64实例覆盖retry/reconcile、
记账时钟/提交后通知、声明普通/控制异常及次生普通/数据库/控制异常；两个独立
owner都只能经捕获异常图恢复，重复retry不重复释放，effects未执行、事务退出、
原回执与已有持久禁读不变。

早先异常继续为主时，Python重新raise也会替换已有context；`reraise_failure`
保存旧context，在raise生效后并回公开图再bare raise，因而原cause与旧/新context
都可达。声明延后传播、投影失败记账及pending-owner guard均用同一机制，且不自动
重试资源。64实例包含自然嵌套两个依赖（cause/context各owner），两阶段最多四个
owner；另六个文件/projection_retry/projection_reconcile×DB/控制记账故障实例
验证同根边缘。普通无owner仍沿用安全存储错误码。

声明实际失败时立即保存对应operation身份，早于可能再失败的围栏提交/通知；
不得等最终传播才设置attempted。32项回归覆盖两入口、多operation、needs_scope、
通知普通owner/控制及失败记账不可写；只记录实际失败操作，不把未执行操作记failed。

重新传播的旧context在raise前已连同旧cause封装并发布到公开cause；构桥/发布/
raise均在控制异常保护内，新控制显式保留待传播错误，原控制优先级不变。
14项独立子进程真实SIGINT回归覆盖旧链读取、构桥、复制cause/context、发布前、
raise前及raise后，普通/控制主异常均保留三个公开owner；每次必须确认信号确已
注入，资源只由公开句柄各释放一次。无自动跨层retry。

延后声明错误在原except内交给外层恢复调用持有，直到整个内层恢复过程退出。
外层try覆盖内层异常处理器入口、主异常选择及helper调用入口，避免依赖callee
内部try已生效。正常回执/事务行为不变；只在中断传播时交还完整异常图。
正式SIGINT回归现24项，包含真实call事件与调用者处理器边缘；有限外部适配扫描
另验证19个调用者/helper/投影/guard边缘均实际注入、全部公开owner恢复且零重复释放。
