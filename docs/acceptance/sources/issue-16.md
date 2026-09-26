# #16 实现：会话与工作记忆

来源：https://github.com/nineofoursyrup/Agent-Alfred/issues/16

## Problem Statement

用户希望私人助手记得当前会话最近发生的事，并在跨 Run 时知道已经执行过哪些动作。
目前会话恢复已有基础，但最近消息行数不能可靠代表完整聊天 Run；历史或工具结果增长时，
缺少完整请求的输入额度控制。遗忘隔离与实际读取关联尚未接入这条真实消费链路，
用户也无法逐次核对模型带了哪些历史、为什么省略以及是否真正发出了请求。

## Solution

为当前 Session 构造默认最近 20 个安全、完整、已记录聊天 Run 的工作窗口，另从真实
工具账选取有界的既往行动证据。每次模型请求按统一输入表示检查字符限额，超限以明确
顺序排除旧内容；必需输入仍放不下时明确停止，保持动作结果和记录状态真实。
检索门与首次回答共用历史窗口，运行详情提供不复制正文的“本次输入”说明，所有实际
使用与遗忘隔离接入同一生产链路。

## User Stories

1. As an assistant user, I want to continue a Session after restarting the application, so that my recorded conversation remains available.
2. As an assistant user, I want to create, list and explicitly switch Sessions, so that I control which conversation supplies context.
3. As an assistant user, I want to keep automatic context within the selected Session, so that unrelated conversations do not influence my request.
4. As an assistant user, I want to use the latest 20 eligible complete chat Runs by default, so that recent conversation stays available within a predictable limit.
5. As an assistant user, I want to configure the working-window size through the existing CLI and environment settings, so that I can balance continuity and input size.
6. As an assistant user, I want to set the working-window size to zero without implicitly disabling tool evidence, so that independent context controls retain their stated meaning.
7. As an assistant user, I want to have complete Runs selected rather than a fixed number of message rows, so that interrupted messages do not split valid conversations.
8. As an assistant user, I want to retain eligible recorded failed and max-steps replies as conversation context, so that the assistant can understand previous unsuccessful work.
9. As an assistant user, I want to view legacy and incomplete history manually, so that context filtering does not erase my original records.
10. As an assistant user, I want to exclude forgotten or unsafe sources before filling the working window, so that safe older conversations can still be used.
11. As an assistant user, I want to keep current new input usable when historical groups are explicitly isolated, so that forgetting does not automatically prevent a new conversation.
12. As an assistant user, I want to configure a deterministic input-character limit with a default of 64,000, so that oversized requests are detected locally.
13. As an assistant user, I want to override the retrieval-gate input limit independently, so that a separate gate model can use an appropriate limit.
14. As an assistant user, I want to have system instructions, content blocks, tool declarations and wrappers included in input measurement, so that the displayed limit reflects the complete normalized input.
15. As an assistant user, I want to see character limits distinguished from provider Token capacity, so that I do not mistake a local guardrail for a provider guarantee.
16. As an assistant user, I want to have the oldest complete conversation groups removed first when total input is too large, so that recent dialogue and action evidence are preserved predictably.
17. As an assistant user, I want to have ordinary tool evidence removed before unknown-result evidence, so that uncertain actions remain visible for as long as the budget permits.
18. As an assistant user, I want to keep my current question and selected memory content intact, so that the system does not silently change what the model is asked to consider.
19. As an assistant user, I want to receive an explicit failure when required input and retrieval reserve cannot fit, so that I can shorten input or raise the configured limit.
20. As an assistant user, I want to have the gate and first answer use the same safe history window, so that retrieval and answering share a consistent conversation basis.
21. As an assistant user, I want to avoid refilling history merely because retrieval returns fewer results, so that the chosen window remains predictable.
22. As an assistant user, I want to have input limits checked before every subsequent model request, so that growing tool exchanges cannot silently exceed the limit.
23. As an assistant user, I want to have an oversized Run stop without automatically repeating actions, so that already performed side effects are not duplicated.
24. As an assistant user, I want to retain tool-action evidence after its original conversation leaves the window, so that the assistant can still know about earlier actions.
25. As an assistant user, I want to receive summaries derived from real tool-ledger entries, so that claims about performed actions have an authoritative basis.
26. As an assistant user, I want to have tool summaries limited to complete entries within 20 entries and 4,000 characters by default, so that action evidence stays bounded and understandable.
27. As an assistant user, I want to have unknown-result entries selected first and newer entries selected first within each class, so that the most relevant uncertainty is prioritized.
28. As an assistant user, I want to see a continuous prefix of ordered entries rather than hidden skipping or partial entries, so that selection is explainable.
29. As an assistant user, I want to see how many entries were omitted and how many have unknown results, so that absence from the summary is not mistaken for proof that nothing happened.
30. As an assistant user, I want to issue a new explicit command with previously used parameters, so that tool evidence does not become an unintended execution lock.
31. As an assistant user, I want to keep remembered subjects and memory bodies out of tool summaries, so that action evidence does not bypass forgetting protections.
32. As an assistant user, I want to send ledger summaries to the answer model without expanding the gate input, so that the gate retains its established retrieval role.
33. As an assistant user, I want to have current-Run actions represented through the RunTranscript and considered for summaries only on the next Run, so that the same action does not occupy both representations unnecessarily.
34. As an assistant user, I want to have fixed prior candidates rechecked for isolation and validity before reuse, so that a snapshot cannot preserve access to invalid evidence.
35. As an assistant user, I want to inspect input characters, limits, selected identities and exclusion reasons in run details, so that I can understand what informed an answer.
36. As an assistant user, I want to distinguish reserved retrieval capacity and preparation failures from actual requests, so that the application does not invent model calls or usage.
37. As an assistant user, I want to retain durable source-use evidence for every real Attempt including retries, so that later forgetting can follow actual use across Runs.
38. As an assistant user, I want to keep the input explanation safe during reload, reconnect and delayed responses, so that obsolete content is not reintroduced through the interface.
39. As an assistant user, I want to keep existing persona precedence, next-Run updates and final recording behavior, so that the new context policy does not change unrelated behavior.
40. As a project maintainer, I want to verify these behaviors through existing complete-Run, adapter and browser boundaries with local fixtures, so that tests prove the production integration without paid model calls.

