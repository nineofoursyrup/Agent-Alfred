# Issue #19 工具实现与验收记录

权威：[完整施工规范 v1](https://github.com/nineofoursyrup/Agent-Alfred/issues/19)。
实现基线：`772b02d1c5181a6e90aa9f7675f498e109a49313`。
本记录属于默认客户端兼容候选 v8；最终候选以完整文件清单及独立评审记录为准。
T49/T50 已在前序提交 a0a115c 获授权并通过真实模型验收；测试临时注入了真实
客户端标识及稳定 OpenCode session 请求头。v8 已在正式路径补齐兼容，并通过
离线 HTTP 验证；尚未新增真实模型复验，历史结果不冒充新候选实测。详见
[默认客户端请求头](opencode-client-headers.md)。

## 公共接缝与数据边界

模型通过 frozen ToolRegistry 获取声明和执行结果；RuntimeHost 串行执行真实
多 Step Run。模型不可提供权限、Origin、路径、租约或确认标志。
共享 MemoryCommandService 使用宿主持有的权限；业务表、记忆操作回执和唯一
工具账仍由共享事务提交。日程也在同一事务提交业务、稳定操作回执与账。
finalizer 独占正式会话消息和 Run 记录。

执行结果分为完整审计投影及模型投影。中央 Redactor 同时覆盖新工具事件；
SSE 去除 audit_content，Trace 中超过 256 KiB 的完整审计写为 0600 工件。
模型截断使用字符上限，携带原 UTF-8 字节数及 SHA-256；摘要不冒充完整结果。
删除成功使用固定无正文回执并停止后续工具／模型；文件未核验则固定失败收尾。

## Schema 子集 v1 与入参

Registry 构造时校验并递归冻结 Schema，缓存模型可见声明。只暴露 `name`、
`description`、`input_schema`。支持：`type`、`description`、`enum`；object 的
`properties/required/additionalProperties`（后者仅布尔值）；array 的
`items/minItems/maxItems`；string 的 `minLength/maxLength`；number/integer 的
`minimum/maximum`。不支持 `$ref`、组合关键字、正则及隐式类型转换；未知或不适用
关键字明确拒绝。所有工具根入参为 object，内置工具拒绝额外字段。

| 工具 | 必填入参 | 可选入参／语义 |
| --- | --- | --- |
| create_event | title, starts_at | ends_at, iana_time_zone, participants, notes；时间必须有 offset，participants 为文本 |
| query_events | 无 | since, until；以 instant 比较，闭区间 |
| save_fact | subject, fact | 仅明确保存；一条命令一个事实 |
| get_memory | kind, id | kind 为 semantic/episodic；返回当前版本 |
| query_memory | kind, text | 显式搜索目标 |
| update_memory | kind, id, expected_version | subject/fact 或 summary/occurred_at/occurred_until，由共享服务检查种类 |
| delete_memory | kind, id, expected_version | 成功只返回安全删除事实、指纹及清理进度 |
| draft_message | body | recipient, subject；程序分配 outbox 文件名，不发送 |
| read_persona | 无 | 完整内容、版本及显式文件覆盖状态 |
| update_persona | content, expected_version | 完整替换；当前 Run 不变，下一 Run 生效 |
| create_skill | name, description, body | 用户同名保护；内置同名只创建持久待确认候选 |

通用错误码闭合为 unknown_tool、invalid_input、configuration_required、
not_authorized、timeout、execution_error、unavailable。Memory 细分错误置于首行
`memory_code`，不新增通用错误码。外部能力未配置、已配置与授权三态正交；
未配置仅引导，denied 不暴露；真正 external 调用前必须持久 started 账。

## 文件持久协议与故障矩阵

文件不使用跨存储原子事务的说法。稳定操作 ID 来自可信 Run/Step/call_id，
与参数指纹分开。记录在 IO 前提交目标、候选、原内容及预期版本；创建独占目标
后、写正文前持久记录 dev/inode。相同正文或同名文件不能证明原操作已完成。

已有受管人格先以 no-replace 原子移动到操作专属 `.original-<id>.md`，
核验保留版本，再独占创建新目标。冲突时保留两者，不无条件回滚覆盖新来的文件。
旧 inode 暂不自动清理，避免仍持有旧 fd 的编辑器丢失修改。待核验人格使用已确认
内容快照并附明确状态说明；不加载不确定候选，无关工具继续可用。

| 状态 | 可证明事实 | 恢复行为 |
| --- | --- | --- |
| 无记录 | 未确认准备完成 | 不报告成功 |
| prepared | 已确认意图；文件可能不存在、部分写入或完整发布 | 核验候选身份、内容及原版本；证据不足保留现场 |
| complete | 文件同步及唯一业务账均已确认 | 回读历史回执，不重写人工编辑后的文件 |
| conflict | 当前或保留版本不符 | 暂停此目标后续写入，保留文件与恢复路径 |

| 持久边界故障 | 预期结果及夹具 |
| --- | --- |
| 准备提交失败 | 不开始文件 IO；FileTools.write 事务边界 |
| 创建后身份记录失败 | 已预留目标保持待核验，不产生成功账 |
| 部分正文写入／控制中断 | 保留操作目标及已有字节，零成功账；acceptance partial_file_write |
| 文件／目录同步失败 | prepared，后续核验同一 inode；recovery_edges deadline 与文件恢复测试 |
| 原人格保留前后人工修改 | 不覆盖；保留版本冲突或晚到目标被 no-replace 保护 |
| 发布后异常／账写入失败 | 同操作核验后补齐一次账；acceptance file_failure_recovery |
| 账完成后回执丢失／重送 | 回读同一事实，不再次创建 |
| close 再次失败／控制异常 | 长寿命 owner 保留重试；Host.close 不提前释放父资源 |

本版直接写独占的操作目标，不再使用匿名 `.create-*` 暂存文件；部分文件始终
有已持久操作记录可解释。同步 IO 在每个可开始的读写／同步阶段检查剩余期限，
不把已开始的系统调用宣称为可强制取消。记忆删除在真实 trace checkpoint 后及
数据库提交前再次检查同一期限；已经提交的事实通过持久回执重新核对。

## 验收入口映射

以下简称均位于 `src/agent_alfred/evals/deterministic/`：
R=`test_tool_registry.py`；H=`test_runtime_tools.py`；F=`test_file_tools.py`；
E=`test_tool_recovery_edges.py`；A=`test_tool_acceptance.py`；P=`test_tool_projections.py`。
共享核心的已有测试补充持久存储／权限／遗忘状态组合；不把核心单测替代真实 Run 接线。

| 要求 | 可复现证据入口 |
| --- | --- |
| T01 | R registry_rejects、malformed_supported_schema |
| T02 | R schema_snapshot、execution_pairs；全部内置声明经 Host 构造 |
| T03 | R external_policy 完整六格 |
| T04 | H calendar_created_then_queried |
| T05 | F draft_creates_independent；A memory_second_fact_failure |
| T06 | H calendar_created_then_queried；浏览器 tools.spec.js |
| T07 | R execution_pairs、progress_permission；A calendar_transaction_rolls_back |
| T08 | R expired_budget；已有 runtime_memory_io_deadline 和 loop 预算回归 |
| T09 | E file_deadline_after_prepare；H deadline 变体确认未删、后项继续 |
| T10 | R progress_permission_and_process_control |
| T11 | R full_unicode_audit；P sse_omits_audit、trace_moves_large |
| T12 | P real_fanout_accepts_tool_events；R central_redactor |
| T13 | A calendar_transaction_rolls_back；E recovery_never_commits；已有 memory_commands |
| T14 | R calendar_same_operation；A completed_skill_creation_replay；人格重送 |
| T15 | A calendar_uses_instants_and_read_does_not_write_ledger |
| T16 | A naive_calendar_rejected_without_writes |
| T17 | H memory_save_uses_shared_service_and_single_ledger |
| T18 | A memory_second_fact_failure_keeps_first_and_per_item_receipts |
| T19 | H 保存与删除测试；P 实际 FanOut；唯一 tool_ledger 断言 |
| T20 | A manual_mutation_is_busy、real_run_rejects_forged_origin；共享 admission 回归 |
| T21 | 共享 memory_commands 重送、版本、key_id、已删除不复活；H 真命令包装 |
| T22 | H memory_edit_invalidates_reference_before_next_step |
| T23 | H deleted_memory_stops_batch（真实 trace、零后续请求） |
| T24 | H 同测试 success/receipt_loss 的 model/audit/SSE 与 fingerprint/key_id 断言 |
| T25 | H 同测试 barrier/deadline；已有 test_forgetting_trace.py 生产队列阻滞矩阵 |
| T26 | A real_delete_receipt_separates_cleanup_failure_and_completion；H needs_scope |
| T27 | F draft_creates_independent、restart_recovers_same_operation；Schema 拒路径字段 |
| T28 | F explicit_persona_override；已有 settings 显式参数与环境优先级 |
| T29 | F managed_persona_changes_only_next_run |
| T30 | E persona_race_retains_human；A existing_persona_same_content_and_completed_replay |
| T31 | E 人格 pending 快照；A next_run_validates_manual_persona；F 下一 Run |
| T32 | F create_skill_writes_valid_file；E invalid_skill_metadata；隔离安装脚本 |
| T33 | F user_skill_created_after_startup_is_preserved |
| T34 | F builtin_skill_candidate_requires_direct_user_confirmation_after_restart |
| T35 | Schema 不接受 confirmed；A CLI；浏览器 tools.spec.js 宿主命令 |
| T36 | A late_approval_rechecks_candidate_and_target 三种变体；disappears 重送矩阵 |
| T37 | F builtin_skill_candidate 跨重启／重送；A completed_skill_creation_replay |
| T38 | A confirmation_cancel_order_has_only_legal_durable_result 两种顺序 |
| T39 | A partial_file_write、cleanup_control、file_failure_recovery；E 资源/期限 |
| T40 | F published_file_failure；A file_failure_recovery_commits_exactly_once |
| T41 | F recovery_preserves_manual_file_edit；E 人格竞态；A same_content_human_file |
| T42 | E persona_race 无关草稿成功；F 无关目标；待核验目标写入拒绝 |
| T43 | F published_file_failure_stops_run；固定 failed 与 operation_id |
| T44 | H 删除成功／异常，F 文件失败，R 控制中断；Runtime 原有记录失败矩阵 |
| T45 | E staging_close_failure（现为操作目标）；A cleanup_control／partial_file_write／open_return_interruption |
| T46 | A cli_candidate_and_operation_commands；tests/browser/tools.spec.js 三条流程 |
| T47 | 全量 pytest + 全量浏览器；既有 Session/Run/finalizer/预算回归 |
| T48 | scripts/check_installed_tools.py：wheel/sdist 新虚拟环境、python -I、临时 cwd |
| T49 | 历史 PASS（a0a115c）：真实模型 save_fact 与唯一账；注入请求头限定，本后继未重新调用 |
| T50 | 历史 PASS（a0a115c）：真实模型跨 Step 创建日程再查询；注入请求头限定，本后继未重新调用 |

## 实际命令与结果

- `uv sync --extra dev --locked`、`npm ci --ignore-scripts`：依赖已安装。
- `uv run ruff check src tests scripts`；`npm run typecheck`：通过。
- `uv run python scripts/check_skills.py`；`uv run python scripts/check_env_example.py`：通过。
- 全量 `uv run pytest -q` 首轮：3283 passed / 14 errors，错误来自 SDK 对环境代理 URL 的解析。
  清除子进程的 `*_proxy` 环境变量后，v2 冻结全量：3339 passed / 1 deselected；v3 两项修复相关 51 项定向测试通过。
  最终冻结候选的全量结果将在本地交付时报告。
- `npm run test:browser`：v2 冻结 60 passed；`npm run test:browser -- tests/browser/tools.spec.js`：3 passed。
- `uv build`：wheel/sdist 构建通过。使用 `uv export --locked --no-dev --no-emit-project --no-hashes`
  固定依赖，在两个独立临时 venv 安装相应产物，并以 `python -I scripts/check_installed_tools.py`
  执行真实草稿、内置覆盖确认、失败后重启恢复：两种产物均 PASS。
- 独立 Standards/Spec 首轮未通过；v2 Standards CLEAR，Spec 的候选取消重送和明确超时两项已修复。最终冻结核对与最新全量结果见本次本地交付记录。不得把此记录当作远端交付或 Issue 关闭。

## v4 独立反例修复

`test_tool_review_v4.py` 覆盖四项独立反例及提交、重启和控制异常边界。
原始反例及 red/green 日志保存在本地 `.scratch/issue-19/review-loop/`，
冻结清单以该目录 v4 交接包为准。

- 审计工件失败的嵌套清理由 Run bundle 长期持有。临时文件删除后的检查句柄
  也有独立重试所有者；清理未结束，父目录不退役、close 不报告完成。
- 预算从 Registry 入口固定计算，记账后、FanOut 准备后提交 started 前、
  事件发布返回后均检查同一绝对期限。准备耗尽时不进入 fn；external 已预留
  的账保存 failed 与“未启动”回执。对于已提交 started 后才耗时的监听器／简单
  emitter，保留已发布事件，finished 明确报告未启动，账不记 unknown。
- 有候选 dev/inode 证据后目标消失即 conflict；显式恢复和自动恢复都保留删除，
  不再创建，也不补成功账。共用 outbox、persona、Skill 路径遵守同一规则。
- v8 仅新增 external_tool_operations，旧迁移不改写。可信 run_id、step_index、
  call_id 联合作为唯一身份，完整参数及工具名指纹验证重送。首次意图与唯一
  tool_ledger 行在同一事务提交；最终状态与经过脱敏的模型回执同事务保存。
  重送回读原回执，缺回执的 started/unknown 拒绝重做；新身份相同参数仍可执行。
  调用方已有事务被拒绝且保持原样。该回执表不替代唯一业务账。

## v6 创建尝试持久边界

v9 前向迁移增加 publication_attempted。旧记录默认 1，因为旧协议的 NULL
candidate_identity 不能证明没有执行过创建；新操作显式写入 0，创建目标前先
提交尝试状态 1，再进入独占 open。尝试提交不确定、open 返回中断或身份提交
失败都不会让下一次恢复把缺失目标当作首次创建。目标消失时保留 conflict 和
零成功账；仅有明确未尝试证据的新准备记录可进入首次创建。已有 complete
仍回读历史回执。`test_tool_publication_attempts.py` 覆盖三类文件路径、显式与
自动恢复、创建返回及身份提交前后普通／控制异常、旧持久记录升级。

## v7 准备提交回执与固定收尾

FileTools.write 覆盖初始准备事务及其返回边界。异常后先保留可达清理所有者，
再回读原操作：只有确认没有持久记录时才报告已知未执行；原意图仍在时保留
原 operation_id 并停止当前 Run 的后续工具与模型。回读失败时返回 unverified
及同一恢复入口，不把数据库不可读误读为没有操作。进程控制继续传播，调用方
事务不被接管；后续 Run 只恢复原意图一次。`test_tool_prepare_receipts.py`
通过真实 Host/SQLite 的提交前后、回读和回滚失败，断言一次草拟不会因模型
重试加自动恢复产生第二份文件。
