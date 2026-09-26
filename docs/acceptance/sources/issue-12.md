# #12 实现：SQLite schema 与幂等迁移

来源：https://github.com/nineofoursyrup/Agent-Alfred/issues/12

## Question

SQLite schema 与幂等迁移。

⚠️ **本票严格限定为「基于注入连接的 schema、幂等迁移与 FTS5」。
状态目录级进程锁与入口描述（`instance_id` / `pid` / `port`）不得纳入本票**——
「取锁 → 绑 `127.0.0.1` → 原子写入口描述」是不可分的整体（[#23](https://github.com/nineofoursyrup/Agent-Alfred/issues/23) §2 定的
`RuntimeHost` 启动序列），拆成两张票必在接缝上出事，故归
[实现：Dashboard HTTP+SSE 骨架与重放环](https://github.com/nineofoursyrup/Agent-Alfred/issues/28) 独占。

#23 裁决「对地图与其他票的影响」里那句「#12：入口描述与状态目录进程锁的落位需与状态目录布局一并确定」**已作废**，见该票的修订评论。

按 brief 至少包含：

- `events`：标题、开始、结束、参与者、备注、创建时间。
- `facts`：主题、事实内容、来源、创建时间 + **FTS5 索引与同步触发器**。
- `episodes`：发生时间、摘要、创建时间 + FTS5 索引。
- `agent_log`：角色、内容、是否已提炼、会话 ID、消息来源、JSON 遥测、创建时间。

约束：

- 连接设置 `busy_timeout`。
- schema 与迁移**必须幂等**（重复执行无副作用）。
- 会话只通过 `session_id` 隔离。
- 数据库连接可注入（离线测试用内存库或临时文件）。
- 单用户假设：**不加 user_id 列**。
- `agent_log` 的 **JSON 遥测列**须容纳 [#3](https://github.com/nineofoursyrup/Agent-Alfred/issues/3) 定的 Run 级 telemetry：各 Step 数组、`ModelResult.attempts` 汇总（含**作废** attempt 的用量）、工具执行记录，以及 **`trace_incomplete` 标记与原因**。它必须与 assistant 回复行**同一个事务**写入（ADR-0004 的持久性屏障），否则会出现「声称追踪完好、磁盘上却什么都没有」的记录。

### 验收

- 迁移跑两遍结果一致的测试。
- FTS5 触发器在 insert / update / delete 三种情况下都同步的测试。
- 确认 Python 3.14 自带的 sqlite3 已启用 FTS5；若未启用，在本票里给出明确的可操作错误信息与处置方案。




## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/12#issuecomment-5427877556

来自 [#4 决定：Tool 协议、错误回喂与副作用 opt-in](https://github.com/nineofoursyrup/Agent-Alfred/issues/4) 的新增要求：**`tool_ledger` 表**。

- 入账范围按 `effect`：`local_write` 与 `external` 入账，`local_read` 不入。
- **`local_write` 与业务写入同一个事务**落账——要么一起提交要么一起回滚。
- **`external` 走状态机** `started → succeeded / failed / unknown`：执行前先落 `started` 行，收尾时更新。外部副作用无法与本地 DB 原子提交，`unknown` 是它的常态终态（预算耗尽后放弃等待、进程被强杀）。
- 按 `(tool_name, 规范化指纹)` 建索引；指纹由 `Tool.summary_keys` 声明的参数生成，且必须在**执行前**算得出来（它同时充当 `external` 工具的幂等键）。
- 保留策略独立于 `agent_log`：对话可压缩可裁剪，副作用记录该长期留。

## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/12#issuecomment-5428674147

#11 骨架评审的 **D-2** 已裁定：包根的 **schema 与迁移** 不在 #11 补空桩，明确记进本票。

#11 票面写「包根：CLI 分发 / Settings / 依赖组装 / schema 与迁移」，决议评论曾把后三块推到 #12/#13。人已选：本票承担 schema 与迁移（与正文一致），#11 保持现状（只有 CLI stub）。

## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/12#issuecomment-5436093062

## 回写修订（2026-08-27，来自 [决定：trace 文件布局、切分与保留策略](https://github.com/nineofoursyrup/Agent-Alfred/issues/22)）

**新增一张表 `trace_prunes` 与对应的幂等迁移。**

`agent_log` 的 JSON 遥测列挂在**消息行**上，装不下「这个 Run 的 trace bundle 被裁剪了」这个 Run 级事实；而它也**不应该**装——裁剪是几十天后的运维动作，`trace_incomplete` 是 Run 收尾时的观测，混进同一列两个就再也分不开（ADR-0020）。

字段至少：

- `run_id` **PRIMARY KEY**
- `prune_requested_at`
- `absence_confirmed_at` —— 「确认目录已消失」的时刻。**不得声称为实际 `unlink` 时刻**，那个时刻证明不了。
- `prune_reason` —— 闭合四值 `manual` / `disk_low` / `age` / `capacity`，一次删除同时满足多条时按 `manual > disk_low > age > capacity` 取，保证记录可复现。

约束：

- **不外键依赖 `agent_log`**：屏障之前就崩掉的 Run 照样可能在盘上留下 bundle，那条消息行可能从未落库。
- **只在裁剪时插入行**，不为每个 Run 预建空行。判读：有行 = 本系统主动删除；**无行却缺 trace** = 人工移除或真正损坏。
- 写入由 `RuntimeHost` 的**唯一写连接**执行（取得进程锁后即可先建立该连接，再执行裁剪），不引入第二条连接。
- 裁剪一批时用**一个批事务**写入——SQLite 单行事务的成本主要在同步而非插入，且崩溃窗已由 `retention-state.json` 的 pending 封住。

`agent_log` 的 JSON 遥测列须容纳 `trace_incomplete` 这条要求**不变**，只是它此后与裁剪事实严格分离。

### 验收补充

- 迁移跑两遍结果一致（沿用本票既有要求，覆盖新表）。
- 同一 `run_id` 重复裁剪不产生第二行（主键 + 幂等写入）。

## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/12#issuecomment-5437625839

## 回写修订（2026-08-27，补录来自 [决定：三类记忆的后端 Protocol](https://github.com/nineofoursyrup/Agent-Alfred/issues/5) 的两条指派）

**这两条是开票时漏的，不是执行方的问题。** #5 关闭时把两样东西指给了本票，但都没写进本票正文或评论，于是本票的边界一直是自相矛盾的——同一条「归 #12」的规则，`consolidation_*` 被执行了，时区那条被跳过了。现在一并补上。

### A. IANA 时区的落盘形式（#5 §7）

原句：

> **只有需要保留当地显示、未来日程或重复规则时才额外保存 IANA 时区名**：固定 offset 对**既成的瞬间**已经足够（那一刻是确定的），但对 DST 规则不足（未来的「每周三 19:00」需要规则而非偏移）。**落盘形式归 [#12](https://github.com/nineofoursyrup/Agent-Alfred/issues/12)**，上述约束由本票钉死。

本票要定的是**落盘形式**，不重新裁决约束。判据是 #5 已经钉死的那条分界：

- **既成的瞬间**（`facts.created_at`、`episodes` 的区间端点、`agent_log.created_at`、`tool_ledger` 与 `trace_prunes` 的各时刻……）：带 offset 的 UTC ISO8601 **已经足够**，**不加**时区名列。给它们加列是无意义的膨胀。
- **未来日程**（`events` 的 `starts_at` / `ends_at`）：**必须额外保存 IANA 时区名**。这正是 §7 点名的场景——固定 offset 表达不了 DST 规则，「下个月的每周三 19:00」用偏移存下来就是错的。

要求：

1. `events` 增加一列存 IANA 时区名（如 `IANA_time_zone`，命名由你定，英文标识符）。**可空**——不带当地语义的日程不强求。
2. 该列存的是**时区名**（`Asia/Shanghai`），不是偏移量（`+08:00`）。存偏移就等于没解决 §7 指出的那个问题。
3. **不做「假定本地时区」的补救**（§7 明文否掉）：读到 naive 值不猜，如实拒绝或留空。
4. 内部比较与排序仍一律按 UTC 绝对时刻，**不得依赖 ISO8601 字符串的字典序**（§7 原文）。

**验收**：一条测试证明「同一个 IANA 时区名下，跨 DST 边界的两个未来时刻，其 UTC offset 不同」——这条测试如果写不出来，说明存的是偏移不是时区名。

### B. `consolidation_batches` / `consolidation_ops` 的具体列（#5 §10）

原句：

> **`consolidation_batches` 归提炼器所有**：不复用 `tool_ledger`……表的具体列归 [#12](https://github.com/nineofoursyrup/Agent-Alfred/issues/12)。

本条是**追认**：这两张表已由 `40f7f98` 落地，且落在本票是对的。补录是为了让边界一致——它与 A 出自同一条规则，不能一个做一个不做。

对照 #5 §10 复核已落地的形状是否够用：

- **确定性批次 ID**，且**每个写项一个稳定操作 ID**；
- 状态机 `started → succeeded / failed / unknown`；
- **只有全部确认成功才标记已提炼**，`unknown` 留待重试或核对；
- **稳定操作 ID 复用为幂等键**。

若已落地的列表达不了其中任何一条，本票补齐。

## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/12#issuecomment-5437634222

## 验收：打回（独立两轴评审，定点 `88ed1c3...ae253b2`）

规划 session 对 #12 全量改动做了一次独立的两轴评审（Standards / Spec 并行、互不见对方上下文），定点取在 **`88ed1c3`**（#12 开工前）而非 `40f7f98`——增量部分的自评已有，这里要的是对全量的独立复核。这张图上有过先例：#11 那两份逐字节相同的 CI workflow，正是靠独立评审才发现的。

**结论：schema 的形状基本是对的，问题几乎全在可观测性上。** `uv run ruff check` 通过、35 passed 都是真的，但其中三条测试在证明与本 diff 无关的东西，而「迁移跑两遍一致」因为版本列只写不读而**恒真**。这类缺陷 Ruff 与 pytest 一条都查不出来。

### 打回清单（按轻重）

**1. `record_trace_prune` 的 `INSERT OR IGNORE`（`schema.py:290`）→ 改 `ON CONFLICT(run_id) DO NOTHING`**

实测：`prune_requested_at=None` 时该行被**静默丢弃且不报错**。ADR-0020 写死「有行 = 本系统主动删除；**无行却缺 trace** = 人工移除或损坏」——静默丢行**正好制造它要防的那个误判**，而且是在最难发现的方向上（少一行，没人报错）。`OR IGNORE` 吞掉的远不止主键冲突。

**2. `migrate` 会提交调用方的事务**

`_SCHEMA_SQL` 内嵌 `BEGIN;` / `COMMIT;` 且走 `executescript`。实测：调用方 `BEGIN` 之后写的行，在 `migrate()` 之后 `rollback()` 不掉。两轴独立复现同一处。

ADR-0009 只约束 Store，所以这不算硬违规；但 ADR-0020 要求裁剪走 `RuntimeHost` 的**唯一写连接**——在同一条连接上误调 `migrate` 就是静默提交别人在途的事务。`record_trace_prune` 自己不提交，那处做对了。

**3. 三条空断言测试：要么真证明，要么删掉并把名字让出来**

两轴**独立收敛到同样三条**：

- `test_agent_log_isolates_sessions_by_session_id` —— 只做了一次 `WHERE session_id=?`，**任何表都成立**，证不出「隔离」。
- `test_local_write_ledger_row_rolls_back_with_the_business_write` —— 两条裸 INSERT 同事务回滚，证的是 SQLite 语义，与本 diff 任何代码无关。#4 那条「`local_write` 与业务写入同一个事务」的持有者在 registry，本票**既没兑现也测不出**，不该用测试名把它占住——占住之后，将来真正实现它的人会以为已经有覆盖了。
- `test_external_..._to_a_terminal_status` —— 只做一次 UPDATE；schema 里没有任何转移约束，`succeeded` 可以退回 `started`，`local_write` 也可以是 `unknown`。

同类还有 `test_trace_prunes_have_no_foreign_key_to_agent_log`：实际断言的是「没有**任何**外键」，名字比断言窄。

**4. `schema_migrations` 只写不读：要么真读真校验，要么删掉**

`migrate` 只 `executescript`，版本列从不被读。迁移全靠 `IF NOT EXISTS`，v2 遇到形状已变的旧表会**静默 no-op**。于是本票最核心的验收项「跑两遍结果一致」变成恒真命题，测不出任何漂移。**只写不读的版本列比没有版本列更坏**——它让人以为有版本校验。

顺带：幂等验收缺一条「跑两遍后 `schema_migrations` 只剩一行」的断言。

**5. `events` 与词汇表的 Event 撞名**

`schema.py:71` 的 `events`（`title` / `starts_at` / `participants`）是**日程**；`CONTEXT.md`「追踪与事件」定义的**事件（Event）**是「业务与模型层向外通知发生了什么的最小单位」，信封带 `seq` / `run_id`——而 `agent_log.telemetry` 里真的存着后者。

「票面写的就是 `events`」不构成理由：**票不是词汇表**。按 `docs/agents/domain.md`，概念不在词汇表里就是信号。两条路选一条：改名（如 `calendar_entries`，测试里的 `create_event` 同源），或给日程补一条 `CONTEXT.md` 词条并在 Event 条目下标 `_Avoid_`。

**6. 撤掉 `connect(path)` 工厂与 `PRAGMA foreign_keys = ON`（`schema.py:243-252`）**

范围蔓延。本票只要求「连接**可注入**」；而 [#22](https://github.com/nineofoursyrup/Agent-Alfred/issues/22) 反过来要求「不引入第二条连接」，一个公开的连接工厂是反方向的诱导。`connect()` 目前也只有测试在调。

**7. 闭合集合收成单源**

`('started','succeeded','failed','unknown')` 在 `tool_ledger`(180) / `consolidation_batches`(209) / `consolidation_ops`(219) 抄了三遍，Python 侧没有常量；`('cli','web')` 在 `_ORIGIN_COLUMNS`(35) 与 `agent_log.source`(167) 各一份。

**同一个文件里 `prune_reason` 已经用 `_PRUNE_REASON_SQL` 做到了单源**——一个文件两套做法。按 ADR-0020 的同一条道理：两份各自演化的集合，只会在一次真实写入时才暴露对不上。

### 判断级、不阻塞合并

- `facts_fts_ai/ad/au`(104-119) 与 `episodes_fts_ai/ad/au`(144-159) 六个触发器近乎逐字重复，测试同形；`test_python_sqlite_has_fts5`(51-55) 复制了 `_fts5_enabled`(241-243) 的实现。
- `record_trace_prune(prune_reason: str | Iterable[str])`(290) 类型自相矛盾——`str` 本身就是 `Iterable[str]`，靠 isinstance 兜；时间戳全是裸 `str`。
- `run_id` / `prune_requested_at` / `absence_confirmed_at` / `prune_reason` 结伴出现在签名、SQL 与四处测试，是一个想诞生的 `PruneRecord`。
- `pick_prune_reason` 两条 ValueError 用了 `prune_reason` / `prune_reasons` 两种单复数措辞。

**未发现逐字节重复的文件或定义**——#11 那类问题这次没有。语言规则无违反：两个文件的标识符与注释全为英文。

### 已真正兑现（复核通过，不必重做）

幂等迁移、两表六个 FTS 触发器（insert / update / delete 全覆盖）、无 `user_id` 列、`busy_timeout` 与连接可注入、`(tool_name, fingerprint)` 非唯一索引（「证据不是锁」）、`local_read` 被 CHECK 拒绝入账、`trace_prunes` 的主键幂等 / 无外键 / 优先级取胜者 / 被调用方绝不 commit。

### 边界判断

「工具账执行路径、RuntimeHost 单写连接、裁剪器调用时机推后」——**三条都对**，确实不是 schema 票的接缝。

唯一划错的是时区那条，但**错因在票不在人**：#5 §7 的指派从来没写进本票。已补录为上一条回写修订（连同 §10 的追认），一并在本轮修复。

## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/12#issuecomment-5438039612

## 回写：打回 1–7 已处理，修订 B 已落地（`ae253b2` → `6d7659c`，已 push）

定点 `88ed1c3`。本票现在的三个提交：`40f7f98`（初版）、`ae253b2`（评论不变量）、`6d7659c`（本轮打回）。确定性套件 **50 passed**，`uv run ruff check` 通过。

### 打回清单逐条

**1. `record_trace_prune` → `ON CONFLICT(run_id) DO NOTHING`**
已改。`prune_requested_at=None` 现在抛 `IntegrityError`，不再静默丢行——ADR-0020 那条「无行 = 人工移除或损坏」的误判源头堵上了。

**2. `migrate` 不再提交调用方事务**
已改。`_SCHEMA_SQL` 拆成 `_V1_STATEMENTS` 逐条 `execute`，不再走 `executescript`，SQL 里不再内嵌 `BEGIN` / `COMMIT`。只在 `not conn.in_transaction` 时自己 `BEGIN` / `commit` / `rollback`；调用方已开的事务，`migrate` 一路不碰（`schema.py:300-333`）。

**3. 三条空断言测试**
- `..._isolates_sessions_by_session_id` → 改成真证明：`session_id` NOT NULL + 索引 + 双会话内容互不泄漏。
- `..._local_write_ledger_row_rolls_back_...` → **删除，未补断言**。#4 那条「`local_write` 与业务写入同事务」的持有者在 registry，本票够不着；留着名字只会占住将来真正实现它的人的覆盖。这是有意留空，不是遗漏。
- `..._to_a_terminal_status` → 改成闭合集 CHECK 的断言，不再假装 schema 里有转移约束。
- `test_trace_prunes_have_no_foreign_key_to_agent_log` → 改名 `test_trace_prunes_have_no_foreign_keys`，名字与断言对齐。

**4. `schema_migrations` 真读真校验**
`_applied_version` 现在真读版本列：等于当前版本直接跳过 DDL，更高版本或无升级路径一律 `SchemaVersionError`。幂等验收补了「跑两遍后 `schema_migrations` 只剩一行」的断言——「跑两遍一致」不再是恒真命题。

**5. `events` 与 Event 撞名**
两条路**都走了**（原文是二选一）：表改名 `calendar_entries`（`create_event` 同源改名），同时 `CONTEXT.md` 补「日程（calendar entry）」词条，并在 Event 的 `_Avoid_` 标上「日程 / `events` 表」。

**6. `connect(path)` 与 `PRAGMA foreign_keys = ON`**
均已删除。本票只留「连接可注入」，不再有公开连接工厂——与 #22「不引入第二条连接」同向。

**7. 闭合集合单源**
`LEDGER_STATUSES` / `SOURCES` 提为 Python 元组，CHECK 由元组生成，与 `prune_reason` 走同一套做法。一个文件两套做法的问题消掉了。

### 修订 B（时区）

- `calendar_entries` 增可空列 `iana_time_zone`，存 `America/New_York` 这类 IANA 名，拒 `+08:00`。既成瞬间不加时区名列。
- 验收测试：同一 IANA 名下，**2027-03-14**（未来 DST 边界）的两个 UTC 瞬间经 `ZoneInfo` 得到不同 offset。
- `parse_instant` 拒绝 naive 值；比较走 aware datetime，不走 ISO 字符串字典序。评审时那条「SQL 字典序必须错」的通过条件已去掉。
- `consolidation_batches.batch_id` / `consolidation_ops.op_id` 已能表达确定性批次、稳定操作 ID 与幂等键，未加新列。

### 判断级、本票有意不动

- **`iana_time_zone` 的 CHECK 仍偏松**：`IS NULL OR = 'UTC' OR instr(name,'/') > 0`，挡不住 `Etc/GMT+8`。往 CHECK 里继续堆字符串规则等于在 SQL 里重写一份 tzdata；这类反例应由写入层 / `parse_instant` 挡，不是表约束的活。
- **`LEDGER_STATUSES` 挂到提炼表上**（词汇是工具账，表是提炼器）。ADR-0009 禁的是复用 `tool_ledger` 这张**表**，不是同一组状态字面量；闭合集单源是打回 7 的硬要求，盖过命名直觉。若要改名，应单开一票连同词汇表一起动。
- **`facts` / `episodes` 两套 FTS 触发器同形**、**`record_trace_prune` 四字段结伴**（那个想诞生的 `PruneRecord`）——两条气味都还在，留给结构票。

Refs #12.


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/12#issuecomment-5438412965

## 验收：再次打回（独立两轴评审，定点 `88ed1c3`）

对 `6d7659c` 的回写做了第二轮独立两轴评审（Standards / Spec 并行、互不见对方上下文），定点仍取 `88ed1c3`（#12 开工前）——要的是对**全量**的复核，不是增量自评。评审范围含三个提交（`40f7f98` / `ae253b2` / `6d7659c`）**外加两处尚未提交的工作区改动**（`schema.py` 的空版本表检查、`test_schema.py` 的若干测试重写）。`7a59a9f` / `b7992f2` 属 #29 的 ADR 文档，已排除。

`uv run ruff check` 通过、**51 passed** 都是真的。上一轮打回的 **1 / 2 / 5 / 6 / 7 属实**，实测复现：`migrate` 在调用方 `BEGIN` 内首迁移后 `rollback()` 能清空全部表；`prune_requested_at=None` 抛 `IntegrityError` 而非静默丢行；迁移跑两遍后 `schema_migrations` 只剩一行。

**但上一轮的两个主症都以新形态复发了**：空断言测试长回来一条，版本列的静默 no-op 换了个入口。以下每条均在 `:memory:` 上实测，非静态阅读。

### 打回清单（按轻重）

**1. 打回 4 只堵了一路：无版本表的旧库仍然静默 no-op**

工作区新增的 `_applied_version` 检查只处理「有 `schema_migrations`、但无行」。**没有版本表的旧库照旧走全量 `IF NOT EXISTS` DDL**。实测：

```python
c.execute("CREATE TABLE trace_prunes (run_id TEXT)")   # 只有一列的畸形旧表
schema.migrate(c)
# -> cols after migrate: ['run_id']
# -> schema_migrations: [(1, '2026-08-27 ...')]
```

畸形表原样留下，version 1 写入，报成功。这正是上一轮打回的原话——「v2 遇到形状已变的旧表会**静默 no-op**」——只是入口从「版本列不被读」换成了「版本表不存在时不校验」。「只写不读的版本列比没有版本列更坏」那条道理在这条路径上一字未改。

**2. 空断言测试复发：`test_consolidation_op_id_is_the_idempotency_key`（`test_schema.py:750`）**

两轴独立指向同一处。`ON CONFLICT(op_id) DO NOTHING` 写在**测试自己的 SQL 里**，`schema.py` 没有任何对应函数——它证的是 SQLite 语义，与本 diff 的代码无关。这与打回 3 里刚被删掉的 `..._rolls_back_with_the_business_write` 是同一款。属于本表的性质只有 `op_id` 主键唯一。

更糟的是断言方向：改写后它断言重放同一 `op_id` 后状态**仍为 `started`**，等于把「不能推进到 `succeeded`」写进了验收——与 #5 §10「状态机 `started → succeeded / failed / unknown`」相反。幂等键的语义是「重放不产生第二行」，不是「首写之后此行冻结」。

**3. `create_event` 违反词汇表的 `_Avoid_`（`test_schema.py:450 / 471 / 516 / 589 / 595`）**

`6d7659c` 把表 `events` 改成了 `calendar_entries`，同一提交给 CONTEXT.md 的「事件」词条追加了 `_Avoid_: 日程、events 表`，又新增「日程（calendar entry）」词条标 `_Avoid_: Event, events`。但工具名 `create_event` 五处原封不动留在禁用词里。`docs/agents/domain.md` 明文：输出命名领域概念时不得漂移到词汇表 `_Avoid_` 列出的同义词。改 `create_calendar_entry`。

**4. `iana_time_zone` 列加了，但 #5 §7 点名的场景仍存不下**

`starts_at` 的 CHECK 强制后缀带 offset（`%Z` / `%+__:__` / `%-__:__`），于是**本地墙钟时间根本写不进去**。实测 `('2026-09-02T19:00:00', 'Asia/Shanghai')` 被 CHECK 拒。

§7 要的是「未来的『每周三 19:00』需要规则而非偏移」。现在的形状是「一个已经折算成绝对时刻的瞬间 + 一个时区名」——绝对时刻一旦定死，DST 规则就没有作用对象了，时区名退化成纯展示信息。列有了，但 §7 指出的那个问题没被解决。要么允许 `starts_at` 存墙钟值（由 `iana_time_zone` 非空来界定这一路），要么明确写下「本票只存绝对时刻，重复规则归后续票」并让 CHECK 与词条一致。

**5. 全库唯一的外键，票面没要求，而且不生效**

`consolidation_ops.batch_id REFERENCES consolidation_batches` 是整个 schema 里唯一一条外键。打回 6 删掉 `PRAGMA foreign_keys = ON` 之后它**完全不起作用**——实测孤儿 `batch_id='NO-SUCH-BATCH'` 被直接接受。

一条不生效的约束比没有约束更坏：它在 DDL 里看着像个保证。而且立场与 `test_trace_prunes_have_no_foreign_keys` 自相矛盾——一处以「没有外键」为验收，另一处留着一条装饰性外键。删掉它，或者说明白它靠哪一层执行。

**6. `starts_at` / `iana_time_zone` 的 CHECK 不构成校验**

`starts_at` 只验后缀：`'garbageZ'`、`'Z'`、`'2026-13-99T99:99:99Z'`、`'garbage+08:00'` 全部入库。`iana_time_zone` 只验含斜杠：`'//'`、`'not a zone/x'` 入库。

回写里「CHECK 偏松、该由写入层挡」的判断本身接受——往 CHECK 里堆字符串规则确实等于在 SQL 里重写 tzdata。但**现在的 CHECK 挡不住的东西远超 `Etc/GMT+8` 那一类**，它连「长得像个时刻」都不保证。要么老实承认它只是形状提示（改名或加注释说清），要么让 `parse_instant` 那条路径成为唯一入口并有测试为证。

### 判断级、不阻塞

- **`LEDGER_STATUSES` 跨表复用**：同时给 `tool_ledger` 与 `consolidation_batches` / `ops` 生成 CHECK。ADR-0009 禁的是复用 `tool_ledger` 这张表、不是同组字面量，回写的这个论证成立；但共用一个以 ledger 命名的常量把两套状态硬绑在一起，工具账改了提炼会静默跟着改。拆 `CONSOLIDATION_STATUSES`（值相同、来源分离）成本近零。
- **`test_migrate_twice_leaves_identical_schema`（185）现在测不到幂等路径**：版本检查生效后，第二次 `migrate` 直接 return，`IF NOT EXISTS` 那条路一次都没跑到。实测 `_V1_STATEMENTS` 重跑确实幂等，但**这个性质现在无测试覆盖**；该测试与 229 行那条也实质重复。
- `..._offsets_across_dst`（278）里 `len(offsets) == 2` 是 `ZoneInfo` 的事实，与列的存储形式无关，列是 TEXT 就恒过；329 行同理，名字承诺的「不依赖字典序」从未被断言。
- 上一轮列的两条结构气味仍在：`facts` / `episodes` 两套 FTS 触发器同形、`record_trace_prune` 四字段结伴（那个想诞生的 `PruneRecord`）。回写说留给结构票，接受。

### 已真正兑现（复核通过，不必重做）

打回 1（`ON CONFLICT(run_id) DO NOTHING`，`None` 抛 `IntegrityError`）、打回 2（`migrate` 不再提交调用方事务，`_V1_STATEMENTS` 逐条 execute）、打回 3 的其余三条（session 隔离改成真证明、终态测试改成闭合集断言、`test_trace_prunes_have_no_foreign_keys` 名实对齐）、打回 5（表改名 + CONTEXT.md 双向词条）、打回 6（`connect()` 工厂与 `PRAGMA foreign_keys` 均已删）、打回 7（`LEDGER_STATUSES` / `SOURCES` 提为元组、CHECK 由元组生成，与 `_PRUNE_REASON_SQL` 同一套做法）。

工作区里 `_SPEC_LEDGER_STATUSES` / `_SPEC_SOURCES` 不再 import 实现常量这一改**是对的**——实测确能挡住 `'running'` / `'telegram'`，验收测试不该读被测实现自己的常量。这个方向请保持。

### 本轮的判断

问题集中在**同一类**：SQL 层写下了看起来像保证的东西（版本列、外键、CHECK、测试名），但没有一条路径真正执行它。Ruff 与 pytest 对这类缺陷一条都查不出来——51 passed 与它们完全兼容。

1–5 请处理；6 可以选择「修」或「明确降级为形状提示」，但不能留在现在这个「像校验但不是」的状态。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/12#issuecomment-5442832518

## 回写：第二轮打回已收口，本票关闭（`0cfe017`，分支 `schema/12-final-closeout`）

定点仍是 `88ed1c3`。本票的四个提交：`40f7f98`（初版）、`ae253b2`（评论不变量）、`6d7659c`（第一轮打回）、`0cfe017`（第二轮打回 + 本次收口）。`uv run ruff check` 通过、`git diff --check` 通过、`uv run pytest` **82 passed**（其中 `test_schema.py` 70 条，原 51 条）。

**提交在分支上，尚未并入 `main`**，等你决定用 PR 还是直接 merge。

### 第二轮打回逐条

**1. 无版本表的旧库静默 no-op** —— 已堵。`_UNVERSIONED_DATABASE`：库里已有本模块管理的对象、却没有版本账，一律拒绝而不是当新库跑全量 DDL。评审里那个「只有一列的畸形 `trace_prunes`」旧库现在被拒，不再原样留下 + 写 version 1 + 报成功。

**2. `test_consolidation_op_id_is_the_idempotency_key` 空断言** —— 已删。`ON CONFLICT` 写在测试自己的 SQL 里，证的是 SQLite 语义；属于本表的性质只有 `op_id` 主键唯一，那条单独断言。断言方向那个错（把「首写后此行冻结」写进验收，与 #5 §10 状态机相反）随之消失。

**3. `create_event` 违反词汇表 `_Avoid_`** —— 已改 `create_calendar_entry`，五处全改。

**4. `iana_time_zone` 加了但 §7 场景存不下** —— 走「明确写下边界」这一路：本票只存**绝对时刻**，当地墙钟的重复规则不在这一层。CHECK、`schema.py` 的注释、CONTEXT.md 的「日程」词条三者现在说同一件事，不再是「列有了但问题没解决」的暧昧状态。裸墙钟 `'2026-09-02T19:00:00'` 被 CHECK 拒，且升级时遇到这种旧行会在**任何破坏性 DDL 之前**停下。

**5. 全库唯一的装饰性外键** —— 已删。`PRAGMA foreign_key_list(consolidation_ops)` 现在是 `[]`，与 `test_trace_prunes_have_no_foreign_keys` 的立场一致。孤儿 `batch_id` 仍被接受，但 DDL 里不再有一条看着像保证的东西。

**6. `starts_at` / `iana_time_zone` 的 CHECK 不构成校验** —— 选「明确降级为形状提示」。IANA 那条重写为：首字母 ASCII 字母、字符集限 `[A-Za-z0-9_+/-]`、无尾斜杠、无空路径段，不再强制含 `/`（`Etc/GMT+8`、`GMT`、`CST6CDT` 现在都过；`zoneinfo.available_timezones()` 全部 598 个名字 0 拒）。它**仍然挡不住虚构名**——`Narnia`、`Mars/Olympus_Mons` 能入库——这条边界写死成了测试（先断言 `ZoneInfo` 抛异常，再断言入库成功），不是注释里的一句话。

### 版本 1 冻结与修复迁移

三个提交各把一个**不同**的 schema 盖成 version 1。因此 v1 的 DDL 钉在 `6d7659c`，改动过的两条语句改为逐字字面量；其余 20 条仍插值闭合集常量（否则闭合集单源——打回 7 的硬要求——会失去 SQL 侧消费者）。唯一的误改路径是给某个闭合集加值，由 `test_version_1_ddl_is_frozen_at_the_shape_the_last_commit_published` 兜住：加第四个 `origin_kind` 会同时打挂闭合集测试与冻结测试。

v2 是**修复迁移**：先认形状（`events` / `calendar_entries` 二选一，皆有或皆无 → fail closed），再逐对象比对归一化 DDL 与已知 v1 形状，再对日程行做前置可转换性检查；三关全过才动破坏性 DDL。重建走「旧表改名让位 → 建新表 → `INSERT…SELECT` 搬运 → 丢弃旧表」。调用方已 `BEGIN` 时，全部 pending migrations 包在一个 SAVEPOINT 里——失败只回滚迁移，调用方在途写入原样保留。

三个历史 v1 库升级实测：7 张表行数据逐字节保留、`events`→`calendar_entries` 完成、`iana_time_zone` 该 NULL 的为 NULL（不猜本地时区）、FTS 仍命中、重跑 `migrate` 后 `sqlite_master` 与全部行均无变化。

### 本轮相对上一版自评的三处收口

上一版工作区自评里有三处经不起复核，一并改掉：

- **删除 `_V2_APPLIED_AT_FIXUP`。** 它把旧的 `datetime('now')` 值改写成 `…T…Z`——票面与两轮打回都没要求过。版本账的行是既成记录，重述别人写下的时刻是没人要求过的写入。现在 v1 那行原样保留（`(1, '2026-08-27 08:28:35')`），v2 自己那行仍是 aware 的。变异验证：把 fixup 加回去，3 条测试挂。
- **`historic_schema.py` 降级为「归一化结构基线」。** 原 docstring 自称「逐字节捕获」，实测**五条**索引语句与真实 `sqlite_master` 不符（`6d7659c` 的 `facts_subject_idx` / `episodes_occurred_at_idx` / `agent_log_session_created_idx` / `tool_ledger_name_fingerprint_idx` / `tool_ledger_session_created_idx`，均差一个尾随换行；`40f7f98` / `ae253b2` 逐字节吻合）。而 `pyproject.toml` 那条 W293 豁免的全部理由正是「它是转录，唯一审计方式是重新捕获并 diff」——现在 diff 不上，`_normalized_master` 也永远看不见。这正是本轮要防的那款「写下像保证的话，无路径执行」。docstring 改为写明保证的是结构、不保证空白、五条索引已知不符、审计方式是**归一化后**比对；W293 豁免撤销，四行尾随空白清掉（`pyproject.toml` 因此回到提交时的状态，本票对它零净改动）。实测三个 commit 归一化后 fixture 与真实捕获 0 处不符。
- **「日程」词条补上排序约束**：比较与排序先解析为 aware instant、归一到 UTC 再比，禁止直接拿时间文本排序（`…Z` 与 `…+00:00` 在同一列合法共存，字典序会排错）。这是 #5 §7 要求 4 的落点。**没有为它伪造测试**——读取层至今不存在，一条断言 `ZoneInfo` 事实的测试只会再造一个上一轮刚被打掉的空断言。约束先立在词汇表里，由实现读取层的那一票兑现。

### 结构气味另开 #35

收口时的独立两轴评审报了 0 条硬违规、6 条判断级结构气味（闭合集裸 `str` / `PruneRecord` 四字段结伴 / `schema.py` 一个文件五个变更理由 / `zone` 参数名 / 只有一个调用点的注入参数 / `_historic_calendar_table` 该搬进 fixture）。全部移交 #35，连同两轮都判为「留给结构票」的 FTS 触发器同形与 `prune_reason` 未收 `Literal`。它们跨本票的接缝，在这里动会把修复迁移的改动面撑到评审看不动。

### 已知余留（#35 之外）

- v1 的 20 条语句仍插值常量，靠冻结测试兜——刻意取舍，理由见上。
- `_normalize_ddl` 剥 `--` 注释，字符串字面量里若含 `--` 会被误伤；方向是误判为不匹配（fail closed），不会放行。
- 形状核对只查 v1 自己建的对象，用户另加的表不管。
- 时区 CHECK 只是形状防线，虚构名可过——已在 CHECK 注释、docstring 与测试三处写死。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/12#issuecomment-5442834390

第二轮打回 1–6 已逐条收口，本票关闭。提交 `0cfe017` 在分支 `schema/12-final-closeout`，尚未并入 `main`。结构气味移交 #35。