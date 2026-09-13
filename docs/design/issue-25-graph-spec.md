## Problem Statement

Alfred 后续流程需要把分类、模型回复、工具调用与完整 Agent 循环组合成可检查的执行图。目前 Graph 仅有占位目录，维护者无法在启动时发现非法读写或控制流，也无法可靠区分有效输出、错误恢复、波撤销和已经发生的费用/副作用。若失败后重跑已执行工具，或把作废分类结果写进会话记录，用户会遭遇重复动作与错误上下文。

## Solution

交付支持 fan-out/fan-in、条件边和专属 error 边的确定性同步 DAG 引擎、四种节点工厂、GraphRegistry，以及真实组件驱动的离线玩具图验收。图在启动组装时编译并冻结，按波读取快照与提交有效产出；结果和事件明确表达恢复、无动作、失败与预算耗尽，真实费用和副作用不随撤销消失。

本规范为 **GRAPH-SPEC-r1**，等义整理自已获 D3 整体确认的 GRAPH-DESIGN-r4。实施与验收以本文为唯一合同，Critical Counterexamples 内完整承载 CE-01–CE-16，不依赖未发布的 companion 或设计聊天。设计源只保留批准溯源，不与本文并行维护验收定义。两个业务 workflow 与用户选图产品路径不在本票范围。

## User Stories

1. As a 图作者, I want 显式声明节点身份、读写与图输入, so that 每个值的来源和可配置跳过行为可被检查。（R01）
2. As a 图作者, I want 用统一返回协议组合四种节点并注入模型依赖, so that 相同图代码可以离线验证和真实执行。（R02）
3. As a 维护者, I want 非法图在启动编译时明确失败, so that 不把配置与拓扑错误留到用户请求之后。（R03）
4. As a 图作者, I want 每波节点获得受声明限制的不可变快照, so that 执行顺序不会悄悄改变输入含义。（R04）
5. As a 用户, I want 失败时只保留合法提交并停止不应继续的动作, so that 不把半份产出或重复副作用当成成功。（R05）
6. As a 用户, I want 能区分正常产出、恢复产出、无动作和失败, so that 了解本次流程实际完成了什么。（R06）
7. As a 维护者, I want 从编译图读取唯一拓扑和版本指纹, so that 后续界面与审计能够判断结构是否兼容。（R07）
8. As a 维护者, I want 事件准确归属节点且只在波提交后宣告完成, so that trace 不留下虚假的成功记录。（R08）
9. As a 用户, I want 整个 Run 共享预算且保留全部真实费用, so that 图、嵌套循环或回退不会扩大已配置额度。（R09）
10. As a 用户, I want 节点只获得声明允许的工具能力且会话只保留实际交付回复, so that 未授权动作和中间候选不会影响后续对话。（R10）

## Implementation Decisions

### 权威来源、批准与实现基线

