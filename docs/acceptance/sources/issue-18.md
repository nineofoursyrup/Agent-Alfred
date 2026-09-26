# #18 实现：记忆提炼与 Markdown 镜像

来源：https://github.com/nineofoursyrup/Agent-Alfred/issues/18

> **2026-09-11 验收完成**：PR #51 已合并至 `c8434da508994e6dc2d2515edb98716278a4f6ec`，两阶段 CI 通过；C01–C30 及质量门槛已核验。[验收与已知限制](https://github.com/nineofoursyrup/Agent-Alfred/issues/18#issuecomment-5630462183)。下文保留已冻结施工规范；完整 Memory 页面仍属 #46。

## Problem Statement

用户需要普通聊天在积累到一定规模后形成可信、可核对的长期记忆，而不是每句话立即入库。当前提炼主体仍未实现，用户无法完整查看提炼进度、审批对人工保护记忆的改写，或通过真实 Markdown 镜像确认保存与遗忘结果。模型输出、数据库提交与文件同步是不同事实，混为成功会掩盖丢数据、陈旧审批及删除后旧副本仍可读的问题。

## Solution

按 Session 累计有效未处理聊天，默认达到 10 个后，在正常聊天保存后的空闲机会启动最多一批。使用 primary 在单 Step、默认 60 秒预算内生成有界语义归并计划和一条情景摘要；真实事务提交后才记提炼成功。人工保护改写整批审批，版本或来源变化使陈旧候选失效；失败显式重试，拒绝和超限跳过分别记处理结果且保留原聊天。

所有长期记忆变更刷新单向只读的 Markdown 镜像。用户能区分已保存、待审批、失败及镜像同步状态；删除后旧镜像禁读并通过持久任务恢复真实清理。完整 Memory 页由 #46 消费本票实际服务并做浏览器集成验收。

## User Stories

1. As a user, I want to have ordinary conversations remembered only after a configured threshold, so that casual remarks are not saved immediately.
2. As a user, I want to accumulate eligible conversations separately for each Session, so that unrelated conversations do not become one episode.
3. As a user, I want to see the current unprocessed count and threshold, so that I understand when consolidation can start.
4. As a user, I want to exclude failed, incomplete, unrecorded, and unsafe conversations, so that automatic memory uses trustworthy source groups.
5. As a user, I want to keep original conversations after consolidation, so that I can inspect the evidence later.
6. As a user, I want to process complete conversations in chronological batches, so that partial excerpts are not mistaken for fully processed sources.
7. As a user, I want to see when one conversation exceeds input capacity, so that I understand why a Session is blocked.
8. As a user, I want to explicitly skip an oversized source without deleting it, so that later eligible conversations can progress.
9. As a user, I want to keep low-volume leftovers below the threshold, so that the configured trigger is respected.
10. As a user, I want to use bounded existing-memory candidates, so that consolidation has a predictable input size.
11. As a user, I want to distinguish zero candidates from a failed memory read, so that storage failures do not masquerade as an empty library.
12. As a user, I want to know that semantic merging only covers visible candidates, so that I do not assume the entire library is duplicate-free.
13. As a user, I want to merge equivalent facts and update explicit changes, so that long-term memory remains coherent.
14. As a user, I want to keep plans, quotations, hypotheses, and completed events distinct, so that the assistant does not invent facts about me.
15. As a user, I want to avoid treating assistant claims as proof of completed actions, so that unverified actions are not remembered as real events.
16. As a user, I want to receive one faithful episode summary even when no new semantic fact is found, so that the conversation has an honest contextual summary.
17. As a user, I want to anchor episode timestamps to the actual source conversation interval, so that recollections and future plans are not assigned invented event dates.
18. As a user, I want to use the selected primary model for each generation, so that consolidation does not silently switch models.
19. As a user, I want to limit each generation to one model Step and a configured duration, so that background work does not grow without a bound.
20. As a user, I want to see real Attempt usage and costs, including failures, so that I can account for the work actually performed.
21. As a user, I want to have malformed model output fail without partial writes or extra repair calls, so that invalid plans do not create uncertain memory.
22. As a user, I want to have at most one automatic batch start after a saved conversation, so that backlogs do not monopolize the application.
23. As a user, I want to have the longest-waiting eligible Session selected, so that older ready work is not repeatedly displaced.
24. As a user, I want to see busy responses while a consolidation execution holds admission, so that concurrent writes remain predictable.
25. As a user, I want to continue chatting while a batch awaits approval, so that human review does not hold the global execution slot.
26. As a user, I want to limit each Session to one unresolved batch, so that competing proposals do not accumulate for the same conversation.
27. As a user, I want to approve the entire batch before protected memory is overwritten, so that explicitly saved information remains under my control.
28. As a user, I want to have approved new and changed records become human-protected, so that later automatic consolidation respects my approval.
29. As a user, I want to preserve original creation provenance and separate approval evidence, so that I can distinguish origin, later changes, and human confirmation.
30. As a user, I want to invalidate stale proposals when their actual dependencies change, so that old approvals cannot overwrite new facts or revive deleted material.
31. As a user, I want to record complete batch sources and actual memory reads, so that future forgetting follows known dependencies.
32. As a user, I want to reject a whole batch without saving its contents, so that unwanted proposals are not repeatedly regenerated from the same sources.
33. As a user, I want to retry failed work explicitly, so that the system does not keep spending money automatically.
34. As a user, I want to retry only the commit when an existing proposal is still valid, so that a storage failure does not require another model call.
35. As a user, I want to use a new proposal revision when regeneration is needed, so that old approval cannot authorize new content.
36. As a user, I want to recover durable results after a restart or lost response, so that successful writes are not repeated and uncertain results are not called success.
37. As a user, I want to restart without automatic new consolidation model calls, so that opening the application does not immediately consume model usage.
38. As a user, I want to read current facts and episodes as Markdown mirrors, so that long-term memory is accessible outside the main interface.
39. As a user, I want to refresh mirrors after every actual long-term-memory change, so that explicit saves, edits, deletions, and consolidation stay reflected.
40. As a user, I want to see database success separately from mirror synchronization failure, so that I know which work succeeded and which needs recovery.
41. As a user, I want to hide stale managed mirrors after forgetting and resume cleanup after restart, so that deleted material is not presented as current memory.
42. As a user, I want to see mirror content organized with concise identity, provenance, version, and time details, so that I can read and verify it.
43. As a user, I want to detect external mirror edits and confirm regeneration for the observed version, so that an old confirmation does not silently authorize a different file.
44. As a user, I want to understand the requirement to stop external editing during managed writes, so that I do not rely on an unsupported concurrent-write guarantee.
45. As a user, I want to see safe queue errors, source ranges, actions, and synchronization state, so that I can resolve approval and recovery work.
46. As a maintainer, I want to verify transaction, approval, deletion, and restart behavior through public services, so that tests prove observable guarantees rather than private call counts.
47. As a maintainer, I want to evaluate model content with at least thirty fixed human-annotated cases, so that quality is assessed separately from protocol correctness.
48. As a maintainer, I want to require all safety cases and at least ninety percent correctness and coverage, so that consolidation meets explicit acceptance criteria.
49. As an implementer, I want to have clear ownership between consolidation services and the Memory page, so that each issue can deliver and verify its actual integration responsibility.