## Implementation Decisions

1. **配置与统一输入计量**

   | 项目 | 契约 |
   | --- | --- |
   | 工作窗口 N | 默认 20；沿用 working_memory_rounds、AGENT_ALFRED_WORKING_MEMORY_ROUNDS、--working-memory-rounds；允许 0 |
   | 每次回答请求输入上限 | 默认 64,000 字符；CLI／环境变量配置 |
   | 检索门输入上限 | 默认继承通用输入上限，可单独覆盖；不是自动推测模型容量 |
   | 工具账摘要 | 默认最多 20 条、合计 4,000 字符，完整账项与提示均计入；不设时间过期线 |
   | 长期记忆资料 | 沿用 semantic、episodic 各 4,000 字符的既有结果契约；外层包装另计入总请求 |

   N=0 只关闭工作窗口，不能把它解释为同时关闭已独立定义的工具账摘要。
   新增限额沿用现有配置优先级和校验风格；非法配置明确报告，不能静默回到另一额度。
   输入限额须为正整数；新增配置字段与 CLI 拼写在实现时遵循项目命名惯例。

   输入计量基于带版本的统一请求输入表示：系统提示、角色与有序内容块、工具声明及
   必要包装转换为确定性紧凑 JSON，再计 Unicode 码点数。工程约定为保留原字符串、
   非 ASCII 字符不强制转义、对象键排序、无多余空格，不作 Unicode 归一化；控制字符
   按 JSON 规则转义。认证、HTTP 头及生成参数不属于该输入表示，不能纳入说明或落盘。
   同一版本必须有固定字段白名单与确定性序列化测试，不依赖供应商 wire 差异。

   整体输入的转义与包装必须实际参与计数：不能把每库 4,000、工具结果截断 8,000 或
   摘要 4,000 简单相加，冒充最终请求长度。每次实际检查使用同一计量器。
   本地字符限额不保证供应商接受请求，也不代替 Token／费用记录；供应商上下文拒绝
   仍应如实报告，不能无声改阈值、换模型或裁更多输入重跑。

