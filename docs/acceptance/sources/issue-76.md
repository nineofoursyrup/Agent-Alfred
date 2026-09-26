# #76 实现：消息分流路由统计的完整用户路径

来源：https://github.com/nineofoursyrup/Agent-Alfred/issues/76

<!-- routing-stats-spec:r1 -->
# ROUTING-STATS-SPEC-r1

Part of [地图 #1](https://github.com/nineofoursyrup/Agent-Alfred/issues/1)；设计来源：[决定 #74](https://github.com/nineofoursyrup/Agent-Alfred/issues/74)。

规范就绪：**READY FOR IMPLEMENTATION**。完整设计与必需验收接缝已确认，无未决验收阻塞；发布/标签/认领/实现状态独立记录于同一LOOP及远端回读。就绪不表示实现、测试或交付完成。

## Problem Statement

用户已经能开启消息分流，却无法可靠回答：一段时间内选择过哪些分支、图失败后多常转入普通循环、上下文恢复出现多少次。把分类标签、图内fallback、图后回退和图前绕行混在一起，或把历史缺失当作未发生，会让看似精确的百分比产生误导。

用户需要跨CLI/Web聊天Run的持久统计，能够识别样本、时间、版本和证据覆盖边界，并在查询失败、重启或trace裁剪后仍看见真实可证明的事实。

## Solution

在Behaviour的消息分流区提供独立、默认收起的“路由统计”。用户选择24小时、7天、30天或全部并主动刷新；页面以计数和百分比表分别呈现五类路由决议、图后故障回退、上下文恢复、图前绕行及被阻止的回退原因。

结果使用SQLite中的Run准入与收尾事实；每项比例显示自己的已知分母、覆盖和未知。语义版本分别统计，未知版本历史只给可信次数与缺失。一次读取是一份一致快照，2秒查询预算内成功就覆盖完整样本，超限明确失败并释放资源。观察不改变既有业务、设置、费用、会话或副作用规则。

## User Stories

1. **R-01** — As a Behaviour用户, I want 在消息分流区找到独立路由统计并看清计数与比例, so that 不用新增页面即可了解分流情况。
2. **R-02** — As a CLI/Web用户, I want 查看跨会话、已准入聊天Run在明确时间范围内的统计, so that 比较一份边界清楚且不混入聚合或探针的样本。
3. **R-03** — As a 分流使用者, I want 分开查看路由决议、故障回退、上下文恢复与被阻止原因, so that 不把图内兜底误认为故障回退，也不把决议误认为成功。
4. **R-04** — As a 查看历史的用户, I want 在重启和trace裁剪后仍读取已记录事实并看见缺失, so that 获得可追溯而不靠猜补的历史计数。
5. **R-05** — As a 验收者, I want 从真实公开用户入口核对持久统计和界面, so that 用可证伪的执行证据判断交付是否满足合同。
6. **R-06** — As a 统计阅读者, I want 看到每个指标的分子、已知分母、覆盖与未知数量, so that 不被失败排除或缺失当零误导。
7. **R-07** — As a 分流使用者, I want 区分图前绕行与图后故障回退，并识别配置状态未知, so that 了解普通循环被采用的真实原因。
8. **R-08** — As a 查看跨版本数据的用户, I want 按统计口径和路由策略版本分别查看结果, so that 不把不可比的历史合成一个百分比。
9. **R-09** — As a 经历中断或保存失败的用户, I want 保留已提交准入资格和实际落库的结果摘要, so that 让统计准确区分已有事实与无法证明的结果。
10. **R-10** — As a Behaviour用户, I want 主动刷新有身份的一致快照并清楚识别失败与旧数据, so that 不会被乱序响应、混合时间范围或伪零误导。
11. **R-11** — As a 查看旧数据的用户, I want 保留可信计数并显式看到未知、坏字段与不可判定的查询, so that 不会把旧默认字段误读为未发生。
12. **R-12** — As a 使用本地助手的用户, I want 在有限查询预算内取得完整精确结果或明确失败, so that 统计不会无限占用资源或把截断样本冒充全部。

## Implementation Decisions

### 来源、批准与单一权威

- 上游为[#71 Resolution r1](https://github.com/nineofoursyrup/Agent-Alfred/issues/71#issuecomment-5742523972)的S05，保持#26消息分流及#27聚合已交付合同；不重开业务范围。
- 用户三轮“全按建议”批准Q1–Q5（D1）、Q6–Q10（D2）、Q11–Q12（D3），随后以“确认”批准完整ROUTING-STATS-DESIGN-r4及其全部验收库存（本规范记为D4）。
- 源设计SHA256：`59d050c3fe9a27cbd0131b87410815451f935b7b4801debd62677608826e3285`。批准记录APPROVAL-r4.md明确取代源正文保留的“待整体确认”历史状态；该状态文字不构成新的待决项。
- 本规范成为单一实施与验收合同，全文内联R-01–12、AC-01–24、CE-01–16、P1–P5、公式、旧数据判读及排除项；无需读取本机companion才能知道必需结果。设计稿和批准材料只作来源历史，不并行维护另一份验收。
- 以下保留源设计的第2–9/11节编号，使AC/CE中“第7节”等引用仍指向同一内联内容。每项AC→R→Q/D的来源由需求表和CE Basis给出，无删除、替换或降级必需案例。
- #72决策现已关闭并由#75承接页面组织/只读拓扑；本票入口与其兼容，但不以#75产品实现、#73单次路径或#70导出为前置，不改动其规范或认领。

### 模块与接口责任

| 边界 | 实施责任 | 必须保持 |
|---|---|---|
| RuntimeHost / 准入与handoff | 捕获并在准入事务保存统计资格/版本；只认admitted | 默认关闭、忙碌拒绝、下一Run生效和两阶段准入语义 |
| 消息分流 / Graph执行适配层 | 保留已提交决议、恢复及实际图前/图后进入证据，区分unknown | Graph/Registry同代、共享预算、取消与副作用禁止重跑 |
| RunRecorder / SQLite与迁移 | 与唯一收尾事务保存统计摘要；恢复后不补造结果 | recorded与业务outcome分轴、冻结迁移不原地改写 |
| 统计读取服务 / HTTP | 一致快照、严格资格/版本/字段判读、全量精确聚合、2秒可中止预算 | 只读、释放资源、错误与零数据分开；不按列表分页取样 |
| Behaviour界面 | 独立面板、时间选择、各轴计数与覆盖、主动刷新及迟到隔离 | 表单/聚合请求状态不变、键盘窄屏可读、无SSE临时累计 |

具体模块拆分、数据形状和API路由名称可按项目惯例决定；公开语义、时机、分母、证据边界和必需验收不能改。以下各节是已批准合同的等义综合，不新增流程编辑、业务策略或后台服务。

### 2. 已确认决定与需求库存

| 要求 | 决定与可观察合同 | 批准 | 验收 |
|---|---|---|---|
| R-01 | Behaviour 消息分流区有独立默认收起的路由统计，计数/百分比表；不增顶级页、拓扑叠加或导出 | Q1/D1 | AC-01、AC-02 |
| R-02 | 跨会话 CLI/Web 已准入 chat；24h/7d/30d/全部，默认7d，按 accepted_at 归窗 | Q2/D1、Q6/D2 | AC-03、AC-04 |
| R-03 | 五类路由决议、图后故障回退、上下文恢复分轴；被阻止的回退另计原因 | Q3/D1 | AC-05、AC-06 |
| R-04 | SQLite 持久摘要为事实源，重启与 trace 裁剪不改写已记录事实；历史不猜补 | Q4/D1 | AC-07、AC-08 |
| R-05 | 真正公开入口的整合验收，替换仅限模型/外部服务及明确的故障控制 | Q5/D1 | AC-09、AC-10 |
| R-06 | 每指标以其已知样本为分母，未知/覆盖显式；终态失败/取消不自动排除 | Q6/D2 | AC-11、AC-12 |
| R-07 | 图前绕行与图后故障回退分开；配置错误的 false 不当用户关闭 | Q7/D2 | AC-13、AC-14 |
| R-08 | 按统计口径与路由策略版本分组；跨重启可合并，图代际/hash不代替行为版本 | Q8/D2 | AC-15、AC-16 |
| R-09 | 准入资格与准入事务绑定，摘要与收尾绑定；不增加逐节点同步写，不恢复业务执行 | Q9/D2 | AC-17、AC-18 |
| R-10 | 主动读取一致快照；滚动UTC窗口；迟到响应隔离；查询失败与零数据分开 | Q10/D2 | AC-19、AC-20 |
| R-11 | 未知版本历史只给可证次数/缺失；坏指标局部未知，样本归属不可判定则整体失败 | Q11/D3 | AC-21、AC-22 |
| R-12 | 两秒预算内精确全量查询；超限整体失败、资源释放，不截样、不建后台分析服务 | Q12/D3 | AC-23、AC-24 |

### 3. 用户路径与表现

1. 进入既有 Behaviour 页，在“消息分流”区展开“路由统计”。入口与“查看流程”独立：不依赖图先加载，也不改变分流开关、模型、表单或正在运行的任务。
2. 默认最近7天，可选24小时、7天、30天、全部；展示服务器确定的实际起止时刻、时区和快照读取时间。页面显示可用本地时区，但判窗统一UTC；7天是7×24小时，30天是30×24小时，不是自然日。
3. 先显示样本说明和启用/关闭/启用未知数量、已持久终态/尚未记录终态数量。只有已启用且有持久终态的样本进入主结果统计。正在执行或记录未落定不作为0回退。持久统计无需引入实时内存计数；同进程记录失败的既有提示可以并列，但不能混入持久结果。
4. 每个可识别的版本组显示五类路由决议表、已知决议覆盖、明确未形成决议、未知决议；故障回退和上下文恢复各显示发生数/已知数及覆盖缺口；图前绕行和回退被阻止以次数、原因展示，不新增未经批准的混合总回退率。
5. 当前版本组优先，其他已知版本分别显示；“历史／版本未知”仅显示可证明的事实次数与缺失数，不输出百分比。说明组内可能包含不同模型、工具和参数，不称分类准确率、成功率、费用节省或因果效果。
6. 首次展开、重新展开、切换范围、主动刷新各读取新快照；不轮询、不累加SSE。页内查看不清空业务表单或更改保存/运行状态；重载恢复默认7天和收起状态，不新增查看偏好持久化。
7. 零Run、无已启用Run、有Run但没有可计算样本是不同提示；分母0显示“无可计算样本”，不显示0%。0次/正分母可以显示0%。次数与分数始终完整，百分比仅是显示投影，不用四舍五入值反算次数。
8. 读取失败、数据归属不可判定、超时分别解释并可重试；旧快照若保留必须标旧、原范围和失败原因。切范围时不得用旧区间数据冒充新结果。页面不提供新增逐Run明细查询、导出或分析筛选；既有运行页入口保持，统计正确性由公开API与真实记录验收。

### 4. 样本、公式与分区不变量

#### 4.1 读取边界

一次响应捕获一个服务端绝对时刻 as_of，以及同一持久读快照。窗口是 [from, as_of)：起点包含、终点不包含。24h/7d/30d 的 from 由 as_of 减准确时长；全部无下界，但仍排除 accepted_at>=as_of 的记录。时钟必须可在测试中控制。as_of 不是声称所有被包含Run都在该时刻完成；它是按准入时刻筛选的窗口上界，结果由该次读快照决定。

A：窗口内 purpose='chat'、gateway为CLI/Web且 admission_state='admitted' 的不同Run集合。一次Run仅由 run_id 计一次；模型重试/Attempt、多个恢复事件、SSE重放、重复刷新不增加Run数。旧列表filter=chat包含aggregation，不能直接复用为本口径。

明确pending/rejected准入、聚合、探针/其他purpose不入A；不能因为数据库已有accepted行就当完成准入。无法判定资格的关键持久数据损坏不能静默排除。设置状态可判定为enabled、disabled、unknown：unknown是合法已定义分类，不是查询失败。

A=E+O+U：E为配置有效且可证明开启，O为有效且可证明关闭，U为无法判定开启/关闭。配置错误的enabled=false占位属于U。此资格取Run准入快照，不取当前开关。

N：E中持久phase=finished的Run；P=E-N为尚无持久终态者。恢复后finished/interrupted也属于N，但缺失的结果指标为unknown，不反推旧内存结局。并行执行/收尾变化在下一次快照可见，不将两个快照的分子分母混用。

每个Run只属于一个版本组。所有以下结果公式在同一可识别版本组的N内计算，不跨策略/口径拼分母；外层计数可以相加但不得产出混合比例。

#### 4.2 路由决议

每个N中的Run恰属：五类决议之一 / 可证明未形成决议 L / 无法判定 U_d。

D=n_quick+n_full+n_fallback+n_no_action+n_context_failure；N=D+L+U_d。
分支比例=n_route/D；决议覆盖=D/N。D=0或N=0时相应比例为空。

决议须是实际已提交的route_decision；classification.category、终态文字、当前图结构均不能替代。错误、取消、后续失败不会撤销已经提交并被持久保存的决议；反之，未提交波的候选输出不能计决议。L只由可信执行摘要明确证明未形成决议，缺route不等于L。

#### 4.3 回退、绕行、恢复

F表示图开始执行后失败，实际进入普通循环；B表示图未开始且因配置/图不可用实际进入普通循环；R表示本Run的project_context→recover_context恢复节点已经成功提交；只选中error边或启动恢复节点均不足。三者各是 yes/no/unknown 的证据状态，不以缺字段或默认0作为no。

对F、R分别定义Y_m=证实yes，Z_m=证实no，U_m=unknown；N=Y_m+Z_m+U_m；K_m=Y_m+Z_m；率=Y_m/K_m，覆盖=K_m/N。分母0为空。R按Run计，单Run多条恢复证据最多加1。

B按发生Run数计，并展示未知数；不另新增一个未经批准的绕行百分比。F与B的yes在同一Run互斥；图内route=fallback可以与F=yes共存，R可与no_action共存。允许回退不等于进入；进入但尚未发模型请求依然算实际进入，model_requests=0不能把它抹掉。

被阻止的回退须有明确blocked及原因，按Run计并按其已记录原因展示；不因F=no就自动判blocked。不改写原业务的副作用、预算、取消和截止规则。若来源或字段矛盾，相关轴unknown，不能挑选一个更方便的真值。

U中的已知图前绕行次数另列“启用状态未知”，不进E的率。关闭样本O不参与启用主统计，但可显示总量。

### 5. 持久证据、版本与历史判读

#### 5.1 最小证据合同

准入：随现有准入事务保存统计所需的可判定设置状态、启用资格、统计口径版本和路由策略版本；只有既有handoff确认admitted才纳样。不能提前写统计成功或绕过原准入拒绝行为。版本与设置从该Run捕获的执行配置取，不在结束时改取最新配置。

执行及收尾：在内存中保留实际发生且已提交的决议、图是否进入、图前绕行/图后回退是否实际进入、上下文恢复证据、blocked原因及各轴证据完整性；在唯一Run收尾事务与现有结果一起持久化。实现可以复用并扩展既有telemetry，不强制新建一套账；但不能继续依靠模糊缺字段或默认值表达未发生。

不要求逐节点持久化或崩溃后恢复完整路径。正常故障/取消必须保留能够证明的已提交事实；若无法完整证明，相关轴unknown。进程突然退出、收尾失败后重启不能补造终态摘要，准入资格仍保留；NoAction仍无助手空消息。统计观察不改变回答、工具、费用、记忆、会话与既有副作用边界。

新准入字段写失败按现有准入失败；收尾失败沿既有recording_failed及协调器处理；统计不要求业务另外重试、自动重跑或静默成功。新增持久数据通过新迁移或既有版本化扩展落地，不原地修改冻结迁移。查询不回写历史、扫描trace补数或读取提示词/正文来推断路由。

#### 5.2 版本

统计口径版本识别样本资格/指标含义/分母规则；路由策略版本识别分类提示词、允许类别和路由/保护规则。字段schema_version只识别结构。当前实现须发布明确稳定的两种语义身份并保存到每个新Run；字段名可由实现按项目惯例确定，不能省略语义身份。

仅展示文案、普通参数、模型指派、工具集合变更不自动改语义版本；影响上述语义的变化必须改对应版本。topology_hash和进程内generation作为既有诊断事实保留其原义，不用于替代版本，不跨进程排序。统计接口读取本次服务端当前版本身份，不拿前端硬编码版本宣称某组是当前。

历史无可靠版本或未来不支持版本归unknown版本组，仅次数和缺失，无比例，不补写当前版本。能识别的不同旧版本按其已定义合同分别统计，不以当前解析规则强读。未知格式中的字段不作事实证据。

#### 5.3 Legacy schema1 的保守判读表

本表仅适用于合法、已记录且来源/结构可识别的既有memory.routing摘要；物理存在JSON不等于证据完整。各轴分别判读，矛盾影响相关轴；版本仍未知时即使事实可信，也只给次数。

| 证据 | 可证明事实 | 禁止推断 |
|---|---|---|
| settings.status=ok且enabled为合法bool | 开启/关闭资格 | 错误配置中的false不能证明关闭 |
| settings缺失/无效 | 资格unknown | 不能推断关闭 |
| 合法route且已记录的已知合同表明它来自committed_state，相关字段无矛盾 | 对应决议 | 不代表分支成功或模型判断正确 |
| 缺route或只有classification | 决议unknown；仅额外显式证据能判L | category不能补route |
| 有效上下文恢复记录 | R=yes | 无关图的恢复不算本workflow的上下文恢复 |
| 正常Completed/NoAction、完整合法摘要且显式recoveries=[] | R=no | 不能用缺数组代替空数组 |
| Failed/BudgetExhausted且recoveries=[]；缺recoveries；CWR无有效恢复证据 | R=unknown | 默认空数组不能证明未恢复 |
| 合法fallback.entered=true且可证明routing_unavailable、未开始图 | B=yes、F=no | 不称图执行失败 |
| entered=true且明确graph_failed、对应图实际已执行 | F=yes、B=no | 不证明普通循环成功或已发模型请求 |
| entered=true但图前/图后来源无法判定 | 可记进入普通循环的已有事实，但F/B分别unknown，不增额外混合率 | 不能强选一种来源 |
| 完整已记录已知schema1摘要中entered=false，且无相反证据 | F=no、B=no | 不证明无图内fallback或无上下文恢复 |
| entered缺失/非bool、摘要不完整或格式未知 | F/B=unknown | 不补false |
| fallback.decision=not_needed或model_requests=0 | 单独没有发生/未发生证明力 | 不进入否分母 |
| graph_result=Failed但无其他执行证据 | 不能仅据此证明图已开始或引擎返回Failed | 该值可能是invoke前的默认值 |
| blocked及原因合法、明确未进入，字段彼此一致 | 被阻止次数及原因 | F=no本身不证明被阻止 |

其他未被版本合同许可的组合保守unknown。业务结果failed与某个已证实的决议/恢复不天然矛盾。恶意字符串、超出闭合类别或未知错误码不展示为任意HTML，也不被归到最相近合法分支。

指标摘要坏了只令受影响指标未知；关键样本身份/资格（purpose、gateway、admission_state、accepted_at）无法可信判定时整次查询失败。enablement=unknown、版本unknown是已定义分类，可继续计数。SQLite不可读/损坏属查询失败，不是零数据。

### 6. 一致性、错误与资源合同

- 一次读取的全部计数、版本身份、分子分母、覆盖与范围来自一个明确的一致视图；不得分别发多次请求拼装为一份“当前统计”。同快照内校验分区恒等式；实现使用锁读作用域或只读事务等方式可自行选择。
- API输出含范围、as_of、读取身份、进程身份、版本组、非负整数计数、分子/分母及可空比例、缺失原因摘要；不需要新增逐Run正文或输入披露。具体路由名/JSON字段由实现保持项目惯例，不能省去这些可观察语义。
- 浏览器维护范围与请求次序，关闭/离开后不由迟到结果复活面板；新进程/重载请求不被旧响应覆盖。失败保留的旧结果连同旧范围/身份整体保留并标旧，不能局部替换总数。
- 查询工作预算2秒，覆盖获取读资源、锁等待、SQL、legacy解析、分组及结果构造，按单调时钟计算；预算到期必须停止工作并释放资源，不能只返回超时而留后台查询继续占锁。它不承诺网络/浏览器端到端2秒，也不对操作系统调度提供硬实时保证。
- 成功一定精确覆盖所选全集，不将分页上限、最大扫描数或超时前已处理部分冒称全量。全部选项允许因预算超限失败并建议缩小范围，不自动把“全部”改为“最近N条”。不新增后台分析服务或统计缓存维护任务。
- 请求撤销/客户端离开能停止后续工作；没有可观察到断连时仍受同一2秒上限约束。到期/撤销处理不得取消正在执行的业务Run。数据库读取不可用与查询超时有不同机器可读原因；可复用503 recording_unavailable / 504 query_timeout惯例，具体无效数据码按项目惯例冻结于实现spec。
- 调用只读统计不经过业务执行，不连模型/MCP服务、不触发工具、不开新Run、不写设置，不无限阻塞Run记录。超时后资源释放可通过真实后续读取与业务操作证实。

## Testing Decisions

### 7. 固定计数验收样例

以下十个Run均属同一已知当前版本、CLI/Web已准入chat、有效启用且具有持久终态。失败/恢复由真实业务路径产生；i/j为在准入资格持久后、结果提交前终止进程并恢复所得。表内yes/no是充分证据结论，不是字段默认值。

| Run | 决议 | 图后回退F | 上下文恢复R | 图前绕行B | 说明 |
|---|---|---|---|---|---|
| a | quick | no | no | no | 正常快速回复 |
| b | full | no | no | no | 正常完整回答 |
| c | full | no | no | no | 工具本地写入后失败；blocked=side_effect_occurred |
| d | full | yes | no | no | 无副作用的图失败后进入普通循环 |
| e | fallback | no | no | no | 未知分类的图内兜底正常完成 |
| f | no_action | no | yes | no | 上下文恢复节点已提交，仍按要求不回复 |
| g | 未形成决议 | yes | no | no | 分类失败后合法进入普通循环 |
| h | 未形成决议 | no | no | yes | 有效启用但图构建不可用，进入普通循环 |
| i | unknown | unknown | unknown | unknown | 准入后崩溃，重启终态interrupted |
| j | unknown | unknown | unknown | unknown | 结果收尾提交前崩溃，重启终态interrupted |

必须得到：N=10；D=6，L=2，U_d=2；五分支次数quick=1/full=3/fallback=1/no_action=1/context_failure=0，full比例3/6=50%，决议覆盖6/10=60%。F发生2、已知8、未知2，率2/8=25%；R发生1、已知8、未知2，率1/8=12.5%；B发生1、未知2；blocked总数1且原因side_effect_occurred。F/R覆盖均8/10=80%。各分支比例之和仅在精确分数层面为1，显示舍入不改变分母。

再加入：k=启用但正在执行；l=有效关闭且已记录；m=配置错误、资格未知且已知图前绕行；以及n=拒绝准入、o=aggregation、p=probe。
必须得到：A=13，E=11，O=1，U=1；当前启用主结果仍N=10、P=1，前述各指标不变；U组另显示绕行次数1，不并入E的B；n/o/p不进A。再次查询/SSE重放/模型重试不增加任何Run计数。

### 8. 编号验收

- **AC-01 / R-01**：从已有导航到Behaviour可找到独立默认收起的路由统计；不以拓扑加载成功为前提，关闭分流仍可查看历史统计。
- **AC-02 / R-01**：计数/百分比表与解释可经鼠标、键盘在桌面和窄屏读取；不新增顶级页、导出、拓扑叠加、编辑或启动Run入口。查看期间业务表单与保存状态保持。
- **AC-03 / R-02**：实际CLI/Web chat跨会话纳样；每run_id只计1；明确pending/rejected准入、aggregation/probe等排除；不复用含aggregation的旧chat筛选语义。
- **AC-04 / R-02**：24h/7d/30d/全部及默认7d正确；UTC [from,as_of) 两端边界、等价时区表示、同秒多Run与滚动窗口移出均准确，全部没有分页截样。
- **AC-05 / R-03**：五类决议均可单独计数，classify输出不等于route，full失败仍留决议；route=fallback不误计图后回退；同Run full+F、no_action+R允许同时计数。
- **AC-06 / R-03**：回退allowed但未进入不计发生；进入且模型请求为0仍计发生；blocked次数/原因来自真实记录；R只在上下文恢复成功提交后按Run计1。
- **AC-07 / R-04**：固定窗口中已记录事实在页面重载、Host真实重启、真实trace裁剪/缺失后保持；trace写失败不等于统计丢失，统计查询不读取trace补数。
- **AC-08 / R-04**：历史未知资格/决议/结果可见；不补当前版本，不以没有routing判断关闭，不回写历史或重放业务。
- **AC-09 / R-05**：成功计数路径通过实际CLI/HTTP→Host→Graph→Recorder/SQLite→统计HTTP→browser，不只测试聚合函数；模型/外部服务按声明替换，真实组件保留。
- **AC-10 / R-05**：故障测试明确注入边界；不mock GraphResult/成品统计。精确旧格式、坏数据与响应乱序测试单列，不能取代真实新Run整合路径；确定性控制不靠sleep/重试掩盖失败。
- **AC-11 / R-06**：第7节全部数字、分区恒等式与非负整数校验通过；分母0返回空比例，unknown不作为no。不同轴不会共用不适合的分母。
- **AC-12 / R-06**：执行中/记录未落定单列；取消/失败的已持久决议纳入，崩溃后interrupted缺结果为unknown；重复读取/重放不重复计数。
- **AC-13 / R-07**：有效启用但图不可用的实际普通循环进入计B，不计F；图已运行后graph_failed回退计F，不计B；来源不明相关轴unknown。
- **AC-14 / R-07**：错误配置的enabled=false不记关闭；未知资格中可证B次数另列，不改变E的分母。记录的设置/图错误可以并存，不伪造互斥唯一原因。
- **AC-15 / R-08**：同语义版本跨重启合并；不同统计口径/策略版本分别统计；同generation/hash不能把不同策略合并，同策略不同模型/工具配置不自动拆桶。
- **AC-16 / R-08**：当前组身份来自服务端；历史未知版本只给次数/缺失且无任何比例；未知未来格式不按当前格式猜解释，当前零样本不回退成历史百分比。
- **AC-17 / R-09**：资格/版本与准入记录原子一致；准入失败、pending→rejected不执行业务/不入A；准入后再改设置不能改变旧Run资格。
- **AC-18 / R-09**：终态摘要同原收尾事务提交；失败/取消保留已提交事实；收尾写失败不伪报保存，重启按持久证据恢复；不添加逐节点同步写或自动重跑。
- **AC-19 / R-10**：所有计数及范围来自一个一致读视图；运行收尾/跨窗变化不导致分子分母混代；旧请求/旧进程/离开前请求不能覆盖新的有效视图。
- **AC-20 / R-10**：首次展开/重展开/切范围/刷新发起读取，静置不轮询；失败保留旧快照时显式标旧；零Run/无启用/无可算样本/读取失败有不同表现，重试可恢复。
- **AC-21 / R-11**：第5.3节所有legacy正反例有读取级验证；尤其Failed/BudgetExhausted空recoveries不计为否，默认Failed/not_needed/model_requests=0不补证据。
- **AC-22 / R-11**：单指标坏字段只降低相关证据覆盖并保留样本；关键样本归属损坏使整次请求失败，无静默跳行、伪零或部分成功。
- **AC-23 / R-12**：超过100条样本的成功统计覆盖全集；锁等待、SQL、legacy解析/分组耗尽2秒预算各能中止，失败没有部分统计。
- **AC-24 / R-12**：超时/取消/断连处理释放读事务、游标/连接及等待资源；后续真实读取/业务提交可继续；不取消业务Run，不产生无限后台统计。

### 9. 公共验收接缝与替换边界

- **P1 新Run整合**：真实CLI与Web submit、RuntimeHost/准入、Graph/GraphRegistry、ToolRegistry、分类与路由业务、共享预算、RunRecorder及临时真实SQLite。ScriptedModel仅替代外部模型输出/异常；外部服务可用可控本地服务；临时本地工具写入用真实Registry执行并检查次数。
- **P2 统计读侧**：真实宿主公开读取服务及HTTP路由→真实存储/查询/聚合→响应。新指标主样本先走P1生成；legacy/坏数据/多版本fixture允许通过真实SQLite装载明确格式与来源，用于读取合同，不能冒称生成路径通过。所有读侧测试都运行真实产品聚合器。
- **P3 浏览器**：真实build_dashboard/服务/SQLite，打开Behaviour、切范围、刷新、查看异常与键盘/窄屏表现。成功计数至少一套端到端真服务；网络代理/测试调度仅控制延迟、失败、乱序、断连及协议异常，不返回伪造成功计数来替代P2。
- **P4 生命周期**：真实进程启动/终止/重启、trace保留器、收尾事务及文件系统；可在公开/明确的故障接缝设置事件屏障、受控IO/SQL故障与注入时钟，准确说明替换位置。不得替换整个Recorder/GraphResult/聚合结果。
- **P5 预算与并发**：真实SQLite查询取消/progress handler或等效可中止执行，受控锁占用、查询工作时钟/故障回调、事务提交屏障。运行deadline由生产单调时钟驱动；测试可控制时间推进，观察真实资源释放和下一次请求，不用固定sleep制造顺序。
- 测试应分别提供命令、候选身份、输入、期望、HTTP/SQLite/页面证据。纯源码核对、离线数学习题与文档检查不等于产品验收；付费模型和私人外部账号不是默认验收前置。

### 现有测试与源码先例

- [src/agent_alfred/runtime/admission.py:391–408](https://github.com/nineofoursyrup/Agent-Alfred/blob/2533d6818c4b6a896f460b20d5e0fe40142d335e/src/agent_alfred/runtime/admission.py#L391-L408)：pending准入事务及绑定；必须与handoff admitted区分。
- [src/agent_alfred/runtime/recording.py:529–590](https://github.com/nineofoursyrup/Agent-Alfred/blob/2533d6818c4b6a896f460b20d5e0fe40142d335e/src/agent_alfred/runtime/recording.py#L529-L590)：Run收尾telemetry与持久事务。
- [src/agent_alfred/runtime/chat_graph.py:81–116](https://github.com/nineofoursyrup/Agent-Alfred/blob/2533d6818c4b6a896f460b20d5e0fe40142d335e/src/agent_alfred/runtime/chat_graph.py#L81-L116)：既有摘要及默认值来源；缺失不能补否。
- [src/agent_alfred/graph/types.py:86–99](https://github.com/nineofoursyrup/Agent-Alfred/blob/2533d6818c4b6a896f460b20d5e0fe40142d335e/src/agent_alfred/graph/types.py#L86-L99)：旧Failed/BudgetExhausted没有recoveries字段。
- [src/agent_alfred/graph/engine.py:256–273](https://github.com/nineofoursyrup/Agent-Alfred/blob/2533d6818c4b6a896f460b20d5e0fe40142d335e/src/agent_alfred/graph/engine.py#L256-L273)：恢复节点成功提交之后才形成recovery事实。
- [src/agent_alfred/runtime/execution.py:491–550](https://github.com/nineofoursyrup/Agent-Alfred/blob/2533d6818c4b6a896f460b20d5e0fe40142d335e/src/agent_alfred/runtime/execution.py#L491-L550)：routing_unavailable及实际进入普通循环。
- [src/agent_alfred/evals/deterministic/test_message_routing.py:112–153](https://github.com/nineofoursyrup/Agent-Alfred/blob/2533d6818c4b6a896f460b20d5e0fe40142d335e/src/agent_alfred/evals/deterministic/test_message_routing.py#L112-L153)：真实上下文恢复与NoAction交叉。
- [src/agent_alfred/evals/deterministic/test_message_routing.py:527–579](https://github.com/nineofoursyrup/Agent-Alfred/blob/2533d6818c4b6a896f460b20d5e0fe40142d335e/src/agent_alfred/evals/deterministic/test_message_routing.py#L527-L579)：trace写失败/取消后的持久事实。
- [tests/browser/routing_server.py:64–97](https://github.com/nineofoursyrup/Agent-Alfred/blob/2533d6818c4b6a896f460b20d5e0fe40142d335e/tests/browser/routing_server.py#L64-L97)：真实服务、ScriptedModelFactory、收尾故障与重启控制。
- [tests/browser/routing.spec.js:48–115](https://github.com/nineofoursyrup/Agent-Alfred/blob/2533d6818c4b6a896f460b20d5e0fe40142d335e/tests/browser/routing.spec.js#L48-L115)：记录失败及trace缺失后真实浏览器恢复。
- [src/agent_alfred/memory/statistics.py:12–84](https://github.com/nineofoursyrup/Agent-Alfred/blob/2533d6818c4b6a896f460b20d5e0fe40142d335e/src/agent_alfred/memory/statistics.py#L12-L84)：分子/分母/null惯例可复用；既有全扫不能冒充有界查询。
- [src/agent_alfred/database_console/worker.py:49–85](https://github.com/nineofoursyrup/Agent-Alfred/blob/2533d6818c4b6a896f460b20d5e0fe40142d335e/src/agent_alfred/database_console/worker.py#L49-L85)：可中止SQLite只读事务先例，不复制控制台临时数据集机制。

CE-05中的FACTS-r3生产规则已由上述chat_graph.py、graph/types.py及本规范第5.3节完整解释；本地FACTS-r3仅为调查历史，不是必须读取的外置验收合同。旧字段判读来自源码核对，不声称这些异常组合在本轮全部重跑。

### 项目检查与证据要求

实现时对同一候选完成所有R/AC/CE及项目当前要求的检查；以其实际工作树适用指令为准。当前已核验源码基线的CI采用Python 3.14、Node 22；门禁包含：

```text
uv run ruff check
uv run python scripts/check_skills.py
uv run python scripts/check_env_example.py
uv run --extra mcp pytest
uv build
uv run python scripts/check_mcp_installations.py --output <临时证据目录>/mcp-artifacts.json
npm run typecheck
npm run test:browser
```

以上为实现阶段门禁清单，本次规范综合没有执行产品检查。新增/受影响的统计、准入/收尾、版本/迁移、Graph事实采集和Behaviour用例须有真实公开路径证据；不得用旧绿灯或源码检查代替本票候选验证。构建及安装矩阵遵循当前CI要求，记录各平台实际运行或NOT RUN；有密钥/付费测试不是默认门禁。

## Critical Counterexamples

### CE-01 图内兜底与图后回退不混算

- **Basis:** R-03/R-06；Q3/Q6；#26业务合同；AC-05/06/11
- **Sequence:** 同一已知版本依次提交：未知分类→route=fallback→完整回答成功；另一Run先route=full，节点无副作用失败且剩余预算充足→实际进入普通循环；第三Run分类失败后合法进入普通循环。
- **Expected behavior:** 决议分别fallback、full、明确未形成；F分别no、yes、yes。前两项分支各1/2，F=2/3；未知分类不是图失败。普通回退本身失败也不撤销已进入事实；无额外重投。
- **Verification:** P1/P2/P3；ScriptedModel控制分类/后续失败，真实图和预算/SQLite；以Run终态持久屏障后读取，不mock GraphResult。
- **Decision status:** CONFIRMED：D1/D2/D3及完整设计批准D4；精确结果与接缝均已确认，无deferred必需案例；产品验证NOT RUN。

### CE-02 缺摘要不能证明关闭

- **Basis:** R-02/R-04/R-07/R-09；Q2/Q4/Q7/Q9；AC-08/12/14/17
- **Sequence:** 各提交有效关闭、有效开启但前置检索失败未进图、配置错误带安全占位false的Run；装载缺routing的旧Run；新启用Run准入后进程退出再恢复。
- **Expected behavior:** 资格分别关闭、开启、未知、未知、开启；前置失败能证明未形成决议时计L，历史缺证据及崩溃结果unknown。不能归普通full、关闭或0回退；未知设置的已知绕行单列。
- **Verification:** P1/P2/P4；真实配置读取/检索准备/准入/重启；外部模型脚本与明确IO故障；旧fixture单列。
- **Decision status:** CONFIRMED：D1/D2/D3及完整设计批准D4；精确结果与接缝均已确认，无deferred必需案例；产品验证NOT RUN。

### CE-03 trace生命周期不改已记录统计

- **Basis:** R-04/R-10；Q4/Q10；ADR-0020/0024；AC-07/19
- **Sequence:** 生成并记录quick、no_action恢复、full故障回退各一Run，固定测试窗口；读统计后真实裁剪所有trace，再重启并读相同窗口；另一次受控trace写失败但收尾DB成功。
- **Expected behavior:** 前三个已记录样本的次数、分母、版本和覆盖保持；trace失败样本依据其真实已保存摘要正常计数。查询不扫描trace；终态持久失败与trace失败不得混同。
- **Verification:** P1/P2/P3/P4；真实TraceStore/保留流程/Host重启；固定Clock与可控trace writer故障；检查查询无trace补数依赖。
- **Decision status:** CONFIRMED：D1/D2/D3及完整设计批准D4；精确结果与接缝均已确认，无deferred必需案例；产品验证NOT RUN。

### CE-04 full决议后失败与被阻止回退

- **Basis:** R-03/R-06/R-09；Q3/Q6/Q9；AC-05/06/18
- **Sequence:** 提交full请求→真实工具写临时本地状态→后续模型失败；对照无副作用合法回退，以及允许回退但进入前停止、进入后首个模型请求前停止。这些停止变体均由正常收尾事务保存事实；硬退出缺失结果另按CE-07处理。
- **Expected behavior:** 已提交full照计，工具只写1次；副作用路径F=no且blocked=side_effect_occurred。allowed未进入F=no；真实进入后零模型请求仍F=yes。取消/预算限制沿既有业务合同，不因统计重跑。
- **Verification:** P1/P2；真实Registry/临时文件或SQLite，脚本模型与进入点确定性屏障；校验工具账与请求次数。
- **Decision status:** CONFIRMED：D1/D2/D3及完整设计批准D4；精确结果与接缝均已确认，无deferred必需案例；产品验证NOT RUN。

### CE-05 旧恢复空数组不是未恢复证据

- **Basis:** R-08/R-11；Q8/Q11；AC-16/21
- **Sequence:** 装载来源明确的legacy schema1读取夹具：Failed/BudgetExhausted且recoveries=[]、缺recoveries、正常Completed/NoAction且完整空数组、合法恢复项、默认Failed/not_needed/model_requests=0；再对照新Run真实恢复。
- **Expected behavior:** 失败/缺字段的恢复轴unknown；完整正常结果空数组可证明no；合法对应恢复项可证明yes。默认值不能补发生/未发生。所有无可靠策略版本legacy组只给可证次数/缺失，无百分比，不改旧数据；不声称每个夹具是本轮实跑产物。
- **Verification:** P2/P3的明确旧格式fixture，来源以FACTS-r3源码生产规则核验；P1真实新Run作独立正对照，不mock成品统计/GraphResult。
- **Decision status:** CONFIRMED：D1/D2/D3及完整设计批准D4；精确结果与接缝均已确认，无deferred必需案例；产品验证NOT RUN。

### CE-06 全量、超时和资源回收

- **Basis:** R-02/R-10/R-12；Q2/Q10/Q12；AC-23/24
- **Sequence:** 生成至少101个合格Run，其中常见分页第一页之外有独有决议；查询全部。分别在锁等待、SQL、legacy解析/分组阶段推进受控单调时钟超过2秒；撤销另一个在途请求；随后提交业务Run并再次读统计。
- **Expected behavior:** 成功包含全部101个且独有决议不漏；各超限整体失败无部分数字/伪零。读资源与任务收尾，下一业务及查询可继续，未取消业务Run；旧快照只可标旧保留。不得把请求返回超时与实际查询终止混为一谈。
- **Verification:** P1/P2/P3/P5；真实SQLite中止/锁、受控工作时钟与事件屏障；记录资源关闭与后续成功证据，不用sleep、超时重试或分页结果替代统计。
- **Decision status:** CONFIRMED：D1/D2/D3及完整设计批准D4；精确结果与接缝均已确认，无deferred必需案例；产品验证NOT RUN。

### CE-07 收尾失败、崩溃与内存成功

- **Basis:** R-04/R-06/R-09；Q4/Q6/Q9；ADR-0024；AC-12/18
- **Sequence:** 启用Run准入完成，内存先产出no_action或completed；收尾事务受控失败。在同进程读统计，再真实退出/重启恢复并读取；对照成功收尾的Run。
- **Expected behavior:** 失败提交前不计持久成功或该route；无持久终态时在P。重启后admitted资格保留、finished/interrupted进入N但结果unknown，不凭旧内存completed补决议。成功收尾者不因重启改变结果。
- **Verification:** P1/P2/P3/P4；真实收尾事务故障/重启/session API，保持原recording_failed语义，检查业务未重跑。
- **Decision status:** CONFIRMED：D1/D2/D3及完整设计批准D4；精确结果与接缝均已确认，无deferred必需案例；产品验证NOT RUN。

### CE-08 恢复与无回复交叉、恢复必须已提交

- **Basis:** R-03/R-06/R-09；Q3/Q6/Q9；AC-05/06/18
- **Sequence:** 合法no_reply分类，真实上下文投影失败→recover_context成功提交→NoAction；对照error边被选但恢复节点未开始，以及恢复节点开始后失败、未提交；再对照恢复已提交后取消且正常收尾。
- **Expected behavior:** 成功案例route=no_action、R=yes、无助手空消息；仅选边/启动但未成功提交不能记R=yes。能完整证明未提交记no，否则unknown；已提交恢复在后续失败/取消且持久保存时仍记yes，每Run最多1。
- **Verification:** P1/P2/P4；既有projection/recovery公开故障回调和真实Graph commit/checkpoint屏障，观察真实恢复记录与SQLite，不能只按最终graph_result猜。
- **Decision status:** CONFIRMED：D1/D2/D3及完整设计批准D4；精确结果与接缝均已确认，无deferred必需案例；产品验证NOT RUN。

### CE-09 准入pending不是已获准

- **Basis:** R-02/R-09；Q2/Q6/Q9；AC-03/17
- **Sequence:** 准入第一事务写pending及资格后暂停handoff；读取统计；随后分别走admitted成功、handoff失败转rejected、第一事务资格写失败。
- **Expected behavior:** pending/rejected均不入A；仅admitted增加1。资格写失败不执行业务、不留被计入的半份准入证据。进程恢复不能把pending误纳为启用终态样本。
- **Verification:** P1/P2/P4；真实准入两阶段事务/交付屏障/SQLite故障，检查run_id与真实执行调用数。
- **Decision status:** CONFIRMED：D1/D2/D3及完整设计批准D4；精确结果与接缝均已确认，无deferred必需案例；产品验证NOT RUN。

### CE-10 统计资格随Run快照而非当前开关

- **Basis:** R-02/R-08/R-09；Q2/Q8/Q9；AC-03/15/17
- **Sequence:** Run A准入捕获开启与策略V1；忙碌时保存设置沿既有拒绝；完成后关闭并提交B；再次开启/改变普通模型参数提交C；多次刷新并重放相同通知。
- **Expected behavior:** A仍启用V1，B关闭，C按自身准入事实分组；一般配置变化不自动拆语义版本。不得用当前关闭使A消失，或开启使B进分母。各Run只计1；统计读取不改变既有忙碌保存行为。
- **Verification:** P1/P2/P3；真实Behaviour mutation/Host与模型请求捕获，确定性收尾屏障，无SSE内存累加。
- **Decision status:** CONFIRMED：D1/D2/D3及完整设计批准D4；精确结果与接缝均已确认，无deferred必需案例；产品验证NOT RUN。

### CE-11 时间两端、时区与无新Run的窗口变化

- **Basis:** R-02/R-10；Q2/Q10；AC-04/19
- **Sequence:** 固定as_of=T，以真实Clock生成accepted_at恰等T-7d、略早于起点、恰等T、略早于T和同秒多Run；读取7天。只推进Clock不增加Run再刷新；用等价UTC/带offset合法历史表示及本地时区变化对照。
- **Expected behavior:** 起点包含、终点排除；按绝对时刻而非文本排序或本地自然日判窗。同秒不同run_id各计1；时间推进可移出旧Run，即使activity_revision没变也要重算；显示区间与数字对应。
- **Verification:** P1/P2/P3；可注入真实接口Clock，时区/旧格式fixture只测读取兼容，API的as_of与统一读快照一并核对。
- **Decision status:** CONFIRMED：D1/D2/D3及完整设计批准D4；精确结果与接缝均已确认，无deferred必需案例；产品验证NOT RUN。

### CE-12 代际撞号与版本隔离

- **Basis:** R-08/R-11；Q8/Q11；ADR-0037；AC-15/16
- **Sequence:** 两个进程均generation=1但策略不同，拓扑hash相同；同策略另有不同generation/模型配置Run；加入未知策略legacy与未知未来格式。当前版本组无样本时读取。
- **Expected behavior:** 不同策略不混率，同策略跨重启合并；模型/工具普通配置不单独拆桶。未知组不标当前、不产生比例，未知未来格式不猜字段；当前零样本仍如实显示，不能用旧组比例顶替。
- **Verification:** P1/P2/P3/P4；真实跨进程现行组，明确多版本持久fixture验证读侧分组，不要求为测试更换生产策略或调用付费模型。
- **Decision status:** CONFIRMED：D1/D2/D3及完整设计批准D4；精确结果与接缝均已确认，无deferred必需案例；产品验证NOT RUN。

### CE-13 局部坏字段与全局样本损坏

- **Basis:** R-06/R-11；Q6/Q11；AC-11/21/22
- **Sequence:** 真实Run摘要中分别控制一个route非法、entered非bool、恢复字段缺失、F/B证据矛盾；保留其他可信字段；另一轮在相关已准入chat样本的accepted_at放非法值，另模拟DB不可读。
- **Expected behavior:** 坏指标及受其依赖影响的轴unknown，其余可信事实保留，样本不悄悄消失。关键归窗/资格无法判定或DB不可读则整次失败，不跳行、不返回部分数字或0。输入字符串作为文本安全显示。
- **Verification:** P2/P3；明确污染fixture经真实SQLite和读取API，验证错误响应与恢复；新样本主路径仍由P1覆盖。
- **Decision status:** CONFIRMED：D1/D2/D3及完整设计批准D4；精确结果与接缝均已确认，无deferred必需案例；产品验证NOT RUN。

### CE-14 快照一致、迟到响应与页面恢复

- **Basis:** R-10/R-12；Q10/Q12；AC-19/20/24
- **Sequence:** 同一查询读取期间另一Run收尾提交；先发7天请求A再发24小时请求B，B先完成；A最后返回。再测试刷新失败、切范围失败、关闭面板/离页后返回、服务重启后的旧响应及重试。
- **Expected behavior:** 每份响应只能看到其一致快照，无混代分母；界面保持最新有效查询，旧请求不覆盖/复活面板。旧结果如保留显示原范围并标旧；失败不冒充空数据；新请求恢复后整体替换，撤销不影响业务Run。
- **Verification:** P2/P3/P5；真实读事务/提交屏障，网络仅延迟或失败真实响应；所有成功数字由真实服务产生；不用固定sleep制造乱序。
- **Decision status:** CONFIRMED：D1/D2/D3及完整设计批准D4；精确结果与接缝均已确认，无deferred必需案例；产品验证NOT RUN。

### CE-15 观察统计不改变业务与空态不混淆

- **Basis:** R-01/R-05/R-10；Q1/Q5/Q10；AC-01/02/09/10/20
- **Sequence:** 页面分流设置未保存/保存冲突、聚合请求进行中时展开/切范围/刷新/收起统计；模拟统计不可用和仅拓扑读取失败；另以空库、仅关闭Run、仅未知结果三种数据打开，静置再重载。
- **Expected behavior:** 统计操作不提交设置、重置表单/聚合请求或执行模型工具；拓扑失败不挡统计，统计失败不锁原操作；三类空态与读取失败可区分，无轮询；重载默认7天收起，键盘/窄屏信息可读。
- **Verification:** P1/P2/P3；真实页面/服务，捕获业务请求/模型/工具数量与原表单状态；仅故障路径允许网络失败注入。
- **Decision status:** CONFIRMED：D1/D2/D3及完整设计批准D4；精确结果与接缝均已确认，无deferred必需案例；产品验证NOT RUN。

### CE-16 完整计数样例与跨轴分母

- **Basis:** R-02/R-03/R-06/R-07；Q2/Q3/Q6/Q7；AC-03/05/11/12/13/14
- **Sequence:** 按第7节构造a–p：a–h/k–m走真实用户入口和明确故障控制，i/j真实终止恢复，n拒绝准入、o聚合、p探针。以固定窗口读取后重复刷新。
- **Expected behavior:** 严格等于第7节：A13/E11/O1/U1/N10/P1/D6/L2/U_d2，full3/6；F2/8、R1/8、B1，blocked1；未知资格另有B1，n/o/p排除。不得把失败排除、未知当否或一次Run按Attempt重复计。
- **Verification:** P1/P2/P3/P4；ScriptedModel只控制业务输入/结果，生产聚合器读取真实持久记录；核对每个run_id原始证据与完整响应/页面。
- **Decision status:** CONFIRMED：D1/D2/D3及完整设计批准D4；精确结果与接缝均已确认，无deferred必需案例；产品验证NOT RUN。

## Readiness and Open Decisions

- **READY FOR IMPLEMENTATION**：完整设计D4已确认；R-01–12、AC-01–24、CE-01–16及P1–P5均有精确预期和可用验收边界。
- 未决产品选择、必需延期案例及验收合同阻塞均为none；没有删除或替换已确认范围。模块布局、内部字段命名等是明确保留的实现自由度，不是产品未决项。
- 规范就绪与GitHub发布、ready-for-agent标签、认领、实现授权分开。完整发布正文已准备；实际发布结果以同一任务入口及GitHub正文/标签回读为准，不以本地readiness冒充发布。
- 实现NOT STARTED；当前CE验证0/16，产品/浏览器/付费模型检查NOT RUN；Standards与Spec产品评审NOT REVIEWED。设计/文档校验不属于产品双轴PASS。
- 如果实际实现暴露无法兑现的冲突，保留原合同、提交最小差异供裁定；不得改变统计分母、降低精度、扩大unknown规则或把必需反例静默排除。

## Out of Scope

### 11. 范围、兼容与实现自由度

范围仅是消息分流的观察能力，不扩为聚合统计、通用分析平台、judge评测、模型准确率/省费证明。保留默认关闭、下一Run生效、CLI/Web共享、忙碌拒绝设置修改、分类转录隔离、NoAction、上下文安全停止、共享预算与副作用禁止重跑等既有合同。

新增必要持久统计资格/摘要及统计只读输出是本票批准的记录扩展；不得借此改变关闭时普通对话的模型/工具请求、回答、费用与会话语义。验证关闭基线时明确区分本票新增诊断字段与业务行为，不为追求旧JSON完全相同而删除已批准的证据字段，也不接受无关变化。

不新增通用选图执行、节点/边/规则编辑、拓扑展示或实际路径展示；不实现#70导出；不扩大原聚合/记忆路径。无新增用户可编辑版本号或统计口径配置。无后台统计服务、定时任务、缓存维护、全量Run明细浏览器或逐Run钻取新功能。

模块边界、数据库形状、API路由名和展示细节可遵循项目惯例，只要守住公开语义、事务边界、两秒可中止资源合同及本库存；不因实现便利修改分母、吞掉未知、放宽必需反例或改变业务回退策略。实现若发现真正无法满足的条件，需提交最小合同差异，由用户裁定，不能把它藏入实现票。

排除来源：Q1/D1排除新增顶级页/拓扑叠加/导出；Q2/D1限定聊天样本；Q3/D1与#71 S05排除准确率/收益证明；Q8/D2限定版本分组而不扩模型效果比较；Q9/D2排除逐节点同步写/业务重放；Q10/D2排除轮询与新增偏好持久化；Q11/D3排除猜补/混算旧版本；Q12/D3排除后台分析服务及截断统计。更早的#71 S06–S08继续排除通用选图、流程编辑和无场景的更多workflow。

## Further Notes

### 领域术语与架构取舍

下列术语保持CONTEXT.md的已确认含义，并随实现局部带入正确基线；不要把当前旧文档checkout的整份CONTEXT覆盖到实现树。

**路由统计**：
一组 Run 的消息分流事实计数，用于观察路由决议、故障回退与上下文恢复的分布；它不证明模型准确率或费用收益。
_Avoid_: 分类准确率、收益评估

**路由决议**：
消息分流为一次 Run 选定的业务分支，包括快速回复、完整回答、保守兜底、不回复或上下文失败提示；选定分支不等于该分支成功完成。
_Avoid_: 最终结局、成功路径

**故障回退**：
消息分流图失败后实际进入普通循环继续处理的行为；分类结果选择图内的保守兜底分支不属于故障回退。
_Avoid_: fallback 分支、上下文恢复

**上下文恢复**：
消息分流在上下文投影失败后经专属恢复路径继续处理的事实；它可以与不回复等路由决议同时发生。
_Avoid_: 普通循环回退

**图前绕行**：
消息分流因配置或图不可用而未开始执行，实际转入普通循环的行为；它与图执行失败后的故障回退分开。
_Avoid_: 图执行失败、故障回退

**统计口径版本**：
一套统计中样本资格、指标含义和分子分母规则的稳定身份；字段形状相同不保证统计口径相同。
_Avoid_: schema_version、图发布代际

**路由策略版本**：
消息分流的分类提示词及路由规则的稳定身份；它不等于拓扑结构、模型指派或某个进程内图实例的代际。
_Avoid_: topology_hash、图发布代际

ADR-0042已确认的取舍：准入资格与Run收尾摘要分别复用原持久屏障，避免另建逐节点同步统计账；因此突然退出时结果可能未知，必须如实显示。各指标按自己的已知证据集算比例，并以统计口径/路由策略语义身份判断可比性，避免字段形状、拓扑哈希或进程代际造成假可比。代价是旧数据覆盖可能较低，未来语义变化需维护显式版本。该取舍承接ADR-0020/0024及ADR-0037，验收只维护于本规范。

### 实施交接与发布路线

- 唯一状态入口：`/Users/nineofour/Agent-Alfred/.scratch/behaviour-routing-stats-74/LOOP.md`；本次规范综合revision 7，接续先读最新revision及发布回读。
- 本地规范：`/Users/nineofour/Agent-Alfred/.scratch/behaviour-routing-stats-74/ROUTING-STATS-SPEC-r1.md`；源批准：同目录`APPROVAL-r4.md`；来源hash与规范hash分别记录，不混为一个身份。
- 预计发布为独立纯实现票“实现：消息分流路由统计的完整用户路径”，原生关联地图#1并引用决策#74；就绪标签建议`wayfinder:task`与`ready-for-agent`。不认领、不启动执行agent；不为#70/#73/#75添加未证明的产品依赖。
- 决策#74保留身份；其resolution、关闭及地图指针由明确发布授权单独覆盖，不将决策票悄悄改写成实现票。发布前重新去重、核验远端；发布后逐项回读正文、标签、父关系及状态。
- 本次仅综合已确认规范；没有产品实现、Git提交/推送/合并或候选评审。实施者须取得相应授权后使用归属明确的隔离工作树，以本规范将每个CE映射到同一候选的实际证据。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/76#issuecomment-5745402021

<!-- issue-76-acceptance:r1 -->
# #76 交付验收

实现已按 `ROUTING-STATS-SPEC-r1` 完成。PR #79 交付统计能力，PR #80 完成交付门禁暴露的页面初始化与测试时序修复；最终候选通过独立双轴、PR-head 与实际 merge-SHA 完整 CI 后验收。

Behaviour 消息分流区的独立统计支持 24h/7d/30d/all、持久准入和收尾证据、语义版本分组、各轴已知分母/unknown覆盖及有界一致快照查询。统计与两份只读拓扑、设置/聚合表单及 trace 导出生命周期可同时工作；不证明模型准确率或费用收益。

## 精确候选与独立评审

- 规范 SHA256 `640d90d5f66d6402f4cf64535a69c03fe9e924140586477bf404bab764416656`；R01–12、AC01–24、CE01–16、P1–P5 未变。
- 功能候选 `routing-stats-c4-8802b341cb5b`，32 个产品文件；完整 tree `c07088d8be4911f3e2ba79cbc09003d58f32351c`；manifest `8802b341cb5b8fc8566e0317b34a11ad87865bf2770e76817115d32953f6fc34`，diff `eb307786238e08a654835f10c28a74c367f7a1246164985f49f6e54375f83c4e`。PR #79 head `eb9f74d168564eab3c655e141428ee94fcdfcb92`，merge `c5b3a12f078af0bf3722d941c8dc3b6d9c07438f`，tree 与 c4 相同。
- 最终接受候选 `routing-stats-c7-e223275b2c40`，比较基线为上述 #79 merge；4 文件后继差异，生产变化仅启动页“新建会话”初始禁用，其余为真实浏览器测试就绪/隔离和回归。
- c7 完整 tree `95106d0fcd3ed88cb832e863c9d10e3968f1cfa3`；manifest `e223275b2c40ca05b32d306c7b4d199f29db0387b033e2bec223bc31baeeded9`；diff `24cbc831c64bd23f401a97d040a7dcedeed4de9288280f038bb39b84b54c80ee`。
- 最终 PR #80 head `8bec8f616fff36d0e14cbbd74a5928d1cdec67ab`；实际 merge `98c8afa19a3484f7fdd4108569d9a6847707f7a1`，merge tree 与 c7 相同，main 包含关系已回读。
- 功能候选 c4 fresh 双轴 PASS；后继 c7 经 fresh `/root/review_c7` 分别委派 `/root/review_c7/standards`、`/root/review_c7/spec`，均无实现历史继承，**Standards PASS / Spec PASS**，阻塞 **0**、可选 **0**、必需证据缺口 **0**。582 tracked 文件 bytes/mode/index、manifest/diff/spec 最终无漂移。
- STD-01、SPEC-01、SPEC-02、SPEC-03、GATE-01 保持 resolved；新增 GATE-02/03/04 均 **resolved @ c7**。资源中断/清理、矛盾持久证据、旧进程迟到响应、真实公共用户路径与 Database取消异常都有原反例、修复及独立回归证据。

## 交付门禁暴露的问题与关闭证据

原 #79 merge CI [35463308106](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/35463308106) **FAILURE**（Python4881PASS，browser279PASS/1FAIL）；PR #80 首轮 [35465211822](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/35465211822) **FAILURE**（Python4881PASS，browser278PASS/2FAIL）。失败历史保留，未用盲重试改写。

- GATE-02：真实新 Session SSE 准入未就绪时按 Enter，没有产生 `/api/runs`，用例却等待202。原键盘用例先等待真实发送可用，再证明 IME/Shift 零提交、换行及普通Enter恰1次202；真实 SSE 屏障与两轴独立探针完成 red→green，原断言保留。
- GATE-03：MCP conflict notice先于新preview刷新，旧button闭包仍持旧token，第二次得到真实409。等待真实refresh替换旧button后应用新preview，得到真实202、已连接且initialize恰1次；MCP业务逻辑不变。
- GATE-04：启动凭据尚未消费时“新建会话”初始可点，空CSRF得到真实403。HTML初始禁用，由既有entry成功路径启用；真实entry-held时0提交，释放后201并可真实聊天。历史基线archive仍有原行为，因此对照harness另用每端fresh context/page，导航前订阅真实 `/api/events`，凭据消费后才创建Session；finally先关context再关server。两轴独立entry-held探针证明旧archive/current两端都先0POST，释放后201/一次POST，真实聊天和reload全文一致。历史代码未修改。

没有加sleep/retry、放宽timeout/原业务断言、注入token或伪造成功响应。c6全browser虽绿仍因archive残留被双轴拒绝，修复后才接受c7。

## 验证及最终两阶段 CI

- 功能 c4 本地 Python4881 passed/1 deselected、browser280、ruff/skills/env/typecheck/build与wheel/sdist×base/mcp均通过。
- 当前 c7：完整 browser **281 passed (6.4m)**；相关用例14PASS；真实双端启动屏障probe1PASS；typecheck、新构建及wheel/sdist×base/mcp四组合PASS。c6 WebPython256成功结果按不变生产输入核对复用；最终远端完整Python重新执行。
- sdist 包含浏览器测试，旧“全部构建输入不变”表述已撤销：本轮重新构建与安装验证，并逐字核对新sdist/wheel与最终候选，包括四个改动文件；不以旧包身份放行。
- 原 PR #79 CI [35462080288](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/35462080288) SUCCESS，后续失败历史见上。
- 最终 PR #80 CI [35467898135](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/35467898135)，head `8bec8f616fff36d0e14cbbd74a5928d1cdec67ab`，**SUCCESS**。
- 最终 merge-SHA CI [35469068283](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/35469068283)，head `98c8afa19a3484f7fdd4108569d9a6847707f7a1`，**SUCCESS**。两阶段分别回读，不以PR绿灯代替实际merge结果。
- 当前代码：[统计文档](https://github.com/nineofoursyrup/Agent-Alfred/blob/8bec8f616fff36d0e14cbbd74a5928d1cdec67ab/docs/routing-statistics.md)、[统计与生命周期测试](https://github.com/nineofoursyrup/Agent-Alfred/blob/8bec8f616fff36d0e14cbbd74a5928d1cdec67ab/src/agent_alfred/evals/deterministic/test_routing_statistics.py)、[进程与持久化测试](https://github.com/nineofoursyrup/Agent-Alfred/blob/8bec8f616fff36d0e14cbbd74a5928d1cdec67ab/src/agent_alfred/evals/deterministic/test_routing_statistics_lifecycle.py)、[真实浏览器统计测试](https://github.com/nineofoursyrup/Agent-Alfred/blob/8bec8f616fff36d0e14cbbd74a5928d1cdec67ab/tests/browser/routing-statistics.spec.js)、[门禁修复](https://github.com/nineofoursyrup/Agent-Alfred/pull/80/files)。

## AC-01–24 逐项验收

| 验收 | 当前候选证据 | 核对结果（PASS） |
|---|---|---|
| AC-01 | CE-15 | Behaviour 独立默认收起，分流关闭仍可看历史；不依赖拓扑 |
| AC-02 | CE-15 | 键盘/窄屏表格可读，设置与聚合草稿及真实待回执请求保持 |
| AC-03 | CE-01/09/10/16 | 真实 CLI/Web 跨 Session；只纳 admitted chat；pending/rejected/其他purpose排除 |
| AC-04 | CE-06/11 | 24h/7d/30d/all；UTC半开两端、offset、同秒及滚动移出；101条真实Run不截样 |
| AC-05 | CE-01/04/08/16 | 五类决议分轴；full失败保留；图内fallback不等于F，full+F与no_action+R共存 |
| AC-06 | CE-04/08 | allowed未进入F=no，已进入0请求F=yes；R仅恢复提交后yes；blocked真实记录 |
| AC-07 | CE-03/14 | Host/服务重启及真实trace删除、absence记录/写失败后已持久事实保持 |
| AC-08 | CE-02/05/07/12 | 旧资格/结果unknown，不填当前版本，不以摘要缺失判关闭，不回写重放 |
| AC-09 | CE-01/15/16 | CLI/HTTP→Host→Graph→Recorder/SQLite→统计HTTP→browser真实链路 |
| AC-10 | CE-03/06/07/09/13/14 | 模型外部替换与故障/legacy污染边界显式；事件屏障、真实组件及HTTP响应乱序 |
| AC-11 | CE-01/13/16 | a–p样本A13/E11/O1/U1/N10/P1/D6/L2/Ud2；full3/6、F2/8、R1/8、B1、blocked1 |
| AC-12 | CE-02/04/07/16 | pending与finished分开；失败/取消保留持久决议；崩溃恢复结果unknown；重复读取不增计 |
| AC-13 | CE-02/16 | 图前绕行B与图后回退F分开，unknown资格的可信B单列 |
| AC-14 | CE-02/16 | 无效配置false不算关闭；未知资格不改变E分母，设置/图错误不猜唯一原因 |
| AC-15 | CE-10/12 | 稳定语义跨重启合并；策略版本分组；模型配置不自动拆桶 |
| AC-16 | CE-05/12 | 当前身份来自服务器；历史/未来未知版本比例null；当前零组不借历史率 |
| AC-17 | CE-02/09/10 | 准入事务持久资格；资格写失败无业务；后续设置不改变旧Run资格 |
| AC-18 | CE-04/07/08 | 唯一收尾事务保留已提交事实；真实写失败/退出/重启无伪保存或重跑 |
| AC-19 | CE-03/11/14 | 真实SQLite读事务一致快照；跨窗、旧请求/旧进程/离页迟到隔离 |
| AC-20 | CE-14/15 | 展开/重展开/切范围/刷新主动读取，无轮询；失败清除旧数字且可恢复；空态区分 |
| AC-21 | CE-05/13 | schema1全判读表；Failed/BudgetExhausted空recoveries不是no，默认字段不补证据 |
| AC-22 | CE-13 | 坏指标/新旧冲突只降低相关轴；坏样本归属整次失败；无跳行、伪零或部分成功 |
| AC-23 | CE-06 | 101真实Run覆盖全部；真实锁、SQL/parse/result受控阶段预算；2秒超限整体失败 |
| AC-24 | CE-06/14 | 真实HTTP断连、超时、构造/清理中断、持续释放失败可恢复owner；后续业务/读取继续 |

## CE-01–16 证据追溯

S=`src/agent_alfred/evals/deterministic/test_routing_statistics.py`；L=`test_routing_statistics_lifecycle.py`；B=`tests/browser/routing-statistics.spec.js`。下表行号绑定最终 head `8bec8f616fff36d0e14cbbd74a5928d1cdec67ab` 的统计文件（与 c4 相同）；产品用例由完整门禁和后继独立回归覆盖。拓扑失败 probe 是已核对输入不变的 c4 独立评审证据。

| CE | 实际入口、预期与证据 |
|---|---|
| 01 | S:18/479、B:105；CLI `_send` 与MainBar跨Session真实Run，committed fallback/full各1/2、F2/3，classification不补route；普通回退失败不撤销进入。 |
| 02 | S:142/163/179、L:57/177；有效关闭/开启前置失败/坏设置/legacy缺失/真实硬退出分开；配置错误false是unknown，U中的实际B单列。 |
| 03 | L:228及B:27；真实封存bundle删除并record_trace_prune、trace写IO失败、真实进程重启，统计不扫描trace且已持久计数保持。基线无自动retention服务，明确只验证手工裁剪合同。 |
| 04 | S:220/538、L:57；真实save_fact与ledger各1次，full在副作用失败/取消后保留；blocked=side_effect_occurred；进入屏障前F=no、进入后零请求F=yes。 |
| 05 | S:69/561、B:233；明确schema1 fixture走真实读取器；Failed/BudgetExhausted空恢复unknown，完整正常空数组no，有效恢复yes，默认not_needed/0不补证据；历史组无%。 |
| 06 | S:108/289/331/361/897、B:270；101真实Run无截样；锁等待与open/sql/parse/result受控工作时钟超限整体失败，真实socket断连回收child/pipes，随后业务提交及读取成功。 |
| 07 | S:236、L:57、B:304；真实SQL trigger使唯一收尾失败，P保留；物理退出/重启恢复后N但结果unknown，不用内存NoAction补造。 |
| 08 | S:191/491/757/967、B:27/304；真实projection/recovery提交屏障、启动前/未提交/提交后取消，R仅已提交为yes；NoAction无assistant空消息，context_failure正例单独覆盖。 |
| 09 | S:409、L:177；真实handoff屏障pending/rejected排除，资格写SQL失败原子失败且无模型业务；只有admitted纳样。 |
| 10 | S:142/787、L:177、B:270；准入资格随Run，不按当前开关变更；忙碌拒绝、设置冲突保持，普通模型变更同语义组，重复读取不重复计数。 |
| 11 | S:265、B:323；注入真实Clock产生边界/offset/同秒Run，只推进时钟则7d 3→1、all5；UTC半开、滚动精确时长，页面显示范围及时区。 |
| 12 | S:448/787、L重启、B:233；稳定现行语义跨Host/模型合并；明确未来语义/格式fixture归未知无比例/不猜字段，空当前组保留。首版未发布其他可识别旧语义，不虚构第二个受支持版本。 |
| 13 | S:304/720/740/860/1042/1063/1138、B:233；实际SQLite字段污染，相关轴unknown其余保留；坏accepted_at/DB不可读整次失败无数字。SPEC-01独立4项回归关闭，DOM仅文本。 |
| 14 | S:380、B:27/72/174/371；同读事务期间真实新提交不能混入快照；真实成功响应延迟/乱序/服务重启/离页，不返回伪成功计数；stats与topology控制器分别取消。 |
| 15 | B:4/72/139/233/270/371与独立topology-failure probe；默认折叠/7d、重载复位，无轮询；未保存、冲突、聚合真实待回执状态保持；3种空态/错误/恢复，键盘与390px；本轴查看narrow.png确认计数/分母换行可读。拓扑失败不影响统计已实测。 |
| 16 | L:57/177、B:343；完整a–p由真实准入/执行/工具/终止生成，SQLite backup只复制真实记录；k挂唯一收尾屏障。HTTP/page核对A13/E11/O1/U1/N10/P1、D6/L2/Ud2、full3/6、F2/8、R1/8、B1、blocked1。 |

## 限制与跨票边界

GitHub Ubuntu CI 已执行；独立 Linux 专项及付费/私人模型测试 **NOT RUN**。模型仅在声明的外部边界替换，准入、Graph、Recorder、SQLite、Host/进程退出、CLI/HTTP和页面保留真实组件。trace 裁剪使用真实 bundle 删除与 prune 记录，当前基线没有自动 retention 服务。预算分阶段验证使用已批准工作时钟/故障控制，另外验证实际锁等待与HTTP断连；不声称对每一种C层压力都单独实跑。

本票不证明模型准确率或费用收益，不包含发布版本/PyPI、部署、通用编辑/运行workflow。#70/#75 是此次固定基线依赖；其交付由各票证据证明，本次不替相邻票关闭未完成事项。完整本地候选、双轴报告、原始日志和交付回读保留于本任务证据包，并沿用原唯一 LOOP。

所有 #76 必需验收已完成；随后以 `completed` 原因关闭本 Issue，并分别回读 REST `closed/completed` 与 GraphQL `CLOSED/COMPLETED`。