## Implementation Decisions

### D00 范围、模块与既有契约

本票构建提炼服务、批次持久状态、受批准约束的整批写入口和真实 Markdown 镜像适配器；扩展宿主的系统 Run 调度及队列/镜像服务，复用现有 Session、ModelClient、预算、Store、MutationGate、遗忘与文件清理能力。修改相关 schema 时使用新迁移，保留现有接口责任和调用方事务边界。

沿用 [Memory 权威裁决](https://github.com/nineofoursyrup/Agent-Alfred/issues/31#issuecomment-5588135839) 的 R01/R02/R07/R09/R10/R13/R14，以及 ADR-0007、ADR-0009；下文冻结 #18 剩余选择并明确修订旧票的无边界去重表述。#18 交付真实业务服务及必要接线；#46 负责完整 Memory 页控件和浏览器集成，不以 #45 清理端口的替身 PASS 冒充真实镜像已集成。

### D01 术语与处理结果

提炼批次是一组来自同一 Session 的聊天及其候选；有效提炼聊天是正常完成、完整保存且允许自动使用的问答；提炼情景的对话区间是这批聊天实际发生的时间范围。工作窗口和提炼来源是不同投影，不能拿最近 N 轮窗口充当未处理来源。

| 来源结果 | 含义 | 再次累计 | 原始聊天 |
| --- | --- | --- | --- |
| 未处理 | 尚未成功提炼，且未被明确拒绝或跳过 | 符合资格时计入 | 保留 |
| 已处理 · 提炼成功 | 该来源所属批次的产物与处理标记已共同提交 | 不计入 | 保留 |
| 已处理 · 用户拒绝 | 用户明确拒绝整批候选，无该批记忆写入 | 不计入 | 保留 |
| 已处理 · 用户跳过 | 用户明确跳过阻塞提炼的超限来源，无记忆写入 | 不计入 | 保留 |

失败、失效和待审批不等于已处理。隔离或未知来源不进入自动提炼，也不因此伪造用户跳过或成功。现有 `agent_log.consolidated` 不能独自表达以上结果；以可区分的持久业务事实落实，迁移使用当前基线之后的新编号，不改冻结迁移。

### D02 来源资格与触发

每个 Session 独立累计；只有 `completed`、已成功记录且有完整 user/assistant 问答的 chat Run 可作为来源，再经遗忘服务确认允许自动使用。failed、max_steps、interrupted、system/probe、未保存或问答不完整的 Run 均不计入。安全性未知不能按安全处理，读取故障不能按零来源处理。

默认阈值为 10 个有效、未处理 Run，可配置正整数。达到阈值只表示可以启动，不代表立即完成或每批必须取满阈值数量。不同 Session 的来源不拼批，低频会话尾部可以长期留在阈值以下；不提供绕过阈值的立即提炼。

每次正常聊天完成并保存后，最多启动一个系统提炼 Run，选择等待最久且可执行的 Session。等待次序依据可恢复的就绪事实及稳定内部次序，不解析不透明 ID，不因反复扫描而重置等待先后。一次触发不连续清空积压；系统 Run、批准、镜像刷新均不冒充新的聊天触发。

同 Session 最多一个未落定批次。待审批或失败待处理时，新聊天继续累计，其他 Session 可继续提炼。模型执行使用现有全局准入，期间新 mutation/聊天请求按现有规则返回忙碌；进入待审批后释放执行占用，不让用户等待审批期间无法聊天。

启动时恢复批次事实和镜像清理，不自动发起新的提炼模型请求。中断执行按持久证据恢复；无法证明结果时不能伪造成功。

### D03 输入选择与配置

配置沿用 CLI／环境变量，启动校验并固定；Memory 页显示当前值，首版不另建配置文件或设置编辑页。

| 配置语义 | 默认值 | 约束 |
| --- | --- | --- |
| 有效来源触发阈值／单批来源数量上限 | 10 | 正整数 |
| 单次提炼执行时限 | 60 秒 | 有限正数，Attempt 重试共享 |
| 旧事实候选数量上限 | 20 | 正整数 |
| 旧事实候选组字符上限 | 16,000 | 正整数 |
| 完整请求字符上限 | 沿用现有 64,000 | 正整数，使用现有统一计量 |

具体参数名沿项目惯例确定并测试，不另外引入模型指派位。字符按现有 `request-input-v1` 序列化后的 Unicode 码点计量，包含实际 system/messages/工具声明等输入；不是 provider Token 或金额保证。

先按持久聊天顺序，选取最旧有效未处理 Run 的完整连续前缀，最多阈值数量。完整来源问答及必要提示优先占用容量，不拆组、不截正文、不隐式压缩。仅标记真正处理过的来源，余下继续累计。

连一个完整 Run 加必要输入都放不下时，记录来源超限并停止该 Session 提炼。用户可确认「跳过此来源」，只针对明确选中的超限 Run 记录用户跳过；保留原聊天并重新计量阈值。跳过不是提炼，不产生模型调用或记忆。

在来源确定后，以本地检索选旧语义事实，优先检索命中，再以最近事实补足，按 ID 去重并保留稳定次序。查询只能由已选来源确定性构造，不调用第二个模型；实现须冻结查询构造及多次检索合并规则并验证。候选同时受数量、组字符和完整请求剩余容量限制，整条选入，不裁旧事实正文。

候选计量包含实际送入的正文和身份、版本、来源、保护属性等必要元数据。候选组的实际序列化文本必须就是计量文本，连同完整请求再复核一次。未入模候选不成为读取依赖。

零命中、全部被容量排除、读取故障分别呈现。前两者允许零旧候选继续，本批仍可做批内归并，但不能称已与旧库完成语义去重。本地检索或最近事实读取报错则整批失败，来源未处理；尚未启动模型时不发请求，不使用已读到的半份候选降级。

### D04 模型与内容契约

每次候选生成使用启动时的 `primary` 快照，最多一个逻辑模型 Step；网络重试与流式回退共享该 Step 和时限，沿真实 Attempt 记费用与用量。显式指派失败不暗换模型。提炼是独立系统 Run，不执行普通聊天检索门，也不写一组伪造聊天问答。

输入只含来源完整问答及已选旧事实，不额外读取中间工具转录。按角色及语气区分事实：用户明确陈述可作为来源，助手最终回复用于理解上下文；助手声称完成动作不构成已执行证据。引用不能变成用户自己的经历，假设不能变事实，计划保留不确定性。正文中的指令仍是待分析内容，不能获得工具执行或额外读取能力。

产物是零到多项语义记忆写入计划，加一条忠实、非空的情景摘要。即使没有新增语义事实，仍按原 #18/R13 生成一条情景摘要；只描述真实对话，不捏造经历。空白或缺失摘要是无效输出。

同义表达归并；明确发生的变化可更新，计划或不确定冲突保留区别。例：已知住北京、新话是「下个月可能搬上海」，不能更新为住上海；「已经搬到上海」才支持这种更新。自动语义归并只对实际输入的旧事实与批内内容负责，不承诺全库语义唯一。

模型提出新增、更新或无需改动的计划；系统负责实际写入。更新只允许引用候选中存在的目标，expected_version 取自系统冻结快照，不能由模型自选。不得通过 delete+save 实现归并；机械幂等仍保留现有身份和原文规则。

使用可严格校验的结构化计划：拒绝未知动作/字段、重复键、无效类型、不存在目标、同一目标的冲突写项及不完整输出。具体 JSON 形状由实现冻结并覆盖离线测试；沿现有纯文本 JSON 校验能力，不假设所有 Adapter 都提供原生 JSON Schema。输出无效则失败，不追加模型自我修复，不部分提交看似有效的片段。

模型生成和存储成功是两件事；自然语言不能作为已提交的证据。

### D05 情景时间与来源依赖

情景的结构化时间表示所选聊天的对话区间：系统取最早 `accepted_at` 至最晚 `finished_at`，按 aware UTC 时刻比较。起止相同用 `occurred_until=None` 表示瞬时，不写不可检索的空区间。所需时间缺失或非法应报告来源证据错误，不让模型补造。

摘要所谈的回忆、计划可以发生在另一时间。保留其语义并带足够的对话日期语境，避免将「昨天」变成随阅读日期漂移的事实；结构化区间不由模型猜测。

每条产物关联整批来源聊天；提炼新增记录的创建 origin 保持批次粒度，改写既有记录不改变原创建 origin。批次／生成执行与各产物保持可追溯关系，使读取依赖能沿已知关系传到后续自动使用，而不是只留下孤立审计行。

在真实 Attempt 发起边界登记所有实际携带的历史组及旧事实 `(kind,id,record_version)`。本地候选选择不等于实际读取，失败的已发起 Attempt 仍保留真实读取与费用证据。模型自报的逐条来源子集不能替代完整来源和读取证据，也不复制旧事实正文来表达关联。

用户接受这种保守粒度扩大未来遗忘的自动使用隔离范围；既有 R09 的原始历史保留、独立记忆不按语义相似度连带删除等边界继续有效。

### D06 批次、审批与原子提交

沿用 R13 状态：`queued/running/awaiting_approval/succeeded/rejected/failed/invalidated`。持久记录 batch_id/revision、来源身份、实际读取的旧事实版本、目标版本、候选/差异、模型执行身份及安全状态；队列列表不另存或展示原始聊天副本。

默认本地路径中，事实、情景、来源关联、处理结果和成功批次事实必须同事务提交。调用方拥有事务；Store 不 commit/rollback，提炼不写 tool_ledger。不能在一个批次里逐项调用会各自提交的公共记忆命令入口，造成部分成功。

提交或批准时在同一 mutation 准入和事务中重新验证来源允许自动使用、所有目标版本、实际入模背景旧事实版本及人工保护。安全令牌过期不自动证明内容已变化；可重读并复核当前事实，但不能绕过真正的来源隔离、目标变化或失效。

普通自动记录可自动归并；覆盖 human_protected 目标时整批进入待审批，未保护新增项也不提前提交。保护属性提升可能不改变正文版本，必须独立重检；不能只检查 expected_version。

整批批准绑定具体候选修订及当前有效依赖，所有获准新增和改写内容都获得人工保护。新增记录的 origin 为 提炼来源（携批次标识）；改写保留原创建 origin，本次 `last_change_origin` 为 提炼来源（携批次标识）。批准事实独立留存，不把获准提炼伪装为 ManualOrigin，也不能把设置 human_protected 当作获得授权。当前 Store 会拒绝未经批准的提炼改写保护内容；#18 必须提供明确、受批准事实约束的批次写入口，保留普通路径的保护约束。

整批拒绝不写该批任何事实或情景，持久记用户拒绝后才推进来源已处理；不自动再次推荐同批。拒绝不显示为提炼成功。

实际入模的任一背景旧事实被编辑或删除，即使不是写目标，也使整批候选失效；来源、目标或已知使用链受删除影响同样整批失效并清候选正文/差异。仅本地命中而未入模的背景事实不因此牵连，但写目标仍须独立校验。

失效状态只保留安全元数据。迟到批准返回批次失效，不复活正文；新规划使用当前允许来源并重新满足阈值，旧批次保持失效。批准与删除按同一准入/事务的真实先后决定结果，覆盖两个顺序的确定性验收。

### D07 失败、重试与恢复

失败不自动循环调用模型，同 Session 等待显式处理。若已有完整且仍有效的候选，只重试提交，零模型请求；若没有有效候选，用户重试才启动新的系统生成 Run，形成新的候选修订，冻结当时 primary 并撤销旧审批。

每次重新生成最多一个 Step 和配置时限，可能再次计费；这不是整个批次生命周期只收一次费用。重新规划不能借重试忽略阈值。配置或上下文变化不允许把老候选冒充新模型生成。

提交响应丢失、进程中断或重启时，先核对已持久提交事实。已提交就读回原结果，不重复写入；未确认不能先标来源成功。恢复需要稳定批次/写项身份及可靠执行证据，不靠重新生成正文判断是否重复。

等待审批、模型执行、数据库结果与镜像同步分别显示。镜像 IO 故障不把成功批次改成数据库失败；模型 Run 已结束也不表示待审批批次已完成。

保留 ADR-0009 的 external 契约：unknown 不能当未执行重做，持久操作状态必须核对。本票不新增 external Store 实现或用外部检索可见性冒充写入回执。

### D08 Markdown 镜像

默认受管输出为配置状态根中的事实镜像和情景镜像，名称与目录沿用下文链接的既有 R14 契约。单向只读派生，不导入用户修改、不编辑原始 Skill、不维护历史镜像；用户自行复制的文件不托管。

所有长期记忆实际变更都触发刷新，包括明确保存、编辑、删除及提炼。根据当前有效 Store 记录重新生成，事实按主题组织、情景按发生时间倒序；稳定排序不从 MemoryId 推断先后。正文优先，每条附精简 ID、来源、版本、创建/修改及发生时间。镜像不包含批次候选、原始聊天或相关度分数。

镜像展示配置路径、生成时间、同步状态及安全的当前预览。只有核验当前输出覆盖所需变更后才报告就绪；写替换失败不能报告新镜像已生成。数据库已写但文件失败时显示「记忆已保存，镜像同步失败」，单独重试文件同步。

删除后旧镜像在应用内立即不可提供；持久任务从当前记录重建真实文件，逐项核验后才完成相应清理。路径按状态根重定位，不把旧机器的绝对路径当唯一身份。暂存资源也属于受管清理范围，不能用额外副本逃避清除。

检测到镜像被外部改动时停止覆盖、显示冲突，等待明确确认重新生成。确认绑定当时的文件身份与观测指纹，写前再次发现变化就拒绝旧确认；不自动导入、不另存历史备份。

保证前提：自动刷新、重建、清理期间停止外部编辑。指纹检查加原子替换不是文件 CAS，系统不保证保护任意非协作并发写入。自由编辑应在用户另存的副本进行。文件冲突阻止遗忘清理完成时如实显示未完成，不能因数据库已删就声称真实文件清理完成。

#18 接入 #45 的投影失效及文件清理端口：SQLite 参与者使用同一连接且不做文件 IO；文件重建在 mutation 准入内、数据库锁外执行，按 generation 核验，未经 verify 不开放预览。不保留被删正文缓存作为重建输入。

### D09 队列与用户操作

读侧至少提供每个 Session 的有效未处理数、配置阈值、阻塞原因，以及批次状态、来源范围、创建/执行/完成时间、候选修订、模型与实际用量、安全错误、镜像同步状态。原始问答通过既有会话入口人工查看，不复制到队列列表。

| 操作 | 适用范围 | 成功事实 |
| --- | --- | --- |
| 批准整批 | 当前有效待审批候选 | 全部获准写入共同提交，批准证据与保护生效 |
| 拒绝整批 | 当前有效待审批候选 | 无该批记忆，来源记用户拒绝 |
| 失败重试 | 失败待处理批次 | 按 S07 分提交重试或重新生成 |
| 跳过超限来源 | 明确选中的超限完整 Run | 无记忆、保留原文，来源记用户跳过 |
| 镜像同步重试 | 同步失败且无未确认外部冲突 | 当前输出经核验就绪 |
| 确认重新生成 | 当前已观测文件冲突 | 仅授权该文件版本，仍须写前校验 |

忙时 409，不把 mutation 请求排队，不提供绕阈值立即提炼。所有响应以持久结果为准，旧批准/旧文件确认/失效候选不可复用；响应丢失后提供查询已确认事实的恢复路径。#46 按此服务契约实现控件和浏览器验收。


## Testing Decisions

### 主验收接缝

以 RuntimeHost 装配的实际提炼/审批服务作为主验收入口：临时真实 SQLite/FTS、ScriptedModel 与捕获 Adapter 请求、实际受管镜像文件共同验证一次执行的外部结果。模型、时钟、文件故障作为受控依赖注入；优先复用现有共享命令服务、遗忘投影事务和文件清理端口，不为每个内部函数另设测试接口。

批准/拒绝/重试/跳过/镜像确认经相同公共业务服务验证；HTTP/SSE 接线验证忙态、稳定操作身份、查询回执和失效结果。完整页面的真实浏览器验收由 #46 承担。上述接缝沿用已确认的访谈设计及验收矩阵，没有增加新的产品前置。

### 什么构成有效测试

验证用户可观察事实：事务中途失败后事实、情景和处理标记共同未提交，原聊天一条不少；批准与删除两种竞争顺序均无复活；真实文件失败和重启后最终清理可核实；真实 Attempt 输入、费用与来源关联一致。不要用私有方法调用次数、模型自然语言自报、接口替身或广泛绿测代替这些证明。并发使用 barrier/event 与可控时钟，不用 sleep 碰运气。

既有先例包括：#17 的真实 Store/FTS 和调用方事务回滚、#45 的来源隔离/投影失效/文件故障恢复、#16 的完整问答及输入容量验证，以及现有 RuntimeHost/ScriptedModel/Adapter 捕获设施。模型内容质量与确定性执行协议分开验证。

### 确定性验收矩阵

| ID | 场景 | 必须观察到的结果 | 依据 |
| --- | --- | --- | --- |
| C01 | 单 Session 的有效来源为阈值减一/等于阈值 | 前者不启动提炼，后者在已定调度机会可启动；不提前保存长期记忆 | Q1/Q5；A03 |
| C02 | 多 Session 各不足阈值，总和足够 | 不跨会话凑数；来源批次保持单 Session | Q1 |
| C03 | completed/failed/max_steps/interrupted/system 混合，并含缺问答、未记录、隔离、未知来源 | 只计正常完成且完整保存、允许自动使用的 chat Run；故障不伪装空结果 | Q6；R13 |
| C04 | 源积压超过阈值，输入仅能容纳部分完整问答 | 选最旧完整连续前缀，数量不超过阈值；不拆组、不截正文、不标未入模来源为成功 | Q12 |
| C05 | 单个完整来源超限，随后用户确认跳过 | 先报告阻塞；确认后来源记用户跳过，原文保留、无记忆；余下来源重新计阈值 | Q19 |
| C06 | 10 个来源选入后剩余空间为零/有限 | 来源优先，旧事实同时满足数量、组字符及总请求限制；零候选可辨识 | Q20 |
| C07 | 本地检索命中与最近事实重叠 | 按既定优先次序去重，整条候选；实际模型输入与计量完全一致 | Q20 |
| C08 | 主模型切换、重试、输出非法或超时 | 每次生成冻结实际 primary；最多一个逻辑 Step，Attempt 共享时限且记真实费用；非法输出不自我修复 | Q3/Q9/Q11/Q15 |
| C09 | 聊天成功保存触发多个可执行 Session | 最多启动最久等待的一批；系统 Run 不递归触发；持准入时新聊天忙碌 | Q4/Q17 |
| C10 | 某 Session 的批次待审批或失败待处理 | 同 Session 不建第二个未落定批次；新聊天继续累计，其他 Session 不受此批次阻塞 | Q7 |
| C11 | 重启时存在积压与失败批次 | 恢复持久事实和文件清理，不自动产生新的提炼模型请求 | Q17 |
| C12 | 提炼写到事实、情景或来源标记中途失败 | 全部共同回滚，原始聊天完整且未成功处理；无 tool_ledger 项 | A43；ADR-0009 |
| C13 | 完整候选仅提交失败后显式重试 | 候选仍有效时只重试提交，零模型请求；提交已确认时读回原事实，不重复副作用 | Q22 |
| C14 | 无有效候选后用户显式重试 | 新生成执行独立记账，候选修订改变且旧审批不可沿用 | Q15/Q22 |
| C15 | 普通自动记录、保护目标及同批未保护新增项 | 自动可归并；覆盖保护目标时整批等待，不先提交新增部分 | A44/A53 |
| C16 | 候选产生后目标只新增人工保护、正文版本不变 | 提交时重检保护，转整批待确认；不能只验 record_version | R02/R13；A53 |
| C17 | 整批批准后写入新增和改写记录 | 全部获准新增/改写内容获得保护；新增 origin 为提炼，改写保留创建 origin 且 last_change_origin 为提炼，批准证据独立 | Q25；R02 |
| C18 | 目标、实际入模背景记忆或相关来源改变/删除 | 整批失效并清候选正文与差异；仅本地选中未入模的背景记录不因此触发失效 | Q24；A45 |
| C19 | 批准与删除分别先取得准入 | 两种顺序均不出现陈旧批准复活删除；已产生独立记录按既定遗忘规则处理 | A46 |
| C20 | 旧批准、旧候选修订及失效后的重新规划 | 旧批准拒绝；失效批次不恢复成功，重新规划用当前有效来源并重验阈值 | Q22；A45 |
| C21 | 明确整批拒绝后重启并继续聊天 | 不保存该批事实或情景，记用户拒绝，不自动再次推荐，原聊天保留 | Q16 |
| C22 | 候选与真实 Attempt 读取记录对比 | 每条产物关联整批来源；只有真实已发起 Attempt 登记实际旧记忆版本；本地选中不冒充读取 | Q23；R07 |
| C23 | 同义新事实、明确变化、计划/引用/助手自述夹具 | 已校验结构化计划被正确执行；机械幂等保留身份，语义更新不用 delete+save；内容判断另按模型质量评测 | Q2/Q8/Q13/Q14 |
| C24 | 跨日来源、不同 offset 及相同起止时刻 | 情景时间由系统产生并按 UTC 比较；批次对话区间与正文所述事件时间区分，相同时按瞬时可检索 | Q21 |
| C25 | save/edit/delete/提炼成功及镜像 IO 失败 | 所有真实变更触发刷新；数据库成功与镜像失败分开，不回滚已保存记忆或伪称镜像就绪 | Q10 |
| C26 | 镜像包含所删条目，替换失败，随后重启重试 | 应用不提供旧镜像；从当前 Store 重建，最终真实文件无所删条目；状态根迁移仍可定位 | A48；R14 |
| C27 | 镜像被外部改动，确认后又发生已观测变化 | 停止覆盖、显示冲突；不导入、不自动另存历史副本，旧确认不能授权新文件版本 | Q18/Q27 |
| C28 | 查看当前已核验镜像 | 正文与精简身份/来源/版本/时间可核对；主题组织事实、时间倒序情景；无候选、原始聊天或分数 | Q26 |
| C29 | 旧事实检索失败、最近事实读取失败或部分读取后故障 | 整批失败，原来源未处理；未启动模型时零模型请求；不以部分或零候选降级 | Q29 |
| C30 | 托管文件的已观测变化及确认失效 | 拒绝陈旧确认、报告冲突；文案和验收明确停止外部编辑前提，不宣称任意并发写入的强 CAS 保证 | Q30 |

这些条件保留并具体化旧票 A03、A43–A46、A48，及既有人工保护 A53 接缝；实现前不得把任何一项标记为已通过。

### 模型内容质量

模型质量另用至少 30 个固定、人工标注案例，评测前冻结来源、候选、期望语义项和禁止输出；覆盖明确事实、同义归并、变化、计划、引用、助手错误自述及零新增事实。标准答案允许同义措辞，不用字符串完全相等判断内容质量。

评分展开：将输出和标准答案按可独立核对的原子语义项对齐，新增/更新/保持的动作及目标也属于核对内容。正确性为正确输出项数除以全部输出项数；覆盖率为被正确表达的应提取项数除以全部应提取项数。逐案留明细，再累计分子/分母，两个指标各至少 90%；空分母标不适用，不补造 100%。情景摘要的事实陈述同样受正确性约束。

安全类用明确禁止项逐案判定并要求全部通过，例如把假设/引用当用户事实、将助手自述当执行证据、未经批准覆盖人工保护内容、使用隔离来源或用陈旧审批提交。结构/事务安全由确定性执行器验收，内容安全由模型案例验收，两份结果分别报告。

报告固定代码候选、语料版本、实际模型/配置、Attempt、用量、两个指标及逐案失败，不把一个模型有限案例通过推及所有模型或全库语义去重。真实模型评测需独立运行授权；本访谈仅确认门槛，当前状态 NOT RUN。


## Out of Scope

- 不实现全库语义唯一保证、第二个模型规划 Step、新检索后端或第三个模型指派位。
- 不允许绕阈值立即提炼、后台无限重试、失败降级为零候选，或为凑容量截断原问答。
- 不新增 external Store 实现；保留其已有 unknown 和稳定操作身份契约。
- 不导入或编辑 Markdown 镜像，不维护历史镜像，不托管用户自行复制的文件，不承诺任意外部并发写入安全。
- 不追溯擦除原始会话/既有 trace，不按语义相似度全域删除独立记忆，不扩展 Skill 匹配或编辑功能。
- 不在本票重做完整 Memory 页、模型设置页、工作窗口或 ToolRegistry；各既有模块按责任提供/消费接缝。
- 本次发布是规范交付，不执行代码施工、真实模型评测、Git 提交/推送/合并或 Issue 关闭。

## Further Notes

### 决策来源与当前证据

Q1–Q30 已于 2026-09-10 逐轮确认，用户随后调用 to-spec 要求合成并发布。本规范是 #18 的实施入口，不要求施工 agent 阅读访谈记录或本地未提交文档。本文替换旧正文中尚未冻结的阈值、模型质量和目录待定表述；已有 Memory 权威裁决未被本票明确细化的部分继续有效。

关键修订：配置沿 CLI／环境变量；启动阈值与实际批量分开；语义去重只承诺实际可见候选与批内范围；成功、拒绝、跳过分开记录；背景事实实际读取后变化也使批次失效；批准事实与创建/修改来源分开；镜像并发保证按停止外部编辑的前提限定。

发布前核查远端 main 为 `665ed94b75a0c86011814413a2980cafcd427001`，提炼器仍是占位。该基线已有完整问答和输入计量、实际读取登记、来源安全/投影事务、人工保护以及文件清理端口，仍须由本票完成真实消费者接入。

### 依赖与交付

本票的原生前置为 [#5](https://github.com/nineofoursyrup/Agent-Alfred/issues/5)、[#17](https://github.com/nineofoursyrup/Agent-Alfred/issues/17)、[#45](https://github.com/nineofoursyrup/Agent-Alfred/issues/45)，发布前均已关闭；GitHub 原生依赖仍为权威。保留既有依赖、标题与施工票身份，不创建重复施工票。

ready-for-agent 表示规范就绪，不表示实现、测试或真实模型质量已经通过。实施仍须独立执行 agent、TDD、适用机械检查和固定候选的 Standards/Spec 两轴独立评审；先核查认领、工作区及实际基线。真实模型评测须另获运行授权；Git 提交、推送、合并、发布及关闭 Issue 按相应明确授权执行。

该段为规范发布时快照。2026-09-11 已完成实施及验收：PR #51 已合并，PR CI 与合并后 CI 均 success（各 Python3746passed/1deselected、browser77passed）；固定32案独立GPT质量评分153/160、22/24，保留两案运行失败及七项内容错误。详见[完整验收记录](https://github.com/nineofoursyrup/Agent-Alfred/issues/18#issuecomment-5630462183)。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/18#issuecomment-5630462183

<!-- issue18-reviewed-closeout-74a7d97 -->
本票实施与验收完成，交付 PR #51。按 Session 的完整来源选择、冻结 primary 执行、持久批次与整批保护审批、显式重试/拒绝/跳过、依赖失效、真实 Markdown 镜像及 HTTP/SSE 消费接缝均已接通。

独立 Standards / Spec 行动项均为 0；代码及文档冻结后按64路径精确暂存，提交树与审阅字节一致。最终两次小增量只有实施文档与测试末尾两个空行；产品模型配置和运行代码保持真实质量评测的 s14 字节。

真实模型质量采用用户在执行前接受的固定32案金标。独立GPT输出审阅（非用户真人判分、非产品模型自评），含情景摘要事实：正确性153/160=95.625%，必提取覆盖22/24=91.6667%，满足各90%门槛；14项冻结禁止义务无触犯。32案首次结果30completed、2failed，QF05非JSON及QF08超时原样保留，各required coverage0/1，precision为N/A；没有用历史好结果替换。QF08无输出不证明主体辨识能力。七个内容错误保留计分，详见当前提交的实施说明。

结构/事务安全与内容质量分开验证。QF17原live只证明awaiting_approval/receipt=null；以原实际输入和捕获最终Text做独立Host/SDK/Adapter+MockTransport离线复放，受保护m09仍北京/v1，新增semantic/episode均0。该补验不是原live临时数据库后态。

本地历史证据保留：s8b全量3733passed/1deselected、browser77；s11全量3727passed/14setup errors/1deselected，原14项为本机代理IPv6 CIDR兼容问题，仅测试子进程适配后受影响文件34passed，分段覆盖3741选中项，不伪称原单次全量绿色；s14直接95项及32dry/3binding通过。最终GitHub全量结果另列如下，未通过跳过失败测试获得成功。

真实调用含历史尝试累计40Attempts：已知input40154/output13284 tokens，2Attempt用量未知，费用未知；当前固定32案已知input33028/output9658，1Attempt用量未知。未为提高分数重新抽样。原证据、逐原子评分、哈希和授权保留在协调工作树的 .scratch/review-repair-loop/evidence/ 与 quality/。


**交付及两阶段 CI（均已核验 success）**

- PR [#51](https://github.com/nineofoursyrup/Agent-Alfred/pull/51)，原提交 `74a7d97dbc627cbe9020f40a1bba41ae1a36b21b`，合并提交 `c8434da508994e6dc2d2515edb98716278a4f6ec`；merge tree 与审阅树一致。
- [PR CI](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34569193928)，head=74a7d97，Python3746passed/1deselected、browser77passed，全部门禁success。
- [合并后 CI](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34569762586)，head=c8434da：Python3746passed/1deselected、browser77passed，全部门禁success。
- [实现说明与真实质量限制](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/docs/implementation/issue-18-consolidation.md)；冻结规范在同提交 docs/design/ 中保留。

**C01–C30 验收索引**

每项依发布规范和随后修复核验接受；链接为当前合并树的主要回归入口，公共路径故障证据及跨模块回归共同构成验收，不声称一个测试穷尽整项条件。

| 条件 | 场景 | 当前测试证据 |
|---|---|---|
| C01 | 单 Session 的有效来源为阈值减一/等于阈值 | [PASS：test_default_threshold_is_ten_and_applies_per_session](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_consolidation_scheduling.py#L464) |
| C02 | 多 Session 各不足阈值，总和足够 | [PASS：test_separate_sessions_below_threshold_do_not_combine](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_memory_consolidation_service.py#L100) |
| C03 | completed/failed/max_steps/interrupted/system 混合，并含缺问答、未记录、隔离、未知来源 | [PASS：test_mixed_history_only_counts_recorded_permitted_complete_chats](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_consolidation_scheduling.py#L733) |
| C04 | 源积压超过阈值，输入仅能容纳部分完整问答 | [PASS：test_fitting_complete_prefix_marks_only_selected_sources](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_consolidation_scheduling.py#L703) |
| C05 | 单个完整来源超限，随后用户确认跳过 | [PASS：test_http_skip_limits_safe_errors_and_generic_run_rejection](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_memory_http.py#L312) |
| C06 | 10 个来源选入后剩余空间为零/有限 | [PASS：test_source_priority_can_leave_zero_candidate_room_without_looking_like_no_hits](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_memory_consolidation.py#L197) |
| C07 | 本地检索命中与最近事实重叠 | [PASS：test_duplicate_candidates_keep_search_first_order](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_memory_consolidation.py#L247) |
| C08 | 主模型切换、重试、输出非法或超时 | [PASS：test_eligible_sources_run_one_sessionless_system_run](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_memory_consolidation_runtime.py#L94) |
| C09 | 聊天成功保存触发多个可执行 Session | [PASS：test_one_opportunity_one_batch_and_later_chat_advances_backlog](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_consolidation_scheduling.py#L110) |
| C10 | 某 Session 的批次待审批或失败待处理 | [PASS：test_unresolved_session_does_not_starve_other_ready_work](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_consolidation_scheduling.py#L631) |
| C11 | 重启时存在积压与失败批次 | [PASS：test_retry_restart_reads_own_durable_result_without_startup_calls](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_consolidation_retry_ownership.py#L309) |
| C12 | 提炼写到事实、情景或来源标记中途失败 | [PASS：test_rollback_after_intermediate_write_keeps_chat_and_drops_facts](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_memory_consolidation_service.py#L186) |
| C13 | 完整候选仅提交失败后显式重试 | [PASS：test_http_commit_retry_and_regeneration_have_distinct_model_effects](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_memory_http.py#L152) |
| C14 | 无有效候选后用户显式重试 | [PASS：test_retry_prepare_failure_keeps_each_generation_revision](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_consolidation_retry_ownership.py#L374) |
| C15 | 普通自动记录、保护目标及同批未保护新增项 | [PASS：test_protected_target_waits_for_approval_and_grants_protection](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_memory_consolidation_service.py#L215) |
| C16 | 候选产生后目标只新增人工保护、正文版本不变 | [PASS：test_protection_only_promotion_keeps_candidate_and_retry_awaits_approval](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_memory_consolidation_service.py#L708) |
| C17 | 整批批准后写入新增和改写记录 | [PASS：test_http_protected_approval_and_delete_never_reveal_candidate](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_memory_http.py#L228) |
| C18 | 目标、实际入模背景记忆或相关来源改变/删除 | [PASS：test_background_keep_edit_invalidates_whole_batch_omitted_hit_does_not](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_memory_consolidation_service.py#L255) |
| C19 | 批准与删除分别先取得准入 | [PASS：test_delete_then_approve_does_not_revive_and_approve_then_delete_stays_deleted](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_memory_consolidation_service.py#L472) |
| C20 | 旧批准、旧候选修订及失效后的重新规划 | [PASS：test_invalidated_submit_replay_does_not_resurrect_candidate_body](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_memory_consolidation_service.py#L617) |
| C21 | 明确整批拒绝后重启并继续聊天 | [PASS：test_reject_marks_sources_without_facts_and_survives_restart](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_memory_consolidation_service.py#L345) |
| C22 | 候选与真实 Attempt 读取记录对比 | [PASS：test_stream_fallback_confirms_both_sent_attempts](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_memory_consolidation_runtime.py#L391) |
| C23 | 同义新事实、明确变化、计划/引用/助手自述夹具 | [PASS：test_identical_consolidation_update_does_not_invalidate_other_batch](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_memory_consolidation_service.py#L893) |
| C24 | 跨日来源、不同 offset 及相同起止时刻 | [PASS：test_distinct_source_dates_yield_distinct_model_date_context](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_memory_consolidation.py#L389) |
| C25 | save/edit/delete/提炼成功及镜像 IO 失败 | [PASS：test_actual_dashboard_idle_and_file_only_changes_publish_persistent_revision](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_memory_sse.py#L98) |
| C26 | 镜像包含所删条目，替换失败，随后重启重试 | [PASS：test_restart_relocation_and_full_store_output](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_memory_mirrors.py#L109) |
| C27 | 镜像被外部改动，确认后又发生已观测变化 | [PASS：test_http_mirror_confirmation_busy_identity_and_durable_query](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_memory_http.py#L57) |
| C28 | 查看当前已核验镜像 | [PASS：test_standalone_save_creates_complete_current_managed_mirror](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_memory_mirrors.py#L24) |
| C29 | 旧事实检索失败、最近事实读取失败或部分读取后故障 | [PASS：test_candidate_read_failure_is_durable_and_does_not_auto_retry](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_consolidation_scheduling.py#L310) |
| C30 | 托管文件的已观测变化及确认失效 | [PASS：test_external_conflict_delete_and_stale_confirmation](https://github.com/nineofoursyrup/Agent-Alfred/blob/c8434da508994e6dc2d2515edb98716278a4f6ec/src/agent_alfred/evals/deterministic/test_memory_mirrors.py#L79) |

跨票边界：完整 Memory 页面的真实浏览器验收由 #46 承担；本票提供可调用HTTP/SSE公共接缝，不宣称已交付该完整页面。Markdown 刷新/重建/清理仍以停止外部编辑为前提，不承诺任意外部并发写入的强CAS。原主目录及决策工作树中的用户改动保留；没有部署、发布版本或删除工作树。


两轮CI中的1deselected均遵循仓库既有 `-m not requires_key` 默认选择；未扩大live调用范围或临时剔除本票失败测试。
