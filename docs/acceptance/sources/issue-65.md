# #65 决定：Database 只读 SQL 控制台的用户路径与验收

来源：https://github.com/nineofoursyrup/Agent-Alfred/issues/65

## Question

如何冻结 Agent-Alfred v1 的 **Database 只读 SQL 控制台**完整用户路径与端到端验收，使独立执行 agent 无需再代替用户作产品取舍即可实现？

总地图：[地图：Agent-Alfred v1 — 本地优先、无框架、可追踪的私人 AI 助手](https://github.com/nineofoursyrup/Agent-Alfred/issues/1)。本票是 HITL 决策票；本 session 只解决本票，使用 wayfinder、grilling 与 domain-modeling，逐轮编号、给出建议、等待用户裁定。

待裁定的问题覆盖：

1. 使用目的、Dashboard 入口、数据库对象和内容范围。
2. 只读保证，以及 SQL、PRAGMA、函数与额外文件访问边界。
3. 查询结果、空结果、错误、分页、行数和大小限制。
4. 慢查询、用户中止、断连与资源释放，以及与聊天和其他数据库操作并存的行为。
5. 原始内容展示与既有脱敏、真实遗忘和来源隔离契约的关系。
6. 浏览器 → 公开 HTTP → 真实 SQLite 的完整验收路径及反例。

先核查当前 main、CI、现有实现和已验收规范，再覆盖完整用户路径，最后沿依赖深入。实现机制只是备选，未经用户确认不得写成已决定。

本轮范围是规划、决策与已授权 tracker 更新；不实现产品代码，不提交、推送或合并，不启动实现 agent。既有工作区修改予以保留。

最终共同确认后，结论写入本票的 **resolution comment**，关闭本票并回填地图索引；依地图约定生成对应纯实现票，写明范围、非目标、验收、依赖和上下文链接。答案不写进本票正文；讨论中的未确认建议不视为结论。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/65#issuecomment-5684880417

## 现状核查 · 2026-09-16（尚未形成产品决议）

### 远端、认领与工作区

- 远端 `main` 为 [`ccdd1f699aa5e5db34ec4d656fb1a6f791a25881`](https://github.com/nineofoursyrup/Agent-Alfred/commit/ccdd1f699aa5e5db34ec4d656fb1a6f791a25881)，对应 [CI](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34992421031) 实时回读为 completed / success。
- 开票前总地图的 37 张原生子票全部 closed，仓库仅总地图开放；标题检索及全票标题核对未发现已有 Database / SQL 控制台同范围票。相关前置票的原生 blocked_by 均已关闭。
- 本票已标记 wayfinder:grilling、由 nineofoursyrup 认领，并通过 GitHub 原生 sub-issue 接入总地图；原生依赖显示无阻塞。
- 主工作区 `/Users/nineofour/Agent-Alfred` 保持 `31daa31010dc96639664da1be0767f51a7de83a0`；既有 `.gitignore` 修改仅增加 `.scratch/` 忽略规则。本次从已存在的远端提交对象导出临时只读检查快照；未同步、切换、清理或编辑主工作区，未提交、推送或合并。

### Database 当前缺口

固定 main 的[页面白名单](https://github.com/nineofoursyrup/Agent-Alfred/blob/ccdd1f699aa5e5db34ec4d656fb1a6f791a25881/src/agent_alfred/gateway/web/assets.py#L26)及 [HTTP 路由](https://github.com/nineofoursyrup/Agent-Alfred/blob/ccdd1f699aa5e5db34ec4d656fb1a6f791a25881/src/agent_alfred/gateway/web/handler.py#L325)均没有 Database 页面或 SQL 查询接口。缺口是导航、对象发现、SQL 提交、真实执行、结果与错误、限制、中止及资源释放的整条路径。

当前库是受管 `db.sqlite3`，包含会话/Run、长期记忆、日程、工具审计和计量、来源与遗忘、提炼与镜像状态等表，以及 FTS 和内部对象。哪些对象/列可进入新 SQL 查询面仍待裁定。

[RecordingStore.reading()](https://github.com/nineofoursyrup/Agent-Alfred/blob/ccdd1f699aa5e5db34ec4d656fb1a6f791a25881/src/agent_alfred/runtime/recording.py#L129)借唯一写连接并持写锁；直接承载任意慢 SQL 会影响写入。现有 busy_timeout 不是查询执行时限；生产代码未接通 SQL authorizer、progress/interrupt 或新查询的独立生命周期。独立只读连接、隔离执行及快照等均只是备选机制，未确认。

### 必须承接的既有契约

- [本地 Dashboard 威胁模型](https://github.com/nineofoursyrup/Agent-Alfred/blob/ccdd1f699aa5e5db34ec4d656fb1a6f791a25881/docs/adr/0014-local-dashboard-threat-model.md)：回环、Host/Origin 校验、写请求 CSRF、无 CORS 放行；不是新增鉴权项目。
- [中央脱敏](https://github.com/nineofoursyrup/Agent-Alfred/blob/ccdd1f699aa5e5db34ec4d656fb1a6f791a25881/docs/adr/0003-central-fail-closed-redactor.md)：HTTP 是进程外输出。当前 [Redactor](https://github.com/nineofoursyrup/Agent-Alfred/blob/ccdd1f699aa5e5db34ec4d656fb1a6f791a25881/src/agent_alfred/redact.py#L76)依赖密钥原值及字段名匹配；SQL 别名、切片和编码应成为反例，不能把“查询后调用脱敏器”当作保护充分的证明。此为静态分析提出的验收风险，尚未运行 SQL 验证。
- [真实遗忘的已修订边界](https://github.com/nineofoursyrup/Agent-Alfred/blob/ccdd1f699aa5e5db34ec4d656fb1a6f791a25881/docs/adr/0007-forgetting-must-be-real.md#L41)：记忆行和 FTS 真删，受管派生副本清理；原始会话和删除前既有 trace 仍可人工查看。新 SQL 结果、缓存、迟到响应与查询文本如何承接该契约，本票必须裁定；不把保留历史误称为遗忘失败，也不让结果副本绕过清理。
- [Dashboard 验收惯例](https://github.com/nineofoursyrup/Agent-Alfred/blob/ccdd1f699aa5e5db34ec4d656fb1a6f791a25881/docs/dashboard.md#L30)：临时状态目录、真实 Dashboard/HTTP/SQLite、ScriptedModel；浏览器跨真实边界验收，并核对响应 status、身份和持久后态。具体 SQL 反例、时间/资源限制待逐轮冻结。

### 总地图事实性校正

已从 Not yet specified 移走 Database 问题，细节仅留本票，由原生开放子票入口发现；已补消息分流、受管人格、Markdown 镜像的关闭票索引。

- [实现：workflow — 消息分流](https://github.com/nineofoursyrup/Agent-Alfred/issues/26#issuecomment-5662264771)与[实现：workflow — 手动聚合](https://github.com/nineofoursyrup/Agent-Alfred/issues/27#issuecomment-5670564906)已有交付证据，不能再称 Behaviour 整页未做。Behaviour 的其他泛称范围仍留给用户判断，本 session 不解决另一张决策票。
- [实现：切片③ — ToolRegistry 与内置工具](https://github.com/nineofoursyrup/Agent-Alfred/issues/19#issuecomment-5612160788)已交付受管人格位置、显式文件覆盖及下一 Run 生效；[实现：会话与工作记忆](https://github.com/nineofoursyrup/Agent-Alfred/issues/16#issuecomment-5614466540)保持兼容。不是“内置与用户目录尚待二选一”。
- [实现：记忆提炼与 Markdown 镜像](https://github.com/nineofoursyrup/Agent-Alfred/issues/18#issuecomment-5630462183)已交付状态根下 `memory/facts.md` 与 `memory/episodes.md` 的当前单向镜像、冲突和删除恢复；不扩大为双向导入或历史镜像。
- 地图旧 schema 记录的“尚未并入 main”已事实性修正：`0cfe017` 是当前 main 祖先；[结构：收拢 schema 模块的六项结构气味](https://github.com/nineofoursyrup/Agent-Alfred/issues/35)为 closed / completed，[拆分 schema 职责并保持历史兼容](https://github.com/nineofoursyrup/Agent-Alfred/pull/64)已合并，PR-head 与实际 merge-SHA CI 均成功。

地图写入前核对原正文未被并发改动，写入后逐字回读一致。

### 当前状态与下一步

已读 wayfinder、grilling、domain-modeling、tracker 约定及相关主线 CONTEXT / ADR / 已验收规范。接下来由用户裁定用途、查询目标、原始内容边界、结果完整性与并存优先级，再展开 SQL/PRAGMA、对象/列、限制、中止、遗忘竞态和验收反例。

**本评论是核查记录，不是 resolution。尚无本票产品问题被替用户确认。** 本轮未实现、未运行产品测试、未读取真实用户数据库内容；CI 结论来自远端现有 run 的实时回读，不冒称本轮重跑。最终共同确认之前不关闭本票、不创建实现票、不启动实现。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/65#issuecomment-5684927877

## 第一轮确认 · Q1–Q5

用户在本 session 回复 **“全按建议”**，确认如下边界。此为逐轮决策记录，尚非最终 resolution。

- **Q1 使用目的**：Database 用于人工诊断与核对，包括聊天保存、记忆及来源、Run 与工具账之间的一致性，以及跨表查询和统计。由用户显式执行 SQL，不自动调用模型生成或执行查询。
- **Q2 入口与数据库边界**：独立 Database 页面，沿用 MainBar；固定查询当前运行实例所用数据库，提供对象目录与 SQL 编辑区。不提供数据库路径输入或额外文件连接。可查询表、列与内部对象范围仍待下一轮决定。
- **Q3 原始内容与保护契约**：允许人工查看业务正文及既有契约允许保留的历史；密钥继续受保护，已删记忆和受管旧副本不能借查询恢复。不承诺全库原值透出；必要时限制对象或 SQL 能力。输入端保护、后置脱敏及具体查询机制尚未决定。
- **Q4 结果完整性**：有界浏览；达到限制时明确“结果不完整”，用户可缩小范围。空结果、错误与截断分别呈现。分页方式、行数与字节限制待定。
- **Q5 并存优先级**：聊天和正常数据库操作优先。查询可与聊天并存、不占 Run 准入；用户可中止，超时或离开页面后停止执行并释放资源。必要时终止查询，避免长时间拖住保存或遗忘操作。具体连接与执行机制待定。

### 已明确的领域术语

**Database 控制台**：供用户人工查询当前运行实例持久数据库的只读诊断页面。它的查询输出受既有脱敏与遗忘边界约束，不等于数据库文件全部原值的公开接口。

规划 session 已在隔离工作区 `/Users/nineofour/Agent-Alfred-issue-65-decision`（`codex/65-database-decision`，基线 `ccdd1f699aa5e5db34ec4d656fb1a6f791a25881`）即时补入 `CONTEXT.md` 词条。只有该词汇表改动，未提交；主工作区既有修改保留，未改产品代码。

下一轮展开对象/内容范围、SQL/PRAGMA 能力、结果呈现和限制、查询并发与临时内容生命周期；任何新建议在用户回复前仍为待裁定。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/65#issuecomment-5684975295

## 第二轮前事实查证（不构成产品确认）

使用 Python 3.14.7 / SQLite 3.53.4、纯合成内存及临时数据库进行两个窄范围机制实验，未修改产品、未访问用户真实数据库。

1. **authorizer 按视图名放行不足以保护原值**：应用视图 `safe` 内部执行 `mask(secret)`，仅 `source == 'safe'` 可读取原表；直接读原表被拒，但用户输入 `WITH safe AS (SELECT secret AS content FROM raw) SELECT hex(content) FROM safe` 仍成功取得合成原值的编码。回调第五参数是名称，不是不可伪造的受信任视图身份。此实验否定该简单方案，不声称否定所有受控视图实现。
2. **只读连接与 SQL 权限是不同边界**：`mode=ro` 拒绝原库 INSERT，但仍允许临时表、ATTACH 内存库及附加库建表。`query_only=ON` 拒绝写入，但用户 SQL 请求 `PRAGMA query_only=OFF` 后可以解除它。仅有只读打开、SQL 前缀检查或 query_only 均不能独立证明本票需要的完整边界。

官方资料：[authorizer](https://www.sqlite.org/c3ref/set_authorizer.html)、[动作类型](https://www.sqlite.org/c3ref/c_alter_table.html)、[query_only](https://www.sqlite.org/pragma.html#pragma_query_only)、[stmt_readonly 的有限语义](https://www.sqlite.org/c3ref/stmt_readonly.html)、[PRAGMA 表值函数](https://www.sqlite.org/pragma.html#pragfunc)。

**下一轮候选方向（待用户决定）**：应用先从真实数据库完整提取明确范围并保护内容，再交给独立临时 SQLite 执行用户 SQL；原值不进入用户 SQL 可访问范围。每次创建短命数据集与长期维护保护投影各有成本，不将任何方案记为已选。

若采用物化路线，必须区分输入和输出：不能偷偷截取源数据前 N 行再给出看似完整的 COUNT/JOIN/空结果。输入范围未能完整准备就失败；输出超过限制则按已确认的有界浏览语义明确标记。提取、脱敏、物化及 SQL/结果处理都需要纳入预算。这是设计推论，尚待用户裁定。

[Python sqlite3](https://docs.python.org/3.14/library/sqlite3.html)提供内存数据库等基础能力，但 backup/serialize 原库只得到原始复制，并不完成脱敏。[interrupt](https://www.sqlite.org/c3ref/interrupt.html)请求可能与近完成的查询竞态，取消请求与真实停止释放必须分开；[SQLite 并存语义](https://www.sqlite.org/isolation.html)也不允许把独立读取等同于绝不影响写入。当前用户确认不授权改变原库 journal 模式或绕过既有受管文件规则。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/65#issuecomment-5685024586

## 第二轮确认 · Q6–Q10

用户在本 session 对第二轮回复 **“全按建议”**。以下追加为已确认决策；第一轮 Q1–Q5 保持有效。本评论不是最终 resolution。

- **Q6 受保护输入边界**：每次查询先构建短命、经过内容保护的诊断数据集，再在独立 SQLite 中执行用户 SQL。源数据来自当前实例真实数据库；查询、分组和统计针对处理后的值。声明的输入范围必须完整准备，不能偷偷只取前 N 行再返回统计；超出准备预算则明确失败。页面展示范围和读取时间。接受重复准备成本及大范围查询可能被拒绝；具体对象清单和 SQL 能力仍待决定。
- **Q7 分页与结果上限**：一次最多 1,000 行、结果数据总量 2 MiB，每页 100 行。翻页只浏览本次已取得结果，不重跑 SQL、不长期保留数据库游标。达到任一上限明确标记截断；不自动查询总行数。首行即超限报告结果过大，不能显示为空结果。重跑由用户显式触发。精确字节计量及类型编码仍待细化。
- **Q8 查询任务与预算**：整个实例最多一条 Database 查询任务，不排队；第二条立即告知已有查询。总预算 5 秒，覆盖数据准备、SQL 执行及结果处理。用户中止后的 1 秒内实际停止和释放资源是验收目标。取消已请求与实际已停止分开显示；浏览器停止等待不能冒称停止。具体实现仍需证明目标可兑现。
- **Q9 草稿与保存**：v1 仅保留当前页面内存中的 SQL 草稿和结果，离页或刷新清空；不做持久查询历史、结果收藏、文件导出。允许用户显式复制。SQL 和结果不进入 URL、应用日志或 trace，不自动重跑。
- **Q10 整体失效**：任何 memory_revision 变化，都取消在途查询、清除临时数据集与已显示结果；迟到响应不得重新显示。断线立即隐藏并清除结果；重连核验后由用户重新执行。保留用户手写 SQL 草稿，但不自动执行。接受无关记忆变化及纯统计查询也会被打断，v1 不引入精细结果来源追踪。

下一轮将展开诊断对象和内容投影、SQL/PRAGMA/函数范围、值与错误呈现及剩余资源边界。未回答的新问题仍是待裁定。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/65#issuecomment-5685089970

## 第三轮前事实查证与文档同步（新建议尚待裁定）

Q6–Q10 已同步到隔离规划工作区 `CONTEXT.md` 的“诊断数据集”“诊断结果”词条，并记录 ADR `0040-database-sql-reads-protected-ephemeral-datasets.md`。本次仍只有规划文档改动，`git diff --check` 通过，未提交或修改产品。

### 数据投影需要保留的事实

- `agent_log.content` 是包含不同内容块的 JSON；既有人工消息读取在 `runtime/runs.py` 解析并保护内容，`messages.message_plain_text()` 仅拼接 TextBlock。不能把整个原 JSON、ThinkingBlock、工具参数和工具结果当作普通消息正文开放。
- `runs.telemetry` 混有 `usage.raw`、来源和其他运行载荷。可由白名单解析生成标准 Attempt 行，保留未知/损坏状态；不能直接复制原 JSON，不能把缺少 Attempt 明细当零调用。
- `tool_metering` 的身份是 `(run_id,step_index,call_id)`；`tool_ledger` 缺少 `step_index`。不能按行数相等或 `run_id+call_id` 强制一一关联；`external_tool_operations` 才有可投影的可信 ledger_id 映射。
- 镜像持久 generation/时间/冲突只是数据库记录。现有镜像 `status()` 会读取真实文件并签发 confirmation，新诊断集合不能复用它而突破当前数据库范围，更不能凭数据库记录声称真实文件此刻 ready。

### SQL 与值类型的窄实验

Python 3.14.7 / SQLite 3.53.4 纯合成内存实验：authorizer 可拒绝 `SQLITE_RECURSIVE`、未列明函数和对象，同时保留非递归 WITH、JOIN、聚合及窗口；递归 CTE 不能只依赖是否出现 RECURSIVE 关键字。EXPLAIN 与 EXPLAIN QUERY PLAN 触发相同授权动作，不能仅靠 authorizer 区分，且解释临时诊断库不等于解释生产索引性能。

值类型实际覆盖：重复列名、int64 最大值、非有限 REAL、NULL、BLOB；无效 UTF-8 TEXT 发生读取错误。因此需要有序列/行数组及逐值类型，不能使用列名作为唯一字典键或把全部整数交浏览器 Number。

资料：[Python execute](https://docs.python.org/3.14/library/sqlite3.html#sqlite3.Cursor.execute)、[SQLite authorizer 动作](https://www.sqlite.org/c3ref/c_alter_table.html)、[SQLite 类型](https://www.sqlite.org/datatype3.html)、[EQP 输出边界](https://www.sqlite.org/eqp.html)、[SQLite limits](https://www.sqlite.org/limits.html)。

### 资源限制的适用范围

5 秒总预算及 1,000 行/2 MiB 输出上限不等于执行内存上限。独立 SQL worker 可用可信初始化的 `hard_heap_limit` 限制该进程中 SQLite 管理的堆分配；必须验证当前构建确实启用相应记账并读回配置。它不是整个进程 RSS 或 Python 字符串/JSON/脱敏内存的上限，不能混称。[hard_heap_limit64 官方说明](https://www.sqlite.org/c3ref/hard_heap_limit64.html)

上述均为只读查证及临时合成实验；产品的范围、SQL 白名单、类型呈现和阈值仍由下一轮用户裁定。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/65#issuecomment-5685139051

## 第三轮确认 · Q11–Q14

用户在本 session 回复 **“全按建议”**。Q1–Q10 保持有效；以下是新增已确认范围，仍不是最终 resolution。

### Q11 诊断对象与内容

首版通过明确命名的 `diag_*` 对象，逐项白名单开放以下五组：

| 数据组 | 已确认内容 |
| --- | --- |
| 会话与运行 | Session、Run、人工可见的用户/助手文本、标准化 Attempt 用量 |
| 当前记忆 | 事实、情景、版本、来源及实际使用关联 |
| 工具记录 | 请求计量、业务账、已有证据支持的关联 |
| 日程 | 时间、标题、参与者、备注 |
| 维护状态 | 遗忘进度、提炼批次及来源、镜像数据库记录 |

正文先保护再入查询集合。排除 thinking、工具参数/结果、原始 telemetry、凭据、指纹、恢复载荷、提炼候选、FTS 内部表及生产库原始 schema。

查询覆盖各 Session，不跟随 MainBar 当前会话缩小。每次完整准备 SQL 引用的诊断对象；v1 不另做输入范围筛选器，SQL WHERE/LIMIT 不绕过输入准备预算。目录展示对象名称、列、来源和保护说明；镜像数据库记录不冒称文件此刻可读。对象及精确列清单由本轮确认范围整理后纳入最终审阅。

### Q12 SQL 能力

允许单条 SELECT、非递归 WITH、子查询、JOIN、筛选、排序、分组、聚合、窗口、UNION、LIMIT/OFFSET，及逐名白名单内的字符串、日期、数学、JSON 标量函数。

拒绝多语句、写入、DDL、事务控制、递归 CTE、所有 PRAGMA（含表值形式）、ATTACH、扩展加载、未知函数及未开放对象。首版不支持参数绑定、EXPLAIN、EXPLAIN QUERY PLAN、json_each/json_tree。完整函数逐项清单将在最终规范中呈现，不以“安全函数”泛称代替。

### Q13 结果值与失败

- NULL 与空字符串分开；整数带类型以十进制字符串传输。
- 按列位置保存结果，允许重复列名，同列不同行可有不同类型。
- BLOB 以带类型和长度的十六进制文本展示并计入大小限制。
- 文本按字面展示，不执行 HTML、不解析 Markdown。
- 非有限浮点数、无效文本编码、无法安全解析的源内容：整次报错，不静默替换或跳行。
- 只有行数或总大小限制可返回完整行前缀并明确截断；不偷偷截短单元格正文。

### Q14 资源限制

| 项目 | 上限 |
| --- | --- |
| SQL 文本 | 64 KiB |
| 输出列数 | 64 |
| 源数据单行/单值 | 2 MiB |
| 完整受保护输入数据集 | 64 MiB |
| 独立执行进程内 SQLite 堆 | 128 MiB |

读取、脱敏、编码按行处理；输入超限明确失败，不查询残缺数据集。采用独立受控执行进程，验证超时、中止及资源回收。SQLite 堆上限不等于整个进程内存上限，也不覆盖 Python；分别验证，不用输出大小冒称内存限制。

下一轮：HTTP 生命周期、请求身份与取消竞态、故障呈现，以及浏览器→真实 HTTP→真实 SQLite→持久后态的验收反例。未开始实现、未提交、未关闭本票。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/65#issuecomment-5685228103

## 第四轮前事实核查：HTTP 与保护失效

以下是现有实现事实，不是新增产品确认。

- 当前 ThreadingHTTPServer 每连接独立线程（`gateway/web/lifecycle.py:850-866`），所以同步 POST 等待受限 worker 与独立 cancel/status HTTP 可以并存；等待时不能持数据库锁、配置锁或 MutationGate。查询无须伪造 Run，也不必把结果送进 SSE。
- 现有 `/api/entry` 提供实例身份和 CSRF；Host/Origin/CSRF 与 no-store 继续适用。普通请求有独立登记与清理（`gateway/web/handler.py:113-142,281-299`）。请求丢失、worker 结束、HTTP 响应结束是不同事实。
- 当前关停在 `gateway/web/server.py:488-543` 先停止新 HTTP，再等待普通请求退出，随后才 Host.close。若只把查询取消放进 Host.close，同步 POST 会先卡住排空；需要更早的查询停止准入与取消接缝。资源未实际退出时沿现有所有权原则保留并报告未完成，不假报成功。
- `Redactor.remember()` 目前只追加密钥集合，没有公开 revision/snapshot/变化回调（`redact.py:76-96`）。Tavily prepare、MCP 配置解析和 Run admission 都可能记住新密钥，甚至配置后续验证失败时也已发生。因此只监听 memory_revision、成功重载设置或 Connections 变化均不充分；不得把 connections_revision 冒充脱敏代次。
- 既有 AccountingSnapshots 具备实例/单调时钟 TTL/数量及字节上限惯例（`runtime/accounting.py:212-236,364-400`）；MCPControl 有操作身份/status/TTL 惯例，但其占用 mutation lease 的部分不能移用于 Database。

下一轮将提出查询句柄、取消先到与迟到请求、结果响应丢失、保护规则变化、关停和清理失败的明确建议。现有资源数字与 Q1-Q14 不重问、不自行改写。

精确对象字段事实已整理为临时候选附录；尤其 runs 无 recording_state 物理列、tool_ledger 无 step_index、镜像与遗忘清理实际错误列名是 error。最终规范会明确投影/派生关系，不使用不存在的列或虚构状态。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/65#issuecomment-5685300034

## 第四轮确认 · Q15–Q18

用户在本 session 回复 **“全按建议”**。Q1–Q14 保持有效；以下追加为已确认决策，仍非最终 resolution。

### Q15 请求身份、丢失响应与期限

先取得一次性查询句柄，再提交 SQL。未使用句柄有效 30 秒；过期、已使用、跨进程句柄不能再启动查询。SQL POST 等待并返回本次结果，取消和状态查询走独立 HTTP；取消先到时，迟到执行也不能启动。

响应丢失后只查无正文终态，明确“结果未收到”；不自动重跑，不缓存结果供重取。终态保留 60 秒；待用句柄与终态记录合计最多 64 条。活动任务不因缓存淘汰而消失。

五秒执行预算从 SQL 获准执行起计，含准备、执行、结果编码；请求正文读取及响应发送各有独立 1 秒期限。

### Q16 完成、取消与真实回收

服务端统一确定完成与取消的先后。完成未确定时收到取消，停止查询并丢弃结果；完成已确定则如实返回“取消前已完成”，不能改称已取消。用户点击取消后，页面仍丢弃迟到结果。

只有 worker、源库连接及临时数据集实际退出，才显示“已停止”；查询占用在实际回收后释放。一秒内未回收，显示清理失败并暂停新 Database 查询、保留清理责任；取消目标未通过，不放宽时限冒称成功。

### Q17 保护规则版本与遗忘

新增独立保护规则版本。已加载密钥集合或脱敏规则变化，也使在途查询、临时数据集和页面结果整体失效；通知只携版本，不携密钥。

数据准备及结果发布均核对保护版本，不能把旧规则数据标成新版本。遗忘宣布受管清理完成前，服务端旧诊断副本也必须完成清理；浏览器沿既有失效、断线规则清除结果。手写 SQL 草稿仍保留。

### Q18 故障与关停

数据库缺失、不可读、结构不兼容，或保护/资源限制无法生效时，明确 Database 不可用，不返回假空结果。不自动建库、迁移、修复或改变 journal 模式。

聊天因记录或计量故障暂停时，只要数据库确实可安全读取，仍可只读诊断。关停先停止查询准入并取消查询，再等 HTTP 排空；不依赖浏览器补发取消。错误只给机器码与安全说明，不回显原载荷、密钥或内部路径。

下一步以截断、缺失与损坏、关联身份、页面生命周期和临时数据文件边界的反例核对剩余细节，再汇总最终决策与纯实现票草案。尚未关闭本票、未创建实现票、未启动实现。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/65#issuecomment-5686193594

## 第五轮确认 · Q19–Q24

用户在本 session 回复 **“全按建议”**。Q1–Q18 保持有效；以下追加为已确认决策，仍待完整决策稿最终确认，不是 resolution。

### Q19 完整与截断

取得第 1,001 行才证明行数截断；恰好 1,000 行且确认 EOF 则完整。2 MiB 按最终 UTF-8 JSON 成功响应正文计量，包含列名、类型标记、转义和元数据，不含 HTTP 头。只保留完整行；首行或列元数据都放不下则报错。

探查下一行发生错误、取消或超时，整次失败。截断后不继续扫描，不保证未读取部分没有错误，也不给未经计算的总行数。用户 SQL 自己的 LIMIT 只界定该 SQL 结果，不证明原库仅有这些行。

### Q20 缺失与损坏

合法缺失明确为未知／未记录，不能当作零；明确记录的空 Attempts 才表示零条。提供覆盖状态，避免已有记录统计冒充完整总量。损坏 JSON、非法类型、重复身份使相关数据集准备失败，不能静默跳过或退化成正常未知。

### Q21 保护后的关联身份

若保护处理会改变身份或关联键，拒绝准备涉及该对象的数据集，并给安全错误说明。不能让不同原身份脱敏为同值后发生伪关联；v1 不另造匿名身份映射。普通正文仍按既定规则保护。

### Q22 页面生命周期

离开 Database 路由、刷新或关页：取消查询，清除草稿和结果。仅切换标签或最小化：保留当前页内存，五秒预算照常。断线清结果、保留手写 SQL。

浏览器后退缓存恢复时，重新核验并显示空结果，不恢复旧数据。服务端期限兜底，不能依赖关页时成功发送取消请求。

### Q23 文件行为

诊断数据集、结果及排序中间内容全部驻内存，不新建内容临时文件。禁止修改原库数据、写入 WAL 内容、执行恢复或改变 journal 模式。

仅允许 SQLite 为读取当前受管数据库所必需的锁及 -shm 协调；不开放任意文件访问。无法保证边界则 Database 明确不可用。这里不承诺控制操作系统 swap。

### Q24 整条路径验收

必须使用隔离测试库，通过真实浏览器 → 正式 HTTP 接口 → 独立执行进程 → 真实 SQLite，验收正常／空／截断／错误／类型、写入与文件及脱敏绕过反例、慢查询与取消竞态及真实回收、断线关停，以及与聊天保存和真实遗忘并发。

无业务写入时原库内容不变；并发时只发生预期业务变更。单元测试是补充，未运行明确标记，不能用模拟成功替代完整路径。

下一步整理最终决策稿与纯实现票草案，连同精确对象／函数名单、接口及验收合同供用户最终确认。未关闭本票、未创建实现票、未启动实现。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/65#issuecomment-5686473812

# Resolution：Database 只读 SQL 控制台 · r1

**状态：用户已最终确认，决策冻结。** 用户在本 session 对完整 r1 回复“确认”；Q1–Q24、对象列、函数名单、HTTP 契约及附录 A–D 均获确认。本评论为 resolution，产品尚未实现。

批准源文件 SHA256：`689368504a8d1ae5143c36efdba8a8f5191685f5521afe36527d3ae0afb72585`。发布时仅将草稿标题、确认状态和规范源指代改为已确认状态，未更改产品条款。

决策入口：[决定：Database 只读 SQL 控制台的用户路径与验收](https://github.com/nineofoursyrup/Agent-Alfred/issues/65)。总地图：[地图：Agent-Alfred v1 — 本地优先、无框架、可追踪的私人 AI 助手](https://github.com/nineofoursyrup/Agent-Alfred/issues/1)。以本条 resolution comment 为规范源；实现票只组织交付范围与验收，不另立一套可独立漂移的决定。

## 1. 依据与范围

核查基线是远端 main `ccdd1f699aa5e5db34ec4d656fb1a6f791a25881`；其 [CI](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34992421031) 成功。基线没有 Database 页面、导航入口或 SQL API；SQLite 生产读写入口会迁移数据库，不能直接当作本功能的只读打开器。

Database 仍在 v1 范围。已有消息分流、受管人格和记忆 Markdown 镜像的交付记录已按证据纠正地图；这不宣布全部 Behaviour 完成，剩余 Behaviour 范围仍留在地图等待另一场决策。

本功能供人核查聊天持久化、记忆及来源关系、Run／Attempt／工具账和相关状态；它不是模型工具，不自动生成或执行 SQL，不产生模型 Run、Attempt、工具业务账或模型调用。

## 2. 完整用户路径

1. 用户从 Dashboard 导航进入 **Database**，MainBar 仍可使用。数据库固定为当前运行实例的受管数据库，不显示或接收可切换的文件路径。
2. 页面先核验实例、可用性、记忆修订和保护规则版本，再展示诊断对象目录、列说明和 SQL 编辑器。目录只含附录 A 的对象；覆盖所有 Sessions，不随 MainBar 当前 Session 暗中筛选。
3. 用户可从目录查看字段／保护说明，把对象名或示例写入编辑器；任何示例都须用户显式执行。页面明确说明：查询受保护的临时数据、SQL WHERE／LIMIT 不缩小准备范围、结果有上限。
4. 点击执行：先取得一次性查询句柄，再提交 SQL。显示准备／执行中以及取消入口；按钮不能让同一点击发起两次查询。其他页面或标签已有任务时立即显示忙，不排队。
5. 服务端从真实源库的**同一个一致读事务**，完整提取 SQL 实际引用的诊断对象所需行，逐行保护后构建临时数据集；不把用户 SQL 交给源库。准备完成立即结束源库读事务，后续用户 SQL 仅在独立 SQLite 数据集运行。
6. 成功时按原列顺序展示结果、来源对象、读取时间和保护依据；空结果显示“0 行”，同时保留列名。100 行一页，翻页只访问本次已收到的行。截断明显标识原因，不能表现为完整结果或提供猜测的总数。
7. 用户可以复制当前 SQL 或已展示的内容。无导出、持久查询历史、收藏、自动刷新、结果重取缓存；SQL 草稿与结果只在当前页内存。
8. 离开路由、刷新、关页时取消查询并清草稿／结果。切换标签或最小化本身不清除；原执行期限继续。后退缓存恢复须显示空结果并重新核验。
9. 记忆修订或保护规则版本变化使正在准备／执行／发送的旧内容失效；清数据集及页面结果、拒绝迟到响应，保留手写 SQL。SSE 断开或浏览器 offline 同样隐藏并清除结果；重连核验通过后仍由用户手动重跑。

页面至少能区分：核验中、可执行、不可用、忙、准备／执行中、已请求取消、结果完整、结果截断、空结果、执行错误、超时、失效、结果未收到、已实际停止、清理失败。不能把网络失败显示成 SQL 空结果。

## 3. 数据、保护与只读保证

### 3.1 保护发生在 SQL 之前

诊断数据集只含附录 A 的显式投影。所有开放字符串值均经过当前保护规则；业务正文也适用。字段名称安全不代表其值安全。SQL 的切片、编码、JOIN、分组、统计只能接触保护后的值。

当前记忆及其版本关系来自当前源库，不恢复已删除事实或回放旧受管副本。原始会话和既有 trace 原有的人工阅读边界保持；Database 只开放 user／assistant 的 TextBlock 文本，且不因此开放 raw trace。

保护会改变身份或关联键时，相关数据集准备失败，不能将多个身份替换成同一个掩码后制造 JOIN。身份键包括对象主键、唯一身份和承载引用关系的列；普通正文被保护不构成失败。v1 不引入替代身份映射。

不开放 thinking、工具参数／结果、原始 telemetry、凭据、指纹、签名、文件恢复载荷、提炼候选、FTS／shadow 表或生产 schema。保护规则更新只广播版本，不广播密钥；版本须随所有实际加载密钥及规则变化推进，不能以配置保存成功或 Connections 观测代替。

### 3.2 完整准备与快照

输入范围是 SQL **实际引用的诊断对象的全部行**，含构造其派生列必需的可信来源关系；v1 无独立输入时间／Session 过滤器。只引用 `diag_runs` 不必准备整个目录；引用 facts 并不开放底层 provenance 表的任意内容。引用零个对象的表达式查询仍经过相同的可用性、预算和生命周期检查。

WHERE／LIMIT／仅选择某一列都不授权跳过该对象其他行的保护与完整性检查。未引用对象的业务行损坏不牵连无关查询，但必要 schema／能力不可用仍明确失败。超过输入预算时整次失败，不能取前 N 行后让 COUNT 看似完整。

所有被引用对象使用同一源库一致读事务。`memory_revision` 不是全库提交号：聊天、账本等更新未必推进它；因此不能用前后 revision 相同冒充事务快照。展示的 `read_at` 是建立本次源库读取的时间，不是每行写入时间，也不保证结果显示时仍是数据库最新值。

### 3.3 原库与文件边界

源库通过当前实例持有的受管身份以只读方式打开，禁止自动创建、迁移、修复、恢复、写原库数据、写入 WAL 内容或改变 journal 模式。不得将活动数据库伪称 `immutable`，不得以原库 `backup()`／`serialize()` 副本充当脱敏数据集。

只允许读取当前受管库及其必要事务伴随文件，允许 SQLite 所需的锁和 `-shm` 协调；用户无法指定文件、ATTACH、扩展、虚表模块或网络目标。诊断数据集、结果、排序／物化中间内容均在内存，不生成内容临时文件；提取连接也适用。可信初始化须核查 SQLite 构建和有效设置，不能仅设置 `temp_store=MEMORY` 就假定保证成立。

这约束应用和 SQLite 的内容文件，不承诺控制操作系统 swap／转储，也不承诺抹除用户已经复制的内容。无合法业务写入的验收场景须证明数据库及事务内容未被诊断改变；允许的锁／共享内存协调单独核查。

## 4. SQL 能力与边界

允许单条 SELECT，含非递归 WITH、子查询、JOIN、WHERE、ORDER BY、GROUP BY、HAVING、聚合、窗口、集合组合和 LIMIT／OFFSET；常规运算、CASE、CAST、DISTINCT、NULL、字符串及数值／BLOB 字面量可用。允许末尾分号及注释；不能按分号文本简单分割语句。

函数严格按附录 B 名单。拒绝多语句、写入、DDL、事务控制、递归 CTE（包括未写 RECURSIVE 但实际递归）、全部 PRAGMA 及 table-valued PRAGMA、ATTACH／DETACH、扩展加载、未知函数及未列对象。无参数绑定／占位符、EXPLAIN／EXPLAIN QUERY PLAN、`json_each`／`json_tree`、任意用户定义函数或排序规则。

可访问本次数据集的 `diag_*` 对象及其显式列，不能借 `sqlite_schema`、`sqlite_master`、隐藏 rowid、生产表名、schema 限定、CTE 重名或其他入口越过名单。CTE 的自定义列是本次 SELECT 的局部关系；重名不赋予访问生产表的权限。普通 BINARY／NOCASE／RTRIM 内建排序语义可用；不注册应用自定义排序回调。

实现须使用 SQLite 提供的真实语法／授权边界及独立数据边界，不能仅靠关键词正则、语句只读判定或可信 view 名判断。用户 SQL 的连接不持有源库 attachment，也不持有文件／网络／应用函数能力。

## 5. 结果与完整性

### 5.1 类型

响应以有序 `columns` 和等长 `rows` 表达，不能用列名作为 JSON 对象键吞掉重复列。每个单元格单独携带 SQLite 动态类型：

| 类型 | JSON 单元格表达 | 展示 |
|---|---|---|
| NULL | `{"type":"null"}` | 专门的 NULL 标记，与空串不同 |
| INTEGER | `{"type":"integer","value":"9223372036854775807"}` | 精确十进制文本，不经 JavaScript Number |
| REAL | `{"type":"real","value":1.25}` | 有限浮点值；不是精确金额承诺 |
| TEXT | `{"type":"text","value":"..."}` | 纯文本，保留内容，不执行 HTML／Markdown |
| BLOB | `{"type":"blob","hex":"00ff","byte_length":2}` | 十六进制及原始字节数；膨胀后的正文计量 |

原数据中的金额／成本 decimal 字符串保持文本；查询主动 CAST 为 REAL 是用户表达式的浮点语义，不可将结果称为精确金额。来源时间文本保持原值，不擅自统一历史时区。

非有限 REAL、不能解码的文本、无法安全解析的受选源内容使整次失败。合法无 TextBlock 的消息为已知空文本；损坏消息不能伪装成空文本。

### 5.2 上限与 EOF

结果最多 1,000 行、64 列、2 MiB。2 MiB 按**最终 UTF-8 JSON 成功响应正文**计量，包含列名、单元格标签／转义、BLOB hex 及信封元数据，不含 HTTP 头。编码统一为 UTF-8、紧凑分隔、禁止 NaN／Infinity；应按实际送出的字节判断，不按 Python 字符数估计。

只返回完整行构成的前缀；不得切断单元格。列元数据或首行无法容纳时返回过大错误，不能伪称空结果。实现须为最终截断元数据预留／核实空间，使任何成功响应都不超过上限。

EOF 才证明完整。1000 行后取得第 1001 行证明行上限截断；下一完整行无法纳入正文证明字节上限截断。实际探查行仍须验证类型；探查出错、取消或超时整次失败。若同时触发两个上限可列出两个原因，不推测剩余行数；确认某上限后不继续扫描剩余 SQL。

`SELECT ... LIMIT 1000` 正常 EOF 可标本条 SQL 完整，不表示输入只有 1000 行。页面显示“返回 N 行／结果截断”而非“数据库共 N 行”。不额外 COUNT，不保留服务端游标；翻页不重新执行。

### 5.3 缺失不等于零

Attempts 的记录覆盖必须与统计一同可见：明确记录、明确空数组、未记录分开表达。进行中的 Run 已有记录只说明“当前已记录”，不承诺最终调用总数；结束状态也不能弥补丢失记录。计量记录与业务账的计数本来就可能不同。

合法缺失的可选 token／cost 值为 NULL／明确未知；非法类型、负 token、坏 JSON、重复身份等损坏使涉及对象的数据集失败，不能复用旧读模型将损坏静默降级为正常未知。未知扩展字段不输出；已知格式中的可选缺失与违反格式分开验证。

## 6. 预算、并发、取消与受管清理

| 项目 | 上限／行为 |
|---|---|
| SQL 文本 | 解码后的 SQL 重新编码 UTF-8 ≤64 KiB；JSON 信封仍受公共 HTTP 体积限制 |
| 原始源值／源行 | 单值及完整行各 ≤2 MiB；读取时先做有界检查，不能先无界装载再检查 |
| 完整受保护输入 | ≤64 MiB，包括所有被引用对象的行、类型／列说明及覆盖元数据 |
| SQLite 堆 | 独立受控执行进程内所有 SQLite 连接合计 ≤128 MiB；能力不可核实则不可用 |
| 同时任务 | 当前 runtime 仅 1 个；不排队，不占 Run admission／mutation lease |
| 执行期限 | 准入至准备、SQL 执行及结果编码总计 5 秒；忙立即返回 |
| HTTP 正文读取／响应发送 | 各自另有 1 秒期限，不能靠慢连接永久占用资源 |
| 用户取消 | 请求被服务端接受后目标 1 秒内实际停止与回收；超限按失败处理 |
| 未使用句柄 | 30 秒有效，一次使用 |
| 无正文终态记录 | 60 秒；与未使用句柄合计最多 64 条，不能淘汰活动任务 |

源行与受保护输入使用有界、可复核的字节记账：TEXT 为严格 UTF-8 字节，BLOB 为原始字节，整数／浮点为其固定宽度数值表示，另计字段类型／长度边界及行／对象元数据；具体 framing 属实现细节，必须固定、记录并用边界测试证明。不得把 Python 对象数、SQLite 页数或压缩大小当作这些字节预算。

128 MiB 是 SQLite 分配器的堆上限，**不是** Python 堆／进程 RSS 上限。实现须限制 Python 侧累积和编码副本，不能宣称已有全进程内存硬上限。源库读取、保护、IPC、SQL、编码和释放全部受可终止生命周期监管；仅 SQLite progress handler 不足以覆盖这些阶段。

普通聊天保存、记忆写入／遗忘优先。源库读事务不能占用现有共享 writer 的 `_db_lock` 执行任意 SQL；发现妨碍正常写入时应取消诊断并释放源库锁，再推进正常操作。不可等待诊断自然跑完才允许遗忘。

服务端串行裁定完成与取消：完成尚未确定则停止／丢弃；完成已确定则说明“取消前已完成”，不能改称已取消。用户点击取消后页面始终拒收该次迟到结果。完成、网络送达与清理是不同事实；HTTP 成功或 abort fetch 不能证明 worker 已退出。

worker、源库连接、数据集和服务端结果缓冲均须有可达 owner。只有实际释放才标已停止并解除相应占用。发送中的响应缓冲也属于受管内容，发送完成／失败／失效后释放；不会因 SQL 已完成而逃出清理责任。旧字节可能已经发出时，浏览器按版本与连接世代拒绝它们。

取消／超时／失效的一秒回收目标未满足：显示清理失败，暂停新的 Database 查询，保留清理责任和可诊断状态。完成清理并通过能力核验后才恢复准入；不能遗失 owner 后将占用“解锁”。关闭 runtime 时先停诊断准入并取消，再等待普通 HTTP 请求排空。

遗忘操作在声明受管清理 complete 前，必须确认服务端所有相应旧诊断副本和发送缓冲已经释放；只隐藏 UI 不算完成。浏览器不能由服务端证明清除了用户外部副本，沿既有断线隐藏与恢复核验契约处理。

## 7. HTTP 与失效契约

沿用现有同源／Host／Origin／CSRF／实例身份防护和 no-store。SQL 不出现在 URL、日志、trace、指标标签或错误回显中。错误和状态端点不返回 SQL／结果正文。SQL 请求虽语义只读，仍必须经过 POST 的 CSRF 防护。

建议冻结下列路由；若执行时仅需调整内部模块／路由组织，须保持一对一公开语义并更新同一规范附录，不能改变产品行为：

| HTTP | 输入与职责 | 输出 |
|---|---|---|
| `GET /api/database` | 核验当前实例及诊断能力，读取静态对象目录和安全状态 | 实例、可用性／原因码、schema 兼容信息、memory／protection 版本、对象列清单、限额 |
| `POST /api/database/queries` | 当前实例；签发随机不透明一次性句柄 | query_id、instance_id、30 秒有效期；无 SQL |
| `POST /api/database/queries/{query_id}/execute` | instance_id、页面已核验的 memory_revision／protection_version、sql；拒绝未知参数 | 同步返回结果或有类型的安全错误 |
| `POST /api/database/queries/{query_id}/cancel` | 当前实例；已签发未执行句柄也能取消 | 无正文状态；取消先到，迟到 execute 不能启动 |
| `GET /api/database/queries/{query_id}` | 核对既有句柄状态；路径不含 SQL | 无正文运行／终态及 cleanup 状态，不返回缓存结果 |

句柄严格绑定进程实例；过期、已使用、跨实例不能重新执行。未使用句柄及终态是有界记录：达到 64 条时先移除过期项；不驱逐仍有效的未使用句柄或终态承诺，仍满则拒绝签发。活动任务独立持有，不依赖该缓存存活。期限用单调时钟判定，绝不能自动重试执行。

执行请求版本与当前状态不一致时返回失效，不偷偷以新版本执行。发布前再次核对 `instance_id + memory_revision + protection_version`；保护采样、源读取及发布要形成可证明的顺序，旧规则数据不能贴新版本。页面另持连接／页面世代，任一旧代响应均不安装。

成功正文语义：`query_id`、`instance_id`、`memory_revision`、`protection_version`、`read_at`、`objects`、`coverage`、`columns`、`rows`、`returned_rows`、`truncated`、`truncation_reasons`。`objects` 为实际完整准备范围；覆盖元数据须说明 Attempt 的已记录／零／未知和进行中 Run，不提供估算总调用量。版本／身份为不透明字符串，避免前端数值精度问题；JSON 列索引／返回行数等受上限约束的小整数可为 number。

status 不持有正文，至少能表达 prepared／running／stopping／completed／cancelled／timed_out／invalidated／failed，外加 cleanup=pending／released／failed；未使用过期后返回 expired，在有界记录退休后返回 not_found。终态出现不自动证明浏览器收到了正文，也不自动证明 cleanup=released。客户端丢失 execute 响应只能查此状态；已完成但未收到结果应显示“结果未收到”，由用户发起新查询。

错误统一包含机器码和固定安全说明，必要时附无正文身份与可行动状态。至少区分 invalid_request／sql_rejected／sql_error／database_busy／handle_expired／handle_used／instance_changed／database_unavailable／input_too_large／result_too_large／resource_limit／query_timeout／query_cancelled／data_invalidated／data_invalid／protected_identity／cleanup_failed。用户 SQL 的名称或数据库异常原文不能直接拼入错误。使用现有 HTTP 风格：400 参数／SQL 边界错误；409 忙、身份／版本／句柄冲突及取消／失效；410 已确认过期；404 无记录；413 尺寸超限；503 不可用／资源或清理失败；504 执行超时。HTTP 连接已失败时不伪造送达错误。

## 8. 不可用与非目标

数据库缺失、不可读、schema 不兼容，保护规则／只读／内存／终止能力无法生效时，页面明确不可用。迁移账只说明哪些迁移执行过，不能单独证明结构完整。只读入口不自动修复，也不为了诊断启动真实模型。

聊天因记录／计量故障暂停并不自动封死诊断；只要可以独立、安全地读取受管库，Database 仍可用。数据库真实损坏时不得假成功。

非目标：任意数据库连接、数据库编辑、管理 PRAGMA、外部文件／网络查询、原值调试后门、raw schema／trace、模型 SQL 工具、当前目录价格估算、数据库总量自动统计、跨请求长期快照、服务端结果重取、导出／持久历史、索引调优、迁移、全进程 RSS 硬上限、全 Behaviour 范围裁定。本 session 不启动实现或 Git 交付。

## 附录 A：诊断对象与列合同

下面全部是**待实现的诊断投影**，不是已有生产表。未列出的列不可访问。原物理 INTEGER／REAL／TEXT 按源类型保留并验证；布尔标志投影为 INTEGER 0／1；明确可缺失值保留 NULL，不能以空串或 0 代替。文本均按第 3 节保护；原始 BLOB 不能绕开一个本应是文本／JSON 的源字段校验。

| 对象 | 开放列 | 可信来源／派生 |
|---|---|---|
| `diag_sessions` | `session_id, created_at, activity_revision` | sessions；无虚构 title |
| `diag_runs` | `run_id, purpose, session_id, gateway, entry_surface_id, phase, outcome, accepted_at, started_at, finished_at, activity_revision, admission_state` | runs；不输出 prompt_preview 或 telemetry |
| `diag_messages` | `message_id, session_id, run_id, role, source, created_at, consolidated, text` | agent_log；message_id=id；text 为受保护后已验证 TextBlock 按原顺序拼接 |
| `diag_attempts` | `run_id, attempt_id, outcome, endpoint_id, model_id, total_input_tokens, uncached_input_tokens, cache_read_tokens, cache_write_tokens, output_tokens, reasoning_tokens, endpoint_reported_cost_usd` | runs.telemetry 的 attempts 列表，仅已知字段 |
| `diag_attempt_coverage` | `run_id, state, recorded_count, run_finished` | 每个 Run 一行，见下方覆盖规则 |
| `diag_facts` | `id, record_version, subject, fact, origin_kind, origin_batch_id, origin_source, origin_call_id, created_at, modified_at, human_protected, last_change_origin_type, last_change_origin_source, last_change_origin_call_id, last_change_origin_batch_id, provenance_state` | facts 及当前版本的 memory_provenance；后五列是派生 |
| `diag_episodes` | `id, record_version, summary, occurred_at, occurred_until, origin_kind, origin_batch_id, origin_source, origin_call_id, created_at, modified_at, human_protected, last_change_origin_type, last_change_origin_source, last_change_origin_call_id, last_change_origin_batch_id, provenance_state` | episodes；派生规则同 facts |
| `diag_memory_sources` | `kind, memory_id, record_version, source_group_id` | memory_sources；版本化来源关系 |
| `diag_history_groups` | `group_id, kind, container_id, evidence, occurred_at` | history_groups 左关联 history_group_times；时间缺失为 NULL，不开放 evidence_id |
| `diag_memory_uses` | `kind, memory_id, record_version, consumer, attempt_id, purpose` | memory_uses；不把 consumer 伪称 run_id |
| `diag_history_reads` | `source, consumer, attempt_id, purpose` | history_reads；组到组的实际读取关系 |
| `diag_tool_metering` | `run_id, step_index, call_id, ordinal, tool_name, source_id, capability_id, effect, requested_at, start_confirmation, result, reason_code, finished_at, operation_id, model_delivery, cost_kind, cost_units, cost_unit, cost_source, cost_service, cost_reason` | tool_metering；reason_code 与 cost_* 为安全派生，不开放 cost 原 JSON |
| `diag_tool_ledger` | `id, tool_name, effect, status, call_id, run_id, session_id, created_at, updated_at` | tool_ledger；不开放 summary／fingerprint，不虚构 step_index |
| `diag_tool_operation_links` | `run_id, step_index, call_id, ledger_id` | external_tool_operations 的已持久关联，不开放 receipt／fingerprint |
| `diag_calendar_entries` | `id, title, starts_at, ends_at, iana_time_zone, participants, notes, created_at` | calendar_entries；正文经保护 |
| `diag_forget_operations` | `operation_id, kind, memory_id, completeness, created_at, current_state` | forget_operations；current_state 为纯读推导 |
| `diag_forget_limits` | `operation_id, group_id, mode` | forget_limits |
| `diag_forget_cleanup` | `operation_id, target_id, revision, state, error_code` | forget_cleanup；error_code 从实际 error 字段映射安全标记 |
| `diag_consolidation_batches` | `batch_id, session_id, revision, status, created_at, updated_at, finished_at, error_code, generation_run_id` | memory_consolidation_batches；不使用同名近似旧表，不输出 receipt |
| `diag_consolidation_sources` | `batch_id, revision, run_id, session_id, ordinal, accepted_at, finished_at` | memory_consolidation_sources；按 batch_id+revision 关联 |
| `diag_memory_mirrors` | `target_id, required_generation, generated_generation, verified_generation, covered_cleanup_generation, generated_at, verified_at, conflict, error_code` | memory_mirrors；仅持久观测，不开放路径／intent／fingerprint，不提供 ready |

### A.1 来源与状态规则

- **Run 不等于已保存回答**：phase 为 accepted／running／finished，outcome 为 completed／max_steps／failed／interrupted 或合法 NULL。不能新增 `recorded`／`readable` 伪物理列，不能由 finished 推出聊天正文已保存。历史 message.run_id 可 NULL，不补造身份。
- **消息**：仅 role=user／assistant。按现有内容块契约验证，已知 Thinking／ToolCall／ToolResult 块不输出；未知或损坏结构 fail-closed。纯文字提取规则与既有人工读路径一致，不能用对 raw JSON 的字符串替换代替结构保护。
- **Attempts 覆盖**：`state=recorded` 表示合法非空 attempts，recorded_count 为当前列表长度；`state=recorded_empty` 表示明确合法空列表且计数为 0；`state=unrecorded` 表示 telemetry 为合法未记录值，或合法对象尚无 attempts，计数 NULL。已存在但不是列表、坏 JSON、列表成员无合法身份或同 Run 重复 attempt_id 均报 data_invalid。`run_finished` 只由 phase=finished 推导，不证明未记录的调用不存在。引用 attempts 时附带以上状态的汇总及进行中 Run 数；无须把每个 Run 的覆盖行塞进响应元数据。可查询 coverage 表取得逐 Run 细节。
- **Attempt 可选数据**：缺失／NULL model 和 usage 可表示未记录；存在时必须是合法结构。endpoint_id/model_id 成对为合法字符串或均 NULL；已知 outcome 为 committed／aborted，合法未记录时为 NULL。tokens 缺失／NULL 保留 NULL，出现值必须为非负整数且不是 bool；费用为有限非负 decimal 的规范化文本，合法历史整数可精确转换，不能走 float。出现非法值使准备失败。仅取这些路径，不投影 raw usage、memory、Graph、Skill 或其他 telemetry。
- **覆盖提示**：Attempt 和工具计量统计都只描述已持久记录，任何缺失不会补造事件或零成本。工具计量没有足够证据时固定提示“缺行不证明未发生请求”；不根据没有 ledger 推导没有 local_read。`COUNT` 的 SQL 数值本身不篡改，但页面同时展示对应覆盖限制。
- **来源**：last_change_origin 只接受 manual(source=cli/web)、tool(call_id)、consolidation(batch_id) 的既定形状，投影对应列，其余列 NULL。provenance_state 按 kind+id+record_version 关联，缺行沿现契约为 unknown；已记录值为 known／known_none／unknown。只显示当前记忆及版本关系，不把 group_id 一概当作 Run。
- **工具关联**：计量主键 run_id+step_index+call_id；业务账没有 step_index。只能用 `diag_tool_operation_links` 中的已持久事实做明确匹配；不得凭 run_id+call_id 补出一对一关系。
- **工具成本**：已知 kind=not_billable／unknown／unrecorded 时金额相关列 NULL；kind=reported 时 units 为有限非负精确 decimal 文本，unit/source/service 是非空文本且 service=source_id。违反已知结构报 data_invalid，不沿旧 fallback 静默吞损坏。cost_reason 仅保留 `http_not_sent, transport_not_sent, not_reported, undeclared_metering, invalid_units, invalid_persisted_metering` 的已有安全字面值；其他合法原因文本映射 `other_recorded_reason`，缺失为 NULL。不引入美元推断或现价估值。
- **错误文本**：tool_metering 的 reason_code 保留 `control_interrupted`／`interrupted_before_dispatch` 两个已核查固定码，其他非空合法文本统一为 `other_recorded_reason`，无原值为 NULL；不是“原文脱敏后随意透传”。forget_cleanup.error、memory_mirrors.error 及 consolidation.error_code 的非空合法文本分别映射 `cleanup_error`／`mirror_error`／`consolidation_error`，无值 NULL。来源类型损坏仍报错。
- **遗忘状态**：current_state 按现有纯读 operation_state 推导，优先 needs_scope，其次 failed，再 cleaning，最后 complete；不调用会记录观察或重建输出的写接口。若为接入本功能需要扩展受管清理状态，必须使该纯读状态继续反映真实未完成清理。
- **镜像状态**：只解释数据库记录的代际与最近观测；不打开镜像文件做验证，不调用会读文件／签发确认的完整 status，不声称文件此刻 ready。
- **结构元信息**：目录静态 manifest 说明名字、类型／可空性、来源、身份关联和保护／覆盖限制。可信只读核查可读取 schema_migrations(version,applied_at) 作为兼容信息；不新增用户可查 schema 表，不向浏览器发生产 DDL 或内部路径。版本账不替代结构检查。

## 附录 B：精确函数名单

这是 v1 的闭合名单；未列别名和后续 SQLite 新增函数不自动开放。函数使用 SQLite 内建实现和合法参数形态，不注册同名自定义回调。应在真实 worker 所链接的构建上验证能力，必要功能缺失则诊断不可用，不能悄悄缩减名单。

| 类别 | 允许名字／调用形态 |
|---|---|
| 数字／NULL／类型 | `abs(X)`, `round(X[,Y])`, `coalesce(X,Y,...)`, `ifnull(X,Y)`, `nullif(X,Y)`, `typeof(X)`；`min/max` 的多参标量形式 |
| 字符串／字节 | `length(X)`, `lower(X)`, `upper(X)`, `trim/ltrim/rtrim(X[,Y])`, `substr(X,Y[,Z])`, `replace(X,Y,Z)`, `instr(X,Y)`, `hex(X)` |
| 匹配 | `like(pattern,value[,escape])`, `glob(pattern,value)`，及其 LIKE／GLOB 中缀与 ESCAPE 语法 |
| 聚合 | `count(*)`, `count(X)`, `sum(X)`, `total(X)`, `avg(X)`, `min(X)`, `max(X)`, `group_concat(X[,separator])` |
| 窗口 | 上述聚合合法的 OVER 形式；`row_number()`, `rank()`, `dense_rank()`, `percent_rank()`, `cume_dist()`, `ntile(N)`, `lag/lead(expr[,offset[,default]])`, `first_value(expr)`, `last_value(expr)`, `nth_value(expr,N)` |
| 日期时间 | `date`, `time`, `datetime`, `julianday`, `unixepoch`, `strftime` 的 SQLite 内建文档形态；modifier 语义随受支持运行时明确验证 |
| JSON 标量 | `json(X)`, `json_array(...)`, `json_object(...)`, `json_extract(X,path,...)`, `json_type(X[,path])`, 单参 `json_valid(X)`, `json_array_length(X[,path])`, `json_quote(X)` |

JSON 访问使用 json_extract；本版不额外开放 `->`／`->>`、JSON 聚合、jsonb 家族、JSON 表值函数。也不开放 random／randomblob、扩展数学／百分位家族、sqlite_*、load_extension、readfile／writefile、REGEXP／MATCH 或其他未列函数。

日期函数允许 SQLite 原生的当前时钟与 localtime／utc 语义；执行时钟不是源库 read_at，页面说明不能混同。TEXT 的 length、ASCII 大小写、NULL、JSON null／不存在路径等沿 SQLite 语义；不把合法 NULL 一概当成执行错误。窗口排序不自动给最终结果排序，稳定顺序由用户外层 ORDER BY 明示。

验证必须覆盖 abs 最小 int64 溢出、sum 溢出、NULL 聚合、JSON 大数转 REAL、substr(BLOB)、超大 group_concat 等真实行为；任何实际非有限值或无法编码值沿第 5 节失败。

能力依据：[SQLite 标量](https://www.sqlite.org/lang_corefunc.html)、[聚合](https://www.sqlite.org/lang_aggfunc.html)、[窗口](https://www.sqlite.org/windowfunctions.html)、[日期](https://www.sqlite.org/lang_datefunc.html)、[JSON](https://www.sqlite.org/json1.html)。这些是语义资料，本功能是否实现仍须附录 C 验收。

## 附录 C：验收矩阵与证据

### C.1 唯一主验收路径

启动隔离状态目录中的**真实产品 Dashboard、正式 HTTP handler、实际受控执行进程和真实 SQLite**。测试库可由测试夹具使用当前迁移建立，再通过公共业务入口生成对话／记忆／工具记录；这是测试准备，不是诊断入口自动迁移。业务可注入 ScriptedModel，诊断不调用模型；不得访问用户真实数据目录。

浏览器负责页面交互和可见结果，HTTP 层记录真实 status／安全 body／实例／查询身份，SQLite 侧独立核验固定夹具与持久状态。对恶意 HTTP／慢传输等 UI 无法表达的反例，直接调用同一正式接口补充；不能用假 handler、模拟 SQL 结果或 mock 取消返回值代替主路径。时序测试可以注入窄范围屏障／时钟，但须保留实际 SQL、HTTP、进程及持久化行为。

每个用例保存可复核的“前置状态 → 请求身份与动作 → 可见响应 → 持久状态／资源证据”。不只截屏，也不只断言私有函数返回值。

| ID | 场景 | 必须证明的结果 |
|---|---|---|
| AC-01 | 进入 Database，查看目录和示例 | 固定当前库；21 个批准对象和精确列；无 raw schema／路径；示例不自动执行；MainBar 可用 |
| AC-02 | 先经聊天完成一条 ScriptedModel 对话，再查询 sessions/runs/messages JOIN | 页面内容与该 Session／Run／已提交消息的真实 SQLite 相符；跨 Session 可见；诊断没有创建新的 Run／Attempt／工具账／模型请求 |
| AC-03 | 查询事实、经历、版本来源、实际使用、日程、提炼与镜像 | 对照夹具逐字段验证派生规则；来源版本不串接；不会读镜像文件并冒称 ready；无遗忘目标正文复活 |
| AC-04 | 正常空查询与语法错误 | 空查询有列、0 行；语法错误为安全 SQL 错误；缺库／坏 schema 不是空结果；修正 SQL 后需显式再执行 |
| AC-05 | int64 极值、NULL／空串、同名两列、混合动态类型、BLOB、Unicode／HTML 文本 | 精确类型和顺序；整数不丢精度，重复列不丢失，BLOB hex／长度正确；HTML／Markdown 不执行 |
| AC-06 | 非有限 REAL、无效 UTF-8、坏源 JSON、已观察到的 peek 坏值 | 整次错误，没有静默替换、漏行或伪完整；合法 SQL NULL 仍是正常结果 |
| AC-07 | 999、1000、1001 行；用户 LIMIT 1000；反复翻页 | EOF／第1001行证据正确；只本地分页，后端不重跑／不保留游标；不自动 COUNT，不冒称库总量 |
| AC-08 | 成功 JSON 正文恰好 2 MiB、下一整行超限、首行／列元数据超限 | 实际 UTF-8 字节准确；包含转义、hex 和信封；每个成功响应≤上限，完整行截断；首行／元数据过大明确报错 |
| AC-09 | 第1000行后／恰好字节上限后的探查发生超时、取消或 SQL 错误 | 不把异常当 EOF 或已证明截断；整次失败并真实清理；上限确认后不声称未读取部分无错误 |
| AC-10 | SQL 大小64KiB±边界、64/65列、源值／行2MiB边界、完整输入64MiB边界 | 每类上限独立核验；WHERE／LIMIT 不能掩盖被引用对象超限；未引用对象的损坏不误伤无关数据查询 |
| AC-11 | 两个对象读取中穿插合法业务事务 | 结果属于一个真实源库事务快照，不能出现跨事务拼接；不以 memory_revision 恰好相同作为证明 |
| AC-12 | INSERT/UPDATE/DELETE/DDL/事务、多个语句、分号注释／字符串 | 所有非法操作被拒；合法单 SELECT 注释／字面分号不误判；数据库、WAL 内容和 schema 无诊断写入 |
| AC-13 | 递归 CTE（有／无 RECURSIVE）、PRAGMA 与 pragma_*、EXPLAIN/EQP、占位符 | 均拒绝，不能用作者视图名、statement_readonly 或关键词漏检；正常非递归 WITH／窗口仍可用 |
| AC-14 | ATTACH、load_extension、readfile/writefile、未知函数／表、sqlite_schema、隐藏rowid、json_each/tree、CTE重名和限定表名 | 无越权读写、无文件／网络副作用；全部函数名单逐项有正例，名单外有反例 |
| AC-15 | 源字段植入已加载测试密钥；SELECT、hex/substr、WHERE、GROUP BY、JOIN、JSON表达式 | SQL 只能看到保护后的值；转换和过滤不能恢复原值；保护改变身份时拒绝，不能产生错误关联 |
| AC-16 | Attempts 明确空／缺失／进行中／重复ID／坏token；工具成本未知／非法／缺关联 | NULL、覆盖和错误分支准确；不把未知当0；不以计量数=业务账数为断言，不伪造 ledger_id |
| AC-17 | 两个标签／独立 HTTP 同时执行；复用句柄；先取消后 execute | 仅一次实际执行、第二任务立即忙、无排队；已取消句柄不会晚启动；句柄签发本身不抢 Run admission |
| AC-18 | 句柄30秒、终态60秒、容量64边界，跨实例、活动期间缓存压力 | 无过期复活／重复执行；容量满明确拒签，活动任务不丢 owner；状态没有 SQL／结果正文 |
| AC-19 | 受支持的昂贵非递归 JOIN／聚合，分别在源提取、保护、SQL、编码取消或超时 | 五秒总预算覆盖全部执行阶段；取消一秒内实际回收；独立 SQLite 堆上限有效且不牵连主进程 SQLite；不是仅 UI停止 |
| AC-20 | 停止调用抛错、worker 迟迟未退出、IPC／发送清理失败 | 清理失败可见、新 Database 查询暂停、owner和重试责任保留；不能把终态或发信号冒充已释放 |
| AC-21 | 完成与取消的两个确定顺序；execute响应丢失后查状态 | 服务端先后诚实，点击取消后页面拒迟到结果；丢响应只得到无正文状态，显示未收到，无缓存重取／自动重跑 |
| AC-22 | SQL 在途时聊天保存、日程／记忆写入；读锁妨碍 writer | 普通操作优先，必要时停止诊断释放源锁；只出现预期业务写入；聊天不需等待诊断五秒自然结束 |
| AC-23 | 真实遗忘：准备中、SQL中、结果发送中、已展示及迟到响应 | 真实记忆和FTS／既有受管副本按原契约清理；诊断副本实际退出才可完成受管清理；所有旧显示被清除、迟到结果不能复活 |
| AC-24 | 已加载测试密钥／保护规则在准备或发布前变化，包括配置准备失败但已remember的值 | 独立保护版本确实推进；旧数据不能贴新版本；通知不含密钥；旧结果失效，手写 SQL 留存且不自动执行 |
| AC-25 | SPA离页、刷新、关页、BFCache恢复、隐藏标签、SSE故障、offline／online | 各分支按第2节分别验收；隐藏标签不误清，离页清草稿；断线清结果保留草稿；恢复核验不自动重跑；不依赖unload取消成功 |
| AC-26 | CSRF／Host／Origin失败、超大／畸形 JSON、慢请求体、慢响应接收、未知请求字段 | 复用公共安全闸；一秒IO期限不会无限占用；响应和错误无SQL、密钥、原载荷及内部路径回显；所有接口no-store |
| AC-27 | rollback模式、合法WAL读取条件、强制临时文件构建、需要恢复／修复的库 | 仅允许当前库必要锁／-shm协调；无内容临时文件／任意附加路径；不能满足条件则不可用，不改journal／immutable规避 |
| AC-28 | 聊天因记录／计量故障暂停、数据库独立可读；之后关停服务 | 可安全只读时仍能诊断；关闭先取消SQL再排空HTTP；无残余进程／源连接／缓冲owner |
| AC-29 | 连续成功、拒绝、取消、超时、断线之后重查资源 | 子进程、连接、文件描述符、任务占用和有界终态计数回到预期；无长期结果／SQL缓存；不要求OS内存字节安全擦除 |
| AC-30 | 日志／trace／缓存及发行包检查 | 没有持久化SQL和结果／隐式模型调用；静态资源和worker入口进入wheel/sdist，安装后的真实入口可运行，不只源码checkout可用 |

不允许用固定 sleep、盲目重试、放宽超时或删反例来得到绿色。测试先等待真实身份／持久化／阶段屏障，再验证结果；取消时间必须绑定服务端接受取消与实际资源释放两个可测时点。允许的测试时钟／屏障须说明注入点，不能替代真正 SQL 或清理动作。

### C.2 仓库门禁与未运行事项

执行 agent 在自己的独立工作树按当时 main CI 核对并运行；当前基线要求：

```sh
uv sync --extra dev --extra mcp --locked
uv run ruff check
uv run python scripts/check_skills.py
uv run python scripts/check_env_example.py
uv run --extra mcp pytest
uv build
uv run python scripts/check_mcp_installations.py --output /tmp/mcp-artifacts.json
npm ci --ignore-scripts
npm run typecheck
npx playwright install --with-deps chromium
npm run test:browser
git diff --check
```

当前 CI 为 Python3.14／Node22；默认离线测试排除 requires_key，Browser retries=0。付费模型不是本功能门禁。完整检查不替代上述新增验收；新增验收也不免除兼容性门禁。

**本规划 session 的状态**：只读核查远端与源码、历史验收、官方 SQLite 文档，并做过少量隔离合成边界实验；没有运行产品实现／新浏览器HTTP路径／完整仓库测试。当前 main CI 成功只证明基线；Database 的 AC-01–AC-30 全部 **NOT RUN**。实际终止／WAL协调／无临时文件／内存上限／遗忘联动均待实现和验证，不提前宣称 PASS。

## 附录 D：交接与上下文

### D.1 规范及实现接缝

- 领域词汇：根 `CONTEXT.md` 的 Run、消息、记忆／来源、工具、用量、并发、资源释放、schema 以及本轮 Database 词条。
- 已发布 ADR：[中央 fail-closed 脱敏](https://github.com/nineofoursyrup/Agent-Alfred/blob/ccdd1f699aa5e5db34ec4d656fb1a6f791a25881/docs/adr/0003-central-fail-closed-redactor.md)、[真实遗忘](https://github.com/nineofoursyrup/Agent-Alfred/blob/ccdd1f699aa5e5db34ec4d656fb1a6f791a25881/docs/adr/0007-forgetting-must-be-real.md)、[调用方拥有事务](https://github.com/nineofoursyrup/Agent-Alfred/blob/ccdd1f699aa5e5db34ec4d656fb1a6f791a25881/docs/adr/0009-caller-owns-the-local-transaction.md)、[Dashboard 威胁模型](https://github.com/nineofoursyrup/Agent-Alfred/blob/ccdd1f699aa5e5db34ec4d656fb1a6f791a25881/docs/adr/0014-local-dashboard-threat-model.md)、[并发修改立即冲突](https://github.com/nineofoursyrup/Agent-Alfred/blob/ccdd1f699aa5e5db34ec4d656fb1a6f791a25881/docs/adr/0016-concurrent-mutation-returns-409.md)、[收尾事务才确认保存](https://github.com/nineofoursyrup/Agent-Alfred/blob/ccdd1f699aa5e5db34ec4d656fb1a6f791a25881/docs/adr/0024-only-the-finalizing-transaction-confirms-recorded.md)、[保存落定才释放准入](https://github.com/nineofoursyrup/Agent-Alfred/blob/ccdd1f699aa5e5db34ec4d656fb1a6f791a25881/docs/adr/0026-admission-is-held-until-recording-settles.md)、[独立工具计量](https://github.com/nineofoursyrup/Agent-Alfred/blob/ccdd1f699aa5e5db34ec4d656fb1a6f791a25881/docs/adr/0032-tool-metering-survives-trace-pruning.md)。
- 实现接缝仅作源码导航：`database.py`／`managed_state.py` 的库能力；`runtime/recording.py` 的共享writer约束；`redact.py` 的 remember；`memory/commands.py`／`forget_ports.py`／`forgetting.py` 的提交、失效、受管清理；`gateway/web/handler.py`／`lifecycle.py`／`server.py` 的HTTP与关停；`ops/static/app.js`／`memory.js`／`stream.js` 的页面与恢复世代。
- 现有 ProjectionParticipant 事务回调禁止 IO；现有 cleanup_port 主要服务镜像，memory notifier 是提交后通知。不得直接在事务回调等待worker退出，也不能把异步通知当作“已实际清理”的屏障。执行 agent 应扩展合适的生命周期／清理协作，保留调用方事务所有权并用AC-23证明。

ADR-0040 和 Database glossary 已在本地决策工作树写成未提交规划文档，尚不存在于 main。执行票应要求将其内容随实现整合到当时文档，并链接最终 resolution；不能仅指向本机临时文件作为跨环境执行依据。

ADR-0040 的可携带摘要：用户 SQL 只查询逐次完整构建、预先保护的短命数据集，以消除输出后脱敏易被表达式绕过的边界。代价为准备成本、大范围失败和对保护后值统计；memory及保护规则版本变化使副本失效，受管清理要等实际释放。

可携带 glossary 定义（整合时只存词汇，不把本规范全文塞入CONTEXT）：

- **Database 控制台**：供用户人工查询当前运行实例持久数据库的只读诊断页面；输出受既有脱敏与遗忘边界约束，不等于数据库文件全部原值的公开接口。
- **诊断数据集**：一次 Database 查询所用的临时数据集合，完整覆盖其声明的输入范围，内容已经过保护；查询和统计描述这个集合中的值，不代表原数据库全部原值或持续更新状态。
- **诊断结果**：用户显式查询诊断数据集所得的有界结果，是可失效的受管临时内容；与用户手写SQL草稿分开，草稿不自动执行。
- **查询句柄**：一次明确Database执行的预先签发身份，可用于提交、取消和核对终态；同一句柄不启动第二次查询，不代表模型Run或浏览器已收结果。
- **保护规则版本**：当前进程已加载密钥集合及脱敏规则这一整套内容保护依据的版本；独立于记忆、连接及其他状态计数，依据变化使旧诊断内容失效。
- **诊断覆盖状态**：说明某类诊断记录明确存在、明确为空或尚无足够记录确认；没有记录不等于事件没有发生，损坏不等于正常缺失。

### D.2 依赖与完成顺序

纯实现票建议标题 **“实现：Database 只读 SQL 控制台”**，标签 `wayfinder:task`、`ready-for-agent`，总地图原生子票；保持未认领、不启动执行。原生前置为本次决策票及已完成的[实现：切片④c — 调用工具并看账（Tools 页 + Ops 账本页）](https://github.com/nineofoursyrup/Agent-Alfred/issues/55)。后者原生传递依赖已覆盖会话、记忆／遗忘、提炼／镜像、Dashboard HTTP及schema基础；相关ADR与来源票作为上下文列出，不重复为每个祖先加边。

本 session 的完成顺序：用户确认整稿 → 写入本票 resolution comment 并回读 → 关闭为 completed并核验 → 地图添加一行结论索引 → 查重并创建上述实现票、挂原生父子和依赖 → 回读正文／标签／状态／依赖。只解决本决策票，不推进其他决策，也不启动实现。

### D.3 确认索引

Q1–Q5：[第一轮确认](https://github.com/nineofoursyrup/Agent-Alfred/issues/65#issuecomment-5684927877)。Q6–Q10：[第二轮确认](https://github.com/nineofoursyrup/Agent-Alfred/issues/65#issuecomment-5685024586)。Q11–Q14：[第三轮确认](https://github.com/nineofoursyrup/Agent-Alfred/issues/65#issuecomment-5685139051)。Q15–Q18：[第四轮确认](https://github.com/nineofoursyrup/Agent-Alfred/issues/65#issuecomment-5685300034)。Q19–Q24：[第五轮确认](https://github.com/nineofoursyrup/Agent-Alfred/issues/65#issuecomment-5686193594)。

本稿 §2 对应Q1–Q5/Q9/Q22，§3 对应Q3/Q6/Q10/Q11/Q17/Q21/Q23，§4及附录B对应Q12，§5及附录A对应Q7/Q11/Q13/Q19/Q20，§6–7对应Q5/Q8/Q10/Q14–Q18，附录C对应Q24及所有反例。完整稿确认后不再重复询问这些决定。

## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/65#issuecomment-5686532220

## 决策闭环与实现交接

用户已最终确认 r1；[决定：Database 只读 SQL 控制台的用户路径与验收 — Resolution r1](https://github.com/nineofoursyrup/Agent-Alfred/issues/65#issuecomment-5686473812) 的发布正文已精确回读，本票经 REST 与 GraphQL 核验为 **CLOSED / COMPLETED**。

- [实现：Database 只读 SQL 控制台](https://github.com/nineofoursyrup/Agent-Alfred/issues/66) 已创建，标签 `wayfinder:task`、`ready-for-agent`，保持 **OPEN、未认领、未启动**。
- 实现票已挂为总地图原生子票；原生依赖指向本决策及已完成的[实现：切片④c — 调用工具并看账（Tools 页 + Ops 账本页）](https://github.com/nineofoursyrup/Agent-Alfred/issues/55)。两个前置均 completed，开放阻塞数为 0。
- [地图：Agent-Alfred v1 — 本地优先、无框架、可追踪的私人 AI 助手](https://github.com/nineofoursyrup/Agent-Alfred/issues/1) 已添加一行 resolution 索引，决策细节仍只在本票；Behaviour 剩余范围未在本 session 裁定。

本票无待裁定产品问题；Database 的 AC-01–AC-30 全部 **NOT RUN**，由实现票兑现。远端 main 仍为 `ccdd1f699aa5e5db34ec4d656fb1a6f791a25881`，该基线 CI 成功不代表 Database 验收通过。本 session 未实现产品代码，未提交、推送或合并；主工作区既有修改保持，规划 CONTEXT／ADR 留在隔离决策工作树且未提交。

批准稿 SHA256：`689368504a8d1ae5143c36efdba8a8f5191685f5521afe36527d3ae0afb72585`。已发布 resolution 正文 SHA256：`b08f28c7be5458730d6a283fc07b2acbd5bda334268078e5f3192ff9c25d86e3`。实现票正文 SHA256：`9ffcb640b443b1851ddc77550994b6c1b8a95db523b331a738bb8e7b8d06db6c`。

