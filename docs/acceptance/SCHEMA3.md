# Schema3：受控校准、agent 审核与分离的评分身份

Schema1/2 的历史解释、原样本与原 judge 不改。Schema3 支持 `offline_fixture`、`calibration`、`formal`，不接受把 trial 原包只改 phase 后运行。最小离线示例由 `examples_v3.controlled_batch()` 生成，明确 simulation，不是已批准的30案或在线授权。

`profile.execution_policy` 是闭合的版本1对象：应用和 SDK 重试均0，stream/stream_fallback=false，仅 DeepSeek 凭据，基础设施错误停机，逐案本地工具 mask，禁止外部业务副作用。策略同时作用产品、辅助与 judge；真实 Attempt 共用同一请求账、起点和 deadline。注入工厂也不能在一次逻辑响应内发第二次有通知的 Attempt；任意恶意 Python 绕过通知不在该能力约束的证明范围。401/400/403/404/422、非法响应与实际模型 ID 失配停止后续 dispatch。缺授权、unknown费用未接受、配置/候选漂移在构造客户端前拒绝。`http_client` 是显式离线受控传输接缝，不能用 simulation 默认访问真实模型。

Schema3 的 case.setup 是闭合声明：version、逐案local_tool_allowlist、公共 memory 保存/删除、具名完整 Skill、公共本地 calendar 种子、明确 simulation 的会话种子、aggregate keywords/sources、routing、可降低的限额、固定 fault_fixture。未知键/任意Python/路径不接受。前置状态须真实读取及重开核验；种子 Run 与首次采样 Run 分列，setup 失败不伪造产品结果。候选中支持的故障范围以 validator 和测试为准，未获批准的故障 ID 明确拒绝。

已批准的固定故障包括 `persona_version_conflict_v1`、`file_publication_unknown_v1` 和既有 `prepared_context_projection_error_v1`。前两项只通过构造期 `persona_read_observer=None` / `file_publication_checkpoint=None` 接入；Host/wiring 转发依赖，不提供 Settings、环境变量或任意脚本入口。observer 的返回值不能替换原 read 结果，异常沿既有 Registry/资源回收机制处理。T02 在真实 read 后，用同一排他准入下另一个公共 update 命令更新版本；其 simulation 来源、独立 operation/call identity、真实回执及重读位于 `evidence.setup.fault_fixture.observations`，不进入模型请求账。它不代表另一个并发用户请求。

T04 在持久 publication intent 之后、创建正文之前抛一次固定 OSError。`evidence.local_artifacts_before_reopen` 和原 tool projections 保留首次 unknown/stop；关闭前独占写入并 fsync `original-observation.json`，其相对路径、sha256 和完整原记录一并嵌入 `evidence.setup.fault_fixture.original_observation`。封存失败直接传播，不进入恢复。Host.start 本身不恢复；无 fixture 的重开 Host 先执行独立的 `查看操作` Run，再执行 `恢复操作` Run，分别保存 `recovery.before_recovery` 与 `recovery`，每项保留 Run/session/observed_at/真实回执及 evidence。两个命令仅使用禁止模型响应的离线传输；任何模型分派都使核验失败。原 `tool_verification` 时间字段保持原义，后续条目另带 acceptance 的 `collected_at`，`local_business.file_operations` 保存最终公共读回。未触发记 `not_triggered`；缺模型动作记 `coverage=not_covered`。动作齐备也只记 `requires_review`，不生成语义 PASS，不自动补调模型。

实际执行导出使用现有 Host 的 run evidence、tool_requests、parameters/model/audit 投影、operation verification 和本地业务读回。新增的是 acceptance result 证据，不是产品 runtime 字段。local_read 没有工具账不等于没有执行；证据缺失记 unavailable/unknown。目录与正文超过边界时拒绝完整性结论，不裁剪后宣称完整。删除回执不带已删正文；未核验原调用与重开后的核验分别留存。judge 看任务/gold/适用/禁止项、原始结果和语义规则；完整setup不作为judge的case输入，避免向judge泄漏已删源材料。

`rubric` 在 schema3 必须为 null；`semantic_rubric` 保存逐项意义，绑定 review_policy 与 judge 协议。grade 的历史字段 `rubric_id` 在 schema3 指向 semantic_rubric.id。`aggregation_policy` 独立保存组阈值、全体预先适用分母、unknown阻塞、禁止项零已确认违规、用户批准和校准证据hash；未批准时为 null，release 保持 BLOCKED。汇总阈值不送入judge，不改变既有 grade/material/calibration 内容身份；语义或judge改变仍需新校准。仅追加汇总政策通过 `EvidenceStore.revise(..., configuration={'aggregation_policy': ...})`，保留原结果/判分；已采样正式批禁止事后改阈值。测试夹具阈值不是通用批准阈值。

`source_family_id` 与版本化 `seen_families` 进入授权绑定；正式集拒绝已见家族、校准家族重叠或未登记来源。字符串家族标识不能自行证明实质独立，材料审批仍须审查语义变体。合成来源可经实际 agent 材料审批后用于另获授权的运行，不把source.kind重贴成真实产品样本。

审核身份见 [AGENT-REVIEW-POLICY-r1](AGENT-REVIEW-POLICY-r1.md)。材料 `agent_approved` 必须带真实 agent provenance、policy_id、case/semantic/seen hash；执行 authorization 仍由用户单独给出，agent 不能代批费用或阈值。