2. **完整工作窗口**

   只从当前 Session 已记录的 chat Run 中选取完整用户／助手消息组。完整不等于成功：
   completed、failed、max_steps 的完整已记录消息对均可成为候选；仍须通过来源安全检查。
   interrupted 的单条用户消息、未保存回复、probe 及无 Run 身份的旧消息不拼作完整组。
   旧消息与不完整记录继续保留人工查看，不删除、不猜配对、不伪造 Run。

   先排除遗忘隔离、未知来源暂停及证据不足的组，再按已有持久顺序选最近 N 组，
   入模保持对话时间顺序。不能先取 N 再过滤，不能用最近 2N 行近似，也不能从不透明
   Run ID 推断时间。实际省略原因应区分不完整、隔离／暂停、N 上限和输入预算。

3. **真实工具账的安全摘要**

   Run 开始时固定当前 Session 的既往账项候选。只使用真实账项的动作、状态、时刻与
   安全标识生成摘要，不回放历史工具消息冒充账证，不复制记忆 subject／正文或任意
   工具结果。当前 Run 新动作通过运行转录提供，到下一 Run 才进入摘要候选。

   候选在每次请求前重新核验隔离与有效性。unknown 优先，其余状态次之；各类内部
   按新到旧的持久顺序排序，时间相同用可复现的持久顺序打破平局，不解析不透明 ID。
   先在仍安全的固定候选中取排序后的连续前缀，同时满足条数和字符额度；第一条放不下
   即停止，不截断条目，不跳过长条挑短条。unknown 的语义保持“结果未知”，不是失败。

   提示列明本次安全候选因限额省略的总数及其中 unknown 数；隔离／未知来源暂停的
   排除数量另列，不能混称为模型仍可使用的证据。所有给模型的提示文字都计入限额。
   省略计数随最终选取重算；如果连必需的安全提示都放不下，按输入准备失败处理，
   不能省掉提示后假装摘要完整。账摘要只进回答模型，不进入检索门。

   摘要是有限证据，缺少记录不能证明动作未发生，也不构成相同参数的新命令禁令。
   既有同一操作身份的回执幂等与“用户再次要求执行”仍按 #19 区分。

4. **共同窗口、预留与逐请求状态推进**

   1. 在既有 Run 准入下取得本次配置、人格及候选，保留来源身份；完成安全过滤。
   2. 构造检索门和首次回答的规范输入轮廓。回答侧包括安全账摘要，并为长期记忆资料
      预留最大允许空间，含所有外层包装及在统一表示中的转义上界。
   3. 两边分别满足各自输入上限；任一侧超限都依次裁最旧完整对话组、最旧普通账项、
      最旧 unknown。检索门不含账摘要，裁账项不能被误认为可以降低检索门输入。
      省略提示变化须重算长度；不裁当前问题、人格、工具声明或当前 Run 转录来过关。
   4. 可裁内容耗尽仍不满足预留要求，检索门发出前就停止，报告实际已有输入、预留与
      超限原因。不先发请求碰运气，不伪造 Attempt、Token 消耗或实际读取登记。
   5. 检索门使用共同窗口及当前问题。普通模型故障仍走既有规则回退；预算错误和
      读取证据错误不能被当成普通 gate 失败吞掉。总 Run 期限仍有效。
   6. 取得检索资料后构造首次回答；实际资料少于预留时不补回旧对话。首次回答沿用
      共同窗口，并检查真实完整输入满足上限。预留契约失守须报告，不能偷偷再裁历史
      改写已承诺的首次共同窗口。安全失效始终优先，不能为维持窗口相同而携带失效内容。
   7. 后续 Step 重新核查来源和记忆版本、构造实际输入并检查总量。历史窗口可继续缩小，
      不因空间空出补回旧对话；账项仍只能来自 Run 开始时的固定候选。实际携带来源
      按本次请求登记，不能复用最初候选表当作全部 Step 的读取证据。
   8. 当前 Run 的工具往返增长导致可裁旧内容耗尽仍超限，停止 Run 并明确说明输入超限。
      不隐式压缩当前 Run，不自动重跑，既有副作用与工具账状态保持真实；正式保存与
      结局仍由既有 finalizer 负责。

