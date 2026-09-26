# #31 决定：切片④b — 看见记忆（Memory 编辑页）

来源：https://github.com/nineofoursyrup/Agent-Alfred/issues/31

## Question

**切片 ④b「看见记忆」**：Memory 编辑页。
端到端可演示的是：说一个事实 → 下一轮命中并显示来源 → 删除它 → 不再命中。

- **只有语义 / 情景是 Store**（[#5](https://github.com/nineofoursyrup/Agent-Alfred/issues/5)）；
  程序性记忆（Skill）只有 `list()` / `load()`，不在可替换后端这一族里。三者在一页上怎么排。
- ⚠️ **不得显示相关度分数**
  （[ADR-0008](https://github.com/nineofoursyrup/Agent-Alfred/blob/main/docs/adr/0008-no-cross-store-relevance-score.md)）：
  检索**只承诺顺序、不承诺可比的量**（FTS5 的 bm25 是负数、余弦是 0..1）。
  那么「为什么这条被召回」在界面上由什么承载。
- **遗忘是真删**
  （[ADR-0007](https://github.com/nineofoursyrup/Agent-Alfred/blob/main/docs/adr/0007-forgetting-must-be-real.md)）：
  被删正文与 subject **不进入任何下游**，只留 `MemoryId` 与 HMAC-SHA256 指纹。
  删除后的界面还能显示什么、不能显示什么。
- **两层去重必须分名**：机械幂等（Store，规范化后的 `(subject, fact)`）
  与语义归并（提炼器，走 `update` 不走 delete+save）。编辑页暴露哪一层。
- 编辑走 **MutationGate**；本地事务归调用方
  （[ADR-0009](https://github.com/nineofoursyrup/Agent-Alfred/blob/main/docs/adr/0009-caller-owns-the-local-transaction.md)）。
- **情景是半开区间不是时刻**，时间一律 tz-aware——时间轴怎么画。
- **顺带定型 `gate.evaluated` 的最终载荷**：[#3](https://github.com/nineofoursyrup/Agent-Alfred/issues/3) §11
  只留了 `decision` / `reason` / `latency_ms` / `hit_count` 的最小骨架，
  并写明「#5 关闭时须回写」。本票是它的落点，需要更多字段就在这里补，并回写修订 #3。

### 验收

- 检索结果页上**不出现任何分数**。
- 删除一条记忆后，其正文与 subject 不出现在回喂、trace、SSE 任何一处。
- 存储故障**抛出**而非渲染成「没有结果」。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/31#issuecomment-5588135839

# 看见记忆：独立实施规范 v1

状态：Q1–Q33及完整规范、具体回写清单已获用户确认（2026-09-09）。本文为最终 resolution；决策完成不代表实现已经验收。本规范可独立阅读，不以访谈记录作为实施前提。

决策来源：[决定：切片④b — 看见记忆（Memory 编辑页）](https://github.com/nineofoursyrup/Agent-Alfred/issues/31)。核查日期：2026-09-08。固定代码证据基线：`55418dad266eed4df8a3e40670a069e3dcddc65d`，本轮 `git ls-remote` 再次确认远端 main 相同。本次回写前再次核验远端main未变；下述责任别名均已链接到真实施工票。

## Problem Statement

用户需要分清“我说过”“已经保存”“确实从长期记忆召回”“实际送入某次模型请求”。目前 Memory、检索和工具主体仍未实现，单凭模型复述不能证明记忆已保存或命中。用户还需要编辑、删除、核验来源，并知道删除覆盖哪些对象、哪些历史仍能人工查看。

## Solution

明确保存使用共享命令并产生提交回执；每个普通聊天 Run 首次回答前评估一次检索门，展示真实的召回、筛选和请求证据；Memory 页提供语义、情景、只读 Skill 三标签。删除硬删条目和索引，隔离已知来源及传递使用历史，清理受管副本，通过可恢复进度证明完成。

### 已裁决、已实现与实施缺口

| 分类 | 当前证据与结论 |
| --- | --- |
| 既有裁决 | [记忆 Protocol resolution](https://github.com/nineofoursyrup/Agent-Alfred/issues/5#issuecomment-5428337164)：Store/SkillCatalog 分族、来源、机械幂等、半开时间、调用方事务、无正文删除结果。 |
| 既有裁决 | [Observer resolution](https://github.com/nineofoursyrup/Agent-Alfred/issues/3#issuecomment-5427536560)：事件信封、Run/Step/Attempt；gate 最终载荷由本规范补齐。 |
| 既有裁决 | [Tool resolution](https://github.com/nineofoursyrup/Agent-Alfred/issues/4#issuecomment-5427830226)：工具账是证据、local_write 同事务、两份结果投影和七个工具错误码。 |
| 已实现 | [看见对话完成证据](https://github.com/nineofoursyrup/Agent-Alfred/issues/36#issuecomment-5559572106)及固定基线：MainBar、Session、Run、准入与回复恢复基础已合并；本轮未重跑其历史测试。 |
| 本次新裁决 | 本文 R01–R16：明确保存、检索载荷与预算、编辑恢复、精确遗忘范围、人工保护与页面组织；依据用户逐项及批量确认的 Q1–Q33。 |
| 下游缺口 | 检索、Store、工具、提炼、完整工作窗口及本页差量尚待下列施工票；本次关闭决策不表示实现完成。 |

### 实施责任别名

| 别名 | 负责施工票 |
| --- | --- |
| T17 | [实现：切片② — 检索门与语义/情景记忆 FTS 后端](https://github.com/nineofoursyrup/Agent-Alfred/issues/17) |
| TF | [实现：记忆遗忘与来源隔离](https://github.com/nineofoursyrup/Agent-Alfred/issues/45) |
| T19 | [实现：切片③ — ToolRegistry 与内置工具](https://github.com/nineofoursyrup/Agent-Alfred/issues/19) |
| T16 | [实现：会话与工作记忆](https://github.com/nineofoursyrup/Agent-Alfred/issues/16) |
| T18 | [实现：记忆提炼与 Markdown 镜像](https://github.com/nineofoursyrup/Agent-Alfred/issues/18) |
| TM | [实现：切片④b — 看见记忆（Memory 编辑页）](https://github.com/nineofoursyrup/Agent-Alfred/issues/46) |

## User Stories

1. 作为用户，我希望明确要求保存时得到提交回执，以确认长期记忆已写入。
2. 作为用户，我希望普通陈述按阈值提炼，以免每句话都立即成为事实。
3. 作为用户，我希望多条保存逐条呈现结果，以判断部分成功。
4. 作为用户，我希望下一轮看到真实召回及来源，以区分长期记忆与聊天窗口。
5. 作为用户，我希望区分召回、拟纳入和实际请求使用，以免把预算耗尽误读成已入模。
6. 作为用户，我希望检索模型失败时有确定性回退，并看得见原因及费用。
7. 作为用户，我希望存储故障明确停止回答，以免空结果掩盖读取失败。
8. 作为用户，我希望列表和搜索展示可信顺序及来源，不显示跨后端分数。
9. 作为用户，我希望编辑保留身份和来源，并在并发修改时保护草稿。
10. 作为用户，我希望断线后能查询原操作，以免重做保存或删除。
11. 作为用户，我希望删除后不再从该条目或已知旧来源自动带回内容。
12. 作为用户，我希望知道数据库删除、副本清理及遗忘完成的区别。
13. 作为用户，我希望多标签及迟到响应不能恢复被删正文。
14. 作为用户，我希望仍能主动查看原始会话及既有 trace，并知道它们不在追溯擦除范围内。
15. 作为用户，我希望按带时区的半开区间查看情景，避免边界漏查。
16. 作为用户，我希望看见有效 Skill 和覆盖来源，而不把目录展示误读成已经匹配或注入。
17. 作为用户，我希望查看提炼队列、失败和镜像同步状态。
18. 作为用户，我希望后台提炼覆盖亲自保存或编辑的内容前等待确认。
19. 作为用户，我希望删除或编辑使陈旧的提炼审批失效，防止内容复活或覆盖新版本。
20. 作为实施者，我希望每条要求都有公共接口、确定性验收及唯一主责票。

## Implementation Decisions

### R01 完整路径与持久化边界（T17、T19、TM；普通提炼 T18）

| 步骤 | 触发、输入与输出 | 成功和失败呈现 | 责任 |
| --- | --- | --- | --- |
| 说一个事实 | 普通聊天进入 Session 会话记录；只有明确“请记住……”或页面保存才立即调用记忆命令。歧义先问清 subject/指代。 | 模型自然语言不证明提交；没调用只可显示未确认保存。 | T19；TM |
| 保存 | 语义输入 subject/fact；工具入口由主模型解析。每条事实独立 operation_id、独立事务。页面与工具共用命令。 | 提交后“已保存”或“已存在”；失败逐条说明。可部分成功，不承诺整段话原子提交。 | T17；T19；TM |
| 下一轮检索 | 新 chat Run，门模型读当前问题和有效工作窗口；决定检索后查两库。 | skip/hit/miss/error 各自显示；Store 故障不生成正常答案。 | T17 |
| 显示来源 | 系统证据展示分组次序、来源、记录版本、选中/请求使用事实；展开回查当前记录。 | 已修改显示当前版本提示；不存在显示不存在；故障显示故障。 | T17；TM |
| 编辑 | 读当前记录后提交 expected_version。 | 成功更新；陈旧版本冲突保留草稿；已删除则清正文草稿。 | T17；TM；T19 |
| 删除 | 页面删除前一次确认；对话唯一明确目标直接执行，歧义澄清。 | 数据库确认后固定无正文回执；清理中/失败/完成如实区分。 | T17；TF；T19；TM |
| 不再命中 | 下一 Run 重新查库，旧 ID 不在 get/search；自动输入与提炼遵守隔离。 | 不能用模型有没有复述作为唯一验收。新明确保存及其它独立记录另算。 | TF；T17；T16；T18；TM |

保存事务和 Run 记录是两个事实。保存已提交后，模型失败或会话记录失败不撤销保存；回执仍可通过 operation_id 读回。普通陈述只在累计达到阈值后提炼；本闭环确定性夹具采用明确保存，不承诺任意自然语言下一轮必命中。

### R02 Store、记录、版本与查询（T17）

沿用既有 SemanticStore/EpisodicStore、Record/Hit 分离、MemoryId 不透明、Origin 闭合联合、transaction_mode 及 normalization_version。所有时间 tz-aware，比较归一 UTC。只实现默认本地 FTS5；不把此页等同于 external Store 的可见性承诺。

记录补充：`record_version`（持久正整数，初始1）、`modified_at`、`last_change_origin`、`human_protected`。创建 origin 永不改写；首次 modified_at=created_at、last_change_origin=origin。subject/fact 或 summary/发生区间确有变化才增加内容版本；无变化 update、重复 save 保留版本、首份原文与创建身份。版本非时间戳，不从 MemoryId 推断。

页读侧另有持久memory_revision，在任何可见记录/保护变更及删除时递增，用于缓存和游标失效，不替代内容版本或事件seq。迁移使用新的schema_migrations编号，不改冻结迁移；旧记录的初始版本只表示从本协议开始观测，不补造修改史。来源未知保留未知证据状态；不得猜关联齐全。新迁移号在实施固定候选时取当前最大号之后，避免与相邻施工占号冲突。

human_protected 与来源独立：明确页面/对话保存和编辑均受保护；提炼不得自行清除此标记。明确重复保存既有自动提炼内容也可原子置保护标记，不因此制造新条目或改写正文版本；提炼提交时必须重检保护属性，不能只检查内容版本。来源/使用关联另存元数据，origin 的批次粒度不改成单一 Run。

既有机械幂等保持 NFKC、trim、内部空白折叠、版本化；不改大小写/标点，不改存储正文。同主题不同表述不自动覆盖；语义归并只由提炼器判断并 update，不能 delete+save。发生唯一键冲突原子返回 existing_id，不合并。

Store update/delete 增加 expected_version 原子比较能力，不注入 txn 参数；调用者不能先 get 后无条件写。update 结果闭合区分 applied（含 changed/version）、version_conflict（current_version）、duplicate_conflict（existing_id）、not_found；delete 区分 deleted、already_absent、version_conflict。底层删除不存在仍幂等返回不存在语义；任何存储故障继续抛出。共享服务把结果投影为 R08 命令协议。

FactQuery.subject 是显式硬过滤。EpisodeQuery 的 since/until 是半开相交过滤，有 text 按 Store 顺序并以发生时间破同分，无 text 仍按 occurred_at 倒序。普通页面“全部条目列表”另用最近创建排序的读接口，不改写无文本 EpisodeQuery 的既有顺序。列表同创建时间使用内部稳定序号破同分，不排序 MemoryId。

### R03 每 Run 一次检索、模型与预算（T17）

每个普通 chat Run 在首次主模型回答前最多完成一次 gate 评估；后续 Step/Attempt 复用，不再检索。下一 Run 重评估。probe/system Run 不因共享基础设施自动检索。编辑失效与删除终止按 R10，不能视为重新检索授权。

门模型按 retrieval_gate 指派；未指派使用 primary，显式指派不可用走规则，不暗换模型。门模型输入当前用户消息及有效工作窗口，无工具能力；结构化输出为 retrieve:boolean、query:string|null、reason_code（R06闭合集）。retrieve=true 时 query 必须非空；false 时 query=null。模型自由散文 reason 或非法形状按输出无效处理。模型可改写 query，两 Store 使用同一文本；v1 自动检索不生成 subject、since、until 硬过滤。

启动门模型需取得共享 RunBudget 的1个 StepLease，重试与流式回退都在同一 Step，Attempt、token 和费用进入现有聚合链；不能因门调用位于主循环前而漏账。预检不可用且没启动模型不扣 Step；已取租约不返还。门模型总预算默认5秒，重试共享且受 Run 总 deadline 约束，真实超时由模型 IO 执行，不能留下后台调用后假称取消。门模型预算耗尽但 Run 尚有时限时可规则回退；Run 总时限或 Step 已无后续额度时不借回退重置预算。

max_steps=0 时不发模型请求；max_steps=1 且门模型用掉一步时可以已有 gate 完成结果，但没有主模型回答请求，实际参考资料请求使用数为0。门失败的真实费用仍计入；规则和本地搜索不产生模型 Step/模型费用。

### R04 确定性回退 v1（T17）

规则只分类，不执行输入。规则版本 `memory-gate-rules-v1`。匹配输入为本次用户原文；规则匹配用的临时规范化与 Store 幂等规范化分名。

寒暄匹配：NFKC、trim、连续空白折成单空格、ASCII 字母转小写，再去掉末尾连续的 `! ? . 。 ！ ？`；整句等于以下一个值才 skip：`你好`、`您好`、`嗨`、`哈喽`、`早上好`、`下午好`、`晚上好`、`晚安`、`谢谢`、`多谢`、`感谢`、`再见`、`拜拜`、`hi`、`hello`、`hey`、`thanks`、`thank you`、`bye`、`goodbye`、`good morning`、`good afternoon`、`good evening`、`good night`。带其它内容不得部分匹配跳过。

纯算式匹配：NFKC 后允许空白、十进制数字、小数点、括号、单目正负号，以及二元 `+ - * / × ÷`；至少一个二元运算符，整句符合通常括号/优先级语法。末尾可有一个 `=` 或 `?`，不包含字母、单位、中文或函数。不求值、不用 eval；非法或无法完整识别即走保守检索。该规则不认定算式结果正确或可计算。

其它全部 retrieve，查询严格使用未改写的用户原文，reason_code=conservative_retrieve。不调用第二个模型。混合句“你好，我不吃香菜”、含单位自然语言计算不能 skip。规则细节是已获批“整句白名单/纯算式/其余检索”的机械展开，随本文最终复核。

### R05 召回、选入与实际请求（T17）

两库各默认 limit=5；每组序列化参考资料预算4000个 Unicode 码点。每组按 Store 顺序整条选入，下一条放不下即停止该组；不跳过长条去抢后位，不截断、模型压缩或借用另一组额度。额度包含该组记录的正文、subject、ID、版本、origin等序列化元数据；固定外层消息标识不计组额度。

组文本采用确定性 JSON 数组：UTF-8 Unicode 原字符、无多余空白、字段次序固定，语义条目依次为 id/version/subject/fact/origin，情景依次为 id/version/summary/occurred_at/occurred_until/origin；origin 按闭合分支输出 type 与其身份字段。数组括号、逗号和转义后的字符均计入码点数。使用 Python len 意义的 Unicode 码点而非 UTF-8 字节、JS UTF-16 长度或 token。实际送入的组文本必须正是被计量文本。

参考资料作为单独 user Message，放在有效工作窗口之后、当前用户问题之前；明确标“检索参考资料”，不写 agent_log，不伪装系统指令。当前 Run 后续 Step 复用；部分可用时附固定“本次仅提供部分召回”的标记，模型和用户均可见。这是输入证据，不能声称模型在回答中实际采纳了每条。

- 两库成功且合计0：miss，继续回答。
- 合计大于0且至少选中1：hit，继续；部分省略标 partial。
- 有命中但选中0：hit + all_excluded，受控失败 memory_input_unavailable，不生成正常回答。
- 任一 Store 故障：error + store_error，不使用半份资料、不生成正常回答；成功库可保留诊断计数。

可注入 `per_store_limit`、`per_store_character_budget` 正整数及 `gate_model_budget_s` 有限正数配置，默认如上。沿现有配置加载方式，不新增本票外的模型指派页面；启动拒绝非法值。超出模型整体上下文窗口的处理归 T16 的完整轮裁剪，不能静默裁正文/当前问题，也不声称字符预算等于 token 容量保证。

### R06 gate.evaluated 最终载荷 v1（T17；TM 消费）

沿既有 Event 信封携带 run_id/source/seq 等身份；不另造计数或时间排序体系。查询与筛选得到终局时每 Run 至多一条，`trace_policy=persist`。业务对象独立产生并进入 Run 遥测，EventSink 缺席或事件丢失不改变事实。未评估、被中断或预算停止而未完成评估不伪造事件。

| 字段 | 类型与含义 |
| --- | --- |
| schema_version | 整数1；无版本的旧骨架按 legacy 阅读 |
| decision | boolean，最终是否查询 Store |
| outcome | skip / hit / miss / error；error 专指 Store 读取失败 |
| decision_source | model / deterministic_fallback |
| reason_code | personal_information / history_recall / greeting / arithmetic / conservative_retrieve |
| fallback_reason | null，或 model_unavailable / model_call_failed / invalid_output / model_deadline |
| rule_version | model 时 null；规则时 memory-gate-rules-v1 |
| gate_step_index | 门模型 Step 索引；未启动时 null，含失败后回退的实际索引 |
| model_ref | 已启动门模型的 endpoint_id/model_id/wire_style；未启动 null；不含凭据与 URL |
| latency_ms | 整次 gate 已用毫秒，单调时钟、非负 |
| timing | model_ms / search_ms / selection_ms 非负；未执行阶段 null |
| stores | 固定 semantic、episodic 两组，形状见下 |
| hit_count | 完整两库已确认召回总数；skip=0；error=null，不把未知补0 |
| selected_count | 选入参考资料总数；skip/miss/error/all_excluded=0 |
| references | 按 semantic 后 episodic 分组，组内保持 Store 顺序的引用数组 |
| input_disposition | not_needed / no_hits / ready / partial / all_excluded / store_error |

Store 项：`status=not_queried|succeeded|failed`，`hit_count` 成功为非负整数、其它为 null；`error_code` 失败为 storage_unavailable/storage_read_failed，其它 null。skip 时两库 not_queried；retrieve 时按 semantic、episodic 顺序执行，前者失败可停止并把后者标 not_queried，不能伪造第二库故障。

引用项：`kind=semantic|episodic`、`memory_id`、`record_version`、创建 `origin`、`rank`（组内从1开始）、`selected`、`omission_reason=null|character_budget|after_prefix_stop|store_error`。已成功一库的诊断引用在另一库失败时全部 selected=false；不展示可用半份资料。

一致性：skip⇔decision=false、reason 为 greeting/arithmetic；其它 outcome decision=true。model/retrieve 的 reason 为 personal_information/history_recall/conservative_retrieve；规则 retrieve 恒 conservative_retrieve。model 来源 fallback_reason/rule_version 均 null；规则来源必须记录真实失败原因与版本。hit 即两库成功且 hit_count>0；miss 两库成功且为0。

载荷及持久检索遥测禁止 subject、正文、原 query、query摘要/指纹、自由 reason、异常散文及相关度分数。未知新版/非法旧数据明确“证据不支持”，不补造0。原始会话与已写 Attempt trace 的保留按 R09，不能由本载荷无正文推断全链路无正文。

### R07 实际输入证据与统计（T17；窗口关联 T16；展示 TM）

在 Adapter 实际发起请求、建立真实 Attempt 身份的边界记录 `step_index/attempt_id/purpose` 和有序的 `(kind,memory_id,record_version)` 引用；purpose 区分 gate/answer。同一步重试每个实际 Attempt 均有证据，禁止从 StepStarted、工具结果或 selected_count 推断请求已发送。局部构造失败无网络往返则不伪造 Attempt；不知道对端是否接收时只称“已发起请求”，不称“模型已阅读”。

同一请求同步记录实际携带的工作历史组ID，用于来源关联；入模证据及关联均不复制正文。gate 读取历史也纳入使用关联。已发起失败 Attempt 照常保留引用和费用；未调用回答时实际参考资料使用为0。展示按 Step/Attempt 查看，不跨重试把条目数相加成“命中数”。

Run 遥测 gate 记录状态为 evaluated/not_evaluated/incomplete/legacy_unknown；只有 evaluated 的四态参与比例。记录来自业务收尾，不回放 SSE；门模型 Attempt 必须汇入 Run 自身消费与价格聚合。已有 usage/pricing 未知语义保持不变。

Memory 页默认 `[现在-7天,现在)`、全部 Session 的已记录 chat Run，可切当前 Session；用同一个查询锚点展示实际范围。skip 比例=S/(S+H+M)，hit 比例=H/(H+M)，error 比例=E/(S+H+M+E)。分母0显示无数据；各分母与数量可见。未记录、未评估、不完整和 legacy 数量分列，不进入上述分母；非 chat 不混入。旧 trace 裁剪不改已持久统计。

### R08 共享命令、API、事务和错误（T17；工具适配 T19；HTTP/页面 TM）

唯一共享记忆命令服务：save/update/delete、get_operation；手动页面写入和工具不能各自写 SQL。命令 envelope v1 为 operation_id、kind、action、payload、expected_version（update/delete 必需）；可信调用上下文携入口 web/cli/tool/consolidation、可空 run/session/call 身份、创建/修改来源及持有的准入权限。调用者不能在普通表单或模型参数里伪造来源、保护标记或租约。

结果使用闭合分支：saved、already_exists、updated、unchanged、deleted、already_absent、version_conflict、duplicate_conflict、not_found、invalid_input、unavailable、execution_error。成功安全回执包含 operation_id/action/kind、相关MemoryId、版本（适用时）、affected_count、committed_at；删除仅有允许审计字段，不返回正文/subject/含正文的参数预览。查询当前正文是独立 get，不把回执当正文快照。

本地事务由共享执行器拥有：Store 变更与FTS、来源关联、最小工具账/审计、成功操作回执同事务提交；Store 不 commit/rollback。失败不能留下成功回执。Dashboard 取现有 MutationGate；Run 工具沿已有 Run 租约传递明确权限，不能重新取门而自判409。手动操作不伪造模型 Run，可空身份配真实入口。local_write 记忆工具账由共享执行器唯一落账，Registry只生成投影/事件，不重复插账。提炼复用事务能力，但写 consolidation_batches，不混入 tool_ledger。

operation_id 在同一次用户动作、重送和重启之间稳定。相同ID相同参数只回读原提交结果；相同ID参数不同拒绝。参数身份使用版本化 canonical JSON 的 HMAC-SHA256/key_id，不保存原正文；创建审计密钥与权限失败遵守既有 fail-closed。旧 key 不可比时拒绝重新执行，报告 operation_unverifiable，不当作不同参数或可安全重做；安全历史回执仍可独立读取。

无回执不等于已失败：客户端显示结果待确认，查询同ID或同ID重送；未提交事务可重新执行，提交但丢响应必须只返回旧结果。先保存后删除时旧保存回执只能说明当时成功，当前正文读取返回不存在。遗忘进度是独立可变状态，不改写历史提交事实。

HTTP v1（TM 适配共享服务，T17 提供同名行为契约）：

| 接口 | 输入/输出 |
| --- | --- |
| GET /api/memory/records | kind，mode=list/search，text/subject/since/until，cursor，page_size；返回 records、next_cursor、read_revision |
| GET /api/memory/record | kind、id；返回当前 Record，未找到404 |
| GET /api/memory/state | 当前持久memory_revision，用于重连和缓存核对；不含正文 |
| POST /api/memory/commands | 上述命令 envelope；返回安全提交回执；允许的action只save/update/delete |
| GET /api/memory/operations | operation_id；返回当时提交结果与独立当前遗忘进度；未找到404仍表示未确认 |
| GET /api/memory/statistics | since/until、可选session_id；返回R07分子分母及证据缺失分类 |
| GET /api/memory/skills | 有效SkillMeta列表；GET /api/memory/skill?name=… 只读加载正文 |
| GET /api/memory/consolidation | 阈值、队列计数、批次状态、来源范围、版本、可用操作 |
| POST /api/memory/consolidation/actions | batch_id、expected_revision、action=approve/reject/retry、operation_id；旧审批失效不可执行 |
| GET /api/memory/mirrors | 每份镜像的配置路径、generated_at、同步状态和当前可安全读取的只读预览 |
| POST /api/memory/forget/actions | operation_id、action=retry_cleanup/resolve_scope；后者携用户明确选择的来源范围ID，不携正文 |

合法写入完成返回200；清理尚未完成也返回已提交的200回执和 cleanup 状态，不把已删除误当未获准Run。400 invalid_input（含非法字段/时间/未知枚举）；404 not_found；409 busy/version_conflict/duplicate_conflict/operation_mismatch/operation_unverifiable/batch_invalidated/cursor_stale；503 unavailable/storage_read_failed/storage_write_failed。响应只携闭合安全码、字段名、ID/版本及可操作入口，不回显原参数/异常。错误响应统一 `{error:{code,…安全元数据}}`；无 raw traceback。

工具维持既有七个 ToolErrorCode：字段非法→invalid_input，版本/重复键/不存在/执行存储故障→execution_error，未启动的后端不可用→unavailable；细节以固定 memory_code 放入结果文本首行 JSON，不新增第八个工具错误码。删除成功的所有结果投影从源头无正文，见R10。HTTP安全策略、Origin校验、同进程宿主和错误基础复用现有 Dashboard。

### R09 遗忘范围与来源隔离（TF；各消费者分别接入）

承诺是“选中条目及受管派生副本遗忘”，不是任意历史表面不可查看。必须硬删该 MemoryId 的行及FTS，不留该记忆墓碑；清除其受管镜像内容、当前详情/检索展开、可再利用的命令回执和工具账内容投影、提炼候选/差异。无正文审计元数据可留。

原始会话与删除前已写入的 trace 不追溯改写，仍可人工打开；它们不能被当作可自动再利用的安全投影。其它独立语义/情景记录即使内容相同也不级联删除。用户另存、截图、浏览器开发工具、应用外副本及已发送模型服务的请求不在可撤回范围内。新一次明确保存可产生新 MemoryId；旧 operation_id 重放不是新授权。不能以删除文本加入 Redactor 或内容黑名单实现遗忘。

持久关联区分 Memory→来源组/提炼批次、Run/Attempt→使用Memory版本、Run/Attempt→实际读取历史组。来源组以完整Run或批次为单位，不存待删正文。删除时隔离来源、直接使用者及沿实际读取链传递的全部已知后继组；工作窗口、工具账摘要、检索门输入与提炼共同排除。隔离不删除原始历史，也不自动删同批其它已有Memory记录，但同组无关内容会一起退出自动上下文。

关联和隔离事实必须持久化，重启不能恢复自动使用；关联缺失显示 needs_scope，列出能证明的范围，要求用户明确选择要隔离的 Session/批次范围。未解决不能宣称完整遗忘已完成；允许读取只能返回安全状态，不能借不确定来源重新提供已知受影响正文。不能把旧库缺关联迁移成“没有来源”；合法手动输入无历史来源与历史来源未知分名。

### R10 删除时序、在途请求和页面失效（TF、T19、TM；引用 T17）

数据库删除与无正文清理意图同事务提交；关联闭包隔离及可在SQLite内完成的投影/候选清除在同事务内生效。确认数据库删除后：

TraceSink 当前有异步写队列，因此“既有trace”不能含糊地按页面出现时间判定。TF在删除事务前为已产生的受影响trace建立非终结顺序屏障，使删除前证据先确认写入其既有历史；屏障失败则本次删除尚未执行，明确失败，不能先确认删除再让旧正文晚到落盘。不得直接使用会封存整个Run的最终flush来代替该屏障；保留既有Run收尾所有权。删除后的新结果和收尾必须无正文。此项是兑现已批准时间边界的实现要求，不扩大历史擦除范围。

1. Run内立刻停止后续模型请求，以固定系统操作回执收尾，不请模型复述；尚未启动工具明确列为未执行，既有动作不回滚。当前工具批未执行项仍形成配对的安全内部结果以保持转录结构（沿既有unavailable工具码，细节memory_delete_boundary），不能再发送给模型；不产生假的 tool.started 或业务账。固定回执是正式终止文本，不另造第二份模型回答。正常完成该受控收尾时Run沿既有completed结局，并附finalization_reason=memory_delete_boundary及未执行call_id清单；不得把completed解释成未执行动作也完成，记录失败/中断仍按既有独立轴处理。
2. 在既有SSE连接发送无正文`memory_patch`，复用有界且必须投递的状态通知机制，送不出则断开重连。载荷固定为schema_version=1、process_instance_id、持久memory_revision、change；change为null（完整失效/重连快照）或包含kind/id/record_version（删除null）、operation_id、action=updated/deleted的单条记录失效。此帧无domain EventEnvelope、无seq、无SSE事件checkpoint，不为手动命令伪造Run；不能塞进只描述Run生命周期的state_patch。客户端拒绝跨进程旧帧及revision倒退，重连读取当前revision并核对内容，不依赖完整事件回放。批次/操作清理状态改变可用change=null提示重新读取相应页面状态；所有通知无正文/subject。
3. 在线页面撤掉详情/检索正文/已删编辑草稿；读取携revision，丢弃变更前启动的迟到响应。断线即隐藏缓存记忆正文，重连核验后再展示；不承诺瞬间擦掉离线/冻结页面，也不等待所有标签确认才完成服务端提交。
4. 跨文件清理按持久意图幂等推进，阻断旧镜像及受管正文继续由应用提供。状态 closed 集为 cleaning/needs_scope/failed/complete（数据库未确认删除时无已删除状态）。可另列每项清理状态；重启续同一操作，不重新保存正文或撤销删除。仅所有受管清理与来源范围都确认才显示“遗忘完成”。

数据库已删但清理失败时显示“记忆已删除，副本清理未完成”及安全重试原因；不能只显示泛化删除失败。工具调用已发生的数据库删除结果不被清理失败改写；固定收尾显示真实阶段。Run若缺持久终态仍按现有 interrupted/recording_state 规则，operation回执独立证明删除已提交。

编辑时剔除当前 Run 参考资料中的旧版本，后续请求不得以旧引用继续携带；不自动查新版本补位。其它原始历史文字按R09范围处理，不用编辑冒充追溯遗忘。Run 内删除完成后，不再有会把此前原话/工具内容重复送入模型的下一次请求。

### R11 页面、来源和时间（TM；查询 T17）

Memory 页三标签：语义记忆、情景记忆、Skill目录；复用 MainBar 和当前标签页Session。前两者支持列表、搜索、新增、编辑、逐条删除。记录展示创建来源、创建时间、最近修改来源/时间、内容版本与人工保护状态。工具来源展示call_id、可访问的Run链接；提炼来源展示batch_id及来源范围；缺关联如实标不可追溯，不能伪造名称。

普通列表默认最近创建优先；搜索按Store序列，无跨库总排序。默认25条、不透明cursor；cursor绑定查询、内部排序位置与read_revision。筛选改变从首页重读；版本变动使游标不可继续时409 cursor_stale，清楚提示刷新，不拼接成遗漏/重复的成功列表。不按MemoryId排序。前后端均不投影 relevance 展示串，即使其中是“bm25 -3.21”也不展示。

情景文本与时间可共同筛选；标注“起点包含、终点不含”。occurred_until=null 显示瞬时；非空显示 `[start,end)`，等端点为空区间而不冒充瞬时；结束早于开始拒绝。查询 `[since,until)`：区间需有交集，瞬时满足 since≤t<until；无界端点分别处理。不同offset同一绝对时刻结果一致。输入须明确时区/offset，不能由服务端把naive值猜成当地时刻；页面可用已展示的本地时区生成aware输入，显示offset供核对。

库为空、搜索无结果、读取失败、保存待确认、版本冲突分开呈现。故障保留未提交表单草稿，但旧列表不得冒充当前成功结果；确认目标删除后清掉正文草稿。成功编辑反馈对应实际提交版本。检索展开无论是否选入，都回查当前内容：同版本可显示；新版本标“当前展示为修改后内容”；不存在不恢复旧正文；故障不当不存在。此规则不自动擦除用户主动打开的既有历史会话/trace。

### R12 Skill 展示接缝（TM）

最低 SkillCatalog 实现在TM，沿既有 list/load 协议：显示有效name/description、builtin/user、overrides_builtin，展开未经改写的正文。名称只查已构建索引，不拼路径；用户同名整体覆盖内置，同目录重复名启动失败。配置故障不得显示为空目录。

页面只读，不创建/编辑/删除，不热重载；文件改变后重启加载。匹配信号、判断者、注入位置/生命周期、运行证据仍归[决定：程序性记忆的匹配信号与注入时机](https://github.com/nineofoursyrup/Agent-Alfred/issues/24)，不添加该票对目录展示的阻塞。T19原有create Skill要求保留，它的文件创建不以Memory页Catalog完成为前置，不在本票暗定热重载或匹配。

### R13 提炼、人工保护及陈旧审批（T18；隔离接缝 TF；面板 TM）

只有未提炼有效聊天累计达到配置阈值才自动运行；产出机械幂等后的事实和一条情景摘要。语义归并由提炼器决定并update。普通自动提炼记录可自动归并；凡覆盖human_protected记录，整批待确认，不先提交未受保护的部分。明确对话工具保存/编辑同样受保护，不能仅看origin=manual。

批次有持久id/revision、来源组、目标ID/expected_version、候选/差异与状态 queued/running/awaiting_approval/succeeded/rejected/failed/invalidated；不存原始聊天副本供队列列表展示。提交前在同一准入/事务内重检来源隔离、目标版本和人工保护。用户确认后提交全部获准写项；明确拒绝为人类处理结果，不能称保存成功。全部写项或明确拒绝项落定才可推进来源处理标记；存储失败/失效不能标已成功提炼，源聊天不可丢失。

删除涉及来源、目标或已知使用链时，整批invalidated，清候选正文/差异，保留安全状态元数据；页面撤旧草稿，迟到批准返回batch_invalidated。目标被编辑版本变化同样整批失效，重新规划并重新确认；同批未受影响候选也不沿用旧批准。重新规划只读当前有效来源并继续遵守阈值，不能借重试强制提炼。批准与删除竞态由同一MutationGate/事务顺序决定，不做先读后写跨锁检查。

队列面板显示待处理数量/配置阈值、批次状态、来源范围、时间、安全错误及批准/拒绝/同批失败重试动作。忙时409、不排队；不提供忽略阈值立即提炼。阈值具体计量、配置与提炼模型质量属于T18原票剩余规范，实施前在那里明确；本票只固定不越阈值、接口字段、保护/失效和事务验收，不声称已裁决所有提炼算法。

### R14 Markdown 镜像与恢复（T18；清理协调 TF；展示 TM）

镜像为单向只读派生，默认可配置状态根下 `memory/facts.md`、`memory/episodes.md`；不导入用户修改，不在页上编辑、列历史镜像。展示配置路径、生成时间、同步状态以及安全的当前预览。内容生成可重新由现存有效Store记录完成，无需在清理任务保存被删正文。

写入采用可验证的替换边界，文件故障不能声称新镜像已就绪。删除后旧文件在应用内不可读；持久任务逐项清理/重建，失败可重试，重启继续。同一受管输出没有额外“历史版本”躲避清除；用户自行复制的文件不托管。TF用注入文件端口验证协议，T18必须用实际镜像实现验证清理和重启，不能用TF替身PASS冒充集成完成。

### R15 会话、窗口与工具账差量（T16；账本 T19；隔离 TF）

复用已实现Session、记录、恢复和分页，不重做壳层。工作窗口从当前Session取最近N个完整已记录chat Run，先排除隔离来源，再选择有效完整组；不以最后2N行冒充N轮。记录本次实际带了哪些组供解释，包含gate和回答使用；跨Session不混入。

工具账由T19/共享执行器及时落账，T16消费真实账项形成动作、状态、时刻、安全ID摘要；记忆相关摘要不复制subject/正文。摘要是证据，不是锁，不因同参数自动拒绝新动作；修订原施工票“连续两轮绝不重复执行”的机械拦截误读，沿已存在的工具裁决执行。

超长窗口按现有可配置N轮边界排除最旧完整组并解释；仍不能满足已配置输入限制则明确失败，不静默裁当前问题或记忆正文。新全局token算法、人格编辑等T16其它范围在原票单独明确，不由Memory决策猜默认，也不得阻塞已存在N轮窗口上的T17交付。

### R16 依赖和交付边界

新增原生阻塞（保留全部已有边）：T17←本决策；TF←T17；T19←T17、TF；T16←T19、TF；T18←TF（已有T17）；TM←T17、TF、T19、T16、T18、已完成看见对话施工。依赖表示整票完成验收的真实前置，不表示所有代码必须等前置完工才可讨论。决策32仍由本决策阻塞；程序性匹配24的原有依赖保持。

执行顺序为 T17 → TF → T19 → T16与T18 → TM。插入TF是已批准遗忘范围引入的真实生命周期需求。TF提供无正文关联、隔离、清理端口/状态机，使用独立确定性夹具从公共服务验证，不反向依赖T16/T18/T19完整实现；各消费者负责真实接入。TM最终浏览器路径负责跨票集成，不能用接口对齐制造循环依赖。

## Testing Decisions

最高可用接缝优先：共享命令服务+临时真实SQLite/FTS、RuntimeHost+ScriptedModel+捕获Adapter请求、HTTP/SSE+真实浏览器。复用已有预算、Run记录恢复和多标签测试设施；不测试私有调用次数代替业务行为，不用真实模型输出作为唯一证明。时间、网络失败、文件故障和并发用注入时钟、barrier/event确定排序，不用sleep竞态。

### Given / When / Then 验收矩阵

每行主责唯一；括号说明下游集成责任。T17/TF夹具不得被标为完整产品路径已完成。

| ID | Given | When | Then | 主责 / 需求 |
| --- | --- | --- | --- | --- |
| A01 | 一条明确保存，空库 | 共享命令提交 | get与FTS可读同ID，安全回执与最小账同事务 | T17 / R01,R08 |
| A02 | 门/主模型按脚本输出save调用 | 对话明确保存 | 真实调用共享服务，提交后才有系统成功回执 | T19 / R01 |
| A03 | 普通陈述且未达阈值 | Run落记录 | 不立即保存长期记忆 | T18 / R01,R13 |
| A04 | 多事实，第二条存储失败 | 顺序执行 | 逐条部分成功，无整段原子或全部失败假象 | T19 / R01 |
| A05 | 规范化相同、大小写不同、同subject异文夹具 | save及重启重送 | 同键同ID保留首文；大小写不同不合并；同主题异文不覆盖 | T17 / R02 |
| A06 | 同记录两个版本编辑者 | 后者交旧expected_version | 原子冲突，无覆盖；duplicate_conflict两条都不变 | T17 / R02,R08 |
| A07 | 无变化编辑、真实编辑、重复save | 执行 | 仅真实正文/区间变化增版本，origin不改 | T17 / R02 |
| A08 | 注入事务中间故障 | save/update/delete回滚 | 业务、FTS、账和成功回执共同未提交 | T17 / R08 |
| A09 | 写入已提交但HTTP响应丢失 | 查/重送同operation_id并重启 | 回同一提交结果，无重复副作用；不同参数拒绝 | T17 / R08 |
| A10 | 已保存后又删除、旧HMAC key不可比 | 读旧回执、重送 | 不复活正文；不可比不被当作可重做 | T17 / R08 |
| A11 | 已有一条真实FTS事实，聊天窗口无原句 | 下一Run查询固定相关词 | Store真命中，origin和版本正确，相关/无关夹具不凑满limit | T17 / R01,R03 |
| A12 | 主模型多Step及Attempt重试 | 完成同Run与下一Run | 同Run一次gate；下一Run重新评估；系统probe不检索 | T17 / R03 |
| A13 | 门显式指派失败/未指派 | gate选择 | 前者规则回退，后者primary；不暗换指派 | T17 / R03 |
| A14 | 门调用重试、5秒/Run deadline可控 | 注入超时 | 共享deadline/Step，真实Attempt全部计费用；Run耗尽不延时 | T17 / R03 |
| A15 | max_steps=0及1 | 分别运行 | 0无请求；1门用完后回答0请求，selected不冒充实际入模 | T17 / R03,R07 |
| A16 | 白名单、纯算式、混合个人句、普通知识句 | 门不可用/非法输出 | 前两skip、后两原文查库；规则非恒量且不求值 | T17 / R04 |
| A17 | 两库各5条，首条或中间超额 | 计量并筛选 | 各自4000码点、完整前缀、不借额度、不跳长项 | T17 / R05 |
| A18 | 0命中、部分可用、命中全超限 | gate完成 | miss继续；partial注明；all_excluded停止答案但仍计hit | T17 / R05 |
| A19 | semantic失败或episodic失败 | 查询 | 明确error，未知数量null，部分资料不进入回答 | T17 / R05,R06 |
| A20 | 已选引用及有效工作窗口 | 捕获实际Adapter请求 | 单独参考user消息位于窗口后问题前；不入agent_log | T17 / R05,R07 |
| A21 | 请求前失败与重试发送 | 捕获实际网络边界 | 无发送不造Attempt；已发送每Attempt有正确引用、版本、purpose | T17 / R07 |
| A22 | gate完成、SSE丢失、Run未记录或旧数据 | 收尾与统计 | 权威遥测不依赖SSE，旧/不完整不补0 | T17 / R06,R07 |
| A23 | S=2,H=3,M=1,E=1及全零夹具 | 查询固定7天范围 | skip=2/6、hit=3/4、error=1/7；零分母无数据；范围可核对 | T17 / R07 |
| A24 | 含独特subject/正文/query/异常标记及分数 | 序列化gate/遥测 | 仅允许元数据，禁止字段/标记均不存在 | T17 / R06 |
| A25 | 宿主已有Run租约，页面并发写 | 工具继承权限并写、页面写 | 工具不自锁，页面409；手动写不创建假Run | T17 / R08 |
| A26 | 通用Registry包裹共享记忆命令 | local_write执行 | 一次真实业务账，两个结果投影与tool事件由Registry负责 | T19 / R08 |
| A27 | 所选记录和无关记录均有索引 | delete提交 | 所选get不存在/FTS不命中，不留正文墓碑；无关记录保留 | T17 / R02,R09 |
| A28 | 来源R1→读取R2→读取R3及无关R4 | 遗忘 | 已知闭包隔离R1/R2/R3，R4保留；无正文关联重启稳定 | TF / R09 |
| A29 | 历史关联缺失 | 遗忘并重启 | needs_scope，未假称完成；用户指定范围后才能完成相应隔离 | TF / R09 |
| A30 | 受管内容投影、历史会话/trace、独立同义记忆 | 遗忘 | 投影清理；人工历史和独立记忆保留且不误称全域擦除 | TF / R09 |
| A31 | 同Run删前有原话，后有待执行工具 | 删除数据库确认 | 固定无正文收尾，无下一模型请求，待执行明确未执行，已做不回滚 | T19 / R10 |
| A32 | 删除回执含审计信息 | 检查model/audit/SSE/新trace输出 | 删除产生路径无subject/正文，HMAC带key_id，异常不泄露 | T19 / R08,R10 |
| A33 | 同Run参考已选旧版本 | 工具编辑后下一Step | 旧引用剔除，无重新检索补位，实际引用证据一致 | T17 / R10 |
| A34 | 删除已提交，文件端口失败或进程在边界中断 | 同operation重试/重启 | cleaning/failed诚实，旧副本禁读，最终complete无复活 | TF / R10,R14 |
| A35 | 两标签、旧读取被barrier延迟 | 一标签删除、再释放旧读取 | 在线详情/检索/草稿清除，迟到响应不恢复正文 | TM / R10,R11 |
| A36 | 已显示正文的页面断线 | 重连时记录已删 | 断线隐藏，核验不存在，不重放缓存正文 | TM / R10 |
| A37 | 当前记录编辑/删除/读取故障 | 展开旧gate引用 | 新版本标明，删除无旧文，故障不当不存在，无相关度分数 | TM / R11 |
| A38 | 相同创建时刻、多页、查询改变/数据变更 | 翻页和刷新 | 内部稳定顺序，cursor绑定查询，无按MemoryId排序；陈旧游标明确重读 | TM / R11 |
| A39 | 跨日区间、瞬时、空区间、不同offset同瞬间 | 边界查询 | UTC半开相交正确；until边界排除；naive/逆序拒绝 | T17 / R02,R11 |
| A40 | 空库、无结果、存储错、版本冲突 | 页面操作 | 独立状态，失败不冒充空，冲突保草稿、已删清草稿 | TM / R11 |
| A41 | builtin/user覆盖、重复名、非法名称 | 构建Catalog/list/load | 整体覆盖透明、重复启动失败、名称只查索引、正文只读 | TM / R12 |
| A42 | Skill尚无匹配功能、文件已修改 | 页面查看/重启 | 无伪造匹配状态，重启才重载，不出现编辑/注入控制 | TM / R12 |
| A43 | 达阈值、提炼中事务故障 | 执行批次 | 回滚事实/情景/标记，原聊天不少；不写tool_ledger | T18 / R13 |
| A44 | 普通自动记录与明确tool/manual记录 | 提炼尝试覆盖 | 前者可自动归并，后者整批待确认；origin与保护独立 | T18 / R02,R13 |
| A45 | 待确认批次，目标删除或改版本 | 旧批准迟到 | 整批invalidated，清正文差异，批准拒绝，不标提炼成功 | T18 / R13 |
| A46 | 删除与批准竞争，固定barrier顺序 | 按两种顺序交替测试 | 同准入/事务决定结果，无陈旧批准复活删除；已生成独立记录按R09处理 | T18 / R09,R13 |
| A47 | 队列failed/awaiting/invalidated、宿主忙 | 页面重试/批准 | 安全状态和操作明确，409不排队，不绕阈值 | TM / R13 |
| A48 | 真镜像含所删记录、写替换失败 | 删除、重启、重新生成 | 应用不提供旧镜像，真实文件最终无该记录；路径按状态根重定位 | T18 / R14 |
| A49 | 最近N+1完整已记录Run、隔离组、另一Session | 恢复工作窗口 | 正确挤出/隔离，不混Session；实际读取关联持久 | T16 / R09,R15 |
| A50 | 真tool_ledger记忆账、连续明确同类新命令 | 生成窗口摘要和执行 | 摘要无subject/正文，展示既有证据，不机械拒绝新命令 | T16 / R15 |
| A51 | 完整轮超过已配置输入界限 | 构造本轮输入 | 从最旧完整轮裁剪并解释；仍超限明确失败，不裁当前问题或记忆正文 | T16 / R15 |
| A52 | 全部真实组件、ScriptedModel、临时SQLite及浏览器 | 明确保存→下一Run命中→来源→编辑冲突→删除→再查 | 页面与业务证据一致；删除后受管路径无正文且旧ID不命中；历史边界可核对 | TM / R01–R15 |
| A53 | 既有自动提炼记录，明确重复保存同内容 | 执行后提炼尝试覆盖 | 保留ID/正文版本并置人工保护；提炼原子重检后整批待确认 | T17 / R02,R13 |
| A54 | 删除前trace停在真实异步队列，注入写失败/成功 | 尝试删除并释放barrier | 失败不执行删除；成功先确定历史写入再删，不提前封存Run，不让旧正文在删除后晚到落盘 | TF / R10 |
| A55 | 无活动Run的手动变更、通知队列溢出、旧进程帧 | SSE失效及重连 | memory_patch不伪造Run/seq/checkpoint；不可投递则重连核验，旧revision不能恢复正文 | TM / R08,R10 |

实施要求：独立agent、TDD、适用机械检查、固定候选的独立Standards/Spec两轴评审。真实模型演示仅在获得相应运行授权/配置后执行，单列证据，不代替离线确定性测试。每票关闭需实际验收，不凭本决策关闭。

## Out of Scope

本调度轮不实现本规范，也不提交/推送/合并Git；施工按独立实施授权执行。不追溯擦除原始会话/既有trace，不做语义等价内容全域删除，不撤回已发外部请求；不新增external Store实现、Skill匹配/注入/热重载、完整Tools/Ops页面、全局token预算或人格编辑产品裁决。T16/T18/T19原有相邻范围保留在原票，不因本票页内接缝被取消。

## Further Notes

### 与既有裁决的明确修订

| 原来源 | 本次获准补充/修订 | 依据 |
| --- | --- | --- |
| 记忆Protocol记录、update/delete | 增内容版本、修改来源/时间、人工保护；原子expected_version及闭合冲突结果；共享稳定操作回执 | Q12、Q17–Q19、Q33 |
| Observer gate最小骨架 | R06完整版本化载荷；reason改闭合码；R07选中与实际请求分开 | Q6–Q16 |
| ADR0007与目标票“任何下游”宽泛验收 | 删除结果无正文仍不变；R09明确受管副本与原始历史/既有trace边界，R10固定收尾/恢复 | Q20–Q24、Q31–Q32 |
| ADR0008不透明展示串 | 本页不显示任何分数或带分数的relevance串，改由origin/版本/顺序解释 | 目标票已有禁令、Q3、Q25 |
| ADR0009调用方事务 | 共享执行器统一记忆命令/回执，提炼仍有独立批次账，不混工具账 | Q18–Q19 |
| 看见对话MainBar展示边界 | 增系统操作回执/检索证据；删除固定收尾不是第二份模型回答 | Q2–Q3、Q22 |
| 提炼自动语义归并 | 人工保护、整批确认、删除/编辑使待确认批次失效 | Q27、Q32–Q33 |

### 未决事项归属与就绪程度

本决策范围内Q1–Q33无未回答产品选择；R04规则取值、序列化/API码等机械展开随最终文稿复核。TF/TM已创建并链接于实施责任表。T16完整token/人格范围、T18阈值计量/模型质量、T19其它内置工具和程序性匹配决策24仍在各自责任票，不能据本规范将其全票标成无需澄清。T17共享命令、FTS、gate与其验收可独立开始；TF依赖其接口后可独立验收。后续若相邻整票过大，调度agent按真实剩余范围再申请拆分，不预先创建重复票。

### 固定源码证据入口

- [领域词汇与既有生命周期](https://github.com/nineofoursyrup/Agent-Alfred/blob/55418dad266eed4df8a3e40670a069e3dcddc65d/CONTEXT.md)
- [检索占位](https://github.com/nineofoursyrup/Agent-Alfred/blob/55418dad266eed4df8a3e40670a069e3dcddc65d/src/agent_alfred/memory/retrieval_gate.py)、[循环接缝](https://github.com/nineofoursyrup/Agent-Alfred/blob/55418dad266eed4df8a3e40670a069e3dcddc65d/src/agent_alfred/loop/assistant.py)
- [工作窗口执行接缝](https://github.com/nineofoursyrup/Agent-Alfred/blob/55418dad266eed4df8a3e40670a069e3dcddc65d/src/agent_alfred/runtime/execution.py)、[MutationGate/API基础](https://github.com/nineofoursyrup/Agent-Alfred/blob/55418dad266eed4df8a3e40670a069e3dcddc65d/src/agent_alfred/gateway/web/api.py)
- [既有遗忘ADR](https://github.com/nineofoursyrup/Agent-Alfred/blob/55418dad266eed4df8a3e40670a069e3dcddc65d/docs/adr/0007-forgetting-must-be-real.md)、[事务ADR](https://github.com/nineofoursyrup/Agent-Alfred/blob/55418dad266eed4df8a3e40670a069e3dcddc65d/docs/adr/0009-caller-owns-the-local-transaction.md)

固定链接用于核实当前实现，不把基线中的stub读成已交付；规范正文是未来实施契约。

