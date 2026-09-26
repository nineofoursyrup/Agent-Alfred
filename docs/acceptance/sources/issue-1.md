# #1 地图：Agent-Alfred v1 — 本地优先、无框架、可追踪的私人 AI 助手

来源：https://github.com/nineofoursyrup/Agent-Alfred/issues/1

## Destination

**Agent-Alfred v1 真实可用**：同一个 Agent 内核同时服务终端聊天与本地九页 Dashboard，记忆 / 工具 / MCP / Graph / 追踪费用 / 门禁全部按 brief 落地，并通过四道门禁——Ruff、全离线确定性测试 100%、Skill 与 `.env.example` 校验、wheel+sdist 在干净临时环境验证。

本地优先（状态留在本地文件与 SQLite，模型与模型目录走云端），**不伪造任何连接状态或执行结果**。

v1 发布结论还须满足[V1-ACCEPTANCE-DESIGN-r4 已确认子合同](https://github.com/nineofoursyrup/Agent-Alfred/issues/83#issuecomment-5747880935)：离线工程验收与发布验收分开，后者需要有效的模型质量证据；完整通用数值门槛仍待真实样本校准。

## Notes

<!-- v1-acceptance-phase-a-policy:r4 -->
- **发布验收分阶段安排（2026-09-20 用户明确批准）**：[已确认子合同及后续校准边界](https://github.com/nineofoursyrup/Agent-Alfred/issues/83#issuecomment-5747880935)。阶段 A 的离线证据机制可依据已批准子合同先形成独立实施规格；完整决策继续 OPEN，通用数值门槛另待校准批准。仅此阶段例外于“整张决定关闭后才出实现票”；A 不被未定阈值反向阻塞，后续校准原生依赖 A。ready-for-agent 只表示 A 规范就绪，不授权代码实施、付费模型或 Git 交付。

- **本地图承载执行**，显式覆盖 wayfinder 的"只规划"默认。票分两类：`决定：` 开头是决策票（HITL），`实现：` / `调研：` 开头是施工票与调研票。
- **分工原则**（2026-08-26 定）：**规划与执行分离**。`决定：` 票是 HITL，必须由驱动这张图的人与规划 session 面对面解决，**不得外包给子代理**（子代理去解决策票 = 自己编答案再自己批准）。`调研：` 与 `实现：` 票**一律交给独立 agent 执行**，规划 session 只负责开票、验收与回填地图。
- **每个 session 只解一张票**；调研票是唯一例外，可并行派多个。
- **纵向切片的开票法**（2026-08-27 定）：一个「可独立演示、测试与回滚」的切片**先开一张 `决定：` 票**
  冻结完整用户路径、页面内容与验收，关闭后再 graduate 出对应的纯 `实现：` 票。
  切片的「纵向」体现在**决策票的范围**上（一次裁定一整条端到端路径所需的全部决定），
  执行仍按分工原则外包。**独立回滚只承诺后片可撤而不破坏前片**，反向不成立。
- **语言**：issue、`CONTEXT.md`、ADR、`docs/` 用中文；代码标识符与代码注释用英文。
- **包名 `agent_alfred`**。**单用户假设写死**：Dashboard 无鉴权、DB 无 user_id 列、Skill 目录无命名空间。（值得一条 ADR。）
- **里程碑按可演示切片推进**：① 真模型能聊 → ② 能记事实并命中 → ③ 能调工具 → ④ Dashboard 看得见 ①②③ → ⑤ Graph / MCP → ⑥ 门禁收口。五张接口决策票钉在对应切片前面，只定形状不定细节；切片跑通后**允许回写修订**接口决策。
- **首个 provider**：OpenCode Go，base `https://opencode.ai/zen/go/v1`（注意与按量版 `.../zen/v1` 不同 base），env `OPENCODE_API_KEY`，模型 `deepseek-v4-flash`。
- **每张施工票的默认验收项**：依赖可注入（**`ModelClientFactory`** / DB 连接 / 记忆后端——2026-08-27 由 [决定：Models 与 Connections 设置页（唯一模型入口）](https://github.com/nineofoursyrup/Agent-Alfred/issues/29) 由「模型客户端」改为「工厂」，因为 SDK 客户端在构造时就捕获密钥），且同一份业务代码能被 `ScriptedModel` 驱动。
- **红线**：不得用静态假数据冒充已连接服务或成功执行结果；一切外部能力如实显示 `未配置 / 已配置未测试 / 已连接 / 错误` 四态。"功能尚未实现" **不是**这四态之一，不得混用。
- 每个 session 先读本地图的「决策记录」，再按需 zoom 具体票；不要一次拉全部票的正文。

### 建图访谈已锁定的前提（不是票，是这张图的地基）

1. **适配器两个，`style` 挂 model。**（2026-08-26 由 [决定：内部消息、工具调用与模型响应协议的形状](https://github.com/nineofoursyrup/Agent-Alfred/issues/2) 重写，取代建图访谈原文。）只写 `AnthropicAdapter`（原生 messages）+ `OpenAICompatibleAdapter` 两个类，加供应商仍是**配置表加一行**、不写代码——但配置行的形状是 `{name, base_url, api_key_env, catalog_url?, models: {<id>: {style, path}}}`：**`style` 挂在 model 上而非 provider 上**（调研 #7 实测 OpenCode Go 同一个 base 下按模型分派三种 wire 形状）。第三种形状 OpenAI `/responses` **v1 明确出局**（见 Out of scope），落在它上面的模型标 `unsupported` 并给出机器可读原因。判断不出 style 的模型落 `unknown`（默认态），须用户手选 style 后方可使用——**但手选不改变支持三态**（2026-08-27 由 [决定：Models 与 Connections 设置页（唯一模型入口）](https://github.com/nineofoursyrup/Agent-Alfred/issues/29) 补充：手选写 `wire_style_override` / `wire_style_source=user_declared`，支持态仍是 `unknown`，见 ADR-0021。配置表行另增可选 `auth_probe` 声明）。**模型支持三态与 provider 连接四态正交**，两个维度分开渲染。
2. **模型配置只在 Dashboard 设置页**完成（不是 .env、不是 CLI 向导）。两页分界由 [决定：Models 与 Connections 设置页（唯一模型入口）](https://github.com/nineofoursyrup/Agent-Alfred/issues/29) 定死：**Connections 只读**（内置端点、密钥状态、四态与两个探针），Models 独占模型的选择与用法。主模型由用户指定；检索门小模型不指定时**回落到主模型**。密钥仍只从环境 / `.env` 读，界面只显示末四位。
3. **模型目录在线拉取**：`catalog_url` 或 `{base_url}/models`，携带该 provider 的 key，10s 超时；进程内缓存，成功 5 分钟 / 失败约 1 分钟；返回含 `pricing.prompt` / `pricing.completion` 时写进程内价格缓存。**拉取失败不影响聊天**，退回内置默认模型并展示错误原因。唯一持久化的本地文件只保存**用户钉选的模型**（[#29](https://github.com/nineofoursyrup/Agent-Alfred/issues/29) 细化为：`schema_version` + `revision` + 钉选记录 + 顶层 `assignments`，记录只存用户事实、不复制会过期的目录值），目录缓存与价格缓存重启即失。
4. **价格链**（按序，2026-08-26 经 [决定：内部消息、工具调用与模型响应协议的形状](https://github.com/nineofoursyrup/Agent-Alfred/issues/2) 修订；**2026-08-27 经 [决定：Models 与 Connections 设置页（唯一模型入口）](https://github.com/nineofoursyrup/Agent-Alfred/issues/29) 全面改名并逐维化**）：`endpoint_reported`（原 `provider_reported`；端点自报实付，调研 #7 实测 xAI 直接返回 `cost_in_usd_ticks`）> `user_override`（**本次新增**：用户手填价，位置必须在目录价之上，否则不叫覆盖）> `catalog`（在线目录缓存）> `free_rule`（免费模型规则）> `model_static`（模型级静态价）> `endpoint_static`（原 `provider_static`；端点级估算价——同一主体的两个 base 价格不同，调研 #8 已证伪 provider 心智模型）> **未知**。~~未知 Provider 默认 $3 / $15~~ **本次废除**——凭空猜出的数字混进「估算」比诚实地说「未知」更有害。费用三态改为**闭合判别式**（[#29](https://github.com/nineofoursyrup/Agent-Alfred/issues/29)）：有效 `endpoint_reported_cost` 存在 → `exact`，且**不再生成任何估算分项**；否则任一必要 Token 为 `None`、或某维 Token > 0 却无该维价格 → 整笔 `unknown`，**不出任何金额**，只单列调用次数与 Token 量；其余 `estimated`。`price_source` **挂分项不挂整笔**——新增 `price_components` 逐维映射（`tokens` / `unit_price` / `source` / `amount` + `stale` 等来源元数据），顶层「同档 / 混合」只是渲染派生值。`price_override` 四维可选，缺省 = 沿链下查、显式 `0` = 声明免费。订阅制（OpenCode Go）**照样按 token 计费**。未知模型**不得**按零费用或均价处理。Token 永远是唯一事实来源，费用在渲染层算。
5. **`web_search` 用 Tavily**，默认未配置，并作为「可选集成注册表」的第一个真实样例。（2026-08-27 由 [决定：Tool 协议、错误回喂与副作用 opt-in](https://github.com/nineofoursyrup/Agent-Alfred/issues/4) 定案，取代建图访谈原文的「六要素」。）注册表只存**不可推导的五项**：字段 / 是否密钥 / 依赖 extra / 健康检查 / 重载范围；「启用条件」完全由「字段 + 是否密钥」决定，**是可推导项，不单独存储**。**`reload_scope` 下沉到字段粒度**——同一个集成会同时踩中「热更新」与「重建 Agent」两档。另加一条正交维度：注册表管的是**可用性**（配齐没有），「准不准动外部世界」由独立的**授权三态**管，见 ADR-0005。

## 决策记录

<!-- issue-25-graph-delivery-c3 -->
- **#25 Graph 引擎已交付（2026-09-13）**：[PR #60](https://github.com/nineofoursyrup/Agent-Alfred/pull/60)，merge `5d16e21d876eb67994bb3a3b5d6c121689a10652`；GRAPH-SPEC-r1 / graph-r1-c3，16 CE、本地门禁及独立双轴通过，#25 CLOSED / COMPLETED。[完整验收与范围排除](https://github.com/nineofoursyrup/Agent-Alfred/issues/25#issuecomment-5653901873)。Linux PR/合并后 CI 均 NOT RUN（用户明确豁免）；Host选图UI与业务workflow继续留给后续消费者。

<!-- 一行一张已关闭的票：gist + 链接。详情永远在票里，本地图只做索引。 -->

- [调研：Anthropic 原生与 OpenAI-compatible 的工具调用与流式差异](https://github.com/nineofoursyrup/Agent-Alfred/issues/7)：内部协议**以 Anthropic 内容块为基线**，OpenAI 侧当有损投影；工具结果必须建模成 **batch**（Anthropic 一条 user 消息装全部 `tool_result`）；**缓存 token 口径三家互不相同**，照抄公式必然算错费用；流式不完整的唯一可靠信号是缺 `message_stop` / `[DONE]`，部分 `arguments` 不得 best-effort 补全后执行。→ `docs/research/provider-protocol-diff.md`
- [调研：MCP stdio 服务器接入与子进程生命周期](https://github.com/nineofoursyrup/Agent-Alfred/issues/9)：**自写约 150 行 stdlib 客户端，弃用官方 SDK**（实测 28 包 / 28 MB，其中约 21 MB 与 stdio 客户端无关）；MCP 已分裂为 legacy / modern 两个世代，v1 只做 legacy 并给 `_meta` 留空位；**工具名的真实约束来自模型厂商而非 MCP 规范**（Anthropic 与 OpenAI 同为 `^[a-zA-Z0-9_-]{1,64}$`，规范允许的点号会 400）；子进程清理必须 `start_new_session=True` + `os.killpg()`。→ `docs/research/mcp-bridge.md`
- [调研：Tavily API 与可选集成注册表的六要素映射](https://github.com/nineofoursyrup/Agent-Alfred/issues/10)：Tavily 单密钥零依赖直连（不用 SDK，四态信息全在原始状态码里）；健康检查用免费 `GET /usage`，但限 10 次 / 10 分钟；**`reload_scope` 必须挂在字段粒度而非集成粒度**——同一个集成会同时踩中「热更新」与「重建 Agent」两档。→ `docs/research/tavily-integration.md`
- [调研：models.dev 目录结构与内置静态价表的生成方案](https://github.com/nineofoursyrup/Agent-Alfred/issues/8)：models.dev 是 MIT，可随包分发，裁剪后 17 KB / 123 模型；**`opencode` 与 `opencode-go` 有 6 处价格不一致**，价格查表键必须是 `(provider_id, model_id)` 二元组；407 个模型有阶梯价，只存基础档并打 `tiered` 标记。→ `docs/research/model-catalog-and-prices.md` + `docs/research/gen_prices_draft.py`
- [决定：内部消息、工具调用与模型响应协议的形状](https://github.com/nineofoursyrup/Agent-Alfred/issues/2)：**统一富内容块模型**（无独立结果批类型，Anthropic 的「结果必须排在文本前」由校验构造器变成结构上不可违反）；**system 是请求级字段不是消息**，永不入历史；**唯一接口 `respond()`，三层同签名** `Loop → RetryPolicy(StreamFallback(Adapter))`，Adapter 不含时间相关行为；**返回 `ModelResult`**（`response` 与 `final_error` 二选一，`attempts` 恒非空且是账本的唯一来源——事件只是通知，因为 `observer` 可以是 None）；作废 attempt **不入历史但照常记账**；usage **两个缓存口径都存、禁止互推**，缺明细时留 `None` 不填 total。词汇定为 **Run / Step / Attempt**，`max_iterations` → **`max_steps`**。→ `CONTEXT.md` + ADR-0001（全同步）+ ADR-0002（富内容块基线）
- [决定：Observer 事件协议](https://github.com/nineofoursyrup/Agent-Alfred/issues/3)：**单一有序事件通道**，闭合 tagged union 七族 + 信封（`seq` 进程内全局单调 + `process_instance_id`，排序只认 `seq` 不认墙钟）；两个正交的量分名——事件级 **`trace_policy: transient|persist`**（`block.*` 全族 transient，故 trace 里没有流式增量，内容审计由 `attempt.committed/aborted` 的**块快照**承载）与 Sink 级 **`flush_at_run_end`**；**中央 fail-closed Redactor 钉在 `FanOutSink` 入口**（值匹配 + 字段名，缺一不可）；发射全程异步，唯独 **Run 收尾同步等关键 Sink 的 `FlushResult`**，与 telemetry、回复原子同事务落库，`trace_incomplete` 由此获得精确定义；账本仍只由 `ModelResult.attempts` 汇总，绝不从可缺席的事件流反推。→ `CONTEXT.md`「追踪与事件」+ ADR-0003（中央脱敏）+ ADR-0004（持久性屏障）

- [决定：Tool 协议、错误回喂与副作用 opt-in](https://github.com/nineofoursyrup/Agent-Alfred/issues/4)：**可用性与授权是两个正交维度**（配齐没有 vs 准不准动外部世界，后者三态 `unset|allowed|denied` 存用户配置、不存 `Tool` 上），暴露矩阵有一处刻意的不对称——`未配置+unset` 暴露**引导 Schema**（参数置空、如实说明不会执行），`已配置+unset` 隐藏；**`timeout_s` 正名 `budget_s`，预算只在启动前生效**，拒绝 `ThreadPoolExecutor` 伪造的「可取消超时」（那只是放弃等待，遗留线程照样写库），真正中止归工具 IO 层，v1 全部工具**串行**（消解 ADR-0001 的自相矛盾）；`ToolSuccess | ToolFailure` 闭合联合 + **七个闭合错误码**，回喂格式为「首行机器可读 JSON + 人类说明」，与 `is_error` 的冗余是有意的（OpenAI 侧投影会丢 flag）；**工具账 `tool_ledger` 是证据不是锁**——`local_write` 同事务落账、`external` 走 `started→succeeded/failed/unknown` 状态机；结果存**两份投影**（`model_content` 截断回喂 / `audit_content` 完整审计），Registry 不碰工件；**不引入 `jsonschema`**（实测 5 包 16 MB），内置走版本化子集校验、MCP 入参透传给服务端。→ `CONTEXT.md`「工具」+ ADR-0005（可用性与授权正交）+ ADR-0006（预算不是超时）+ 回写修订 [#3](https://github.com/nineofoursyrup/Agent-Alfred/issues/3)

- [决定：三类记忆的后端 Protocol](https://github.com/nineofoursyrup/Agent-Alfred/issues/5)：**程序性记忆不在可替换后端这一族里**——只有 `SemanticStore` / `EpisodicStore` 是记忆库，`SkillCatalog` 只有 `list()`/`load()` 且名称只经已构建索引解析（`load("../../.ssh/...")` 在形状上封死）；**检索只承诺顺序、不承诺可比的量**（FTS5 的 bm25 是负数、余弦是 0..1，一个 `score: float` 换后端即静默失效），且 `search` 只回后端自判相关的条目、可空可少于 limit，**存储故障必须抛出、不得吞成空结果**；去重分**机械幂等**（Store，规范化后的 `(subject, fact)`，NFKC+空白折叠止步于此并版本化）与**语义归并**（提炼器，取代走 `update` 不走 delete+save）两层且必须分名；**遗忘是真删**且被删正文与 subject **不进入任何下游**（不回喂、不进 trace、不投 SSE），只留 `MemoryId` 与 **HMAC-SHA256** 指纹（裸摘要对低熵短句可字典反推）；**本地事务归调用方**，`local_atomic` 的 Store 绝不自行 commit，`external` 走 `consolidation_batches` 状态机、全部确认成功才标记已提炼；情景是**半开区间**不是时刻，时间一律 tz-aware、比较基于绝对时刻。→ `CONTEXT.md`「记忆」+ ADR-0007（真实遗忘）+ ADR-0008（不暴露分数）+ ADR-0009（调用方拥有本地事务）

- [决定：Graph 节点与 state 协议](https://github.com/nineofoursyrup/Agent-Alfred/issues/6)：v1 是**确定性 DAG**（fan-out / fan-in / 条件边 / error 边，节点最多访问一次，**无回边、无 `max_visits`**）；**wave 是拓扑分层不是线程**——同层串行但共读本波开始时的不可变快照，写入缓冲、波末**整批提交或整波不生效**，配合**全局单写者**（含图输入）与控制流敏感的 definite-assignment 读校验；节点唯一签名「只读 state → `NodeOutcome`（只含 writes）」，四形态只是四个工厂，**路由是独立纯函数**；**引擎不存在按名字跳转的 Interface**，决议下沉到**边**（`taken`/`not_taken`），失败**不得冒充 skipped**；结果走**一条闭合联合**通道（`Completed` / `CompletedWithRecovery` / `NoAction` / `Failed` / `BudgetExhausted`），异常只留 `GraphInvariantError`；`builder.compile()` **启动期一次性冻结**，`describe()` 两层（topology 进 hash、presentation 不进）；事件扩为**九族**、信封加 `node_id`、`node.finished` **推迟到波提交后**、回滚发 `node.aborted`；`max_steps` 变**全 Run 共享的 `RunBudget`**（`StepLease` 扣 Step 不扣 Attempt、回滚不退、回退不重置）；**`fallback_safe` 砍掉**，回退只认副作用三态；**`agent_log` 写入权收归 Run finalizer 独占**，新增「运行转录」与「会话记录」分名。消息分流**默认关闭**。→ `CONTEXT.md`「图」+ ADR-0010（波快照与单写者）+ ADR-0011（能力缩减不是沙箱）+ ADR-0012（共享预算不重置）+ 回写修订 [#3](https://github.com/nineofoursyrup/Agent-Alfred/issues/3) / [#13](https://github.com/nineofoursyrup/Agent-Alfred/issues/13) / [#16](https://github.com/nineofoursyrup/Agent-Alfred/issues/16)

- [实现：仓库骨架（pyproject / uv / Ruff / 包边界 / .env.example / CI）](https://github.com/nineofoursyrup/Agent-Alfred/issues/11)：**首个 commit 与四道门禁里的前三道落地**。核心依赖只有 `anthropic` / `openai` / `python-dotenv` / `rich`，Ruff 钉死 `0.16.4`，extras `mcp` / `search` 是**空占位**（v1 走 stdlib，注释写明装了不带依赖）。**「需密钥测试不得默认必需」做成机制而非目录约定**——`requires_key` marker + `addopts = -m "not requires_key"` + `evals/conftest.py` 缺变量时 skip 而非 fail，`testpaths` 只作第二道防线。CI 用 `uv sync --extra dev --locked` 把 `uv.lock` 纳入约束。经三轮独立 code-review：最值得记的是 `.github/workflows/` 一度有 `ci.yaml` 与 `ci.yml` 两份逐字节相同的文件，**每次 push 跑两遍 CI 且已开始漂移**（加 `--locked` 时只改了一份），是「为了让 run 出现」引入的，靠独立评审才发现。→ 绿 CI [run 33008325491](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/33008325491)，HEAD `4b2c960`

- [决定：Dashboard HTTP+SSE 骨架与断线续传](https://github.com/nineofoursyrup/Agent-Alfred/issues/23)：**单进程 `RuntimeHost`** 独占 `process_instance_id` / `seq` / `FanOutSink` / 唯一写连接 / `ReplayRing` / `RunCoordinator`，状态目录进程锁 + 固定端口 `7717` + 原子入口描述（**绝不自动换端口**）；底座是 stdlib `ThreadingHTTPServer` 线程 per 连接；**重放环只装 `replayable` 事件**——该谓词与 `trace_policy` **分名**（今天同真是巧合），元素是**逻辑事件**、驱逐原子，故「半个事件」无法表达，另留单调 `replay_floor_seq`；**`id:` 只出现在「可重放且已完整」的边界**，且因 SSE 规范 dispatch 先赋值再判空、新流缓冲初始为空，响应体**必须以无数据 `id:` 帧重新种入游标**；游标是**曾签发的完整事件检查点**而非区间内任意数，非法闭合四因（`malformed`/`instance_mismatch`/`too_old`/`ahead`），`current_run_state` 三态（`unrecoverable` ≠ `absent`）；**活跃 Run 的唯一重放源就是环**（回复与 telemetry 要到屏障才落库）；两级队列 + **可重放帧必先原子入环再投 ingress**（先排队后入环会让恢复源在拥塞时失去权威性），三处均**帧数 + 编码字节双重计额**；**发布两阶段**——准备在锁外纯函数、提交在短临界区定量，`seq` 由此定义为**发布线性化顺序**，不可读作因果先后；`FlushResult` 拆成闭合联合，`SSESink` 在类型层面说不出 `flushed`；传输层通知**不占 `seq`**（占了会让别的连接凭空缺号）。→ `CONTEXT.md`「追踪与事件」修订 + 新增「入口与并发」+ ADR-0013（重放恢复事实不恢复呈现）+ ADR-0014（威胁模型：防网页来源不防本地进程）+ ADR-0015（锁外准备、锁内线性化）+ ADR-0016（409 绝不排队）+ 回写修订 [#3](https://github.com/nineofoursyrup/Agent-Alfred/issues/3) 三处

- [决定：trace 文件布局、切分与保留策略](https://github.com/nineofoursyrup/Agent-Alfred/issues/22)：本票**取得状态目录布局的所有权**（此前 [#12](https://github.com/nineofoursyrup/Agent-Alfred/issues/12) 与 [#23](https://github.com/nineofoursyrup/Agent-Alfred/issues/23) 都明确排除，无票拥有）：根 `~/.agent_alfred/`，仅由**绝对路径**的 `AGENT_ALFRED_HOME` 覆盖、不探测 XDG，顶层是**当前保留项**而非封闭清单。**Run bundle 是 trace 的原子单位**——理由是 Run 完整性、工件生命周期与导出三条边界**同构**（不是 `rmtree` 原子，它不是；也不是防交错，`RunCoordinator` 已保证），于是「轮转不得发生在屏障中间」结构上不可违反；bundle 经 **staging 目录 + 同日期内无覆盖原子改名**发布，堵掉「有目录无 meta」的永久残骸。**不透明标识不进路径**：`run_id` 格式无契约，目录名用 `run_dir_name = <HHMMSS>Z-<run_storage_id>`（SHA-256 截 128 bit，不用 HMAC），工件只用 `seq`（`tool_call_id` 是模型厂商给的不透明值），指针一律相对 bundle 根且解析必须拒绝绝对路径 / `..` / 链接 / 越界。元数据落**同级 `meta.json`** 而非 trace 头行——「无 `seq` 即元数据」的判别式会把**崩溃截断的坏行**误判成元数据。**屏障之前没有任何持久性承诺**：逐条写 + 屏障单次 `fsync`，单 drain 线程只保证不交错、不保证行原子，读取器**只容忍唯一一条末尾截断行、任何内部坏行一律失败**；写失败在有限重试后的首个不可恢复错误处**熔断到 Run 粒度**，残留必为无空洞的连续前缀。阀门：单 bundle **512 MiB**、准入要 `max(1 GiB, 5%) + 512 MiB`、运行中每约 16 MiB 或写大工件前复查，撞线前拒整条记录、**绝不中止业务 Run**。保留 **90 天 + 2 GiB**（可配置默认值，各自以 `0` 禁用），**`run.finished` 只决定完整性、不决定能否到期裁剪**（否则崩溃残骸永久堆积）；租约与 `deleting` 预约在**同一把进程锁**下线性化，先落 pending 才允许删除。**裁剪是独立生命周期事实**，进独立 `trace_prunes` 表、不改写 telemetry 也不动 `trace_incomplete`。`usage.jsonl` 降为**显式派生导出**，SQLite telemetry 是唯一持久事实源。顺带纠正 [#3](https://github.com/nineofoursyrup/Agent-Alfred/issues/3) §12「文件与**目录**强制 `0600`」（目录不可遍历）为**托管目录 `0700` / 托管文件 `0600`**。→ `CONTEXT.md`「trace 的落盘与保留」+「派生导出」+ ADR-0017（Run bundle 与原子发布）+ ADR-0018（派生存储标识）+ ADR-0019（屏障前无持久性承诺）+ ADR-0020（裁剪是生命周期事实）+ 回写修订 [#3](https://github.com/nineofoursyrup/Agent-Alfred/issues/3) / [#12](https://github.com/nineofoursyrup/Agent-Alfred/issues/12)

- [决定：Models 与 Connections 设置页（唯一模型入口）](https://github.com/nineofoursyrup/Agent-Alfred/issues/29)：**Connections 只读**（内置端点、密钥状态、四态、两个探针），Models 独占模型的选择与用法——「配置表加一行」是开发者机制不是用户数据，自定义端点因此**出 v1 范围**。**钉选是持久化记录不是收藏夹**，键 `(endpoint_id, model_id)`，只存用户事实、不复制会过期的目录值，于是「目录拉不到照样能聊天」是结构结果而非特判。**连接四态是不落盘的观测**（随附 `checked_at` / `checked_via` / 机器可读原因，重启与配置变更一律打回「已配置未测试」），**目录健康另立一维**（`unfetched/fresh/stale/unavailable`）——`catalog_url` 可以不是推理端点，故目录拉取**永不**推动四态，哪怕请求与凭据探针逐字相同（按动作语义分离比按 URL 偶合推断可预测）。**用户声明不是系统验证**：手选 style 后支持态仍是 `unknown`，`support_basis` 只记系统依据；`assignable = pinned && (supported || (unknown && style ∈ {anthropic, openai}))`，**连接观测不参与指派**（否则每次重启后全体模型集体不可指派）。密钥被移除或模型翻 `unsupported` 只改变运行事实、**保留指派**，下次发送如实失败为 `endpoint_unconfigured` / `model_unsupported`，**绝不自动换模型**。两个探针分开：**凭据探针**须由配置表冻结成五项齐全的可执行契约、无声明就明示「无免费认证探针」（目录 200 可能只是匿名可读）；**推理探针**挂在**具体模型行**上（两个指派位可能同端点，「该端点的模型」不唯一），是走全局准入、记正常 Attempt/usage/trace 与 Run 级 `purpose` 但**不写会话消息**的独立 Run。目录只自动拉**当前指派端点**、其余懒加载，**绝不打开一页就并发拉全部**；stale 目录价仍作 `estimated` 但须带 `catalog_fetched_at` + `stale=true`（带时间与降级标记的旧观测强于更旧的静态表或「未知」）。设置写入用**双重冲突判据**（`expected_revision` 防陈旧标签页 + 加载时 `sha256` 防进程外编辑，分因 `stale_revision` / `external_change`，一个字节不写），落盘顺序是先持久化再发布内存快照；该文件**不是受支持的人工编辑接口**。价格全面逐维化并肃清 `provider` 词汇。→ `CONTEXT.md`「模型接入」+「用量与费用」修订 + ADR-0021（指派与证据语义）+ ADR-0022（双重冲突判据）+ 回写修订 [#13](https://github.com/nineofoursyrup/Agent-Alfred/issues/13) / [#14](https://github.com/nineofoursyrup/Agent-Alfred/issues/14) / [#15](https://github.com/nineofoursyrup/Agent-Alfred/issues/15) + graduate 出 [实现：Models 与 Connections 设置页（唯一模型入口）](https://github.com/nineofoursyrup/Agent-Alfred/issues/34)

- [实现：SQLite schema 与幂等迁移](https://github.com/nineofoursyrup/Agent-Alfred/issues/12)：**三个提交各把一个不同的 schema 盖成了 version 1**——`IF NOT EXISTS` 配上只写不读的版本列，让本票最核心的验收项「迁移跑两遍结果一致」变成恒真命题，两轮独立两轴评审才刨出来。收口方式：v1 的 DDL **冻结**在最后真正发布过的形状（`6d7659c`），改动过的两条语句改为逐字字面量，其余二十条仍插值闭合集常量（闭合集单源是上一轮的硬要求，全部字面化会让常量失去 SQL 侧消费者），唯一的误改路径——给某个闭合集加值——由一条冻结测试兜住；形状再要变一律新开迁移号。v2 是**修复迁移**：认形状 → 逐对象比对归一化 DDL → 行级可转换性检查，三关全过才动破坏性 DDL，不认识的形状**在第一条 DDL 之前**停下并给出备份与处置建议，「静默跳过」与「先删了再说」两个都不选。**调用方已 `BEGIN` 时，全部 pending migrations 包进一个 SAVEPOINT**——失败只回滚迁移、保留调用方在途写入，这是 ADR-0009「调用方拥有本地事务」在迁移这一侧的落点。**版本账只追加自己那一行**，不回头改写前几版留下的时刻：那些行是既成记录，重述别人写下的时刻是没人要求过的写入。时区列存 IANA 名不存偏移、不从 naive 值反猜本地时区（[#5](https://github.com/nineofoursyrup/Agent-Alfred/issues/5) §7），CHECK 明确降级为**形状提示**（`Narnia` 这类虚构名照样入库，这条边界写死成测试而不是注释）。最值得记的教训与 [#11](https://github.com/nineofoursyrup/Agent-Alfred/issues/11) 的双份 CI 同源：**在 SQL 里写下看起来像保证的东西——版本列、外键、CHECK、测试名——但没有任何一条路径真正执行它，Ruff 与 pytest 一条都查不出来**，绿色的 51 passed 与它们完全兼容；两轮打回的十三条几乎全属这一类，连新写的 fixture 自称「逐字节捕获」都在复核时被实测出五条索引不符。→ `CONTEXT.md`「schema 与迁移」+「日程」词条的 UTC 排序约束，历史交付提交 `0cfe017`（已核实包含于当前 `main`），后续结构整理已交付，见 [结构：收拢 schema 模块的六项结构气味](https://github.com/nineofoursyrup/Agent-Alfred/issues/35)

- [决定：切片④a — 看见对话（MainBar 壳层 + Gateway 收件箱 + Loop 逐轮页）](https://github.com/nineofoursyrup/Agent-Alfred/issues/30)：**页名换轴**：导航「运行」、页标题「运行详情」，用户文案停用「逐轮」与「Loop」——该页也收系统 Run，而系统 Run 根本不经过对话循环。**MainBar 是壳层不计页**，且是当前会话的唯一主对话，运行详情只呈过程证据、不复制第二份正式回答；**Session 是服务端签发的持久身份、按标签页隔离**，进程内不设「全局活动会话」（参考实现的全局可变活动会话、打开收件箱即切会话都不能套用），失效时保留草稿、绝不静默改投默认会话。**Run 状态分两轴**——`phase=accepted|running|finished` 与 `outcome=null|completed|max_steps|failed|interrupted`，塞进一列就再也分不出「没跑完」与「跑完了但确认不了」；**`interrupted` 是认识论结论不是死法**（持久索引无法证明业务终态，**不得借 trace 猜测**，旧进程展示过的 `completed` 重启后不是事实）。**又添两个单调整数且三者正交**：`seq`（发布线性化）/ `state_revision`（进程内快照版本）/ `activity_revision`（持久状态变化顺序，单行持久时钟事务内分配，已终态集合不再移动，故 keyset 续页无须伪造快照；trace 裁剪与连接通知都不推动它），**禁止比较或互推**。**SSE 顶层闭合三类载荷**：`domain_event` 用 `seq`，`transport_notice` 与 `state_patch` 都不占 `seq`（占了会让别的连接凭空缺号），后者是绝对替换、自带 `state_revision`，服务端先更新权威快照再投递、投递不可靠即断开。**只有数据库收尾事务提交可以确认 `recorded`**——追踪刷写、`trace_policy=persist`、SSE 游标与重放环一概不作数，于是「回复完成 · 未保存」成为必须说得出口的合法状态；记录失败后以 `503 recording_unavailable` **关闭准入**，保住那个有界的 `unrecorded_terminal_projection` 单槽。v3 前向迁移新增 `sessions` 与最小 Run 索引，`runs.telemetry` 独占新 Run 的 Run 级遥测、`agent_log.run_id` 可空且有 run_id 者不得再携 telemetry。施工所有权三分：v3 迁移 / 活动时钟 / 准入与启动恢复 / finalizer 归 [#13](https://github.com/nineofoursyrup/Agent-Alfred/issues/13)，202/409/503 与三类载荷、原子快照、分页 API 归 [#28](https://github.com/nineofoursyrup/Agent-Alfred/issues/28)，三个界面层 graduate 出 [实现：切片④a — 看见对话](https://github.com/nineofoursyrup/Agent-Alfred/issues/36)。**[补正裁决](https://github.com/nineofoursyrup/Agent-Alfred/issues/30#issuecomment-5444200695)**（冻结后补三个缺口）：原裁决对 `pending` 期间的准入沉默，于是「不得覆盖有界单槽」没有执行路径——补正为 **`run.finished` 只推进到 `recording_pending`、准入租约持有到记录落定**，期间第二次提交返回**现有的 409**（阶段显示「正在保存」，不新增错误码），成功则**先更新快照为 recorded 再释放租约**、失败则**先进 `recording_failed` 再开始返回 503**，两处次序均不可交换，状态机由此可执行：`idle → accepted → running → recording_pending → idle | recording_failed`；`recording_pending`/`recording_failed` 是**协调器状态**，与 Run 的 `recording_state` 分名。**v3 必须逐字回填历史 Session**——按 `agent_log` 里每个不同的 `session_id` **原值**一对一插行，「仅服务端生成」是**创建 API 规则而非数据库格式约束**，`created_at` **逐字复制最小 `agent_log.id` 那行的时间文本**（不对从未受格式约束的时间文本取 MIN 或做字典序猜测），`activity_revision` 按各 Session 的最大消息 id 从旧到新依次分配、重跑不再分配。补正过程中连带暴露**第三个缺口**：回填出的旧 Session 点开是空的（旧消息无 `run_id`，而分页按 Run），于是**旧消息走第二段游标**（第一段 `(activity_revision, run_id)` 耗尽后按 `agent_log.id` 取 `run_id IS NULL`），**绝不伪造合成 Run**——往 Run 索引里塞从未发生的 Run，等于往 ADR-0023 唯一有资格证明终态的证人嘴里塞话；无 Run 的历史 Session 标题从首条旧用户消息脱敏限长派生。参考实现「同一互斥锁包住执行与提交直至返回」可借鉴（租约即是），但它把 Session 仅当消息标签、无权威表故无回填问题，这一条不可照搬。→ `CONTEXT.md` 新增「Run 的生命周期与记录」+「追踪与事件」「入口与并发」「三级计数」修订 + ADR-0023（interrupted 是索引证明不了终态）+ ADR-0024（只有收尾事务确认 recorded）+ ADR-0025（状态补丁自带 revision 不占 seq）+ ADR-0026（准入租约持有到记录落定）+ ADR-0027（历史 Session 逐字回填、旧消息不伪造 Run）+ 回写修订 [#13](https://github.com/nineofoursyrup/Agent-Alfred/issues/13) / [#28](https://github.com/nineofoursyrup/Agent-Alfred/issues/28) / [#36](https://github.com/nineofoursyrup/Agent-Alfred/issues/36)

- **看见记忆已裁决**：[完整规范与实施归属](https://github.com/nineofoursyrup/Agent-Alfred/issues/31#issuecomment-5588135839)。明确保存→下一Run检索与来源→删除闭环；冻结gate事件、共享命令、人工保护及受管副本遗忘边界，原始会话/既有trace仍可人工查看。施工复用检索、工具、窗口与提炼票，补遗忘核心和Memory页；决策完成不代表实施完成。

- [决定：切片④c — 调用工具并看账（Tools 页 + Ops 账本页）](https://github.com/nineofoursyrup/Agent-Alfred/issues/32#issuecomment-5647315861)：已冻结真实工具授权、独立持久计量、完整 Run 不可变账目快照与经校验的人工历史；对应实现已完成验收并合并，规范与交付证据见本票结论。


- **程序性记忆已实现并验收**：[#24 SKILL-SPEC-r1](https://github.com/nineofoursyrup/Agent-Alfred/issues/24#issuecomment-5654273925) 的 Q1–Q28 / CE-01–CE-30 已完成；自动/指定/禁用 Skill、每 Run 固定快照、普通循环与聊天 Graph 共享预算和截止时间已接通。[PR #61](https://github.com/nineofoursyrup/Agent-Alfred/pull/61) 已合并，PR CI 与实际合并提交 CI 均通过；[完整验收与交付证据](https://github.com/nineofoursyrup/Agent-Alfred/issues/24#issuecomment-5655138173)。#24 已 CLOSED / COMPLETED；本结论不代替 #26/#27、选图 UI 或业务 workflow 的独立交付。<!-- issue-24-delivery:skill-c3-e81bac5ede2c -->

<!-- issue-27-delivery:aggregation-c10-a29f39ed9d08 -->
- **手动聚合已实现并验收（#27，2026-09-15）**：Behaviour / CLI 的固定目标会话与三来源聚合、零资料零模型请求、一次 primary 起草、失败不回退、可恢复草稿及遗忘保护已交付；草稿不自动进入工作窗口、聚合历史或提炼。[PR #63](https://github.com/nineofoursyrup/Agent-Alfred/pull/63) 已合并（`e52d83290e0d5690025e5f907760a8c3592065ee`），AGGREGATION-SPEC-r1 / aggregation-c10，独立双轴、16 CE、PR-head 与实际merge-SHA Ubuntu CI 全部通过，#27 **CLOSED / COMPLETED**。[完整验收、两阶段CI与范围边界](https://github.com/nineofoursyrup/Agent-Alfred/issues/27#issuecomment-5670564906)。付费模型语义非默认门禁，本次NOT RUN；不代替选图UI、更多来源、自动工作流或相邻未交付范围。

<!-- database-decision-precheck:2026-09-16 -->
- [实现：workflow — 消息分流](https://github.com/nineofoursyrup/Agent-Alfred/issues/26#issuecomment-5662264771)：Behaviour 的默认关闭消息分流开关及 CLI/Web 共享路径已实现并验收；手动聚合的交付见本节对应索引，不扩大为全部 Behaviour / workflow 范围已完成。
- [实现：切片③ — ToolRegistry 与内置工具](https://github.com/nineofoursyrup/Agent-Alfred/issues/19#issuecomment-5612160788)：受管人格位置、显式文件覆盖、版本核对及下一 Run 生效已实现并验收；[实现：会话与工作记忆](https://github.com/nineofoursyrup/Agent-Alfred/issues/16#issuecomment-5614466540)保持该既有契约。
- [实现：记忆提炼与 Markdown 镜像](https://github.com/nineofoursyrup/Agent-Alfred/issues/18#issuecomment-5630462183)：状态根下 `memory/facts.md` 与 `memory/episodes.md` 的单向当前镜像、外部编辑冲突及删除恢复已实现并验收。

<!-- database-decision-resolution:r1 -->
- [决定：Database 只读 SQL 控制台的用户路径与验收](https://github.com/nineofoursyrup/Agent-Alfred/issues/65#issuecomment-5686473812)：已冻结固定实例的受保护只读 SQL 诊断路径、结果边界、取消／遗忘联动及完整验收；后续由独立实现票承接，决策完成不代表产品已实现。

<!-- issue-66-delivery:issue66-successor-5828ca8ff1db -->
- **Database 只读 SQL 控制台已实现并验收（#66，2026-09-19）**：按 [#65 Resolution r1](https://github.com/nineofoursyrup/Agent-Alfred/issues/65#issuecomment-5686473812) 交付固定 21 对象的受保护只读诊断、结果边界、取消/遗忘联动与安全生命周期；真实隐藏标签和 Safari BFCache 验收通过。[PR #67](https://github.com/nineofoursyrup/Agent-Alfred/pull/67) / [修复 PR #68](https://github.com/nineofoursyrup/Agent-Alfred/pull/68) 已合并，最终 merge `2533d6818c4b6a896f460b20d5e0fe40142d335e`；独立双轴 PASS，最终 PR-head / merge-SHA CI 均通过，#66 **CLOSED / COMPLETED**。[逐 AC 验收、两阶段 CI 和范围边界](https://github.com/nineofoursyrup/Agent-Alfred/issues/66#issuecomment-5741333678)。不扩大为相邻 Memory 观察项、部署或 PyPI 发布已完成。

<!-- trace-export-decision-resolution:r1 -->
- [决定：trace 导出的完整用户路径与验收](https://github.com/nineofoursyrup/Agent-Alfred/issues/69#issuecomment-5742037842)：TRACE-EXPORT-SPEC-r1 已冻结完整 Run 的两档净化导出、完整性/缺失声明、下载与遗忘清理边界，以及 AC-01–AC-30、CE-01–CE-12；[实现：trace 导出的完整用户路径](https://github.com/nineofoursyrup/Agent-Alfred/issues/70)已通过 [PR #77](https://github.com/nineofoursyrup/Agent-Alfred/pull/77) 交付，完整两档 ZIP、失效和可重试清理已实现；c9 独立双轴 PASS，AC01–30 / CE01–12 与 PR-head / merge-SHA Linux CI 均通过。#70 已 CLOSED/COMPLETED；[完整验收记录](https://github.com/nineofoursyrup/Agent-Alfred/issues/70#issuecomment-5744558623)。`requires_key` 默认排除，NOT RUN；不表示相邻 #76、部署或 PyPI 发布已完成。

<!-- behaviour-v1-scope-resolution:r1 -->
- [决定：Behaviour 页的 v1 剩余范围与验收](https://github.com/nineofoursyrup/Agent-Alfred/issues/71#issuecomment-5742523972)：两条现有业务 workflow 足够；页面组织与只读拓扑、单次 Run 路径、消息分流统计仍为 v1 必需范围，已由三张原生决策子票承接；本票只完成范围裁定，新增能力尚未实现或验收。

<!-- behaviour-topology-resolution:r1 -->
- [决定：Behaviour 页组织与只读流程拓扑的用户路径与验收](https://github.com/nineofoursyrup/Agent-Alfred/issues/72#issuecomment-5742990883)：Behaviour 双区块、真实 describe 只读拓扑、快照身份与手动刷新、错误恢复和既有表单兼容已冻结；BEHAVIOUR-TOPOLOGY-SPEC-r1 的 R-01–R-10、AC-01–AC-18、CE-01–CE-08 由[实现：Behaviour 页组织与只读流程拓扑的完整用户路径](https://github.com/nineofoursyrup/Agent-Alfred/issues/75)承接。设计和 #75 实现验收均已完成，交付证据见下方 #75 索引；#73 的设计前置已满足，不要求等待本实现完成。

<!-- routing-stats-resolution:r1 -->
- [决定：消息分流路由统计的口径与验收](https://github.com/nineofoursyrup/Agent-Alfred/issues/74#issuecomment-5743304454)：消息分流跨CLI/Web已准入聊天Run的路由决议、图前绕行/图后回退与上下文恢复统计已冻结；持久证据、各轴已知分母/覆盖、语义版本、历史未知和两秒一致快照查询由ROUTING-STATS-SPEC-r1的R-01–12、AC-01–AC-24、CE-01–CE-16约束。[实现：消息分流路由统计的完整用户路径](https://github.com/nineofoursyrup/Agent-Alfred/issues/76)承接；设计和 #76 实现验收均已完成，交付证据见下方 #76 索引；不证明模型准确率或收益。

<!-- issue-75-delivery:topology-c3-94ee41491ecf -->
- **Behaviour 页面组织与只读流程拓扑已实现并验收（#75，2026-09-20）**：两区块默认收起的真实 compiled describe 图、完整文字明细、快照身份/失效/恢复，以及既有设置草稿和聚合在途状态隔离已交付；鼠标选择与准入未确认状态覆盖问题经确定性回归和独立复审关闭。[PR #78](https://github.com/nineofoursyrup/Agent-Alfred/pull/78) 已合并，merge `3cd9e80d35b0030b0e4591b1d4ee53588aacdb9a`；BEHAVIOUR-TOPOLOGY-SPEC-r1 / topology-c3-94ee41491ecf，双轴 PASS、18 AC / 8 CE、PR-head 与实际 merge-SHA Ubuntu CI 均通过，#75 **CLOSED / COMPLETED**。[逐项验收、两阶段 CI 与范围边界](https://github.com/nineofoursyrup/Agent-Alfred/issues/75#issuecomment-5744243483)。本票不替代 #73 单次 Run 路径、#76 路由统计、#70 trace 导出或整个 Behaviour v1 的剩余交付；付费模型语义评估 NOT RUN（规范不要求）。

<!-- graph-run-evidence-resolution:r1 -->
- [决定：Graph 单次运行路径与证据的用户路径与验收 #73](https://github.com/nineofoursyrup/Agent-Alfred/issues/73#issuecomment-5744822747)：运行详情内的历史拓扑/实际路径、当前与历史手动快照、节点/边/wave 提交撤销、恢复与图外回退、持久性/裁剪/未知及刷新竞态已冻结；**GRAPH-RUN-EVIDENCE-SPEC-r1** 的 **R-01–R-10、AC-01–AC-22、CE-01–CE-12** 由[实现：Graph 单次运行路径与证据的完整用户路径 #81](https://github.com/nineofoursyrup/Agent-Alfred/issues/81)承接。#73 设计已完成，#81 ready-for-agent、未认领；新增产品实现/验证 NOT STARTED/NOT RUN，不表示 Behaviour v1 已全部交付。

<!-- routing-stats-delivery:c7 -->
- **消息分流路由统计已实现并验收（#76，2026-09-20）**：Behaviour 独立统计提供 24h/7d/30d/all、持久准入/收尾事实、语义版本分组、已知分母与 unknown 覆盖，以及两秒有界一致快照查询；R01–12、AC01–24、CE01–16 已核验。[PR #79](https://github.com/nineofoursyrup/Agent-Alfred/pull/79) 与修复 [PR #80](https://github.com/nineofoursyrup/Agent-Alfred/pull/80) 已合并，最终 merge `98c8afa19a3484f7fdd4108569d9a6847707f7a1`；候选 `routing-stats-c7-e223275b2c40` 双轴 PASS，[PR-head CI](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/35467898135) 与[实际 merge-SHA CI](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/35469068283) 均 SUCCESS（各 Python 4881、browser 281），#76 **CLOSED / COMPLETED**。[逐项验收与原始失败/修复证据](https://github.com/nineofoursyrup/Agent-Alfred/issues/76#issuecomment-5745402021)。独立 Linux 专项及付费/私人模型 NOT RUN，GitHub Ubuntu CI 已执行；不证明模型准确率或费用收益，不代替 #73/#81 或整个 Behaviour v1 的剩余交付。

- 2026-09-20：[#81](https://github.com/nineofoursyrup/Agent-Alfred/issues/81) 已完成并按 completed 关闭：Run 详情手动读取单次路径、历史捕获结构与 trace 保留、失败/裁剪/未知证据及公开 HTTP/browser 验收。最终 C6 双轴 PASS；[PR #82](https://github.com/nineofoursyrup/Agent-Alfred/pull/82) 合并为 `da5a9e3`，[PR-head CI](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/35477668650) 与 [merge-SHA CI](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/35478877496) 均 SUCCESS；完整 [22 AC / 12 CE 验收记录](https://github.com/nineofoursyrup/Agent-Alfred/issues/81#issuecomment-5746548404)。范围仅 #81。<!-- issue-81-delivery -->

## Not yet specified

以下都在终点范围**之内**，只是还不够锐利、开不出票。上游票关闭后graduate 成正式票。

- **剩余离线断言缺口的修复与分票**：阶段 A 将交付来源 → 已批准义务 → 现有测试/公共路径证据 → 缺口映射；待实际缺口明确，再决定哪些应留在原能力、哪些适合新票。不以测试总数或旧票关闭证明覆盖完整，也不按 brief 中“8 大类、30+ 条”的摘要数字编造原始断言。

- **`CONTEXT.md` 与首批 ADR**：随决策票关闭而逐步长出，不预先规划。

## Out of scope

- **通用选图执行、新增流程编辑、无明确场景的更多 workflow**（[决定：Behaviour 页的 v1 剩余范围与验收](https://github.com/nineofoursyrup/Agent-Alfred/issues/71#issuecomment-5742523972)裁定）：v1 的两条业务路径已足够，不为内核能力预设新的执行入口或编辑器；用户不能任意运行注册图、修改节点/边或分类规则。只读选择观察对象及既有开关、目标、来源等参数操作保留，不属于此排除。

- **多用户、远程访问、任何形式的鉴权**：单用户假设已写死。
- **语音渠道与消息渠道适配器**（Telegram / 微信 / 邮件等）：v1 只有 CLI 与 Web 两个 Gateway。brief 里它们本就是"可选适配器"，留接口位不做实现。
- **向量检索后端与托管记忆后端**：只定义 Protocol 并保证接口一致性测试，默认实现固定为 SQLite FTS5。
- **发布到公共 PyPI**：只验证 wheel/sdist 能在干净临时环境安装并运行。
- **OpenTelemetry 导出的实际接线**：brief 定为可选；v1 只保证"未安装依赖时提示并继续本地 JSONL"这条路径。
- **自定义 `ModelEndpoint`（用户在界面上增删端点）**（[决定：Models 与 Connections 设置页（唯一模型入口）](https://github.com/nineofoursyrup/Agent-Alfred/issues/29) 裁定）：Connections 页 v1 **只读**内置端点。「加一家 = 配置表加一行」说的是开发者的扩展机制，不是用户可编辑数据。任意 URL 的 SSRF 面、无密钥认证、端点身份与唯一性、被引用时的删除语义足以自成一条独立纵向切片，而六个里程碑没有一个依赖它。「本地优先」承诺的是**状态本地**，并不承诺 v1 本地推理——因此指不了本地 Ollama / LM Studio 是**已知且接受**的代价。
- **OpenAI `/responses` wire 形状**（[决定：内部消息、工具调用与模型响应协议的形状](https://github.com/nineofoursyrup/Agent-Alfred/issues/2) 裁定）：v1 只支持 `messages` 与 `chat/completions` 两种形状。落在 `/responses` 上的模型（grok、GPT 系列）在 Models 页如实标 `unsupported` + `unsupported_wire_style: responses`，不静默隐藏、不伪装成连接错误。v1 没有任何一张票需要这些模型；写第三个适配器只会让协议票与施工票一起膨胀。
















## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/1#issuecomment-5560219362

2026-09-06 用户批准的 #14 实施边界修订已同步 #14 与 #34：生产白名单零条；测试规则仅验证机制；OpenCode 鉴权按 messages/chat 路由区分；重启仅清内存覆盖而保留已落盘 trace。实施与验收尚未完成。详见这两票最新边界修订；不关闭任何票。