5. **实际读取关联与失败边界**

   源版本、隔离或候选读取无法确认时，不得把失败转换成“零历史”继续发送。
   模型实际 Attempt 边界须登记确实携带的完整历史组及账项来源，包括重试／回退中
   确实发出的请求；候选、预留及准备失败不算读取。持久登记失败必须阻止无证发送，
   并遵守调用方事务所有权与 Run 记录边界，不能替 caller 提交或回滚。

6. **运行详情与入口契约**

   运行详情提供可展开的“本次输入”，按真实模型 Attempt 关联以下无正文信息：

   - 模型用途、Step／Attempt 身份、计量版本、实际输入字符数与适用上限。
   - 实际带入的历史 Run、账项安全标识及其来源关系。
   - 不完整记录、隔离／暂停、N 限制和字符限制造成的排除数量与原因。
   - 摘要省略总数及 unknown 数量，及有限摘要不能证明动作未发生的提示。

   预留和准备阶段排除另存为容量选择的解释，不能合并到实际发送字符数中。
   未发出的超限请求展示“输入准备失败”，有明确用量／预留诊断，没有虚构 Attempt。
   未知或读取失败必须如实显示，不能填 0。

   历史链接复用现有人工查看入口；说明不复制完整提示词、人格正文或被隔离正文。
   历史上实际携带过某个 ID 仍是事实；该来源后来被隔离时，说明不能继续提供自动复用
   正文的路径。页面失效、重连与迟到响应继续遵守现有安全规则，不另建正文缓存。
   CLI 的请求失败也须给可操作原因，重启后 --session 续接原能力保留。

7. **生产集成与存储所有权**

   复用 RuntimeHost 的统一准入和配置捕获、Run 执行器的上下文加载、Assistant 请求构造、
   RunMemory 检索与版本核验、ToolRegistry 工具账、MemoryCommandService 历史评估和消费登记、
   实际 Attempt 通知，以及 Dashboard 既有运行详情读取／更新链路。
   不要另建一套会话存储、检索门或副作用账；需要扩展的输入选择与计量职责集中在运行上下文
   构造边界，调用方交付完整请求及来源，不让 UI 自行重算权威结果。
   既有持久结构能表达所需事实时优先复用；确需新增持久结构时采用新的编号迁移，
   不原地修改已发布迁移，不替 caller 提交或回滚。无正文输入说明通过既有运行查询／更新
   接口暴露，新增字段遵循兼容性和实例／修订语义，不指定第二条状态权威。
   完整消息组与来源证据完整性是两个独立判断；来源缺失不能登记成明确无来源。
   Session 的新建、列出、显式切换、重启恢复及人工历史分页保持原契约。

## Testing Decisions

测试边界已由用户在本次 to-spec 明确确认：“符合，按此发布”。

1. **以完整 Run 为主测试入口。** 通过 RuntimeHost.submit → wait 驱动真实准入、上下文
   选择、检索、工具执行和收尾；使用真实临时 SQLite／FTS、共享记忆命令服务和
   ToolRegistry，只在模型外部边界使用 ScriptedModel／工厂替身。断言模型实际收到的
   完整请求、公开结果、持久来源和业务账，不用私有 helper 的调用次数证明集成。
2. **复用适配器与本地假传输边界。** 针对真实 Attempt 身份、重试、流式回退、编码失败
   与发送前登记失败，使用既有 Adapter＋MockTransport 模式核对是否真的发生了传输。
   ScriptedModel 不能单独证明 SDK 的真实发送顺序；不新增更低层的测试入口。
