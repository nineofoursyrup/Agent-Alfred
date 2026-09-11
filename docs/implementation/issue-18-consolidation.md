# Issue 18：提炼输入、批次与运行时执行

权威行为见发布施工票
[Issue #18](https://github.com/nineofoursyrup/Agent-Alfred/issues/18)
（D 节；旧 S07 文本对应 D07）。本记录是提炼、批次与运行时接缝的实现说明，末节汇总最终本地验收事实。
本地验收不等于发布完成；最终 GitHub 终态以实际 PR 合并与 Issue 状态为准。
原冻结发布规范保留为当时的历史快照，不随本说明回写。

## 公共接缝

`agent_alfred.memory.consolidation`：

- `select_consolidation_sources`：同一 Session 内、达到正整数阈值后，按调用方
  提供的最旧顺序取完整连续前缀；数量不超过阈值；用现有
  `request-input-v1` / `input_characters` 计量必要提示加来源问答。
- `prepare_consolidation_request`：在已选来源之后绑定旧语义候选。
- `prepare_consolidation_input`：快照测试用的两步包装。
- `parse_consolidation_plan`：有限 JSON 计划；版本与情景时间取自服务端已选输入。

提供的来源/候选快照**不**证明真实安全、完整资格或 Attempt 读取。后续服务必须
用遗忘读闸与存储加载建立资格，并在真实 Attempt 边界登记读取。

## 冻结规则

检索查询由已选来源按顺序拼接每条 `user_text` 与 `assistant_text`，以单个空格
连接；不调用第二个模型，不做 NFKC。合并规则为检索命中优先、最近事实补足，按
`MemoryId` 去重并保留首次出现次序。候选组计量文本是无空白 JSON 数组，字段次序
为 `id/version/subject/fact/origin/human_protected`；外层 `长期记忆候选` 标题
计入完整请求而不计入 16,000 组额度。整条选入，不拆不截，不跳过超长项。

计划对象只允许 `semantic` 与 `episode_summary`。动作为 `create` / `update` /
`keep`。`expected_version` 与情景区间由已准备快照填入；模型自带版本或时间戳为
未知字段。重复 JSON 键、未知字段/动作、错误类型、输入外目标、同一目标冲突写、
空摘要、非 `end_turn`、缺少最终文本或多个 `TextBlock` 均拒绝。模型响应须恰有
一个 `TextBlock`；允许辅助 `ThinkingBlock`，但其内容完全排除于计划解析之外。
工具调用及其它不支持的块仍拒绝；最终文本必须是原始 JSON，Markdown 围栏或
前后解释文字不被剥离或修正，仍严格拒绝。错误码不回显正文或异常原文。

情景区间为已选来源最早 `accepted_at` 至最晚 `finished_at`，先变为 aware UTC
再比较；两端相等则 `occurred_until=None`。缺失、naive 或结束早于开始不猜测，
并在序列化请求之前返回来源证据错误。

每条来源在入模前插入一条独立 user 消息，正文为
`来源语境={"run_id":…,"accepted_at":…,"finished_at":…}`：时间保留来源自身
aware `isoformat()`（含 offset），不改写随后的用户/助手原文。该信封属于必要
提示/来源容量，与容量选择和实际 `ModelRequest` 共用同一构造器；
不在候选装入后再追加未计量的日期脚注。系统提示要求把「昨天」「下周」锚定到
信封时刻，而不是阅读日。检索查询仍只拼接用户与最终助手正文。

## 批次生命周期（slice 2）

`MemoryCommandService.consolidation` 在真实 SQLite / Store / 遗忘接缝上持久化
批次。v11 新增 `memory_consolidation_*` 表，不改冻结的
`consolidation_batches`/`consolidation_ops`。队列元数据只有 Run 身份与安全状态，
不含原始问答。

公共动作：`submit`（调用方提供已生成的模型输出，本层不发网）、`approve`、
`reject`、`retry`、`skip_oversized`。候选先独立提交；事实、情景、来源成功标记
与成功回执在后续同一事务。Store 不 commit。覆盖人工保护目标时整批
`awaiting_approval`。批准写入走 `apply_approved_consolidation`，绑定已持久化的
批次修订与批准行及计划正文；普通 `update` 不接受解绑布尔许可。
`ProjectionParticipant.invalidate` 在来源隔离或入模依赖被编辑/删除时清候选正文。
明确重复保存仅提升 `human_protected`、正文版本不变时，不得把有效候选标成失效；
提交或重试时重检保护，覆盖保护目标则整批 `awaiting_approval`，无部分写入。
这是 C16 的提交时重检，不是把保护提升当成依赖失效。真正的正文编辑或删除仍失效。
动作回执只存 `action` / `batch_id` / `revision` 等身份；候选正文来自当前有效修订的
批次投影。失效、拒绝、重新生成修订或重启后重送都不得从旧 receipt 复活正文。
成功提交后立即清候选正文；已落定成功批次不因产物删除改成失效。审批写入故障
持久为 `failed` 并保留未消费批准，显式 retry 可无模型提交。无变化 update 不失效
其它批次。

本层不登记伪造 Attempt 读取；真实 `register_read` 由运行时的实际请求边界登记。

## 稳定重试与运行时所有权（s3j-g3）

`RuntimeHost.retry_consolidation` 先经共享动作入口验证原始
`operation_id/batch_id/expected_revision`、动作、可信来源和 HMAC/key_id。
有效完整候选只重试提交，不新建 Run、不再次调用模型。需要重新生成时，
动作持久记录 `generation_required`；此时尚未准入，不代表原失败批次的结果。

新的内部准入参与者在原 accepted Run 事务内，将操作回执的
`generation_run_id` 与 Run 行共同提交；逻辑发布和控制异常恢复仍归现有
RunAdmission。事务失败不会留下孤立绑定，忙碌或配置失败后相同动作可继续。
一旦已有 Run 绑定，就读取该 Run 的准入与终态证据，不把不确定结果当未执行。
内部 begin 动作用 Run 身份，与用户原始 retry 参数身份分开。

读回匹配该 Run 的实际批次修订。后继 Run 替换当前 batch 之前，同事务保留前一
操作的无正文结果元数据；旧 retry 不读取后继候选或 products。已准入但尚未
begin 的操作从 runs 读回 interrupted/unconfirmed 等证据，不冒用前一生成错误。
准备读取失败也分配新的批次修订；低于阈值或来源超限读回实际 begin 结果，
不伪称已生成。`get_action` 提供不触发 mutation 的持久结果查询，已保存的
结果可以在 recording 失败时继续查询；重放动作仍校验 HMAC/key_id。

本变化扩展现有 JSON 安全回执，不新增表或修改已冻结迁移。回执中的
`generation_result` 仅含批次/Run 身份、状态、时间、来源 ID 与安全提交回执，
不保存模型计划、旧事实、请求或情景正文。

RunRecorder 的原收尾事务结算本 Run 遗留的 running 批次，处理 begin 已提交
但返回边缘被打断的情况；已成功或待审批批次不受 recording 结局改写。
通知在提交后发送。重启先恢复 Run，再结算遗留批次，启动不发模型请求。

发送前在实际 Attempt preflight 再核对 batch/Run 所有权和旧事实内容版本；
来源安全检查与版本检查分别生效。仅提升人工保护不拒绝输入，在整批提交/
审批处重检。目标、实际背景与来源变更的验证覆盖准备后、实际 Adapter 发送、
finish 和批准边界。测试中的受控端口持有真实 Run 权限；普通并发用户命令
仍返回 busy，不支持绕过准入的外部 SQL 并发写入。

维护回归入口：`test_consolidation_retry_ownership.py`、
`test_consolidation_host_dependencies.py` 与已有
`test_memory_consolidation_runtime.py`。采用 ScriptedModel、MockTransport、
实际 SQLite/TraceSink 和只管理自有临时子进程的崩溃重启测试；不使用真实模型。

## 历史生成结果与正文清理（s3k-g3）

所有 `begin_generation` 动作（包括自定义初次 operation_id）都持久绑定实际
生成 Run。修订交接前，与 retry 意图一起归档自身的安全结果；历史动作查询
不以当前批次的后继结果代替自身结果。旧格式回执只按同修订或已有终态证据
恢复；缺少证据时返回 `generation_unconfirmed`，不猜测后继状态或 Run。
failed finish/approve 仍保留各自动作的失败事实。

修订交接在同一事务内清空被替代修订的请求、候选、计划和情景正文；成功、
拒绝和失效清空该批次截至目标修订的所有正文。启动恢复及遗忘参与者也清理
旧版本遗留的历史副本，包括当前批次已终结的情况。安全来源/读取身份元数据
保留，当前仍有效的 failed/awaiting 候选继续用于显式重试或审批。清理失败
必须回滚同事务的删除或修订交接，不报告已经成功清理。

维护测试覆盖普通成功/拒绝后删除、控制异常失效、多轮 retry 的 begin 查询、
自定义初次身份、旧回执重启兼容，以及 SQLite 清理失败后的事务回滚与重送。

## 聊天后调度与可恢复就绪事实（s4-g3）

v13 只追加无正文的 `memory_consolidation_ready` 和
`memory_consolidation_triggers`。来源始终从完整持久 chat 日志与遗忘资格加载，
不用工作窗口凑数。就绪次序来自达到阈值的来源 user 日志内部序号，同一未消费前缀的反复扫描/重启保留次序；消费或资格变化后
按当前前缀的就绪点更新持久次序。隔离/未知或未成功保存的来源不参与计数，读取故障
产生可见失败事实；不会把候选检索失败降级成零候选。

正常聊天的完整问答和触发身份在同一个录制事务提交。录制 settlement 发布
recorded 状态并释放原 Run 租约后，通过既有 mutation 准入扫描一次，再异步
准入最多一个最旧可执行 Session 的提炼 Run；不在同一 worker 等待后继完成。
原聊天结果先已发布，正常路径的 done 通知在调度回调返回后发送。若调度及其错误
回执无法完成，独立尝试 done 通知，保留失败调度的 settlement owner 供恢复/close
继续处理；通知失败也保留自身 owner，不以通知成功代替调度收尾。回调具备独立重试阶段；
持久机会在调用 admission 前消费，accepted Run 与 trigger 绑定在同一准入事务，
返回丢失不会再次消费。拒绝或未确认交接保留真实状态，后继仍归既有 admission/
execution/recording owner；旧 finalizer 不能释放新 Run。

失败、未记录聊天与所有系统 Run 不创建机会。批准、拒绝、重试、跳过均不伪造
新的聊天触发；同 Session 的 queued/running/awaiting/failed 阻塞不会饿死别的
就绪 Session。启动只重建业务/就绪事实并使未消费机会过期，不调用模型。
一次回调不递归清积压，后续正常聊天才提供下一次机会。

超限记录绑定实际最旧完整 Run 及测得的字符数/上限；显式 skip 重验该身份和
当前计量，仅给该来源记 user_skipped，保留原聊天，零模型/记忆写入。重新计算
阈值并更新下一阻塞；旧身份不能跳过新来源。候选读取故障保存失败批次及已选
来源元数据，不标来源成功，也不在下一聊天机会自动重试。

维护入口新增 `test_consolidation_scheduling.py`、`test_schema_v13.py`。
既有 `_traced_host` 夹具只新增可选正整数 threshold 参数，原默认 2 不变。
v12 测试在受控 v12 ledger 上继续验证 v12 终点；总体 schema 明确登记 v13。
旧独立 threshold=1 手工传播夹具在 s4 证据中保留原件，并提供 threshold=2、补足
一个独立完整来源的版本化后继；原传播与立即失效断言没有弱化。

## s4b 修复：完成通知隔离与当前前缀

载入、计数与纯来源选择均排除空串/仅空白的 user 或 assistant；正常含首尾空白的
非空内容按原文保留。此规则也适用于重启扫描的已有历史，不只拦新触发入口。

就绪顺序以当前有效、未消费前缀达到阈值的内部日志序号为准。相同前缀不因扫描
重置；成功提交、approve/reject 消费来源时同事务更新/移除 ready，skip 和资格变化
重新验证时更新。失败和 awaiting 不代表消费；低余数退出 ready，再积累时使用新的
真实就绪点。固定 v13 迁移不改写。

维护回归新增 `test_consolidation_scheduling_repair.py`：持续调度读取与回执双重故障、
通知返回丢失、原聊天结果可达、后继真实 submit/close 所有权、空白问答及历史保留、
success/approve/reject/skip/isolation/低余数重积累后的公平性与重启稳定性。

## 历史后续义务（s6 前快照）

以下保留当时的待办状态；最终本地验收见末节。真实镜像 IO / CleanupPort 清单、队列 HTTP/SSE 与批准设计整合已在后继片段落实，见下文；当时整合验收仍待完成。镜像同步失败不得改写已成功批次。
当时人工标注质量包及真实 primary 内容评测仍需独立准备和运行授权，状态为 NOT RUN。

## s6：可调用的队列、操作与镜像接口

本片段接入实际 Dashboard HTTP、RuntimeHost、SQLite 和受管文件；完整 Memory 页控件由 #46 消费这些接口。状态码与回执是业务事实，SSE 只提示重新读取。

- `GET /api/memory/consolidation` 返回 Session 的有效未处理数、阈值、阻塞和批次摘要。可选 `session_id`、`limit`（1–100，默认 50）、`offset`（0–1000000，默认 0），两份列表各有 `*_has_more`。分页是当前视图，变更后从首页重新读取；不承诺跨页快照。列表没有原始问答或候选正文。
- 同一路径单独传 `batch_id` 读取候选详情；当前来源和实际入模记录通过读取校验后才有 `candidate_available=true`、`plan`、`episode_summary`。完成、失效或依赖被删后不开放旧正文。`source_range` 是来源对话的 UTC 区间；`run` 与 `model_usage` 是实际执行记录，未知用量保持未知，不补零。
- `GET /api/memory/mirrors` 返回当前两个文件的配置路径、生成时间、同步状态；可选 `name=facts|episodes`、`preview=0|1`。预览使用同一当前文件核验，不提供冲突、未核验或删除后的旧正文。
- 任一读取路径单独传 `operation_id` 可查询对应操作的已持久结果；未知操作返回 404。镜像动作使用 mirrors 路径，提炼动作使用 consolidation 路径。历史回执只代表自己的事实，不借用后继批次或文件的成功。

`POST /api/memory/consolidation/actions` 使用现有同源、CSRF、JSON 和请求大小防护。必须提供 `schema_version:1`、`action`、非空 `operation_id`，再按动作提供以下字段；未知字段和伪造 origin、permission、模型输出均拒绝。

| action | 其余必需字段 |
| --- | --- |
| approve / reject / retry | batch_id、expected_revision（正整数） |
| skip_oversized | session_id、run_id |
| mirror_retry | name |
| mirror_confirm | name、observation（当前镜像读接口的 confirmation 对象） |

写入沿用现有可信准入，忙时 409，不排队。陈旧批次/身份、操作参数冲突、外部文件冲突返回 409；输入不合法 400，存储不可用 503。同步完成返回 200；retry 获准启动独立模型 Run 返回 202，仍须查询 operation 和 Run 的实际结果。完整有效候选的提交重试不再调用模型；没有有效候选才由 Host 固定当前 primary 并重新生成。没有绕阈值 generate 动作，通用 Run 提交仍拒绝 consolidation purpose。

同 operation、同参数重试读取原回执；不同参数拒绝。响应丢失不能视为成功，客户端先查 operation。批准绑定候选修订；镜像确认绑定当时观测文件的身份、指纹和所需版本，旧确认不授权覆盖后继文件。自动刷新、重建和清理期间须停止外部编辑，指纹检查加替换不是任意并发写入的 CAS。

镜像错误与数据库提交分别呈现。受管文件重建、确认和恢复已在 s5/s5b 实现；本片段补 operation 查询和同步重试身份，不改变原有清理完成条件。

## memory_patch 与验证状态

实际 MemoryCommandService notifier 接入 SSEBroker。`memory_patch` 只有 `schema_version:1`、`process_instance_id`、持久 `memory_revision`、`change:null`；没有正文、subject、领域 seq、SSE id/checkpoint，也不伪造 Run 或复用 lifecycle 的 state_patch。v15 将批次、动作、阻塞、调度及镜像状态纳入持久 revision；文件同步结束后再次通知最终状态。通知失败不撤销已经确认的数据库事实。

有界 ingress 或连接无法必送时关闭旧连接；重连携带当前进程/修订，客户端据此重新读上述接口。旧进程或旧修订不能使当前状态倒退。memory_patch 不是提交回执，批次状态、Run outcome、DB 提交和镜像同步各自核对。

以下为 s6 历史验证快照，不代表最终状态。s6 定向验证及 C01–C30 的准确测试/继承证据见冻结包 `evidence/s6-g3/coverage.md`；完整路径为工作树 `.scratch/review-repair-loop/evidence/s6-g3/`。本片段不例行重跑历史 68 个脚本或全部旧套件。全量整合、frontend typecheck/browser 回归及独立两轴验收尚待协调器；至少 30 个人工标注案例和真实 primary 内容质量评测仍 **NOT RUN**，不能把 ScriptedModel 的结构/事务验证称为内容质量通过。

## s6b：读取版本与最终执行证据

队列、批次详情、镜像（包括一次读取两个文件）及 operation 的当前投影，都在读取完整结果前后校验持久 revision。读取期间发生记忆状态变化时返回 `409 memory_changed`，客户端重新发起读取；服务器不无界重试，也不为 GET 占用 mutation 准入。成功结果使用读取前固定的 revision，不能将旧正文标成删除后的新状态。文件读取仍在 SQLite 锁外执行，原有当前正文/预览校验继续生效。

v16 在同一个 Run 记录事务内，将提炼 Run 的真实状态、时间、用量和准入证据变化计入 memory_revision，提交后沿既有通知路径发送 memory_patch。正常成功、待审批和失败批次的 Run 收尾都适用；事务回滚不会留下虚假的新 revision，相同证据重复写入不会增加版本，无关 chat Run 更新不冒充提炼变化。启动恢复完成 Run 与文件恢复后，发布当前持久 revision；恢复不启动新的模型请求，缺失的历史用量保持未知。v1–v15 迁移定义保留。


## 最终本地验收（s14 固定结果与后续独立复核）

同一 s14 产品字节的 32 案固定首次结果已采集齐：30 案 Run 为 completed，
2 案 failed（QF05 非 JSON 输出、QF08 timeout）。失败原样保留，不以重生成
替换失败，也不从旧候选中挑选较好结果。运行完成状态不等于输出全部正确。

用户已采用固定 gold；GPT 对原始输出进行独立逐原子审阅，并非用户真人逐项
判分，也不是产品模型自评。协调器提供的本地评分为正确原子 153/160，
即 95.625%；必提取覆盖 22/24，即约 91.6667%。最终 scorer 汇总由协调器
另行冻结，本说明不追加质量断言或承诺模型无错误。

七个错误原子为：QF17 冗余 create 与回应主体错误；QF19 无依据地断言未搬迁；
QF20 无依据地否定杭州居住；QF21 系统主体错误与「无实质信息」概括；
QF30 将九次写成十次。冻结禁止项未见触犯；QF08 没有输出，不能据此证明
其主体辨识等能力。结构安全另有同字节的确定性证据，不以语义分数替代。

QF17 对捕获输出进行一次真实 Host/SDK/Adapter + MockTransport 离线复放，
其 expected input 与原实际请求相同。未批准时 m09 仍为「目前居住北京」、v1、
`human_protected=true`，整批新增 semantic/episode 均为 0，状态仍为
awaiting_approval。这是独立离线再现，不冒充已删除的原 live 临时数据库后态。

本地门禁按实际范围继承：

- s8b：全量 Python 3733 passed / 1 deselected，browser 77 passed，
  typecheck、skills、env 等门禁通过。
- s11：原全量 3727 passed / 1 deselected / 14 setup errors，原 exit1 保留。
  代理 IPv6 CIDR 解析问题仅在测试子进程适配环境后，原受影响文件 34 passed；
  与原全量组成 3741 个选中测试的分段覆盖，不是同一环境单次全量绿色。
- s14：95 项直接检查、32 案离线 dry-run、3 项绑定检查通过；未变化的
  frontend 与其它机械门禁按原证据继承，未声称重新运行。

上述证据位于工作树 `.scratch/review-repair-loop/evidence/` 下对应冻结片段；
原 s6 NOT RUN 和原发布规范均是历史记录。提交、PR、CI、合并与 Issue 收尾
由协调器按实际 GitHub 结果确认，不从本地指标推定已发布或已关闭。
