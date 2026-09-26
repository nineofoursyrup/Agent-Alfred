# #13 实现：切片① — OpenCode Go 真模型走通最小循环与 CLI

来源：https://github.com/nineofoursyrup/Agent-Alfred/issues/13

## Question

切片①：用真实模型走通最小闭环——`Assistant.respond()` + 循环 + CLI 能聊天，并落库。

范围：

- provider 用 **OpenCode Go**：base `https://opencode.ai/zen/go/v1`，env `OPENCODE_API_KEY`，模型 `deepseek-v4-flash`（OpenAI 兼容）。
- `Assistant.respond(message, *, events: EventSink | None = None, source="cli", stream=False)` 的骨架：加载人格文件 + 当前本地时间 → （检索门此票先留桩）→ 取当前会话最近 N 轮 → 调模型 → 无工具时直接返回文本。
- `max_steps` / `max_tokens` / `overall_deadline` / `per_attempt_timeout` / 流式失败回退全部可配置，达到 `max_steps` 时给出**明确的受控失败消息**。
  - ⚠️ 配置项由 [#2](https://github.com/nineofoursyrup/Agent-Alfred/issues/2) 更名：`max_iterations` → **`max_steps`**（Step = 一次模型响应 + 随后的工具批；Attempt = 一次真实网络往返。`iteration` 夹在两者之间会被读成「包不包含重试」，而这个歧义直接关系到账单）。
  - 超时是**两个预算**：`overall_deadline`（覆盖全部重试与回退，`time.monotonic()` 绝对时刻）+ `per_attempt_timeout`，向 Adapter 传 `min(剩余 overall, per_attempt)`。
  - ⚠️ 参数由 [#3](https://github.com/nineofoursyrup/Agent-Alfred/issues/3) 更名：`observer` → **`events: EventSink | None`**。「Observer」已退出词汇表——事件消费端是**单向**的（`emit` 无返回值，业务读不回任何东西），`Observer` 这个 GoF 名字暗示它可以反应、可以介入。类型与参数名两层签名统一。
- 本票须发出 [#3](https://github.com/nineofoursyrup/Agent-Alfred/issues/3) 定的 `run.*` / `step.*` 事件，并在 `finally` 中发 `run.finished`（`outcome` 四值之一）；Run 收尾走**持久性屏障**（ADR-0004）：只等 `flush_at_run_end=True` 的 Sink，再把回复、Run 级 telemetry 与 `trace_incomplete` 原子写入同一事务。
- 用户消息、回复、来源、逐轮遥测写入 `agent_log`。
- ⚠️ 由 [#23](https://github.com/nineofoursyrup/Agent-Alfred/issues/23) 修订，三处：
  - **`EventSink` Protocol 改为准备 / 提交两阶段**（ADR-0015）：准备在锁外、纯函数、可并发；提交在 `FanOutSink` 的短临界区内、只做定量入队。`Event` 分裂为 `UnsequencedEvent` / `SequencedEvent` 两段冻结类型，`seq` 在提交时由 `FanOutSink` 分配，定义为**发布线性化顺序**（不是业务线程的发起时序，不可读作因果先后）。
  - **`FlushResult` 拆成闭合联合**：`BarrierFlushResult(flushed|failed)` | `BestEffortFlushResult(best_effort)`。持久性屏障成功的判据收紧为 **`flushed` 且 `dropped_events == 0`**；返回缺失、超时、异常或 `failed` 一律置 `trace_incomplete`。
  - **引入 `RuntimeHost` 与 `RunCoordinator`**：本票是它们的落位点。Host 独占 `process_instance_id` / `seq` 分配 / `FanOutSink` / 唯一数据库写连接；`RunCoordinator.submit()` 保证任意时刻至多一个 Run（CLI 与 Web 共用），忙时立即拒绝、绝不排队（ADR-0016）。

- CLI 终端富文本渲染。

### 强制验收项（建图时约定）

> 模型客户端必须是**通过构造函数注入的**，且**同一份循环代码**能被一个 `ScriptedModel`（返回预设响应）驱动，并有一个离线测试证明这一点。

### 其他验收

- 与真实 OpenCode Go 的一次真实对话截图 / 日志。
- 重启进程后会话历史仍在 SQLite 里。





---

⚠️ **由 [#29](https://github.com/nineofoursyrup/Agent-Alfred/issues/29) 修订，两处：**

- **强制验收项改写**：模型客户端必须通过注入的 **`ModelClientFactory`** 取得（离线工厂返回 `ScriptedModel`），而不是构造函数直接注入一个 `ModelClient` 实例。正确结构是 `RuntimeHost` 长期持有可注入的工厂与**按不可变配置版本管理的传输池**，Run 准入时一次性捕获 `assignments` + style + 凭据快照并取得对应客户端。理由：SDK 客户端在**构造时**捕获密钥，调研 [#10](https://github.com/nineofoursyrup/Agent-Alfred/issues/10) §9 那条「值只在请求发出的那一刻被读」是关于工具侧 `urllib` 直连的，不能照搬到模型侧。密钥轮换使该端点的连接观测、目录缓存与**旧客户端缓存**一并失效。
- **Run 增加 Run 级 `purpose`**（`chat` | `inference_probe`）。推理探针是一次真实计费的**独立 Run**：走同一个 `RunCoordinator` 准入，记正常 Attempt / usage / trace，但**不写会话消息**——「`agent_log` 写入权归 Run finalizer 独占」那条因此要按 `purpose` 分流。探针的凭据快照只存在于内存，**不进事件、不落盘**。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/13#issuecomment-5428675450

#11 骨架评审的 **D-2** 已裁定：包根的 **Settings / 依赖组装** 不在 #11 补空桩，明确记进本票（切片①走通循环与 CLI 时就要读配置、把依赖装起来）。

#12 承担 schema 与迁移。#11 不补 `settings.py` / `wiring.py` / `schema.py` 空文件。

## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/13#issuecomment-5429259343

## 回写修订（来自 [决定：Graph 节点与 state 协议](https://github.com/nineofoursyrup/Agent-Alfred/issues/6)）

两处改动，都落在本票的循环骨架里：

1. **`max_steps` 实现为 `RunBudget`，不是一个每条路径各拿一份的整数。**（ADR-0012）
   模型响应在**首个网络往返之前**通过 `RunBudget.reserve_step(node_id)` 原子取得一次性 `StepLease`，
   **无论成败都消耗一个 Step**；重试与流式回退**共享同一份租约**，各由自己的策略限次
   （即：**扣 Step，不扣 Attempt**——把重试算进 `max_steps` 会让网络一抖用户的轮数就凭空少一轮）。
   预算余额不足时**不发网络请求**，以专门的 `StepBudgetExceeded` 失败。
   本票现在只有一条消费路径，但接口形状必须现在就定成共享的——将来图、嵌套循环与回退的普通循环
   要共同持有同一份，事后改会让「上限被悄悄乘以路径数」在中间那段时间里成立。

2. **`agent_log` 的写入权收归 Run 收尾处独占。**
   循环内部**不直接写 `agent_log`**：它只读取会话记录快照、在内存中维护**运行转录**
   （本 Run 内供后续 Step 使用的已提交消息序列，含中间工具往返），
   待最终回复确定并准备交付时，由持久性屏障（ADR-0004）把**唯一一组**用户消息、助手回复与
   Run 级 telemetry 同事务落库。

   本票原文「用户消息、回复、来源、逐轮遥测写入 `agent_log`」据此更正：
   **逐轮的中间消息不进 `agent_log`**，只进追踪。词汇见 `CONTEXT.md` 新增的
   「会话记录 / 运行转录 / 工作记忆」三词分名，以及修订后的「提交」词条。

## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/13#issuecomment-5443665266

## 回写修订（来自 [决定：切片④a — 看见对话](https://github.com/nineofoursyrup/Agent-Alfred/issues/30)）

本票新增以下所有权；完整裁决以决策票的 resolution comment 为唯一权威，本评论只记录本施工票接住的闭包。

### 开工门槛

**真实门槛是 schema v2 基线已经进入 `main`**，不能拿 [实现：SQLite schema 与幂等迁移](https://github.com/nineofoursyrup/Agent-Alfred/issues/12) 已关闭代替；当前 `schema/12-final-closeout` 尚未并入 `main`，在此前不得从旧基线另起 v3。

### v3 前向迁移

- 新增 `sessions(session_id, created_at, activity_revision)`；Session 标识由服务端生成，客户端不可自选。
- 新增最小 Run 索引：`run_id`、`purpose`、可空 `session_id`、Gateway、可选 `entry_surface_id`、中央脱敏后的限长 `prompt_preview`、`phase`、`outcome`、接受/开始/结束时刻、`activity_revision`、Run 级 `telemetry`。
- `phase=accepted|running|finished` 与 `outcome=null|completed|max_steps|failed|interrupted` 分轴；CHECK 禁止非法组合，迁移用带旧 phase 条件且必须影响恰好一行的更新保证。
- 新增单行持久活动时钟；Session 创建和 Run 的 accepted/running/finished 转移（含启动恢复）在同一事务内取得 revision，并同步更新关联 Session。
- `runs.telemetry` 独占所有新 Run 的 Run 级遥测；`agent_log.run_id` 可空，增加 `UNIQUE(run_id, role) WHERE run_id IS NOT NULL`，并以 CHECK 禁止有 run_id 的消息再携 telemetry。历史无 run_id 的 telemetry 原样保留，不伪造 Run。
- 这是 v3 新迁移，**不得修改已经冻结的 v1/v2 迁移**。

### RuntimeHost / RunCoordinator / finalizer

- 先预留唯一交接槽，再提交 accepted 事务，再发布工作项；发布失败收为 `finished/interrupted`，若收尾也失败则交由新实例恢复。
- `interrupted` 表示「持久 Run 索引无法证明业务终态」；仅 `started_at IS NULL` 可显示执行前中断，其余不得借 trace 猜测。
- 启动恢复把遗留非终态 Run 收为 finished/interrupted，并取得新的持久活动 revision。
- finalizer 在同一事务中写 Run 终态、唯一聊天消息对（仅 chat）、Run telemetry 与既有追踪状态；interrupted 不生成助手消息。
- 提供与传输无关的运行状态发布接口及有界的 `unrecorded_terminal_projection` 所有权；记录失败后保留该投影，并使准入返回可由 HTTP 层映射为 `503 recording_unavailable` 的结果，直至恢复或重启。
- 进程内 `state_revision`、事件 `seq`、持久 `activity_revision` 三者正交，不得比较或互推。

### 验收补充

- v3 在真实 v2 基线上升级、重复迁移幂等、旧数据逐字保留；非法 phase/outcome 和有 run_id+telemetry 的消息均被数据库拒绝。
- accepted 落盘失败、交接失败、运行中进程终止、run.finished 后记录失败、启动恢复五条路径均有确定性测试。
- system Run 无 Session/消息也能持久保存 Run telemetry；chat Run 每种角色至多一行。
- 活动 revision 与状态转移同事务，终态 Run 此后不再移动；trace 裁剪不得删除 Run、telemetry 或会话记录，也不得推进活动 revision。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/13#issuecomment-5444205873

## 回写修订（来自 [决定：切片④a — 看见对话](https://github.com/nineofoursyrup/Agent-Alfred/issues/30) 的补正裁决）

完整裁决以决策票的[补正裁决评论](https://github.com/nineofoursyrup/Agent-Alfred/issues/30#issuecomment-5444200695)为唯一权威，本评论只记录本施工票接住的闭包。

### 开工门槛：已满足

上一条回写里写的「当前 `schema/12-final-closeout` 尚未并入 `main`」**已经过时**。schema v2 基线已于 `df5ea0f`（PR #37）进入 `main`，本票的真实开工门槛**已解除**，v3 可以从 `main` 的 v2 基线另起。

### RunCoordinator：租约持有到记录落定

原裁决对 `pending` 期间的准入保持沉默，「不得让第二个未记录终态覆盖有界单槽」因此没有执行路径。补正后本票拥有：

- `run.finished` **只把协调器状态推进到 `recording_pending`**，不释放任何东西。
- 原 Run 在收尾事务得出 `recorded` 或 `failed` **之前始终占有唯一准入租约**；期间的第二次提交返回**现有的 `409 run_in_progress`**，不新增错误码。
- 提交**成功**：**先**把权威快照更新为 `recorded`，**再**释放租约。
- 提交**失败**：**先**进入 `recording_failed`、保留同一投影并关闭准入，**再**让后续请求返回 `503 recording_unavailable`。
- 两处次序都不可交换：先释放租约会让下一个 Run 在快照仍写着旧终态时被准入；先返回 503 而状态未落定，是对外宣告一件内部还没成立的事。
- 完整可执行路径：`idle → accepted → running → recording_pending → idle | recording_failed`。
- `recording_pending` / `recording_failed` 是**协调器状态**，与 Run 的 `recording_state`（`pending|recorded|failed`）是两个量，不得互相替代。

### v3 迁移：逐字回填历史 Session

在既有 v3 迁移条款之上新增：

- 为 `agent_log` 中**每一个不同的既有 `session_id` 原值**插入一条 `sessions` 行，一对一。
- 「Session 标识仅由服务端生成」是**创建 API 的规则**，**不是数据库格式约束**——`sessions` 不得因历史标识不合新格式而拒绝它们，不要为此加 CHECK。
- `created_at` **逐字复制**该 Session **最小 `agent_log.id`** 那一行的时间文本。**禁止**对时间文本取 `MIN` 或做字典序比较：`agent_log.created_at` 从未受严格格式约束，按文本取最小取到的可能不是最早那条。
- `activity_revision` 按各 Session 的**最大消息 id** 从旧到新，依次从持久活动时钟取唯一值；最近写过消息的历史 Session 因此排在更前。**迁移重跑不再分配。**
- 这仍是 v3 这一号迁移的内容，**不得回头修改已冻结的 v1/v2**。

### 验收补充（在既有五条路径之外新增）

**竞态（确定性）**：以**闩锁暂停 finalizer 事务**，在已收到 `run.finished` 但事务尚未提交时发起第二次提交，断言四件事——**没有新 `accepted` 行、没有工作项发布、`unrecorded_terminal_projection` 的 `run_id` 未被覆盖、返回 409**；随后分别验证提交成功后可以准入、提交失败后持续 503。

**迁移**：从**真实 v2 数据库**种入多个任意 Session 标识、非规范时间文本、消息与 legacy telemetry，断言：

- `sessions` 与既有 `session_id` 原值**一对一**回填；
- 原字段**逐字不变**（含非规范时间文本与 legacy telemetry）；
- `activity_revision` **唯一且有序**，顺序符合各 Session 的最大消息 id；
- **重复迁移幂等**，不再分配新的活动序号；
- 分页收件箱**可见**，打开后旧消息**完整**。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/13#issuecomment-5461659879

实现与最终复审已完成，关闭本票。

交付分支：feat/13-slice-1
最终提交：745c4a0 Close issue 13 final lifecycle review gaps

最终独立复审：Standards 0 个 actionable findings；Spec 0 个 actionable findings。ADR-0017 staging 回收边界已补齐：删除前校验托管名称、根目录、未知条目及每个现存条目的实际文件类型，同时保留合法部分 staging 的崩溃恢复。

本地门禁：
- uv run ruff check .：通过
- uv run pytest -q：316 passed, 1 deselected
- trace bundle 定向：22 passed
- 四文件回归：90 passed
- git diff --check main...HEAD 与工作区检查：通过

requires_key 测试默认 deselected；此前已完成一次真实 OpenCode Go 生产链路验收，确认真实 Attempt、completed、trace bundle 发布、聊天消息落库，以及新建 RuntimeHost 后从 SQLite 读回会话。