3. **复用浏览器与真实本地宿主。** 覆盖运行详情“本次输入”、准备失败、历史链接、
   reload／重启／重连／迟到响应及 CLI 续接。界面断言以用户可见行为为准，不绑定内部
   DOM 实现；已有 HTTP/SSE 权威与记录状态保持。
4. **沿用仓库先例。** 已有 RuntimeHost 检索门集成测试会检查真实 FTS 引用、gate/answer
   请求、Step 预算和落盘；工具集成测试通过真实日程／记忆命令检查唯一账项及唯一消息对；
   来源隔离测试覆盖重启、借用事务和来源失效；适配器测试已覆盖无事件汇时的真实发送
   证据；浏览器测试已覆盖工具往返、会话恢复和刷新。扩展这些高层行为，不复制生产算法
   写一套只会随实现一起出错的期望值计算器。
5. **确定性与范围。** 实现使用 TDD，故障排序由 FakeClock、Event／barrier 或受控传输
   驱动，不用 sleep 或概率循环。机械检查和浏览器验证沿用项目配置。真实模型测试
   仍需单独授权；本次规格发布不声称这些场景已经通过。

以下 19 个场景为必须覆盖的验收矩阵；既有 A49、A50、A51 和原始三项验收全部保留。

| 编号 | 场景 | 必须观察到的结果 |
| --- | --- | --- |
| V01 | N+1 完整组与多条零散消息交错 | 按完整 Run 取 N，最旧整组退出，无半组 |
| V02 | 最新组被隔离，更早组安全 | 先过滤再补足 N；两侧均不携隔离正文 |
| V03 | failed/max_steps、interrupted、probe、旧无 Run 消息 | 完整安全 chat 可用；其他不拼组，人工查看仍保留 |
| V04 | 不同 Session、N=0、CLI 重启恢复 | 不串 Session；N=0 仍允许独立账摘要；--session 可续 |
| V05 | 相同输入含汉字、组合字符、emoji、控制字符与嵌套 JSON | 同版本确定性计量，等于限额通过，大于限额不发送 |
| V06 | 工具声明／人格／工具结果包装使输入超限 | 全部实际表示计入；不只计算正文或截断前长度 |
| V07 | gate 上限更小、不同模型、检索资料为空／满额 | 共同窗口同时满足两侧，空召回不补回历史 |
| V08 | 原始 8,000 字符加包装与二次转义 | 预留按完整表示上界计算，不能低估并破坏首次共同窗口 |
| V09 | 固定输入加预留超限、实际召回可能为空 | gate 前停止；预留与实际分列，零虚构 Attempt |
| V10 | 老动作已退出对话窗口 | 仍可从同 Session 真实账产生摘要，不回放工具消息 |
| V11 | unknown 超过 20 条或字符额度，第一条过长 | 优先级及连续前缀正确，完整条目、省略计数与提示均有界 |
| V12 | 总预算不足以容纳窗口和摘要 | 按对话→普通账→unknown 顺序整组裁剪，提示重新计数 |
| V13 | 当前 Run 新动作、候选后续失效 | 新动作只在转录；失效旧证据退出；下一 Run 才可纳入新账 |
| V14 | 工具多 Step 累积超过限额 | 每次请求前检查；耗尽旧上下文后停止，不重跑已发生动作 |
| V15 | 明确再次执行相同参数 | 摘要不机械拦截；同操作回执重送仍遵守既有幂等 |
| V16 | gate／answer 真实重试和无请求分支 | 每个真实 Attempt 的实际来源持久可核对；无请求不造读取 |
| V17 | 来源／登记失败，遗忘隔离和记忆版本改变 | 不无证发送、不吞失败；遵守现有失效及删除收尾 |
| V18 | Dashboard 成功输入、准备失败、重连和迟到响应 | 实际／预留／省略可解释，不伪造 Attempt、不泄露正文 |
| V19 | CLI／Dashboard、Session／finalizer、#17/#19/#45 集成回归 | 复用既有契约，无双写、事务越权或人格回退 |