- 本票属于 [#1](https://github.com/nineofoursyrup/Agent-Alfred/issues/1)，继承 [#25 原始要求](https://github.com/nineofoursyrup/Agent-Alfred/issues/25)、[#6 裁决](https://github.com/nineofoursyrup/Agent-Alfred/issues/6#issuecomment-5429217158) 与 [#3 事件回写](https://github.com/nineofoursyrup/Agent-Alfred/issues/3#issuecomment-5429258906)。Q1–Q7 对旧文的明确修订如下，遇旧文字冲突以这些已批准修订为准。
- D1：用户对 Q1–Q3 回复“都按建议”；D2：用户对 Q4–Q7 回复“都按建议”；D3：用户对完整设计、测试接缝与排除项回复“确认”，均发生于 2026-09-13。D3 的 r3 对象 SHA-256 为 `9fbfd17253ff9068af6a1d4c08ee7f97f298a3a9474da4c645fdcdbeb4c902a0`；r4 仅登记批准，SHA-256 为 `3104da6f2432c4fc9336c4ee8c8aa30e998ddb4861463d3de53538efa6a6c46c`。
- 基线为 `7f38e8b6abe77022315584e0fd9d098d9219c470`。规范整理时远端 main 与此一致；#25 OPEN、未认领，原生前置 #13/#19 CLOSED。实施前重新检查远端、工作树归属和新增合同。

- [ADR-0010：波快照与单写者（本规范 Q1/Q3/Q4/Q7 修订提交边界）](https://github.com/nineofoursyrup/Agent-Alfred/blob/7f38e8b6abe77022315584e0fd9d098d9219c470/docs/adr/0010-wave-snapshot-and-single-writer.md)
- [ADR-0011：能力缩减不是沙箱](https://github.com/nineofoursyrup/Agent-Alfred/blob/7f38e8b6abe77022315584e0fd9d098d9219c470/docs/adr/0011-capability-reduction-not-sandbox.md)
- [ADR-0012：共享预算与不回补](https://github.com/nineofoursyrup/Agent-Alfred/blob/7f38e8b6abe77022315584e0fd9d098d9219c470/docs/adr/0012-run-budget-is-shared-and-never-reset.md)
- [ADR-0032：真实工具计量及故障停止](https://github.com/nineofoursyrup/Agent-Alfred/blob/7f38e8b6abe77022315584e0fd9d098d9219c470/docs/adr/0032-tool-metering-survives-trace-pruning.md)

### 公共接口与复用边界

构建入口：`builder.add_node(node_id, ...)`、`declare_input(key, required=True|False)`、`add_edge(...)`、`add_conditional_edges(source, path, path_map, required_reads=(), optional_reads=())` 和 error 边声明；`compile() -> CompiledGraph` 一次完成全部校验并生成冻结产物。只有编译图可以执行，`CompiledGraph.invoke()` 返回闭合结果，`describe()` 从同一编译缓存生成拓扑。GraphRegistry 在启动构建 `graph_id -> CompiledGraph`，未知名称响亮失败，选择执行哪张图属于外层；一次 Run 至多执行一张图，不引入回边或 max_visits。

四工厂统一“只读 state → NodeOutcome(writes)”业务返回，路由不在返回值中。上下文由工厂受控绑定；fn 只给 PureNodeContext，tool 只接注册 tool_name，agent 静态工具白名单不能被普通授权策略替代。NodeContext.error 是专用错误信息通道，不在业务 state 中塞入 __error__。

实际可执行工具集合遵守白名单、注册、配置与授权；白名单内未配置工具沿现有策略提供配置指引，白名单外连引导 Schema 都不存在。白名单校验同时覆盖模型可见定义与强行调用执行；不能只从模型请求里隐藏。新受限视图须共享真实计量/账本与授权事实，不能复制出另一份业务账。

| 公共模块 | 基线能力与本票适配责任 |
|---|---|
| `graph/` | 当前只有占位；交付 builder/compile/invoke/describe、四工厂和 Registry，内部拆文件由实施者决定 |
| `loop/budget.py` | 已有 RunBudget.reserve_step/StepLease；直接共享，不创建第二套预算 |
| `loop/assistant.py` | respond 已接受 client/budget/working_memory/tools；补充节点身份与候选产出/转录接缝，继续复用现有循环 |
| `model.py` / `attempt_io.py` | 使用 ModelClientFactory/ScriptedModel 注入及真实 Attempt 路径，不捕获真实密钥，不以假计量替代 |
| `tools/__init__.py` | 复用 Registry.execute 与真实工具路径；with_policies 不能实现 local_read/local_write/external 全种类的白名单 |
| `events.py` / 遥测与 trace | 已有 node_id 信封；补 graph/node 载荷、波提交/撤销语义和内部模型/工具归属，保留中央净化与现有封闭 wire 规则 |
| `tools/metering.py` / `tools/ledger.py` | 现有身份带 step_index；零 Step tool_node 需可区分且稳定的合法持久身份，不能靠扣模型 Step 或伪造事件 step_index 适配 |
| `runtime/recording.py` | 复用 RunRecorder.settle 与正式会话记录；以窄集成接收图执行适配结果，不增加 Host 用户选图入口 |

不预先规定新数据库列、迁移编号或私有类名。若实现需要持久 schema 变化，采用新迁移保留已有账目与身份；不能改写已发布迁移来绕过兼容。保持既有 API 默认普通循环的行为，节点包装不能接管外层 Run finalizer。

### state、控制流与结果闭合

确定性同步 DAG、节点至多访问一次、同层声明顺序、单写者、声明读视图、optional 缺席不填 None、边级决议与 OR fan-in、编译冻结、纯 describe 与版本化完整哈希、共享预算不退、账本副作用三态、finalizer 独占会话记录，全部继续有效。

`writes` 沿用原文允许上限（实际集 ⊆ 声明集），不擅自收紧成每次写齐。成功节点漏写后，下游 required 缺席必须在调用前失败；编译的来源分析不能证明任意 callable 每次都履约。普通节点调用前 required 缺席按该节点普通执行失败处理，可经专属 error 边接管；路由器 required 缺席按 Q4 终止整波。result 终点实际漏写 output_key 按终点执行失败，不可补 None 冒充成功。

不可变快照包含嵌套数据不被节点就地影响的保证；具体冻结表示与实现接口由实施设计选择，不能仅冻结外层 Mapping 后声称满足契约。

沿用后续票的 Run 级停止约定：[工具实现合同](https://github.com/nineofoursyrup/Agent-Alfred/blob/7f38e8b6abe77022315584e0fd9d098d9219c470/docs/implementation/issue-19-tools.md) 的删除成功固定无正文收尾、文件结果未核验停止，以及 [MCP 规范](https://github.com/nineofoursyrup/Agent-Alfred/blob/7f38e8b6abe77022315584e0fd9d098d9219c470/docs/design/issue-21-mcp-spec.md) 的结果未知、迟到结果不恢复原 Run，与 [ADR-0032](https://github.com/nineofoursyrup/Agent-Alfred/blob/7f38e8b6abe77022315584e0fd9d098d9219c470/docs/adr/0032-tool-metering-survives-trace-pruning.md) 的计量故障停止继续有效。Graph 不得借 error 边启动后续模型/工具或掩盖这些固定结局；这些不属于普通节点错误恢复。

边级决议适用以下固定规则：普通边源 succeeded 则 taken，skipped/failed 则 not_taken；条件组仅在源 succeeded 时执行一次 path，命中标签对应边 taken、其余 not_taken；源 skipped/failed 时整组 not_taken 且 path 不调用；error 边只有可恢复的源 failed 时 taken。path 是纯函数，只返回代码声明的闭合标签，不存在按任意节点名跳转 API。fan-in 必须等待全部入边决议，任一 taken 执行，否则级联 skipped；配置跳过必须事先 skippable_by_config 声明。波撤销时这些候选决议不对后继生效。

闭合结果为：

```text
Completed(output)
CompletedWithRecovery(output, recoveries)
NoAction(reason_code, recoveries=())
Failed(error, side_effect_state, committed_state)
BudgetExhausted(error, side_effect_state, committed_state)
```

仅前两种有 output；合法显式 None 与缺失 output_key 不同。预期执行结局均走返回值，编译后的引擎内部不变量违规才抛 GraphInvariantError，进程控制中断不得吞掉。每次图必须结算恰好一个成功静态终点；未接管失败优先终止，不因已存在一个成功终点就掩盖它。

预算在首个模型网络 Attempt 前取得 lease，重试/流式回退共享租约；fn/tool 不产生 Step。llm_node 耗一个 Step，agent_node 耗一至多个，耗尽时不发请求；零 Step 的确定性 error 节点允许恢复。最后一份预算恰好耗完并非失败，成功恢复也不能被 remaining=0 覆盖；未恢复的耗尽才 BudgetExhausted。波回滚只撤销产出有效性与运行转录追加，预算、原始 Attempt outcome、Token、缓存明细、工具成本和已知/未知费用照实保留，Step 标记 aborted_by_wave。

事件包括 persist 的 graph.started（含 graph_id/topology_hash）、graph.finished、node.started/finished/skipped/aborted。node.started 及时反映开始；node.finished 等波成功决议后发出，已接管失败为 outcome=failed；整波撤销仅给已 started 节点发共同失败原因的 node.aborted。node.skipped 原因闭合为 all_inbound_not_taken 或 disabled_by_config。嵌套 attempt/block/tool 事件必须使用实际 node_id，fn/tool 的 step_index=None。未开始不能伪造事件、调用或账。trace 仍可保存作废事实，不伪造未发生。

副作用 none 才允许外层考虑普通回退；occurred/unknown 禁止自动重跑。Run 强制收尾也禁止继续，不得仅凭 side_effect_state=none 绕过。正常可确认工具错误与执行结果未知沿既有合同区分。

### 执行适配与验收范围整合

#25 的公共验收保持 GraphRegistry → CompiledGraph.invoke → 四种真实节点工厂，agent_node 复用 Assistant，tool_node 复用 Registry/账本。ScriptedModel 经注入边界驱动同一份执行代码；外部服务与时钟/ID可受控，生产循环/调度/账本/收尾不能换成假成功。新增测试与适用项目门禁须由实施会话实际运行，本设计会话全部 NOT RUN。

固定收尾不伪造已抵达终点：若非终点节点确认删除后触发全 Run 收尾，图返回 Failed，携能独立识别“强制收尾、禁止继续”的结构化事实（不是只靠事件或可变上下文）；外层 Run 仍保留已完成删除的 completed 和原固定系统回执。文件/MCP 未核验及计量失败保留各自 failed 结局。Graph 的 Failed 表示声明的拓扑未完成，不能把它直接显示为“删除失败”；执行适配保留这两个层次的不同事实。不得仅因 side_effect_state=none 就忽略强制收尾并开启普通回退。

复用既有可信收尾记录和 RunRecorder.settle 验证最终 reply/outcome/error、固定收尾字段、唯一一组 agent_log 和真实费用；存放不可变收尾记录的内部类型由实施者选择。此映射遵守既有终点与固定收尾合同，已由 D3 整体确认，不新增第六种 GraphResult，也不为保存回执执行后继业务节点。

### 已确认的协议修订

确认来源 D1：2026-09-13，用户对第一轮 Q1–Q3 回复“都按建议”。以下三项 confirmed；整体批准另见 D3，不包含实现授权。具体类型名称可以机械调整，可见行为不可默改。

### Q1 被 error 边接管的失败与波提交

设计依据：同波 A 产出 a，B 失败并有 error 边到 R。#6 §3/§5 的任一失败（包括 path 失败）阻止波提交，与 §11 允许被接管失败的波成功决议存在冲突。

决定（Q1 / D1）：普通执行失败若被显式 error 边接管，丢弃失败节点所有候选写入与转录，允许同波其余成功节点通过整批校验后提交，提交 B=failed 与 error taken，下一波再运行 R。未接管失败或波写集校验失败才整波撤销。需明确限定旧文中“任一失败”的适用范围，不把两段假装成天然一致。path 失败按 Q4 整波撤销并终止，不随普通异常放宽。Run 级强制停止仍优先。

代价：有错误恢复的波允许部分节点业务失败，但成功节点写入和全部边级决议仍以一个批次生效。该决定避免为一个已被接管的普通失败重跑成功节点；尚未接管或整波不合法时仍保留原子撤销。

### Q2 恢复后抵达无动作终点

设计依据：B 失败，R 成功抵达 no_action。#6 §6 要恢复写入结果，§7 的 NoAction 只有 reason_code，且仅两种 Completed 有 output。

决定（Q2 / D1）：保留五种顶层结局，将 NoAction 扩成 `NoAction(reason_code, recoveries=())`；正常无动作恢复记录为空，经 error 恢复则必携记录，仍无 output。本规范明确修订 #6 的该返回成员，不能把恢复藏在可选事件流。

### Q3 路由器读取源节点刚写下的值

设计依据：分类节点 C 候选写下 category，C 的 path 在波末提交前据此选分支；同波另一节点 D 也有候选输出。

决定（Q3 / D1）：路由器只见“波初快照 + 自己源节点的候选 writes”，再按路由器声明的 reads 缩小视图；看不到 D 的候选写入。普通节点仍只见波初快照。path 结果也是候选，只有按 Q1 成功决议才生效。编译分析把源节点写入纳入该路由的合法来源；运行时仍检查实际 required 缺席。



确认来源 D2：2026-09-13，用户对第二轮 Q4–Q7 回复“都按建议”。四项 confirmed；D1 与 D2 共同确认 Q1–Q7，整个整合稿后经 D3 确认。

### Q4 路由失败后的恢复边界

设计依据：A、B 同波，A 已有候选写入，B 的 path 抛异常、返回未知标签或缺实际 required 数据；B 声明有 error 边。旧协议禁止路由失败波提交，Q4 明确其终止行为。

决定（Q4 / D2）：这类路由失败整波撤销并直接返回 Failed，不再启动 error 目标，不重跑同波节点。波写集校验失败同样不能用 error 边掩盖；外部节点返回越权写入是运行合同失败，只有编译后引擎内部不变量被打破才抛 GraphInvariantError。该决定明确限定 #6“节点失败走 error 边”的适用范围：普通执行失败可以恢复，控制流无法合法决议的波直接结束。

代价：一个原本可用作兜底的 error 节点不能接管路由程序错误；换来不新增“波已回滚但部分失败决议仍生效”的第二套状态语义。源节点自己抛普通异常仍按 Q1。

### Q5 错误节点的唯一归属

设计依据：A、B 同波失败，共用 R；R 只执行一次且 NodeContext.error 只容纳一个来源。另一反例是 R 被配置跳过或由普通入边提前执行，A 失败后已无可用恢复。

决定（Q5 / D2）：每个源节点最多一条 error 边，每个 error 目标只归属一个源节点，恰有一个 error 入边、没有普通或条件入边、不可配置跳过；沿用错误节点不可再有 error 出边。各自恢复后可以经普通边汇合到共享节点。编译时拒绝不满足专属性的图。

代价：两个来源不能共用同一个错误节点身份；可以复用同一个处理函数构造两个节点，保留来源和恢复记录的一一对应。

### Q6 终点是成功分支的出口

设计依据：T 成功产出后沿普通边继续调用 effectful 工具 U；或 T 自己失败需走 error 边恢复。

决定（Q6 / D2）：终点不得有普通或条件出边，允许 error 出边（若该终点同时是错误节点，仍适用禁止链式恢复）；成功只结束该分支，其他已可达分支继续按确定性规则结算。失败终点不计成功抵达，可由专属 error 节点恢复。整个图必须结算出恰一个成功终点，不因先看到一个成功终点就提前报成功。

静态已可证明没有任何合法终点的空图/无终点图在 compile 拒绝；拥有合法静态终点但本次全被跳过，invoke 返回零终点失败。多 root 可以作为初始波，不新加强制单入口条件。

### Q7 已知失败时停止未启动动作

设计依据：A、B、C 按声明顺序同波执行，B 抛没有 error 接管的普通异常，C 尚未开始但会产生副作用。

决定（Q7 / D2）：立即停止启动 C 和后续波，撤销本波已经产生的候选；为已经 started 的节点发 node.aborted，未启动 C 不造 node.started/node.finished/node.skipped 事件、工具账或模型请求，图失败结果的安全诊断标明未执行节点及原因。已经 committed 的先前波保持；预算与副作用事实不回补。整波写集校验若直到波末才发现失败，则如实保留已经发生的 C 调用与实耗，不能声称 C 没执行。

首个使图必须终止的原因按稳定执行/校验顺序成为主错误；已接管的普通错误不会短路其他同波节点。Run 级固定收尾、中断和内部不变量继续优先遵守原约定，不用一般失败规则吞掉它们。

### R01–R10 要求索引

R01–R10 继承原票与上游要求，并应用 Q1–Q7 明确修订，不以本表代替对应详细合同。

| ID | 必需合同 | 对应验收 |
|---|---|---|
| R01 | builder 的 node_id 显式提供、匹配 `^[a-z0-9_]{1,64}$`、唯一、禁保留名/双下划线前缀；声明 required_reads/optional_reads/writes/skippable_by_config/TerminalSpec；图输入 declare_input(required=...) 为所有权表中的 writer | CE-04、CE-14 |
| R02 | 四工厂 fn_node/llm_node/tool_node/agent_node；唯一业务返回 NodeOutcome 只含 writes，不含路由；依赖经构造注入，同一生产代码由 ScriptedModel 驱动 | CE-03、CE-06、CE-07、CE-15 |
| R03 | 一次 compile 完成全局单写者、路径敏感 required/optional、条件组最多一个/标签映射闭合、error 专属性与链禁止、终点归属/出边、工具白名单名称存在/唯一/有序、无环等全部校验；非法定义在启动组装失败，执行产物冻结 | CE-04、CE-11、CE-12、CE-14 |
| R04 | 拓扑分波，同层声明顺序串行；普通节点读同一波初不可变快照并过滤声明；路由读自己源的候选；optional 缺席不填 None；正常 writes 为上限，required 实际缺席调用前失败 | CE-03、CE-05、CE-09 |
| R05 | 结果/路由 provisional，波末写集校验后整批提交；Q1 允许被接管普通失败，Q4 路由失败终止，Q7 未接管失败短路；普通/条件/error 边按已定 taken/not_taken，fan-in 等全部决议后任一 taken 即执行，否则级联跳过 | CE-01、CE-09、CE-10、CE-13 |
| R06 | NodeContext.error 独立于 state，携 node_id/code/message/side_effect_state；失败候选永不提交；五态结果 Completed、CompletedWithRecovery、NoAction（Q2 有 recoveries）、Failed、BudgetExhausted；仅前两者有 output，恰一成功终点 | CE-01、CE-02、CE-08、CE-12、CE-13 |
| R07 | describe 缓存、纯函数、无 IO；schema_version/topology_hash/topology/presentation；hash 包含输入声明及 required 性、node_id/kind/读写/工具白名单/可跳过/终点、普通/条件/error 边与 path_map，路由声明读依赖保留在拓扑；排除展示/布局/提示词/代码；对象键和无序集合排序，有序声明保序，无空白 UTF-8 JSON → SHA-256；trace 完整摘要，UI 仅前16位且比较版本 | CE-09、CE-14 |
| R08 | graph.started/finished 与 node.started/finished/skipped/aborted 两族均 persist；node.finished 仅波成功决议后，handled failure 发 failed；aborted 携共同失败原因；skip 闭合原因为 all_inbound_not_taken/disabled_by_config；内部 attempt/block/tool 事件保留 node_id；每 Run 至多一张图 | CE-01、CE-06、CE-09、CE-13、CE-15 |
| R09 | RunBudget 全 Run 共享；llm 一 Step，agent 一至多 Step，fn/tool 不产生 Step、事件 step_index=None；首次模型 Attempt 前取 lease，重试不重扣，耗尽不请求；撤销不退且 Step 标 aborted_by_wave，真实 Attempt/Token/费用保留；副作用三态来自真实共享账 | CE-06、CE-07、CE-08、CE-15 |
| R10 | fn 仅 PureNodeContext，无连接/路径/sink/Registry，能力缩减不声称沙箱；tool 仅注册 tool_name，不接任意 callable；agent 白名单同时过滤定义、引导 Schema 与执行，空集不发工具定义；GraphRegistry 启动注册，未知名响亮失败，选择权在外；finalizer 独占会话记录 | CE-07、CE-14、CE-16 |

## Testing Decisions

### 验收方法与公开入口

只通过公共构造、调用、返回值、实际请求、事件和持久事实证明合同；不以私有函数次数、实现镜像断言或静态成功替身冒充行为验证。每条非法图规则各有独立 compile/启动拒绝测试；编译合法的“第三节点越权返回”用于波末纵深校验，不篡改私有编译表来制造冲突。用确定性时钟、ID、脚本模型、barrier 或精确 IO 故障控制次序，不用 sleep 猜竞态。

公共主接缝为拟交付的 GraphBuilder.add_node/add_edge/add_conditional_edges/compile → CompiledGraph.invoke/describe，以及 GraphRegistry 公共注册/查找。使用真实引擎、RunBudget、Assistant、ToolRegistry、临时 SQLite/受管文件、事件汇与遥测。允许替换模型网络为 ScriptedModel、注入时钟/ID，精确外部 IO 故障；不替换生产调度、Registry、账本与 finalizer 的业务结果。正式会话记录用图执行适配结果 → 真实 RunRecorder.settle/SQLite 的窄集成验证。#25 不要求给 RuntimeHost.submit 增加用户可选图入口、业务 workflow 或选择页面；完整 Host 选图验收由后续消费者票承担，不把未运行的产品路径计为通过。

### IC-01–IC-03 整合验收

- **IC-01 真实执行边界：** 图/四工厂/Assistant/Registry/预算/SQLite/收尾真实运行；模型及外部网络可替换，精确时钟/ID/故障边界可注入。纯逻辑玩具节点本身是图定义，不能以模拟调度结果替代引擎。MCP unknown 用受控真实服务边界复现，不要求私人服务或付费调用。
- **IC-02 图与 Run 固定收尾：** 图未抵达声明终点时不伪造 Completed；Failed 中包含可信、可被无事件消费者识别的强制停止事实，外层删除成功 Run 仍 completed，未知/计量故障保持 failed，原固定回执不变。GraphResult 无 output 的约束与 Run 最终回复不是同一层级。
- **IC-03 验收归属与门禁：** finalizer 以真实 RunRecorder/SQLite 窄集成验证；本票不增加用户选图入口或两个 workflow。实施按基线 `.github/workflows/ci.yml` 完成 Ruff、全离线 Python、Skill/.env 一致性、wheel/sdist 干净安装、Dashboard typecheck 与浏览器回归，不静默跳过已配置门禁；新增 Graph 公开入口必须纳入非 editable 安装验证。所有原票必需验收及本稿 CE 均要逐项证据，双轴在同一冻结候选完成；本会话尚未运行上述门禁。

当前基线门禁命令：`uv run ruff check`、`uv run python scripts/check_skills.py`、`uv run python scripts/check_env_example.py`、`uv run --extra mcp pytest`、`uv build`、`uv run python scripts/check_mcp_installations.py --output <临时报告路径>`、`npm run typecheck`、`npm run test:browser`；依赖准备遵循 CI。实施前重读当前配置，不把此处命令清单当作已运行日志。

IC-01–IC-03 的继承要求已定；这里的具体接缝与 GraphResult 映射已由 D3 整体确认。内部模块/类名、冻结容器表示、零 Step 工具的无碰撞持久身份、注入参数的机械设计交实施者决定，不能偷偷扣模型 Step 或伪造事件 step_index。

### 现有测试先例

- [test_loop.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/7f38e8b6abe77022315584e0fd9d098d9219c470/src/agent_alfred/evals/deterministic/test_loop.py)：ScriptedModel 驱动真实 Assistant，捕获完整模型请求与公共事件。
- [test_budget.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/7f38e8b6abe77022315584e0fd9d098d9219c470/src/agent_alfred/evals/deterministic/test_budget.py)：共享 lease 消耗与耗尽不重置。
- [test_tool_registry.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/7f38e8b6abe77022315584e0fd9d098d9219c470/src/agent_alfred/evals/deterministic/test_tool_registry.py)：声明冻结、执行校验、授权、真实事务账与事件所有权。
- [test_ops_metering.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/7f38e8b6abe77022315584e0fd9d098d9219c470/src/agent_alfred/evals/deterministic/test_ops_metering.py)：真实工具请求和持久计量，区分 not_started 与真实开始；借用夹具，不扩大本票 Host 选图范围。
- [test_mcp.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/7f38e8b6abe77022315584e0fd9d098d9219c470/src/agent_alfred/evals/deterministic/test_mcp.py)：deadline 后停止批次及迟到成功不得恢复旧 Run。
- [test_interrupted_attempt_accounting.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/7f38e8b6abe77022315584e0fd9d098d9219c470/src/agent_alfred/evals/deterministic/test_interrupted_attempt_accounting.py)：中断路径保留真实 Attempt 与费用。
- [test_attempt_outcomes.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/7f38e8b6abe77022315584e0fd9d098d9219c470/src/agent_alfred/evals/deterministic/test_attempt_outcomes.py)：Attempt outcome 与 wire 回归。

## Critical Counterexamples

以下是唯一权威验收清单；它们描述合同，不是已观察到的 Graph 缺陷或已通过的测试。

### CE-01 被接管失败的同波产出

- **Basis:** #6 §3/§5/§6/§11；Q1。
- **Sequence:** A、B 同波；A 返回 a，B 的普通执行失败，有 error 边到下一波 R；分别再测无 error 边、整波写集校验失败；path 异常/未知标签由 CE-10 覆盖。
- **Expected behavior:** 按 Q1 / D1，已接管时只提交 A 的有效写入，B 的候选永不提交，R 只读已提交状态；未接管/整波校验失败则全波撤销，无 node.finished，已 started 节点发 node.aborted；不重跑 A。
- **Verification:** compile/invoke、事件序列、R 的声明视图和 GraphResult；真实引擎，故障 callable 只作为玩具节点输入；path 抛出及未知标签按 Q4 不恢复，由 CE-10 验证。
- **Decision status:** confirmed（Q1 / D1）；路由失败与短路时机分别依 Q4/Q7。

### CE-02 恢复后的无动作

- **Basis:** #6 §6–§8；Q2。
- **Sequence:** B 失败，经显式 error 边执行 R，R 是固定 reason_code 的无动作终点；关闭可选事件 sink 重跑。
- **Expected behavior:** 按 Q2 / D1，NoAction 有原 reason_code 和非空 recoveries，无 output；正常无动作 recoveries 为空，不伪造助手空回复。
- **Verification:** invoke 返回值及字段缺席；事件 sink 缺席不丢恢复事实；真实调度。
- **Decision status:** confirmed（Q2 / D1）。

### CE-03 源节点候选值驱动路由

- **Basis:** #6 §3–§5；Q3。
- **Sequence:** C、D 同波分别写 category、other；C 的 path required 读取 category 决定分支，并尝试越权读取 other；另测 C 实际漏写 category。
- **Expected behavior:** 按 Q3 / D1，合法 path 可读 C 候选 category，不能读 D 的候选或任何未声明键；required 缺席在 path 调用前失败；失败不发布候选成功路由。
- **Verification:** compile/invoke、路径调用参数、下游实际调用与事件；真实引擎，无私有调度替身。
- **Decision status:** confirmed（Q3 / D1）；路由失败后的调度依 Q4。

### CE-04 编译校验与路径来源

- **Basis:** #25 全部编译要求；#6 §4/§9。
- **Sequence:** 分别构造非法/重复/保留 node_id、输入与节点重复 writer、不存在或并非全部可达路径有来源的 required、无任何来源的 optional、多条件组、不闭合映射、错误节点再挂 error、终点输出归属错误、非法 reason_code、非法/重复工具白名单、环；另造 skippable writer 支配下游与绕行两图。
- **Expected behavior:** 每条非法图 compile 失败且无模型/工具 IO；支配 taken 路径的 required 合法，存在绕行且缺来源的 required 非法。运行输入缺席仍须在使用前被阻止。
- **Verification:** 公共 builder.compile 与启动组装/Registry，逐项独立非法夹具；真实工具声明目录。
- **Decision status:** confirmed by existing #6/#25；精确错误类别由实施整理，不缩减项目清单。

### CE-05 快照、缺席与嵌套修改

- **Basis:** #6 §2–§4，ADR-0010。
- **Sequence:** 同波 A/B 先后执行，A 尝试改输入嵌套值并返回候选；B 读声明的快照；分别输入缺席和显式 None；另测成功 writer 漏写后 required 消费者。
- **Expected behavior:** A 不能污染 B 或调用方输入；B 不看 A 同波候选；缺席与 None 可区分；漏写不补 None，required 消费者调用前失败。
- **Verification:** invoke 的外部输入、节点捕获视图、结果与调用证据；深层容器夹具，真实引擎。
- **Decision status:** confirmed by existing #6/#25；普通节点与路由缺席分别依 Q1/Q4，禁止把缺席补成 None。

### CE-06 波撤销不退 Step 与实耗

- **Basis:** #25 波回滚验收；ADR-0012。
- **Sequence:** 三节点同波，前两节点完成模型调用，第三个返回越权/冲突写集；之后以同一 RunBudget 尝试后续允许的执行。
- **Expected behavior:** 全波业务写入及转录候选不生效；前两节点无 node.finished、有 node.aborted；租约仍扣除，Step 标 aborted_by_wave，原 Attempt outcome、Token 与费用保留；无额度时不发请求。
- **Verification:** invoke → 真实预算/模型执行/遥测，ScriptedModel 捕获请求、固定时钟；不能用非法静态图代替运行时纵深校验夹具。
- **Decision status:** confirmed by existing #25；实际构造 fixture 与机械错误类型交实施者。

### CE-07 工具能力及副作用账

- **Basis:** #6 §13–§14；ADR-0011。
- **Sequence:** agent_node 白名单外同时放入 local_read、local_write、external 和未配置工具；模型强行输出白名单外调用；另跑白名单空集、合法 effectful 调用及纯函数图。
- **Expected behavior:** 白名单外定义及引导 Schema 不暴露、强行调用不执行；空集不发工具定义；合法 effectful 调用按 call_id 恰一条业务账；纯函数前后受保护 DB/文件不变。
- **Verification:** 真实 Registry/Assistant/SQLite/文件、ScriptedModel 请求和调用结果；不把策略授权接口伪作完整白名单；不声称 Python 沙箱隔离。
- **Decision status:** confirmed by existing #6/#25。

### CE-08 Run 级固定收尾压过图分支

- **Basis:** #19 工具实现合同、#21 MCP 规范、ADR-0032。
- **Sequence:** 图内工具确认删除，或文件/MCP 结果未核验，或计量落定失败；该图尚有普通/error 后继模型和工具节点；再注入迟到外部结果。
- **Expected behavior:** 各自原有固定收尾保留，零后续模型/工具调用，不被图恢复或 NoAction 掩盖；迟到结果不重启旧 Run、不重复业务账。
- **Verification:** invoke 内真实 tool_node/agent_node → Registry/业务服务/SQLite；外部 MCP 采用受控子进程或其传输故障边界；以请求和持久事实证明停止。
- **Decision status:** confirmed by existing 上游合同；停止行为已定；本文件“执行适配与验收范围整合”中的 GraphResult 映射已由 D3 确认，不代表已验收。

### CE-09 确定性、终点与哈希

- **Basis:** #6 §1/§5/§8/§10；#25。
- **Sequence:** 同图同输入、固定时钟/ID和模型脚本跑两次；构造条件 fan-out/fan-in 的单路径/全跳过；分别成功到零/一/多终点；分别改变展示和结构字段。
- **Expected behavior:** 同层声明序稳定且事件逐条相同；全部入边决议后按 taken 规则执行/跳过；恰一个成功终点才可能成功；展示变化 hash 不变，规定结构字段变化 hash 改变，缓存 describe 无 IO，比较用 schema_version 与完整摘要。
- **Verification:** compile/invoke/describe/GraphRegistry、真实事件序列；可注入确定性时钟/ID而不删掉关键事件后再比较。
- **Decision status:** confirmed by existing #6/#25；节点声明顺序为有序结构，必须保序计入 hash，重排声明改变摘要。

### CE-10 路由失败不能发布半份控制流

- **Basis:** #6 §3/§5/§11，Q4。
- **Sequence:** A/B 同波，B 的 path 抛异常、未知标签、缺 required 三种；B 有 error 目标 R；A 已有候选写入。
- **Expected behavior:** 按 Q4，全波写入和候选路由均撤销，所有已 started 节点 aborted，无 node.finished；R 零调用；invoke 返回 Failed，之前波的 committed_state 保留，Step 和实耗不退。
- **Verification:** 真实 builder/compile/invoke、CapturingSink/RunBudget/ScriptedModel；path 故障可编程控制，不修改私有调度状态。
- **Decision status:** confirmed（Q4 / D2）。

### CE-11 error 目标不争抢来源

- **Basis:** #6 §6 的 NodeContext.error、节点最多访问一次，Q5。
- **Sequence:** 编译共享 error 目标、混合入边、可跳过恢复节点、多条源 error 出边及链式恢复的非法图；合法图 A/B 各失败，各有 R_A/R_B，再汇合。
- **Expected behavior:** 按 Q5，非法图启动拒绝；合法图两条 error 来源与恢复记录可区分，各恢复节点最多执行一次且不会无 error 提前执行；汇合遵循全部入边决议后的 OR 规则。
- **Verification:** 公共 compile/invoke，真实引擎与 error 上下文捕获，不靠私有字段检查。
- **Decision status:** confirmed（Q5 / D2）；汇合后终点仍受恰一成功终点约束。

### CE-12 终点出边与失败终点

- **Basis:** #6 §6/§8/§9，Q6。
- **Sequence:** 编译终点带普通/条件出边的图，及空图/无终点图；合法 result 终点抛普通异常或漏写 output_key，走 error 恢复；对照显式写下 None；另跑多终点可达图。
- **Expected behavior:** 按 Q6，非法静态图 compile 拒绝；失败或 output_key 缺席不能伪造 Completed(None)，显式 None 则是合法值；失败终点不计成功终点，成功终点不启动正常后继；其他分支结算后成功终点数不为一即 Failed。
- **Verification:** 公共 builder/compile/invoke、输出字段与实际工具计数；真实调度与临时工具账本。
- **Decision status:** confirmed（Q6 / D2）；漏写 result 输出按普通节点合同失败、允许其合法 error 接管，禁止新加空值兜底。

### CE-13 不可恢复失败的未启动后项

- **Basis:** 确定性同步执行、真实副作用与事件事实；Q7。
- **Sequence:** A/B/C 同波，B 未接管异常发生在 C 开始前；对照 C 已执行后波末写集校验失败；另测 B 可被接管以及预算耗尽。
- **Expected behavior:** 按 Q7，前者 C 零调用且无伪造 started/finished/skipped 和账，已 started 节点 aborted；后者承认 C 的调用及实耗；可接管错误继续，零 Step 恢复成功保留恢复结果，未恢复耗尽才 BudgetExhausted，普通失败才 Failed。
- **Verification:** invoke → 真实预算/Registry/临时 SQLite/ScriptedModel，请求和持久证据与未执行清单一致；节点顺序控制故障，不依赖 sleep。
- **Decision status:** confirmed（Q7 / D2）；已定预算不回补和恢复结局沿用 #6 §12。

### CE-14 输入声明、冻结与注册表

- **Basis:** #25 builder/compile/declare_input/describe/GraphRegistry；R01/R03/R07/R10。
- **Sequence:** 编译图后再改原始节点声明、reads/writes 容器、path_map 或 presentation；调用者输入缺 required 或试图提供一个由节点拥有的 key；注册/查找合法图与未知 graph_id；多次 invoke 使用独立输入。
- **Expected behavior:** 已编译执行/description/hash 不随原可变定义漂移；输入不得绕过声明 writer 覆盖节点所有权，required 输入缺席在业务节点启动前返回可诊断 Failed，零模型/工具 IO；输入 optional 缺席合法；未知 registry 名明确失败；一次 invoke 的状态/错误/恢复不泄漏到下一次，预算由调用方传入的同一 Run 身份决定、不由引擎重置。
- **Verification:** 公共 declare_input/compile/invoke/describe/Registry，真实冻结产物、预算和模型请求捕获；通过修改原公开输入触发，不修改私有编译结构。
- **Decision status:** confirmed（既有约束 + D3 整合验收）；精确输入拒绝与证据要求已确认，无已运行结果。

### CE-15 四工厂的共享预算和节点归属

- **Basis:** #6 §11–§15、ADR-0011/0012；R02/R08/R09。
- **Sequence:** 依次用 llm_node、包含两次 Step 的 agent_node、fn_node、tool_node；受控模型让一个 Step 重试；耗尽后尝试新模型请求并以零 Step 节点恢复；不同 tool_node 发出调用；另测普通回退共用已消耗 budget。
- **Expected behavior:** 请求前取 lease，重试沿原 Step，不够额度零请求；fn/tool 事件 step_index=None 且不扣模型预算；attempt/block/tool 事件都归属实际图 node_id，无串到 LOOP_NODE_ID 或 None；每次工具调用身份可区分且账唯一；最后一个合法 Step 成功不会被 remaining=0 改成失败；恢复成功保留恢复结局，后续调用方不能通过新预算伪造自动回退额度。
- **Verification:** invoke → 真实 Assistant/attempt IO/Registry/RunBudget/计量 → ScriptedModel/真实 SQLite；捕获完整信封、请求与共享预算；工具身份通过公共账目读取断言，不以私有实现结构为验收。
- **Decision status:** confirmed（既有 #6/#25 + D3）；预算、事件合同及接缝已确认。

### CE-16 有效回复、波撤销与唯一 finalizer

- **Basis:** #6 §12/§15；#19/#21 固定收尾；IC-01–IC-03。
- **Sequence:** 图内分类/agent 产出候选后波撤销；以同一 Run 预算执行允许的后续普通循环；另跑有最终回复的成功/恢复图，以及非终点删除成功、文件/MCP 结果未核验、计量失败强制收尾；通过真实记录器落库。
- **Expected behavior:** 作废图输出不进入后续有效模型请求或 agent_log；原始 Attempt/费用仍进总账；节点不写 agent_log，最终只由 recorder 保存唯一一组用户消息与实际交付回复。删除强制收尾 Graph 未完成但 Run completed 且固定无正文回执保留；其余固定失败不被包装成成功；NoAction 没有助手空回复，不伪造会话内容。原始 trace 可保存作废事实，不承诺抹除真实历史。
- **Verification:** Graph.invoke/执行适配 → 真实 Assistant/Registry/RunRecorder.settle/SQLite；ScriptedModel 捕获后续请求；读取正式消息、Run outcome、遥测与工具账。不是 Host 用户选图/UI 的验收替身。
- **Decision status:** confirmed（既有约束 + D3）；收尾映射和接缝已确认，相关运行验收全部 NOT RUN。

## Readiness and Open Decisions

- **规范版本：GRAPH-SPEC-r1。设计与接缝：CONFIRMED（D1–D3）。就绪性：ready-for-agent。** R01–R10、Q1–Q7、IC-01–IC-03、CE-01–CE-16 均有来源与预期，没有必需行为延期、未决取舍或验收合同阻塞。
- 本次为等义综合，没有删除、替换或豁免既有要求/CE。内部实现选择不重新访谈；如发现真实不可满足的契约矛盾，提交证据与最小取舍，不能自行删减范围。
- 就绪性不等于发布状态或实现授权。发布状态由同一 LOOP 与发布回读证据单独记录；标签只在获准发布时设置。设计/规范检查不是产品验收，运行时 CE/门禁全部 NOT RUN，双轴 NOT REVIEWED。

## Out of Scope

以下沿原票及 D3，不能计为通过或悄悄纳入本票：

- 两个业务 workflow（消息分流、手动聚合）、Behaviour/Graph 页面、用户选图入口和完整 Host 选图产品路径；后续消费者票负责。允许窄执行适配与真实 finalizer 验证，不要求加业务开关。
- 并行线程图执行、回边/max_visits、模型直接指定任意下一节点、运行时配置悄悄改变已编译拓扑。
- 绝对 Python 沙箱隔离、直接 callable 工具绕过 Registry、由图作者布尔 fallback_safe 覆盖真实副作用账。
- 付费模型调用、私人外部服务或真实用户副作用作为必需验收。离线受控外部服务与真实临时文件/SQLite 仍在验收范围。
- 借规范整理自动实施、认领、提交、推送、合并或关闭 #25；这些后续动作按用户明确授权。

## Further Notes

### 追溯与唯一验收合同

- `docs/design/issue-25-graph-design.md` @ GRAPH-DESIGN-r4（SHA-256 `3104da6f2432c4fc9336c4ee8c8aa30e998ddb4861463d3de53538efa6a6c46c`）是冻结的设计/批准来源。本文保留其 R/Q/IC/CE 身份；从本规范起只维护本文的验收定义，设计源不并行演化。
- 原 ADR-0010 的“任一失败全波撤销”由 Q1/Q4/Q7 细分；路由视图由 Q3 明确；NoAction 由 Q2 增加恢复记录；Q5/Q6 明确 error 专属性及终点出边。支持性的本地 CONTEXT 与 ADR-0010 变更随实施接续，不以远端旧 ADR 原句反向覆盖已批准规范。
- 本规范全部关键验收内联，无依赖本机私有路径的验收 companion。源码和测试参考链接固定到已核验基线，只是接口先例，不是 Graph 验收通过证据。

### 实施交接

唯一状态入口为 `/Users/nineofour/Agent-Alfred-issue-25-design/tmp/agent-work/issue-25/LOOP.md`，本次规范整理完成时 revision 6；设计工作树 `/Users/nineofour/Agent-Alfred-issue-25-design`，分支 `codex/25-graph-design`，base/HEAD `7f38e8b6abe77022315584e0fd9d098d9219c470`。进入实施前先读该入口的最新 revision，核验本规范内容哈希、批准范围和远端发布事实。

实施者须把每项 R/CE 映射至可重现命令、测试入口、结果和同一候选身份。实际产品变更使用确认过归属的隔离工作树，保留主工作区用户改动；不得把规范 ready-for-agent 当成任务已验收或已获发布代码权限。当前实现负责人尚未指派，未创建执行会话。
