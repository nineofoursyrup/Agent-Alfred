# #19 实现：切片③ — ToolRegistry 与内置工具

来源：https://github.com/nineofoursyrup/Agent-Alfred/issues/19

# ToolRegistry 与内置工具：完整施工规范 v1

## Problem Statement

用户已经能通过同一个运行宿主使用 CLI 与 Web 对话、检索长期记忆，但模型尚不能通过通用工具循环可靠完成实际动作。共享记忆命令、工具消息协议和遗忘协调核心已存在，不能据此宣称创建日程、保存事实、草拟消息、修改人格或创建 Skill 已经可用。

文件工具还有一个必须解决的失败窗口：文件可能已经写成，而 SQLite 工具账尚未确认。把这种结果报告为“什么都没发生”，会诱使用户或模型重复操作。人格修改的生效时间、Skill 覆盖的真实用户确认也必须有可验证的边界。

本规范属于 [实现：切片③ — ToolRegistry 与内置工具](https://github.com/nineofoursyrup/Agent-Alfred/issues/19)，合并原票、既有权威裁决及本会话已确认的 Q1–Q15。全部原有内置工具仍在范围内；本规范是施工要求，不是实现完成或测试通过证明。

## Solution

在既有 RuntimeHost 中接通不可变 ToolRegistry 与串行多 Step 工具循环，使模型声明的工具调用真实执行、逐项产生配对结果、及时落账，并让 CLI 与现有 Web 对话入口呈现真实结果。复用 ModelClientFactory、RunBudget、Session、运行转录、Run 收尾、中央脱敏和共享记忆服务。

数据库工具以事务确认业务与账；文件工具以持久操作记录、文件核验和恢复确认完成。未确认结果与失败、已完成分开表达。人格工具只修改受管人格并在下一 Run 生效；Skill 创建保护已有用户文件，覆盖内置 Skill 必须经宿主确认真实用户对具体候选的授权。

整票交付包含离线确定性验收、临时状态目录中的真实模型单工具及跨 Step 多工具演示、适用机械门禁、同一固定候选的 Standards/Spec 独立两轴评审。真实模型演示须有单独授权；未执行时保持 NOT RUN，不以离线替身冒充。

## User Stories

1. 作为用户，我希望自然语言请求能触发真实工具动作，以便得到可检查的结果。
2. 作为用户，我希望一次回复中的多个工具按原顺序执行，以便理解先后关系和部分成功。
3. 作为用户，我希望模型读取前一步工具结果后继续下一步，以便完成依赖前一步结果的任务。
4. 作为用户，我希望未知工具、非法参数和普通工具异常有明确结果，以便修正请求而不是失去整个进程。
5. 作为用户，我希望预算耗尽后不再启动动作，以便限制执行范围。
6. 作为用户，我希望系统如实区分未启动、失败和结果未知，以便决定下一步而不误以为动作已取消。
7. 作为用户，我希望外部能力只在配置和授权均满足时执行，以便控制它何时可以动外部世界。
8. 作为用户，我希望本地日程创建与查询使用真实数据，以便后续查询能找到刚创建的日程。
9. 作为用户，我希望不同时区表示的日程按同一实际时刻比较，以便查询顺序可靠。
10. 作为用户，我希望明确保存事实后得到系统提交回执，以便知道长期记忆确实已保存。
11. 作为用户，我希望普通陈述不会被工具自动等同为保存命令，以便保持既有阈值提炼边界。
12. 作为用户，我希望多事实保存逐条说明结果，以便知道哪些已保存、哪些失败。
13. 作为用户，我希望编辑和删除核对记录版本，以便不覆盖或删除后来更新的内容。
14. 作为用户，我希望同一次操作重送只返回原结果，以便响应丢失不会造成重复副作用。
15. 作为用户，我希望工具账记录实际动作而不机械拦截新命令，以便明确再次操作仍能执行。
16. 作为用户，我希望删除记忆后本轮不再向模型发送旧内容，以便兑现已批准的遗忘边界。
17. 作为用户，我希望数据库已删除与副本清理进度分开显示，以便清理故障不会把已提交删除误报成未执行。
18. 作为用户，我希望草拟消息生成可读的本地 Markdown 文件，以便自行编辑和使用，而不是自动发送。
19. 作为用户，我希望系统生成安全的草稿文件名，以便模型不能通过参数写入任意路径。
20. 作为用户，我希望修改人格保留未提及的原有约定，以便一句局部要求不会清空其他行为设定。
21. 作为用户，我希望人格修改在下一 Run 生效，以便当前 Run 的行为保持一致。
22. 作为用户，我希望显式指定的人格文件保持优先且不被工具修改，以便应用不会覆盖我的外部配置。
23. 作为用户，我希望人工编辑的受管人格在下一 Run 校验，以便有效修改可使用、非法内容有明确错误。
24. 作为用户，我希望创建 Skill 不覆盖已有用户 Skill，以便保护已有内容。
25. 作为用户，我希望覆盖内置 Skill 前看见完整候选，以便知道自己批准了什么。
26. 作为用户，我希望确认由宿主直接接收，以便模型不能自行批准自己的候选。
27. 作为用户，我希望重启后仍能重读、批准或取消候选，以便未完成的确认不会丢失。
28. 作为用户，我希望陈旧候选和迟到确认被拒绝，以便旧授权不能覆盖新内容。
29. 作为用户，我希望 Skill 创建结果明确提示重启加载，以便不把文件创建误解为已匹配或已注入。
30. 作为用户，我希望文件与工具账都确认后才收到成功回执，以便“成功”有持久证据。
31. 作为用户，我希望中断后恢复同一次文件操作，以便不重复创建也不丢失可核验结果。
32. 作为用户，我希望恢复保留后来人工修改的文件，以便修复程序不会破坏我的工作。
33. 作为用户，我希望结果待核验时本轮停止后续动作，以便模型不会立即重复执行。
34. 作为用户，我希望只有受影响目标被暂停，以便新 Run 仍可使用无关工具。
35. 作为用户，我希望 CLI 和 Web 对同一动作给出一致的系统事实，以便入口不同不会改变授权与恢复语义。
36. 作为评审者，我希望每项要求对应可复现的公共路径证据，以便不把接口替身通过当成整票交付。

## Implementation Decisions

### D01 权威、基线与已有实现

证据固定于远端 main 772b02d1c5181a6e90aa9f7675f498e109a49313，tree 为 921ad242b997005c54a7448a57ca5fe2b1e71d84；与本地已审候选 3b67deb5409cf4cfb4ea22f25b8e85b47c003fc6 的 tree 一致。施工开始须重新核查，不能把此点时快照当成永久最新状态。

已有：模型侧 ToolSpec/工具消息块与两个适配器、RunBudget/运行记录/Session/CLI-Web 壳层、共享记忆 CRUD/FTS/回执/检索门、遗忘核心及真实非终结 trace 屏障。缺口：通用 Registry、工具执行循环、内置工具业务接线、受管人格更新、Skill 确认入口和文件操作恢复。现有 Skill 校验脚本不等于完整 Skill 创建服务；已有遗忘端口验收不等于本票消费者已接入。

必须阅读的主源：

- [Tool 协议、错误回喂与副作用 opt-in 裁决](https://github.com/nineofoursyrup/Agent-Alfred/issues/4#issuecomment-5427830226)，尤其 §4–§13。
- [三类记忆的后端 Protocol 裁决](https://github.com/nineofoursyrup/Agent-Alfred/issues/5#issuecomment-5428337164)，尤其时间、事务、遗忘及 Skill 目录。
- [看见记忆：独立实施规范](https://github.com/nineofoursyrup/Agent-Alfred/issues/31#issuecomment-5588135839)，尤其 R01、R08–R10、R12、R15–R16 及 A02/A04/A26/A31/A32。
- [记忆遗忘与来源隔离交付及下游边界](https://github.com/nineofoursyrup/Agent-Alfred/issues/45#issuecomment-5602762043)。

主线领域词汇与 ADR 继续适用。本规范仅以 D05、D08–D12 和 D14 明确修订文件事务、人格生效及确认行为；不把 Memory 决策工作树中尚未合入的文档视作已发布主线事实。

### D02 Tool 与 Registry 的公共协议

Tool 为冻结声明，保留 name、description、input_schema、fn、effect、summary_keys、emits_progress、parallel_safe、budget_s；构造时按最小权限注入静态依赖。ToolContext 只携可信调用级身份、入口、绝对 deadline 和按声明提供的事件端口。Run 权限由宿主传递，不能来自模型参数或由 run_id 反推领取。

effect 为 local_read/local_write/external。ToolSuccess 与 ToolFailure 是闭合结果联合，结果内容沿既有文本块协议；可携摘要与工具成本，未知成本不按零处理。模型侧 ToolSpec 恰好只有 name、description、input_schema。

Registry 构造后不可变；工具名按 ^[a-zA-Z0-9_-]{1,64}$ 校验，重复名称和非法声明在启动期拒绝。内置入参采用显式版本化 Schema 子集，注册时拒绝未知关键字、调用时返回可操作的 invalid_input，不引入通用 jsonschema 依赖。子集版本、支持关键字及每个内置工具的入参由实施说明和契约测试明确固定，不静默忽略校验。后续 MCP 接线归对应票，不在本票接入服务器。

### D03 能力暴露、预算与执行循环

可用性与授权正交。local_read/local_write 恒暴露，不套用 external 授权三态；Skill 覆盖确认是具体内容的用户批准，不改工具的 effect。

external 暴露矩阵保持：未配置且 unset/allowed 时暴露同名、空参数的引导声明，调用返回 configuration_required；denied 时隐藏；已配置但 unset/denied 时隐藏；已配置且 allowed 才暴露真实能力并执行。运行故障如实返回 unavailable 等状态，不伪造连接成功。本票以可注入端口检验通用政策，不顺带实现外部集成设置页。

v1 全部工具串行，parallel_safe 为 false、parallel_group 为 null。Registry 使用单调时钟，将工具预算与现有上层期限取更早者，传递绝对 deadline；已耗尽就不启动。启动后的 IO 由工具自身遵守剩余期限，不用遗留后台线程假装可取消。

循环将 committed 的模型回复及其工具批结果追加到 RunTranscript，再在预算内继续下一 Step；同一 Step 多工具保持调用顺序，每个 call_id 有配对结果。RunBudget 在 gate、回答及后续 Step 间共享，不因工具或重试重新拿满。每个 Step 记录固定 tool_names 快照。只有 Run finalizer 写一组正式用户消息与助手回复到会话记录，中间工具往返不写成额外会话轮。

普通、已知未执行或已知失败的单工具结果按原协议回喂，其他工具按顺序继续；D07 删除边界和 D12 文件结果待核验是明确的终止分支。工具异常捕获 Exception，进程控制异常继续沿既有 interrupted 与资源清理路径传播。

### D04 结果、事件、投影与账本

七个 ToolErrorCode 保持 unknown_tool、invalid_input、configuration_required、not_authorized、timeout、execution_error、unavailable；使用原始 call_id、is_error 及首行机器可读错误 JSON。额外业务细节使用版本化、安全的细节字段，不扩第八个通用错误码。

Registry 独占 tool.started/tool.finished 和两份结果投影。工具只能在 emits_progress 为真时发送 transient 的 tool.progress。model_content 默认按可配置 8000 字符上限截断并显式标记，保留 UTF-8 original_bytes 与完整结果摘要；audit_content 保留完整当次结果。两份投影作为实际执行事实保存，不让页面按未来阈值重算。SSE 只传模型投影及安全元数据，完整审计经中央 Redactor 进入 TraceSink；超出既定 256 KB 内联边界的工件由 TraceSink 管理，Registry 不另写审计文件。记忆删除的允许摘要使用既有 HMAC/key_id，通用内容摘要不能替代它。

local_read 不写业务工具账。已发生的 effectful 动作有且只有一份权威业务账；未启动不造成功账或 tool.started。工具账是证据不是重复调用锁：同参数的新明确动作可以执行；同一次操作重送或恢复则重用原操作身份。外部动作沿既有 started/succeeded/failed/unknown 约定；工具成本与模型 Token 费用分列。

### D05 数据库写入与文件写入的持久性分界

SQLite 业务写入继续与权威工具账同事务提交。共享记忆命令由其执行器唯一落账，Registry 不再插第二条。失败回滚不得留下成功业务结果或成功回执，不得回滚调用者不属于本操作的工作。

本地文件仍是 local_write，但文件系统与 SQLite 没有普通共同事务。用户已批准对旧 Tool 裁决 §7 作此窄范围修订：文件操作使用持久操作记录、发布核验和恢复协议，只有目标文件持久事实与对应账均确认后才报告成功。文件 rename 本身不是文件与数据库一起提交的证据。

跨文件操作至少可区分：尚未执行的已知失败、正在或等待核验、已确认完成、恢复冲突；这些是操作事实，不强迫为工具错误码或 Run outcome 增加同名取值。具体状态枚举、迁移、日志布局与写入顺序由实施 agent 设计，须满足 D11–D12 的确定性验收。新增持久结构用前向迁移，已发布迁移不原地重写。

### D06 内置能力与共享记忆适配

| 能力 | 本票交付行为 |
| --- | --- |
| 创建日程 | 写入现有 calendar_entries 领域存储及真实工具账；原票 events 为旧表名，不重建已退役表。沿用 title、starts_at 及可选 ends_at、iana_time_zone、participants、notes 等现有字段；身份与创建时间由服务生成。 |
| 查询日程 | 查询真实已保存日程，local_read 不落业务工具账；时间入口拒绝 naive 值，比较与排序按 UTC instant，不用带不同时区文本的字典序。已有 IANA 时区信息保留。 |
| 保存事实 | 明确保存才调用共享语义记忆命令，提交后返回 saved/already_exists 等真实回执。普通陈述仍留会话并遵守提炼边界。 |
| 修改／遗忘记忆 | 复用共享 get/query/版本化 update/delete，目标不明确时澄清；唯一明确目标直接执行。遵守 D07，不自写另一套 SQL。 |
| 草拟消息 | 独立本地 Markdown 文件、路径回执与真实账，行为见 D08。 |
| 修改人格 | 完整读取与版本核对、受管写入及下一 Run 生效，行为见 D09。 |
| 创建 Skill | 不覆盖用户文件，内置同名先确认，行为见 D10。 |

操作身份由可信调用适配器管理，单独区分 call_id、operation_id 和确认编号；模型不能伪造 origin、human_protected、来源完整性或准入权限。多事实逐条独立 operation 与事务，第二条失败不回滚第一条。业务保存成功与 Run 记录成功是两个事实，后续模型或会话记录失败不撤销已保存记忆。

共享记忆重送沿既有 HMAC/key_id 参数身份与回执规则：同 ID 同参数回读，不同参数拒绝，旧 key 不可比拒绝重做；旧保存回执不恢复已删正文。字段非法映射 invalid_input，版本/重复键/不存在/存储执行故障映射 execution_error，未启动的后端不可用映射 unavailable，细节使用安全 memory_code。来源及权限必须使用当前真实上下文；工具包装不伪造完整来源证明。

日程的新增重复规则、提醒、外部日历同步均不由“创建／查询”暗中引入。旧工具错误文案示例中的无 offset 时间不能覆盖现有 aware instant 约束。需要身份定位、限量读取的内部适配可复用现有服务，不以此扩大成另一套 Memory 页面或检索产品。

### D07 记忆编辑、删除与遗忘核心接线

工具继承当前 WorkItem 的准入权限，调用共享命令服务；手动动作经过同一 MutationGate，忙时明确冲突、不排队、不绕过正在执行的 Run。必须复用已交付的 checkpoint_barrier 和遗忘端口，不拿最终 flush 提前封存 Run。

编辑后剔除本 Run 参考资料的旧版本，下一 Step 不再发送旧引用，不重新检索补位。实际送入资料与 input-attempt 证据一致；现有 RunMemory 的更新失效能力须接上真实多 Step 循环。

数据库删除确认后立即进入 memory_delete_boundary：不再调用模型，后续未启动工具形成配对的安全内部结果，沿 unavailable 细节码说明未执行；不生成虚假的 started/业务账。已完成动作不回滚。以无 subject/正文的固定系统回执作为正式终止文本，正常受控收尾使用既有 completed 及 finalization_reason、未执行 call_id 清单；recording_state 和中断仍独立处理。

删除回执、模型投影、审计、SSE、新 trace 与异常文案均不回显被删正文、subject 或参数预览。持久删除事实与当前遗忘进度分开：needs_scope、cleaning、failed、complete 如实显示；清理失败不能把已提交删除说成未执行。原始会话和删除前已落定 trace 仍可人工查看，独立同义记忆不自动连带删除，不以文本黑名单冒充来源隔离。

本票负责真实 Registry 权限、命令、结果及终止接线。完整工作窗口、历史安全摘要和真实历史消费登记由“会话与工作记忆”交付；真实提炼/镜像由相应票交付；Memory HTTP、memory_patch wire、范围确认 UI 和重连由 Memory 编辑页交付。本票保留真实来源身份、读写闸及后续端口契约，不用自身局部 PASS 宣称这些消费者已完成。

### D08 outbox 草稿

每次新草拟动作生成独立 UTF-8 Markdown 文件，位于有效状态根下的 outbox；包含可选收件对象、主题和正文，不发送、不接外部投递。文件名由程序分配，模型不得指定任意路径。同一次操作恢复不生成第二份文件；用户明确再次草拟可生成新文件，不按正文相同机械去重。

仅在 D05 的完成条件满足后返回成功及实际可读路径。用户可以自由编辑已生成草稿；工具账只证明当时生成成功，不宣称当前内容仍与当时相同。重新启动应用不以旧账为理由重建被人工修改的成功文件。

### D09 人格读取、修改与生效

人格优先级为 CLI 显式文件、环境变量显式文件、受管人格、内置默认。工具只管理有效状态根内的受管人格。任何显式人格文件正生效时，工具拒绝修改并提示先取消覆盖配置，不暗中保存一份不会生效的受管版本，也不写显式文件。

工具先读取完整人格及版本，再提交完整修改内容并原子检查预期版本。模型应保留用户未要求改变的约定；版本变化拒绝覆盖并重新读取，不自动强行应用旧候选。用户明确的修改请求直接执行，歧义澄清，不增加一律二次确认。

当前 Run 固定人格快照；修改或人工编辑的受管人格从下一 Run 读取、校验并生效。受管人格不存在且从未创建过时使用内置默认；存在但无效、不可读或恢复尚未核实时不能静默回退、不能加载不确定新内容。人格仍为请求级 system 输入，不写会话记录或运行转录；当前本地时间按既有请求语义生成，不固定成历史时间。显式文件的配置加载路径保持可兼容，不把新增受管更新实现成任意路径热写。

本票交付人格写入、版本和下一 Run 接线；“会话与工作记忆”复用该约定，继续负责完整窗口与全局输入限制，不再为人格另造实现。

### D10 Skill 创建、真实用户确认与重启

沿既有内置／用户 Skill 目录及可注入位置，名称经验证的索引解析而非直接拼路径；用户同名整体覆盖内置并透明标记，同一目录重复身份按既有规则拒绝启动。工具创建必须检查当前磁盘与有效身份，不能因运行期目录快照未刷新而覆盖后来出现的用户文件。

创建同名用户 Skill 一律拒绝，要求换名；仅与内置 Skill 同名时，先持久保存候选，展示名称、描述、完整正文及整体覆盖提示。候选已保存不等于 Skill 已创建。确认绑定具体候选内容和内置版本，变化即失效。完整候选可通过可信宿主读口重读；通用模型投影截断不能被当成已经向用户展示完整候选的证据。

用户在现有 CLI/Web 对话入口提交“确认创建〈编号〉”或“取消〈编号〉”，由宿主直接识别处理，不将授权判断交给模型；工具参数 confirmed=true、模型输出或引用文本均不是用户确认。候选编号定位持久候选，确认动作在同一准入中重检内容、内置版本和用户同名文件，再执行受管创建。两入口共用业务路径，本票只补现有对话所需交互，不建 Skill 管理页。

候选跨重启保留，可重读、确认或取消。迟到首次确认必须核对当前事实，重复确认回读原操作结果，不重复创建；取消只撤销候选，不删除已存在 Skill。取消与确认的先后顺序须由同一操作所有者序列化，不允许已取消候选复活。确认后文件结果未核验时沿 D11–D12 恢复，不以再次确认造成第二次写入。

创建及人工编辑的 Skill 重启后加载、校验，回执明确此时机；不热重建当前 Run 的能力集合，不声称已匹配或注入。有效目录读取和 Memory 页展示仍由既有责任票交付，本票只复用或提供创建所需的共享目录校验接缝。

### D11 文件操作恢复与人工修改保护

outbox、受管人格和 Skill 创建共用持久操作与恢复行为：保存稳定操作身份、目标身份、内容版本核验依据及可重读的结果进度。恢复所需记录必须足以区分原操作与新命令，不靠“存在一个同名文件”或参数指纹猜成功。

首次发布、文件持久化、账确认、回执交付均可能失败或被中断。自动恢复先核验：可证明已完成则补齐确认；可安全继续则恢复同一次动作；目标被后来人工修改、条件变化或证据不足时保留现场并报告冲突／待核验，不自动覆盖、删除或再次创建。只有确认安全的目标版本才可发布和使用。成功操作的重启不会反复改写已经完成的目标。

只暂停未核实目标的后续写入，不一概停用全部工具；任何可用无关能力仍可在新 Run 使用。人格／Skill 的不确定新内容不加载。恢复与普通写入共用宿主准入和目标版本检查，不持 SQLite 事务锁做文件 IO。新增资源的中断清理沿现有可重试所有权约定，完成项不重复产生副作用。

涉及记忆受管结果投影时继续遵守 D07 的无正文与来源隔离边界，不以恢复日志保留本应清理的可复用记忆结果。是否为待核验不得由模型自然语言决定。

### D12 文件结果待核验时的固定收尾

若文件操作无法确认是否完成，当前 Run 停止后续工具和模型请求，保留已完成动作，并为未启动调用提供配对的安全内部结果及明确未执行清单；不产生未启动工具的 started 或成功业务账。固定系统回执说明结果待恢复／核验，包含操作编号和现有对话可达的重读／恢复入口，不假称未发生副作用或成功完成。

此分支与正常删除后的 completed 固定收尾分名；应使用现有失败 outcome 与可识别原因表达本 Run 未能确认完成，操作当前状态和 recording_state 独立保存。进程控制中断仍用既有 interrupted。具体原因常量与安全细节字段是实施机械设计，不能扩大通用工具错误码集合。当前 Run 不自动重做；新 Run 的无关工具仍可使用，受影响目标继续 D11 的暂停策略。

### D13 门禁、真实演示与交付证据

全部内置能力均须有真实服务、临时 SQLite／文件证据。离线分别覆盖同一 Step 多工具和模型读结果后跨 Step 再调用；未知工具、非法参数、普通异常、预算耗尽、控制中断及两种固定收尾分支均受测。

真实单工具演示使用合成事实并明确保存，核对共享命令回执与唯一真实业务账。真实多工具演示创建一条测试日程，再由模型读取工具结果并查询刚创建的记录，证明跨 Step 执行；不能由脚本直接第二次调用工具替模型演示。使用临时状态目录，不改用户已有数据。模型未按预期调用或结果不符时如实记录未通过，不改写证据、不无限重试掩盖失败。

沿项目配置运行适用 Ruff、离线 Python、技能与环境一致性、Web 类型和浏览器检查，并验证 wheel/sdist 隔离安装。交付每项验收的证据入口、实际命令及结果；整票同一固定候选必须得到独立 Standards/Spec 结论。普通绿测试或旧候选 PASS 不替代本票验收。模型演示无授权则 NOT RUN，保持剩余项可见。

### D14 对旧裁决的明确修订与不变量

| 原表述或缺口 | 本规范确定的行为 | 来源 |
| --- | --- | --- |
| 所有 local_write 都与业务同一 SQLite 事务 | 数据库工具继续同事务；文件工具仍为 local_write，但以持久操作、核验与恢复确认，不宣称跨存储原子事务 | Q5、Q9、Q14 |
| 人格只在配置加载时读取，修改无生效语义 | 只写受管人格，保留显式覆盖优先级，完整内容版本核对，下一 Run 生效 | Q2–Q3、Q6–Q7、Q15 |
| 目录覆盖规则已定，创建覆盖授权未定 | 用户同名拒绝；内置同名绑定完整候选确认，宿主直接处理确认／取消，持久候选恢复 | Q4、Q8、Q12–Q13 |
| 文件失败只有普通异常，可能被模型重复执行 | 结果待核验停止本 Run，固定系统回执；恢复保留人工修改，未核实目标暂停写入 | Q5、Q9、Q14–Q15 |
| 演示仅写单工具／多工具 | 全部工具离线覆盖；真实保存事实及创建后查询日程；同 Step 多工具另有离线验证 | Q1、Q11 |
| 草拟消息只有本地 outbox 要求 | 独立 UTF-8 Markdown、程序生成文件名、可选收件对象／主题／正文、仅生成不发送 | Q10、Q15 |

不变项：工具账不是去重锁；模型不能自行授权；Store 不越权提交或回滚；记忆账由共享执行器唯一拥有；Run finalizer 独占正式会话记录；删除时无下一模型请求；原始历史人工查看边界、预算及中央脱敏规则保持。

### D15 原票范围与分工

本票负责 Registry、通用循环、全部内置工具、创建所需的最小目录接缝、人格更新与读取接线、宿主确认和文件恢复。保持“会话与工作记忆”的完整窗口／安全账摘要、“记忆提炼与 Markdown 镜像”的真实批次／镜像、“看见记忆”的完整 Memory 页面／HTTP／通知 wire 归属。Memory 检索与遗忘核心复用，不重新实现。

内部类名、SQL 表结构、新迁移编号、文件操作阶段名、支持 Schema 子集的具体展开、错误细节常量由实施 agent 依据上述行为设计并记录；这些是机械选择，不重新访谈已确认产品决策。若发现真实不可满足的契约矛盾，提交证据和最小取舍，不自行删减整票范围。

## Testing Decisions

### 测试接缝与先例

主接缝为 RuntimeHost 的真实 Run 及现有 CLI/Web 对话入口：使用 ScriptedModel、真实临时 SQLite、真实文件系统及宿主重启，检查最终文件／记录／回执、请求捕获、工具账和 trace。第二接缝为 ToolRegistry 的公共构造、声明及执行入口，直接验证启动拒绝、Schema、授权、预算和结果协议。模型、时钟、文件 IO 和持久化失败为可注入边界，不把生产业务换成静态成功返回值。

先例包括现有模型工具协议与适配器序列化测试、共享记忆命令事务与回执测试、遗忘真实 trace 屏障／中断恢复测试、Session 与浏览器 HTTP/SSE 流程测试。主要断言外部可见行为与持久事实，不窥探私有函数调用次数或用实现镜像测试代替契约测试。竞态与控制中断用确定性 barrier/event 或精确故障边界，不靠 sleep 与概率循环。

测试接缝确认状态：用户已于 2026-09-10 确认上述两个接缝。Q1–Q15、整体验收行为及测试入口均已确认。

### 验收矩阵

以下保留完整施工要求。T01–T50 已在已合并 v8 候选完成本票验收，逐项证据及限定见本票最终验收评论；T 编号是本票规范内验收身份，A 编号保留 Memory 权威规范的原身份。

| 验收 | Given / When / Then | 归属 |
| --- | --- | --- |
| T01 | 非法／重复工具名或未知 Schema 关键字；构造 Registry／启动宿主；明确拒绝，未发送模型请求 | D02 |
| T02 | 合法内置声明；请求 schemas 并执行参数校验；只暴露三字段，非法参数为 invalid_input 且无业务副作用 | D02 |
| T03 | external 可用性与授权各组合；暴露和执行；符合完整矩阵，未配置引导不执行实际能力 | D03 |
| T04 | 模型返回一条工具调用；经真实 Run 执行；原 call_id 配对、业务结果正确、后续回答能读结果 | D03 |
| T05 | 同一 Step 返回多条调用；执行；严格原顺序、逐项配对，单项已知失败不虚构整批原子性 | D03 |
| T06 | 第一工具结果决定第二次调用；多 Step Run；请求捕获证明结果入转录后才发下一请求 | D03 |
| T07 | 未知工具或普通 fn 异常；执行；原 call_id 错误结果、进程存活，安全错误文案可操作 | D03–D04 |
| T08 | gate/回答消耗共享预算，工具启动前期限已到；执行；无新动作、无预算重置，max_steps 结局真实 | D03 |
| T09 | 工具 IO 消耗部分期限；串行后项执行；传递同一上层期限剩余额度，无后台假取消 | D03 |
| T10 | emits_progress 为真／假；工具运行；只有获准工具可发进度，Registry 唯一负责 started/finished | D04 |
| T11 | 长结果、非 ASCII 和审计阈值边界；投影／trace／SSE；截断可见、字节和摘要可核对、完整审计不从 SSE 泄出 | D04 |
| T12 | 含合成密钥的工具结果与普通异常；走真实 FanOutSink；两投影与工件保持中央脱敏，不绕过 TraceSink | D04 |
| T13 | 数据库业务写入或工具账写入失败；执行并重读；业务与账一起回滚，调用者原事务不被越权回滚 | D05 |
| T14 | 相同参数的两次明确新动作及同一次动作重送；执行；新动作可执行，重送只返回原事实，无机械参数锁 | D04–D06 |
| T15 | 合法日程及不同 offset 表示；创建、查询；真实持久记录、按 instant 比较、读工具不产生业务账 | D06 |
| T16 | naive 或非法日程输入；执行；明确 invalid_input，不猜本地时区，不留下成功记录 | D06 |
| T17 / A02 | 明确保存且模型实际输出 save；真实 Run；共享服务提交后系统才确认成功 | D06 |
| T18 / A04 | 多事实第二条存储失败；逐条执行；第一条保留，逐项回执和账可核对 | D06 |
| T19 / A26 | Registry 包装真实记忆命令；持已有权限执行；仅一份业务账，投影与 tool 事件由 Registry 负责 | D04、D06 |
| T20 | 参数伪造 origin/保护/租约，或手动写与 Run 并发；执行；伪造不可授权，手动忙时冲突，合法 Run 工具不自锁 | D06–D07 |
| T21 | 版本冲突、重送参数变化、旧 key 不可比、保存后已删；执行；沿共享安全错误／回执规则，不覆盖、不复活 | D06 |
| T22 / A33 接入回归 | 当前参考资料被真实工具编辑；下一 Step；旧引用剔除，无重新检索补位，实际请求证据一致 | D07 |
| T23 / A31 | 同 Run 删除前已有原话且后续工具待启动；删除确认；固定无正文收尾、零后续模型请求、未启动清单准确 | D07 |
| T24 / A32 | 删除成功或删除相关异常；检查系统回执／模型投影／审计／SSE／新 trace；无 subject/正文，HMAC/key_id 正确 | D07 |
| T25 / A54 接入回归 | 真实 trace 队列阻滞或屏障失败；从工具尝试删除；失败不删，成功先确定历史边界，Run 后续能正常收尾 | D07 |
| T26 | 数据库已删且遗忘为 needs_scope/cleaning/failed/complete；固定收尾与重读；删除事实与当前进度分开，不谎报全域擦除 | D07 |
| T27 | 两次新草拟、同操作恢复与含任意路径参数请求；执行；独立可读 Markdown、正确字段、恢复不重复、路径不可逃逸 | D08 |
| T28 | CLI／环境／受管／默认人格组合；启动和修改；优先级正确，显式文件生效时拒写且不产生隐藏修改 | D09 |
| T29 | 原人格有多项约定、仅要求修改一项；读取、修改、下一 Run；其余保留，当前 Run 快照不变，下次使用新内容 | D09 |
| T30 | 读取后人工更改人格版本；提交；拒绝覆盖，原人工修改保留，可重新读取 | D09 |
| T31 | 人工编辑受管人格为有效／非法内容，或恢复未核实；下一 Run；有效按时生效，无效明确失败，不加载不确定新内容 | D09 |
| T32 | 无同名 Skill；创建并重启；文件与账核实后成功，重启加载，当前运行不伪造热重载／匹配／注入 | D10 |
| T33 | 同名用户 Skill 启动后才被人工创建；调用创建；仍拒绝覆盖，不依赖陈旧索引误判 | D10 |
| T34 | 仅内置同名；提交候选；持久待确认、完整内容可读、清晰覆盖提示，没有已创建文件的假回执 | D10 |
| T35 | 模型 confirmed=true、模型文本或引用内容模拟确认；调用；不能批准；真实用户确认从 CLI/Web 由宿主直接处理 | D10 |
| T36 | 候选内容／内置版本变化或同名用户文件出现；迟到首次批准；拒绝旧批准且不覆盖 | D10 |
| T37 | 待确认候选跨重启、重复确认；重读和批准；同一候选可核对，同一操作只有一次实际创建 | D10 |
| T38 | 取消与确认按两种 barrier 顺序发生；执行；只有合法先后结果，取消不删现有 Skill，不复活被取消候选 | D10 |
| T39 | 文件写入前失败、部分写入／同步／发布／账确认／响应阶段中断；重启；实际状态可区分、未双确认不报成功 | D05、D11 |
| T40 | 文件已发布但账或响应未确认；恢复；核验原目标版本后补齐，不再生成第二份或第二笔业务动作 | D11 |
| T41 | 待恢复期间人工修改目标，或成功后人工编辑 outbox；恢复／重启；保留人工内容，旧账不驱动覆盖重建 | D08、D11 |
| T42 | 某目标未核实；对该目标及无关目标发起新动作；前者暂停，后者在宿主可准入时正常执行 | D11 |
| T43 | 文件结果待核验且还有模型／工具步骤；执行；固定非成功收尾、零后续请求、未启动不造账，操作进度独立可读 | D12 |
| T44 | 普通已知失败、删除成功、文件未确认、控制中断；运行与记录；不同分支 outcome/reason/recording_state 不混用 | D03、D07、D12 |
| T45 | 控制异常或清理再次失败落在资源交接边界；关闭／恢复；保留可达 owner 和重试进度，完成项不双写或双释放 | D11 |
| T46 | 确认、取消、结果重读、恢复分别从 CLI/Web 进入；同一临时宿主；授权与业务事实一致，重连后回执可读 | D10–D12 |
| T47 | 普通聊天、已有检索、Session 恢复与运行记录；完整回归；正式消息仍由 finalizer 独占，输入与 Token 账不被工具接线破坏 | D01、D03 |
| T48 | wheel/sdist 在干净环境安装；执行代表性工具／确认／恢复路径；非 editable 产物含真实所需资源，不依赖工作目录草稿 | D13 |
| T49 | 获得真实模型调用授权；明确保存合成事实；模型实际调用共享服务，回执与唯一业务账一致 | D13 |
| T50 | 获得真实模型调用授权；创建日程再查询；真实模型跨 Step 消费前结果，最终查询命中真实新记录 | D13 |

验收证据应能从要求编号定位公开入口、夹具、实际结果和候选版本。对于新恢复协议，实施前先给出状态表与每个持久边界的故障矩阵，再以 red → 最小实现 → green 推进；不要求用户选择日志文件或内部类型等机械细节。

## Out of Scope

- 外部日历同步、消息发送、提醒及新增重复日程规则。
- Tavily、MCP、Graph 的实际集成及完整 Tools/Ops 管理页面。
- Memory 编辑页、完整 SkillCatalog 页面、memory_patch 的 HTTP/SSE wire 与重连产品路径。
- Skill 匹配、注入、热重载、修改或删除既有用户 Skill；任意路径文件编辑器。
- 自动提炼批次与 Markdown 记忆镜像、完整工作记忆窗口及新的全局 token 算法。
- 追溯擦除原始会话／既有 trace、删除用户另存副本、语义同义内容全域删除。
- 自动取得付费模型调用许可；以规范发布代替施工、认领、提交、推送、合并或关闭原票授权。

## Further Notes

### 决策追溯

本会话依次确认 Q1–Q5、Q6–Q11、Q12–Q15，并最终确认三处接缝冻结稿。下表把每项确认映射到本规范，避免整理时漏项。

| 确认 | 冻结主题 | 规范 | 验收 |
| --- | --- | --- | --- |
| Q1 | 全部原工具及整票验收 | D06、D13 | T01–T50 |
| Q2 | 人格只写受管位置 | D09 | T28 |
| Q3 | 下一 Run 生效及职责 | D09、D15 | T29、T31 |
| Q4 | 用户同名拒绝，内置同名确认，重启加载 | D10 | T32–T34 |
| Q5 | 文件与账双确认及结果待核验 | D05、D11–D12 | T39–T44 |
| Q6 | 人格优先级及显式覆盖拒写 | D09 | T28 |
| Q7 | 完整人格与预期版本 | D09 | T29–T30 |
| Q8 | 完整候选绑定确认 | D10 | T34–T36 |
| Q9 | 自动安全恢复、保留人工修改、暂停目标 | D11 | T39–T42 |
| Q10 | 独立本地 Markdown 草稿 | D08 | T27 |
| Q11 | 两种多工具离线形态及真实演示 | D03、D13 | T05–T06、T49–T50 |
| Q12 | 宿主直接处理对话确认／取消 | D10 | T35、T46 |
| Q13 | 候选持久化、陈旧确认、幂等与取消 | D10 | T36–T38 |
| Q14 | 未核验停止本 Run，固定非成功回执 | D12 | T43–T44 |
| Q15 | 人工编辑及各自生效／账本边界 | D08–D11 | T31–T32、T41 |

### 依赖与任务归属

原生前置为 [Tool 协议裁决](https://github.com/nineofoursyrup/Agent-Alfred/issues/4)、[真模型最小循环与 CLI](https://github.com/nineofoursyrup/Agent-Alfred/issues/13)、[检索门与语义／情景记忆 FTS 后端](https://github.com/nineofoursyrup/Agent-Alfred/issues/17)、[记忆遗忘与来源隔离](https://github.com/nineofoursyrup/Agent-Alfred/issues/45)。本次核查全部 closed；原票 open 且未认领。GitHub 原生依赖与实际任务归属需在开工时重查。

仍被本票阻塞的工作包括会话与工作记忆、Memory 编辑页、可选集成与 Tavily、MCP 桥接及 Graph 引擎。规范发布不提前解除这些依赖。当前主工作目录落后于主线且保留用户改动；实施应使用核验过的独立工作树，不能复用其他票的施工目录或覆盖未提交文档。

### 规模与施工组织评估

本票包含工具循环、事务接线、文件恢复、人格更新及持久确认五类行为，风险明显高于“补几个工具函数”。保留原票的全部范围和最终整体验收责任；当前不创建新票、不修改依赖。

可按四个纵向阶段评估实施粒度：真实日程创建／查询贯通 Registry 和多 Step；共享记忆命令及编辑／删除收尾；outbox 文件核验／恢复及受管人格；Skill 候选确认／重启／创建和两入口回归。最后运行整票门禁与真实演示。阶段是组织建议，不是删减验收或已批准的子票。

建议在新实施会话投入大规模代码前核对每段是否能在一个新上下文内完成并独立验收；若确需多票，以本规范进入 to-tickets 提出纵向拆分供用户确认，保留原票的整合验收。不能仅因已有原票就保证一次会话足够，也不能为遵循流程自动创建重复票。

### 交付状态

本票已完成实现、独立 Standards/Spec 双轴 CLEAR（0项）、离线门禁及默认客户端 T49/T50 真实验收，并通过 PR #49 合并。合并提交 459f91137e4638dc3300a61b890f8480a0071c2c 的 tree 与验收 v8 完全一致；PR及合并后CI均成功（Python3432 passed/1 deselected，浏览器60 passed）。真实演示仅两个合成Run、7次Attempt，无临时请求头注入；两次检索gate走invalid_output确定性fallback，不声称模型JSON判定通过。完整证据映射见最终验收评论。#16/#18/#46等下游范围仍按D15分别交付；本票验收不替代其完成。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/19#issuecomment-5427878588

[#4 决定：Tool 协议、错误回喂与副作用 opt-in](https://github.com/nineofoursyrup/Agent-Alfred/issues/4) 已裁决，本票按其 §4–§13 实现。落地时特别注意六条：

1. **预算不是超时**（ADR-0006）：`budget_s` + 单调时钟绝对 deadline，**耗尽即不启动**；不得用 `ThreadPoolExecutor` 伪造可取消。v1 全部工具**串行**，`parallel_group` 恒 `None`。
2. **不引入 `jsonschema`**（实测 5 包 16 MB）：内置工具走**版本化 Schema 子集**，注册期拒绝未知关键字并**启动失败**；调用期轻量校验产出可操作的 `invalid_input`。
3. **工具名注册期按 `^[a-zA-Z0-9_-]{1,64}$` 严格校验**，不合规直接启动失败。
4. **两份投影由 Registry 统一生成**，工具禁止自行截断：`model_content`（8000 字符上限 + 显式截断标记）与 `audit_content`（完整）。字节数按 UTF-8。**Registry 不碰工件**。
5. **`tool_ledger` 是证据不是锁**：不拦截重复调用。
6. 捕获边界是 `except Exception`，**放行 `KeyboardInterrupt` / `SystemExit`**（`run.finished.outcome` 有 `interrupted` 这一档）。

原验收里的「未知工具」路径请按裁决 §8 断言：用模型给的原始 `call_id` 回喂 `ToolResultBlock(is_error=True)`，首行为 `{"ok":false,"code":"unknown_tool",...}`。

## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/19#issuecomment-5612160788

Issue #19 验收收尾总结（完整施工规范 v1）

PR #49 已合并，合并提交 `459f91137e4638dc3300a61b890f8480a0071c2c`，tree `6cf3b11d821cb38aa3c0b69218063fb5aad375f6` 与独立评审及真实验收 v8 完全一致。Standards / Spec 均 CLEAR，行动性发现0。以下是本票当前结果；正文验收状态已更新；实现说明中的旧演示限制属于冻结时历史状态，由本总结补充最新结果，不改写原要求。

[PR CI](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34394922728) 与 [合并后精确main CI](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34395716613) 均成功：Python3432 passed / 1 deselected，浏览器60 passed，Ruff、skills、env、TypeScript通过。v8本地wheel/sdist构建及各自全新环境隔离安装通过，实际运行草稿、确认、重启恢复和默认客户端头部消费者。相关公开入口：[隔离安装检查](https://github.com/nineofoursyrup/Agent-Alfred/blob/459f91137e4638dc3300a61b890f8480a0071c2c/scripts/check_installed_tools.py)、[浏览器工具流程](https://github.com/nineofoursyrup/Agent-Alfred/blob/459f91137e4638dc3300a61b890f8480a0071c2c/tests/browser/tools.spec.js)、[完整实现与测试入口](https://github.com/nineofoursyrup/Agent-Alfred/blob/459f91137e4638dc3300a61b890f8480a0071c2c/docs/implementation/issue-19-tools.md)。

测试缩写：R=[test_tool_registry.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/459f91137e4638dc3300a61b890f8480a0071c2c/src/agent_alfred/evals/deterministic/test_tool_registry.py)；H=[test_runtime_tools.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/459f91137e4638dc3300a61b890f8480a0071c2c/src/agent_alfred/evals/deterministic/test_runtime_tools.py)；F=[test_file_tools.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/459f91137e4638dc3300a61b890f8480a0071c2c/src/agent_alfred/evals/deterministic/test_file_tools.py)；E=[test_tool_recovery_edges.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/459f91137e4638dc3300a61b890f8480a0071c2c/src/agent_alfred/evals/deterministic/test_tool_recovery_edges.py)；A=[test_tool_acceptance.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/459f91137e4638dc3300a61b890f8480a0071c2c/src/agent_alfred/evals/deterministic/test_tool_acceptance.py)；P=[test_tool_projections.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/459f91137e4638dc3300a61b890f8480a0071c2c/src/agent_alfred/evals/deterministic/test_tool_projections.py)。共享核心和既有运行回归见同目录对应测试；表中为实际测试入口简名。T01–T48由同候选离线门禁及独立评审覆盖，未为关票重新执行测试。

| 要求 | 结论 | 证据入口 |
| --- | --- | --- |
| T01 | PASS | R registry_rejects、malformed_supported_schema |
| T02 | PASS | R schema_snapshot、execution_pairs；全部内置声明经 Host 构造 |
| T03 | PASS | R external_policy 完整六格 |
| T04 | PASS | H calendar_created_then_queried |
| T05 | PASS | F draft_creates_independent；A memory_second_fact_failure |
| T06 | PASS | H calendar_created_then_queried；浏览器 tools.spec.js |
| T07 | PASS | R execution_pairs、progress_permission；A calendar_transaction_rolls_back |
| T08 | PASS | R expired_budget；已有 runtime_memory_io_deadline 和 loop 预算回归 |
| T09 | PASS | E file_deadline_after_prepare；H deadline 变体确认未删、后项继续 |
| T10 | PASS | R progress_permission_and_process_control |
| T11 | PASS | R full_unicode_audit；P sse_omits_audit、trace_moves_large |
| T12 | PASS | P real_fanout_accepts_tool_events；R central_redactor |
| T13 | PASS | A calendar_transaction_rolls_back；E recovery_never_commits；已有 memory_commands |
| T14 | PASS | R calendar_same_operation；A completed_skill_creation_replay；人格重送 |
| T15 | PASS | A calendar_uses_instants_and_read_does_not_write_ledger |
| T16 | PASS | A naive_calendar_rejected_without_writes |
| T17 | PASS | H memory_save_uses_shared_service_and_single_ledger |
| T18 | PASS | A memory_second_fact_failure_keeps_first_and_per_item_receipts |
| T19 | PASS | H 保存与删除测试；P 实际 FanOut；唯一 tool_ledger 断言 |
| T20 | PASS | A manual_mutation_is_busy、real_run_rejects_forged_origin；共享 admission 回归 |
| T21 | PASS | 共享 memory_commands 重送、版本、key_id、已删除不复活；H 真命令包装 |
| T22 | PASS | H memory_edit_invalidates_reference_before_next_step |
| T23 | PASS | H deleted_memory_stops_batch（真实 trace、零后续请求） |
| T24 | PASS | H 同测试 success/receipt_loss 的 model/audit/SSE 与 fingerprint/key_id 断言 |
| T25 | PASS | H 同测试 barrier/deadline；已有 test_forgetting_trace.py 生产队列阻滞矩阵 |
| T26 | PASS | A real_delete_receipt_separates_cleanup_failure_and_completion；H needs_scope |
| T27 | PASS | F draft_creates_independent、restart_recovers_same_operation；Schema 拒路径字段 |
| T28 | PASS | F explicit_persona_override；已有 settings 显式参数与环境优先级 |
| T29 | PASS | F managed_persona_changes_only_next_run |
| T30 | PASS | E persona_race_retains_human；A existing_persona_same_content_and_completed_replay |
| T31 | PASS | E 人格 pending 快照；A next_run_validates_manual_persona；F 下一 Run |
| T32 | PASS | F create_skill_writes_valid_file；E invalid_skill_metadata；隔离安装脚本 |
| T33 | PASS | F user_skill_created_after_startup_is_preserved |
| T34 | PASS | F builtin_skill_candidate_requires_direct_user_confirmation_after_restart |
| T35 | PASS | Schema 不接受 confirmed；A CLI；浏览器 tools.spec.js 宿主命令 |
| T36 | PASS | A late_approval_rechecks_candidate_and_target 三种变体；disappears 重送矩阵 |
| T37 | PASS | F builtin_skill_candidate 跨重启／重送；A completed_skill_creation_replay |
| T38 | PASS | A confirmation_cancel_order_has_only_legal_durable_result 两种顺序 |
| T39 | PASS | A partial_file_write、cleanup_control、file_failure_recovery；E 资源/期限 |
| T40 | PASS | F published_file_failure；A file_failure_recovery_commits_exactly_once |
| T41 | PASS | F recovery_preserves_manual_file_edit；E 人格竞态；A same_content_human_file |
| T42 | PASS | E persona_race 无关草稿成功；F 无关目标；待核验目标写入拒绝 |
| T43 | PASS | F published_file_failure_stops_run；固定 failed 与 operation_id |
| T44 | PASS | H 删除成功／异常，F 文件失败，R 控制中断；Runtime 原有记录失败矩阵 |
| T45 | PASS | E staging_close_failure（现为操作目标）；A cleanup_control／partial_file_write／open_return_interruption |
| T46 | PASS | A cli_candidate_and_operation_commands；tests/browser/tools.spec.js 三条流程 |
| T47 | PASS | 全量 pytest + 全量浏览器；既有 Session/Run/finalizer/预算回归 |
| T48 | PASS | scripts/check_installed_tools.py：wheel/sdist 新虚拟环境、python -I、临时 cwd |
| T49 | PASS | v8正式默认客户端真实save_fact；持久事实、系统回执与唯一memory_save成功账匹配；1 Run / 3 Step / 3 Attempt。 |
| T50 | PASS | v8正式默认客户端真实create_event于Step1创建，query_events于Step2返回同一ID；唯一日程及成功账；1 Run / 4 Step / 4 Attempt。 |

D条款对应：

| 条款 | 验收对应 |
| --- | --- |
| D01 | T47；固定基线与合并tree、已有全量回归 |
| D02 | T01–T02；Registry声明/Schema/启动拒绝 |
| D03 | T03–T09、T44、T47；权限矩阵/串行循环/共享预算 |
| D04 | T07、T10–T14、T19；结果投影/事件/唯一账 |
| D05 | T13、T39；SQLite事务及文件持久边界 |
| D06 | T14–T21；日程及共享记忆工具 |
| D07 | T20、T22–T26、T44；权限、引用失效、真实删除固定收尾 |
| D08 | T27、T41；独立草稿及人工编辑保护 |
| D09 | T28–T31；人格优先级/版本/下一Run生效 |
| D10 | T32–T38、T46；Skill保护、真实宿主确认与两入口 |
| D11 | T39–T42、T45；恢复、人工修改/删除保护及清理所有者 |
| D12 | T43–T44；文件待核验固定失败与零后续动作 |
| D13 | T48–T50；两种隔离产物、真实演示、全量门禁和独立双轴 |
| D14 | T13、T27–T46；按窄修订保留数据库/文件/人格/确认不变量 |
| D15 | 仅本票Registry/工具/最小接缝；下游#16/#18/#46保持各自责任 |

T49/T50 使用正式 build_default_host、opencode-go / deepseek-v4-flash / openai，无临时请求头、HTTP client或factory注入。两个合成Run、7次Attempt，Host首次close成功；未追加Run或更换模型。两次检索gate因invalid_output使用确定性保守fallback（T49 gate输出达到768 token上限），因此本次证明工具真实执行，不声称gate模型JSON判定通过。服务端未返回精确费用。专属合成状态及脱敏证据仅本地保留，未上传凭据或原始演示数据。

后续边界：#16 会话与工作记忆的完整窗口/安全摘要，#18 真实提炼与Markdown镜像，#46 Memory页面/HTTP/memory_patch/SSE与重连仍由各自票负责；本票完成其真实Registry消费接线，不宣称这些下游产品完成。MCP、Graph、外部发送与同步、Skill匹配/注入/热重载不在本票范围。

本票T01–T50及D01–D15在上述边界内满足，按completed关闭。关闭仅作用于Issue #19，不删除分支、工作树或验收目录，不同步主工作树。