时间顺序也是准入条件：schema3真实执行的case与语义规则批准必须不晚于共享预算的实际起点；若已有输出，还必须不晚于最早采样时间。校准源在独立`import/read`和被正式批引用时都按源自身的原共享预算起点与最早采样核验，regrade/新judge预算不能抹去父链中继承结果的原起点；只有预算或只有样本时也分别构成执行事实。产品/judge公共入口传同一个`AuthorizedBatch.started_at`，不能以judge续接的新现在放过事后批准；校准引用逐样本核查语义批准时间。新版agent panel要求每名盲标者开始时间不早于绑定目标的首次输出完成时间，且两份标注完成时间不晚于裁决开始；按带时区的实际时刻比较，不猜最小时间间隔。旧schema1/2 human解释保持不变。

schema3正式执行还要求汇总阈值批准、所引用校准批准不晚于上述材料时间上界，阈值批准不得早于所绑定校准批准；这些检查发生在预占和模型工厂之前。已有`budget_started_at`或产品`results`的正式证据在`EvidenceStore.import_batch/read`校准引用处，对case、语义规则、汇总和所引用校准批准统一核对时序，以原共享预算起点和最早采样中的较早者为上界；汇总批准还不能早于校准批准；公共`report/verify`重读无效包会返回相应BLOCKED原因，不发布新报告。尚无执行事实的正式草案可先导入，待执行预检按实际起点核查。独立执行授权的`at`不得晚于本次注入clock的实际现在；合法judge续授权可晚于旧共享起点，原账本与deadline不重置。允许等时并按时区比较。该准入校验不证明签名真实性或更强的全局时钟保证，旧schema解释不改变。

acceptance创建的前置、采样、重开核验Host及业务证据读取资源，在构造前取得可恢复所有权，覆盖构造返回、start、执行和close边界。close返回False或抛错不得当作成功清理；未完成owner随异常保留，不能降级为available/unknown而丢失资源。恢复成功后重复retry不重复已经完成的关闭。

业务读回的state、outbox目录及正文fd使用既有`OwnedDescriptor`与`ResumableRollback`。原生`close`抛错时错误必须上抛，不能返回completed；但此时fd是否已释放可能无法确定。既有core在调用native close前消费fd号码，不重试不确定的号码，以免误关已复用的资源。owner retry结束只表示没有可安全重试的关闭动作，不能作为全部fd已释放的证据。测试分别保留native close前人工故障仍留1个fd（由测试显式清理）、可恢复边缘实际关闭及fd复用后不被重试误关的观察。

schema3的`judge_profile`顶层只接受`id/model/protocol`，其独立model覆盖与产品模型使用同一闭合字段检查。未知重试/采样控制在导入、读取与执行前拒绝，不能把未执行的控制值计入已生效的身份。schema1/2保留既有解释。

公开 CLI 的 `verify`、`report` 以及导入后返回摘要均会调用 `publish_report`：追加 `reports/<id>.json` 并更新 `latest-report.json`。退出码2仍可能已写报告。无写入核验使用 `EvidenceStore.read` 与纯 `report(batch, store=..., candidate_root=...)`，输出到stdout，运行Python `-B`；配合前后文件清单、内容hash和最新指针核对，不能仅凭方法名断言只读。

## c22 离线修复：截断投影与 judge-citations-v4

当工具的完整审计文本是 JSON 数组且超过模型投影上限时，能安全解析的数组只保留上限内完整数组项的原始文本片段，不重编码数值或转义内容；解析超出安全上限、解析失败、或数组项解码后触发中央脱敏时，从该项起隐藏并标记其余内容未知。数组形状但无法安全解析时不展示半条记录。非数组文本预览仍可能在字段中间结束，并明确标记。原始 audit、哈希和工具执行事实不变。这个离线可验证的输入边界不证明模型之后不会推断不可见内容。

新判分请求使用 `judge-citations-v4`。保留 v3 的完整 `reference_catalog` 和逐字引用校验，另给出从同一目录机械筛选的 `reference_guide`，指向任务、首次结果、实际工具/聚合证据和独立恢复观察的常用入口。guide 不改写 raw、不能把错误引用自动纠正为合法引用，引用可解析也不证明语义支持。新指令明确首次观察与后续恢复、命令文字与实际动作、unknown 与成功、无记录与已证明未发生的区别。历史 v3 包及原判仍按原协议身份只读验证；新在线 judge 执行要求当前 v4 身份、新材料和独立授权，旧校准批准不得沿用。ScriptedModel 只验证 wire、校验和拒绝行为，真实 judge 可靠性仍待新校准。

## c24 ST-01：重复 JSON 成员与原文投影

超限数组在任何嵌套对象中出现重复键（包括转义解码后相同的键名）时，整个数组预览保守隐藏为 `[]` 并明确标记其余未知；即使重复键的值不敏感，或位于本来不会展示的尾项，也不沿用 last-key-wins 结果证明原文安全。完整数组校验与原文项边界解码共用拒绝重复键的 decoder，保留所有获准原文项的数值和转义文本。该修复不更改 audit 原文边界、非数组前缀行为、限额内直通行为或 judge-citations-v4 身份；c23 原评审及后续 ST-01 FAIL 分别保留。

## schema3 基础设施错误整批停机

共享请求记账在模型调用返回 `final_error` 时保留原 Attempt/usage 并停止后续辅助、产品与 judge dispatch，包括429/5xx、传输超时及无HTTP状态或稳定错误代码的连接失败。该边界属于模型调用，不把工具业务拒绝、产品回答错误、合法unknown或HTTP200后的judge格式/引用错误变成基础设施错误。后者仍保留原judge_error，不自动补判。实际模型身份不符继续停机；schema1/2原错误分类语义不扩张。

judge续接沿原stop_reason、requests及budget_started_at；检查共享停止/额度后才构造judge工厂，已停止的产品批不能通过换store或进入judge阶段重开请求。已存在判分仍要求显式新regrade合同，不清空grades来续跑。零重试与停止后续逻辑请求分别验证，不能用零重试推导整批已停止。