## Out of Scope

- 新建模型历史搜索工具、跨 Session 自动召回、隐式对话压缩或新的设置页面。
- 替换 Session／agent_log／RunTranscript 的所有权，或重写已经交付的人格、删除收尾、
  ToolRegistry、检索门与 MemoryCommandService 核心。
- 将工具账变成重复动作锁，或宣称本地字符上限等于模型 Token 容量。
- #18 的提炼与 Markdown 镜像、#46 的 Memory 编辑页及其独立产品交付。
- 自动提交、推送、合并、发布应用、关闭 Issue，或未获授权的真实计费模型调用。

## Further Notes

- 本规格整合已确认的 Q1–Q14、两项架构取舍及 19 个验收场景。产品共识已确认；
  测试边界也在本次发布前单独确认，无新增产品访谈。
- 主线事实基线为 `459f91137e4638dc3300a61b890f8480a0071c2c`，tree 为
  `6cf3b11d821cb38aa3c0b69218063fb5aad375f6`；施工时必须重新核对实际基线与认领状态。
- 本票是 [地图 #1](https://github.com/nineofoursyrup/Agent-Alfred/issues/1) 的既有施工票。
  原生依赖仍是权威：[schema #12](https://github.com/nineofoursyrup/Agent-Alfred/issues/12)、
  [运行与 CLI #13](https://github.com/nineofoursyrup/Agent-Alfred/issues/13)、
  [ToolRegistry #19](https://github.com/nineofoursyrup/Agent-Alfred/issues/19)、
  [遗忘与来源隔离 #45](https://github.com/nineofoursyrup/Agent-Alfred/issues/45)，本次查证均 CLOSED。
- 沿用 [#31 权威规范 R07/R09/R15 与 A49–A51](https://github.com/nineofoursyrup/Agent-Alfred/issues/31#issuecomment-5588135839)
  及 [#16 三词分名与工具账回写](https://github.com/nineofoursyrup/Agent-Alfred/issues/16#issuecomment-5429259801)。
  本规格明确补齐原票中尚未决定的全局输入限制；人格优先级和下一 Run 生效已由 #19 D09/D15
  决定并交付，本票复用，不再作为未决项。
- [#45 收尾](https://github.com/nineofoursyrup/Agent-Alfred/issues/45#issuecomment-5602762043)与
  [#19 收尾](https://github.com/nineofoursyrup/Agent-Alfred/issues/19#issuecomment-5612160788)
  明确把真实完整窗口、安全摘要及消费链路留给本票，前置测试通过不能代替本票验收。
- 尊重既有 ADR 中的遗忘、调用方事务、Run 收尾独占记录、旧消息不伪造 Run 和真实
  Attempt 语义。两项本次设计取舍是：行动证据候选独立于工作窗口；共同窗口提前
  预留最大检索资料容量。它们的完整规则已在 Implementation Decisions 中写明，
  不依赖读者访问任何本地草稿或未合并 ADR 才能实施。
- 实施沿用原票要求：独立 agent、TDD、适用机械检查、固定候选的 Standards／Spec
  独立两轴评审；先核对 worktree 归属。ready-for-agent 表示规格就绪，不代表实现完成，
  不自动授权 Git 发布动作或关闭票。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/16#issuecomment-5429259801

## 回写修订（来自 [决定：Graph 节点与 state 协议](https://github.com/nineofoursyrup/Agent-Alfred/issues/6)）

本票的「工作记忆是读侧投影」这一点不变，但两个词的边界现已钉死（`CONTEXT.md` 新增三词分名）：

- **会话记录（`agent_log`）**：跨 Run 存续的对话本身。一次 Run 只留**一组**用户消息与助手回复，
  由 Run 收尾处**独占**写入——任何节点、任何循环都不直接写它（详见 [#13](https://github.com/nineofoursyrup/Agent-Alfred/issues/13) 的回写修订）。
- **运行转录（RunTranscript）**：当前 Run 内供后续 Step 使用的已提交消息序列，含中间工具往返，随 Run 结束而消失。
- **工作记忆**：从**会话记录**取的最近 N 轮窗口，不是从运行转录取。

由此本票的两条实现细节需要明确：

1. 「取当前会话最近 N 轮」的数据源是**会话记录**，而中间工具往返本就不在里面——
   因此本票原列的「工作记忆中保留**已执行工具摘要**，避免跨轮重复执行有副作用的动作」
   **不能靠回放历史消息实现**，只能读 `tool_ledger`（[#4](https://github.com/nineofoursyrup/Agent-Alfred/issues/4) 定的工具账）。
   顺带提醒该票已裁定的边界：工具账是**证据不是锁**，系统不据此拦截重复调用，
   本票那条验收（「同一有副作用工具在连续两轮不被重复执行」）须据此重写为
   「模型看得见上一轮已执行过什么」，而不是「系统拦下第二次」。

2. 「截断策略要可解释（Dashboard 上要能看出这轮带了哪些历史）」中的「历史」即**会话记录**。

## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/16#issuecomment-5614466540

## #16 实现验收与发布收尾

已按当前规格及 V01–V19 验收矩阵完成实现，独立 Standards CLEAR（0 项）/ Spec CLEAR（0 项）。

- 工作窗口从当前 Session 的完整、安全、已记录 chat Run 选择，先过滤再取默认最近 20 组；N=0 独立于工具账摘要。会话切换、重启续接和人工查看原记录保持既有契约。
- 完整请求使用版本化、确定性的 Unicode 字符计量；默认 64,000 字符，检索门可单独配置。检索门和首次回答共享安全窗口及检索预留；每个后续请求再次校验，按整组顺序裁剪历史与账项，必需输入仍超限时停止，不重做已经发生的动作。
- 行动摘要来自真实 tool_ledger，默认 20 条/4,000 字符，unknown 优先、完整连续前缀及省略计数可核对；它是证据而非重复执行锁。
- 每个真实 Attempt 的来源登记与遗忘隔离接入生产链路。登记遵守有效截止时间；未发送的 provisional 记录不会形成永久消费隔离；确认故障不覆盖原始控制异常。
- Dashboard 提供无正文的“本次输入”解释，区分准备、实际调用及来源待确认。独立终态事实在登记缺失时仍可恢复；缺失终态或流式元数据如实未知，刷新不会伪造暂态正文。

发布证据：
- [PR #50](https://github.com/nineofoursyrup/Agent-Alfred/pull/50) 已合并；修复提交 `53719454c5bb5eaee45fbdcd066ef00157b5975f`，合并提交 `665ed94b75a0c86011814413a2980cafcd427001`。
- 合并 tree `45f9b3d0d43d2406157f783969fd8271950b8885` 与独立评审的 candidate-v7 完全一致；远端 main 已回读一致。
- [PR CI](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34446455805) SUCCESS：Python 3476 passed / 1 deselected，浏览器 77 passed，静态门禁全部通过。
- [合并后 main CI](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34447006629) 在准确合并提交上 SUCCESS：Python 3476 passed / 1 deselected，浏览器 77 passed，静态门禁全部通过。

以上远端 CI 是各自提交上的实际新运行，与下述本地 Python 复用证据分开记录。根据用户明确授权，在合并后 CI 成功后以 completed 关闭本票。

本地验证：candidate-v7 浏览器全量 77 passed，Ruff/typecheck/env/skills/diff-check PASS；Python 3476 passed / 1 deselected 为 candidate-v6 原始结果，核对 v7 Python 与配置字节未变后明确复用，不冒充 v7 新运行。故障排序、真实 Adapter + MockTransport、RuntimeHost/SQLite/FTS 与本地浏览器路径有回归覆盖。

未运行真实计费模型；本地字符限额不等于提供方 Token 容量保证。#18 记忆提炼/Markdown 镜像与 #46 Memory 编辑页仍是独立 OPEN 交付，本次未修改关联票或地图。
