# #35 结构：收拢 schema 模块的六项结构气味

来源：https://github.com/nineofoursyrup/Agent-Alfred/issues/35

Part of #1

# schema 结构整理实施规范

revision: SCHEMA-SPEC-r1
readiness: ready-for-agent（规范就绪；Issue 发布和标签状态见同一 LOOP.md）

任务：[结构：收拢 schema 模块的六项结构气味](https://github.com/nineofoursyrup/Agent-Alfred/issues/35)；地图：[地图：Agent-Alfred v1 — 本地优先、无框架、可追踪的私人 AI 助手](https://github.com/nineofoursyrup/Agent-Alfred/issues/1)。

## Problem Statement

作为 Agent-Alfred 的维护者，修改持久化逻辑时需要同时面对一个承担迁移定义、版本执行、Run/Session 写入、裁剪和时间解析的 schema.py。旧票基于 973 行与早期迁移的判断已经过时：当前文件为 1642 行，注册 v1–v18，部分业务迁移已拆出。职责混合使结构整理容易误改历史 DDL、事务所有权、公开导入或测试替换入口。

用户需要在保留已有数据库、CLI/Dashboard 行为和调用方式的前提下改善维护边界；不需要新数据库功能或更强的时间校验。

## Solution

保留 schema.py 的公开兼容入口，将迁移、Run/Session 写入、裁剪和时间解析移到各自内部模块，复用已有闭合集类型，并显式登记历史测试数据库的表名。保持发布过的迁移产物、数据、错误和事务行为；结构目标由依赖/接口检查和独立评审验收，行为目标由真实 SQLite 与公开调用路径验证。

本文为 SCHEMA-DESIGN-r5 已批准设计的等义实施规范。R01–R07、D01、V01、CE-01–CE-08 全部内联，本文是后续实施与验收的唯一权威合同；设计文档与 A01 快照保留为批准历史，不作为第二份持续维护的验收正文。无必须另读的验收 companion。

## User Stories

1. **R01**：作为已有数据的用户，我希望结构整理后历史数据、升级结果和输入行为保持一致，以便继续使用现有数据库。
2. **R02**：作为维护者，我希望迁移、Run/Session 写入、裁剪与时间解析各有明确归属，以便局部修改和评审。
3. **R03**：作为维护者，我希望结构目标与行为回归分别验收，以便确认改动有效且没有削弱既有保障。
4. **R04**：作为维护者，我希望复用已有类型并明确裁剪原因，以便表达接口含义，同时保持运行时接受范围。
5. **R05**：作为调用者，我希望继续使用现有裁剪记录签名，以便无需引入当前没有生产需求的 PruneRecord。
6. **R06**：作为调用者和测试维护者，我希望公开名称、异常身份及迁移注册表替换仍然生效，以便沿用现有集成和故障验证。
7. **R07**：作为测试维护者，我希望历史数据库的表名由夹具显式声明，以便准备数据时不必猜测 SQL 文本。

## Implementation Decisions

### 批准来源与基线

- Q1–Q3 和 Q4–Q7 分别获用户「都按建议」确认；整体设计 D01、V01、CE-01–CE-08 获用户「确认」（A01）。本次用户调用 to-spec 要求等义综合，不重新访谈。
- 源设计：[issue-35-schema-design.md](issue-35-schema-design.md)，SCHEMA-DESIGN-r5，SHA256 `86ee2eaae0a2cebbd87012ce789e3930537ebeffae00499ef1027785f8cb776f`。
- A01 审阅原件为 SCHEMA-DESIGN-r4，SHA256 `8b3ea996b3d6c4ed86055227c0c4485a4f4d5dbea3b45ed7494834532eb2e4b8`；r5 仅增加批准状态。批准记录与原件位于本票同一状态目录。
- 代码基线及本次核验的远端 main 均为 `e52d83290e0d5690025e5f907760a8c3592065ee`。行号/文件规模只作该基线的事实，不是验收目标。
- 领域约束：CONTEXT.md「schema 与迁移」、ADR-0009（调用方事务）、ADR-0020（裁剪生命周期事实）、ADR-0027（历史 Session 原值回填）。不重开已确认边界。

### 已批准的精确要求

| 要求 |决定 | 确认来源 |
|---|---|---|
| R01 | 保持现有行为；不新增迁移号，不改已发布 SQL 产物、历史数据、版本账、输入接受范围或业务状态机。独立缺陷先记录并判断是否阻塞重构。 | Q1 |
| R02 | 按当前职责拆分 schema.py，包含迁移定义/执行、Run/Session 写入、裁剪与时间解析归属；保留已有业务迁移模块；不重做 Host/Memory。 | Q2 |
| R03 | 结构与行为分别验收；冻结、升级、事务、幂等和裁剪断言不削弱；允许必要导入与夹具组织调整；变异验证针对契约破坏。 | Q3 |
| R04 | 复用 RunPhase/RunOutcome，补 PruneReason 的 Literal 类型；保留显式优先级、校验位置/异常/接受范围；时间仍为原始 str。 | Q4 |
| R05 | 本次不新增 PruneRecord，保留四个关键字字段的调用签名，改善类型与归属。 | Q5 |
| R06 | 保留 schema 公开入口、调用签名与异常身份；schema.migrate 每次捕获一份 schema.MIGRATIONS 交给内部执行器；私有测试引用可随归属调整。 | Q6 |
| R07 | historic_schema.py 显式登记每个历史 commit 的 calendar_table；三个独立历史 DDL fixture 逐字保留，不由当前生产代码生成。 | Q7 |

### 旧票六项的最终处置

| 旧项 | 当前事实 | 本票处置 |
|---|---|---|
| 裸 str / 闭合集 | RunPhase / RunOutcome 已有 Literal；裁剪原因仍为 str；时间刻意弱校验。 | 复用已有类型、增加 PruneReason；不统一全项目类型，也不加强时间校验。 |
| PruneRecord | 四个关键字字段仍在，但写函数仅被 Python/浏览器测试准备代码调用，尚无生产调用。 | 经 Q5 裁决不实施对象封装。 |
| 多职责文件 | 1642 行，v1–v18、Run/Session 写入、裁剪和时间解析共处；部分业务迁移已分文件。 | 按 D01 拆内部职责，保留公开入口和已有业务迁移位置。 |
| zone 命名 | 实际值为 SQL 表达式 iana_time_zone 或 NULL。 | 改为揭示 SQL 表达式的局部名称，如 zone_sql；不改列名、SQL 值或判定。 |
| 两 helper 参数 | migrate 捕获 registry，再把 registry/known 传给 helper，表达调用内一致输入。 | 原“无用参数”判断不成立，保留显式传递及快照语义。 |
| 历史表名推断 | test_schema.py 通过历史 DDL 文本相等推断 events/calendar_entries。 | 用 fixture 的显式元数据替代推断，历史 DDL 不变。 |

### D01：模块与接口合同（A01 已确认）

保留 `agent_alfred/schema.py` 为公开兼容入口；内部实现放进 `_schema/` 私有包。以下文件名是可按仓库惯例微调的建议落点，职责与依赖约束是验收对象。

| 建议落点 | 所有权 |
|---|---|
| schema.py | 公开名称显式导出；migrate(conn) 捕获当前 MIGRATIONS，再调用内部执行器。无 DDL、无 Run/裁剪 SQL。 |
| _schema/contracts.py | 迁移记录类型、唯一异常类、schema 当前闭合集与类型；复用 outcomes.py / run_phases.py。不导入 Host、数据库入口或迁移执行器。 |
| _schema/migrations.py | 冻结 DDL、历史形状验证、版本回填和迁移注册；保留业务迁移函数的必要延迟导入。 |
| _schema/runner.py | 连接配置、FTS 检查、版本账/受管对象准入、pending 顺序及事务/SAVEPOINT 执行。接收入口捕获的注册表，不读取另一份默认全局。 |
| _schema/run_records.py | Run/Session/活动序号的既有写函数及转移校验；不接管调用方事务。 |
| _schema/pruning.py | 原因优先级选择和裁剪事实写入；签名、冲突处理及事务所有权不变。 |
| _schema/instants.py | 原 parse_instant 实现；只搬迁，不与 tools/calendar.py 或 memory.storage 的同名/近义函数合并。 |
| evals/deterministic/historic_schema.py | 历史 DDL 与 calendar_table 元数据；不反向借用生产迁移来生成期望。 |

### 依赖规则

1. 内部模块不反向导入公开 schema 入口；调用内 registry 通过参数传递。
2. contracts 是叶子依赖；运行时写入不加载迁移定义。runner 只依赖 contracts/标准库，不导入 migrations；migrations 可复用 runner 中已有的表存在性查询，避免复制 SQL helper。迁移注册由 migrations 构造，公开入口将注册表传给 runner；二者不依赖 Run 写入模块。
3. v13 当前存在间接回边：consolidation_scheduling → consolidation → runtime.input_budget → runtime/__init__ → host → schema。保持执行期延迟导入，禁止机械提升到顶层；不扩大到重构整条依赖链。
4. 已分出的业务 migration 文件保持原所有权；本票不强制每版本一文件，不另建通用数据库框架。
5. 冻结 DDL 可搬文件，但字符串产物与执行顺序保持；有意重复的 FTS 定义不在本票抽象去重。迁移逻辑也不得改变回填、识别、错误或数据转换行为。

### 兼容清单

保留基线使用的公开名称及其语义：

- migrate、configure_connection、Migration、MIGRATIONS、MIGRATION_VERSIONS、LATEST_MIGRATION_VERSION、MANAGED_OBJECTS、RETIRED_OBJECTS、BUSY_TIMEOUT_MS。
- insert_session、insert_accepted_run、allocate_activity_revision、update_run_phase、parse_instant、pick_prune_reason、record_trace_prune。
- Fts5UnavailableError（含 sqlite_version 属性）、SchemaVersionError、RunPhaseError：定义一处、各入口引用同一类，不能复制同名异常。
- 公开闭合集常量保持值、顺序及既有容器语义：PRUNE_REASON_PRIORITY、PRUNE_REASONS、LEDGER_STATUSES、CONSOLIDATION_STATUSES、SOURCES、ROLES、LEDGERED_EFFECTS、ORIGIN_REQUIRED_COLUMN、ORIGIN_KINDS、GATEWAYS、PURPOSES、PHASES、OUTCOMES、RUN_PHASE_TRANSITIONS。
- 类型注解按 R04 改善；运行时参数形态、关键字限制、默认值、返回值与错误边界保持，不新增隐式解析步骤。Migration 保持 NamedTuple(version, apply, managed_objects) 的字段及 tuple 语义；configure_connection 仍返回原连接；两个 insert 与 allocate 返回 int，migrate/update_run_phase/record_trace_prune 返回 None。
- schema.MIGRATIONS 的测试替换只决定后续调用捕获什么；一次调用中校验、受管对象集合与执行须使用同一份捕获值。LATEST_MIGRATION_VERSION 等现有派生常量不因 monkeypatch 被自动改为动态计算。
- database.py 继续使用同一个 schema 模块对象，既有 database_module.schema.migrate 测试替换仍生效。
- 唯一已查到的 schema 私有跨模块读取为 test_schema_v12.py 的 _V3_RUNS；移至冻结 DDL 所属模块，保留两项冻结断言。不把所有内部函数、导入来的标准库名称或私有 DDL 变成新公开承诺。

### 类型与领域文档

PruneReason 表示 ADR-0020 已定义的闭合原因，不创造新的领域概念。Literal 负责静态表达，原校验负责运行时收窄；显式优先级顺序不依赖集合迭代。时间原文不是“已校验的 Instant”，不能为了类型美观而误标。origin_kind 等既有 DDL 闭合集不因本票统一成新 Enum。

本轮无新增领域术语，不修改 CONTEXT.md。模块分工可逆，事务与冻结约束已有 ADR；不为此复制一份新 ADR。

## Testing Decisions

### V01：已批准门禁与验证原则

### 验证层次

1. 结构验收：每个职责只有一个实现归属、内部无反向入口导入、公开兼容清单完整、迁移无需依赖 Run 写入、已有业务迁移位置不变。用依赖检查和独立评审，不以文件行数或目录数量当质量指标。
2. 固定基线/候选分开：历史 fixture 和基线快照有明确 SHA/hash；数据库比对使用同一 SQLite 版本；不能在候选中生成自证期望。新增测试只填缺口，已有等价测试可直接映射 CE。
3. 变异验证针对 DDL 漂移、SAVEPOINT 回滚失效、去掉终态/旧 phase 守卫、屏蔽 MIGRATIONS 替换等合同破坏。每个相关变异必须有检测证据；不要求改回 zone 命名或搬回文件就让行为测试失败。
4. 默认必需检查遵循当前项目 CI：Ruff、Skill/.env.example 校验、完整离线 Python（MCP extra）、Dashboard typecheck/browser、构建及 wheel/sdist × base/MCP 安装验证。
5. Linux 本票无豁免。未来若获交付授权，分别核对 PR-head CI 与实际 merge-SHA CI；设计确认不授权触发交付。付费模型语义评估不属于本票默认门禁，NOT RUN；不伪造测试通过。
6. 检查失败先识别与本次候选的关系，不降级验收、不删用例凑通过；修复后重新绑定候选与证据。基线变动先核差异与相关合同，不机械重放旧行号。

### 既有检查入口

- `uv run ruff check`
- `uv run python scripts/check_skills.py`
- `uv run python scripts/check_env_example.py`
- `uv run --extra mcp pytest`
- `uv build`
- `uv run python scripts/check_mcp_installations.py --output <本票证据目录>/mcp-artifacts.json`
- `npm run typecheck` / `npm run test:browser`

以上均为未来实施阶段检查；本设计阶段未执行。代码审查需对同一个冻结候选完成 Standards/Spec 两轴，本轮子代理事实调查不算代码评审。

### 需求与验收追踪

| 要求 | 核心验收与位置 |
|---|---|
| R01 | CE-01/02/03/05/07/08；现有迁移、裁剪、Run 和数据库打开契约 |
| R02 | D01 结构检查；CE-05/06 的 Run/Host、独立导入及安装包 |
| R03 | V01；CE-01–CE-08 证据与契约破坏变异验证 |
| R04 | CE-01/03；类型与闭合集接口检查，原校验边界不变 |
| R05 | CE-03；保留 record_trace_prune 签名，检查无新增 PruneRecord |
| R06 | D01 兼容清单；CE-02/04/05/06/08 |
| R07 | CE-01/07；三个历史 DDL fixture 与显式 calendar_table |

既有测试参照：evals/deterministic/test_schema.py、historic_schema.py、test_schema_v3.py、test_schema_v12.py、test_run_terminal.py、test_managed_state_ownership.py；安装参照 scripts/check_mcp_installations.py。模型只在实际 Host 路径的外部边界替换为 ScriptedModelFactory；其余真实组件与特殊失败控制按各 CE 限定。

## Critical Counterexamples

本节是实施与验收的权威清单，逐项来自 SCHEMA-DESIGN-r5 的同编号 CE（批准 A01）。CE-01–CE-08 均为 confirmed（A01）；要求来源 R01–R07 已确认。它们是待验证的契约风险，不表示当前实现存在相应缺陷。未运行任何产品验证。

### CE-01：类型或拆分改变冻结 SQL、历史数据或幂等性

- **Basis**：R01/R03/R04/R07；冻结迁移词条、ADR-0027。
- **Sequence**：从独立基线保存 DDL/迁移输出、三个历史 v1 fixture 和带原始数据的库；候选分别从空库、三个历史 v1 形状、基线生成的 v2–v18 前缀库进入公开 migrate；成功后再次 migrate。涵盖 v18 对 runs 索引/触发器的保留。
- **Expected behavior**：既有 DDL 字符串产物不变；相同初态得到相同 schema、业务行、索引、触发器和 FTS 可查询结果；历史 applied_at、Session 身份/时间原文逐字保持；版本账连续且只追加缺失版本；二次调用无业务或版本账变化。新追加 applied_at 可因实际执行时间不同而不同，但须满足现有 aware UTC 格式。
- **Verification**：真实 SQLite + schema.migrate。既有三个历史 fixture 为独立预期；后续前缀库由固定基线在独立进程/环境生成并记录基线 SHA。比较实际 sqlite_master、业务行和版本账；DDL 原文检查是补充，不能用候选生成双方预期。SQLite 自动附属对象按同一 SQLite 版本比较；不通过放宽业务字段消除差异。
- **Decision status**：confirmed（A01）。

### CE-02：失败迁移越权提交或留下半迁移

- **Basis**：R01/R03/R06；ADR-0009、migrate SAVEPOINT 合同。
- **Sequence**：分别在空库/已升级库上由调用方 BEGIN 并写入 caller_notes；通过 schema.MIGRATIONS 追加受控迁移，先创建表和索引再抛 RuntimeError 或控制异常；调用 schema.migrate。另测没有调用方事务的失败。
- **Expected behavior**：外层事务存在时，仅本次 pending migrations 的 DDL/账目回滚，调用方此前写入保留且未提交，仍由调用方决定 commit/rollback；无外层事务时没有半迁移与虚假账目；原异常不被吞掉。SQLite 自身已撤销事务的错误路径保持原语义，不承诺无法保留的数据。
- **Verification**：真实 SQLite、公开 migrate、公开 MIGRATIONS 测试替换。精确控制失败发生在创建对象之后；检查 in_transaction、实际对象、版本账、调用方行，随后显式提交/回滚。不能只 mock commit 次数。
- **Decision status**：confirmed（A01）；替换入口已由 Q6 确认。

### CE-03：裁剪整理改变时间、原因或重复写语义

- **Basis**：R01/R04/R05；ADR-0020。
- **Sequence**：用当前接受的非空任意时间文本写一条裁剪事实；同 run_id 以不同字段再写；另测多原因乱序/重复、空集合、未知原因、NULL 时间，以及同一事务批量写后回滚。
- **Expected behavior**：时间原样保存；同 run_id 保留首条，不覆盖；manual > disk_low > age > capacity 的确定性优先级不变；空/未知原因仍 ValueError，NULL 仍由数据库约束拒绝；批量回滚无记录；无消息行也能记录裁剪，不改 telemetry/trace_incomplete。
- **Verification**：原 schema.record_trace_prune / pick_prune_reason，真实 SQLite 查询 trace_prunes 和已有 Run 观测。使用固定输入，不假造连接。
- **Decision status**：confirmed（A01）；签名及类型原则已确认。

### CE-04：公开测试替换被搬迁后的另一份全局屏蔽

- **Basis**：R06、migrate 调用内快照语义。
- **Sequence**：先替换 schema.MIGRATIONS 为注册表 A，调用公开 migrate；A 中受控迁移在执行期间把入口全局换为 B，随后应执行的 A 尾项有可观察标记/受控失败；之后在独立新库再调用一次，再恢复测试替换。
- **Expected behavior**：当前调用的受管对象识别、版本校验与执行仍按 A；下一次才采用 B；A 的尾项确实执行，不能以未执行注入项的绿灯冒充通过。派生常量维持基线语义，不在此引入注册表热更新产品功能。
- **Verification**：真实 schema.migrate / SQLite；仅测试替换 schema.MIGRATIONS，使用有序执行标记和数据库对象/账目证明实际顺序。另保留现有失败迁移与旧版本前缀测试。
- **Decision status**：confirmed（A01）。

### CE-05：Run 写路径搬迁丢失终态、行数或调用方事务保障

- **Basis**：R01/R02/R06；Run 两轴/活动序号合同、ADR-0009。
- **Sequence**：正常走 accepted → running → finished；在调用方事务中先分配 activity_revision，再尝试 finished → running/finished、accepted → completed/max_steps、旧 phase 不匹配的重复写，以及 Run 更新后目标 Session 不存在的路径；让异常退出调用方事务。
- **Expected behavior**：合法路径保留；非法转移或行数不符抛同一 RunPhaseError；调用方回滚后 Run/Session/时钟均还原。update_run_phase 不自行 commit/rollback；调用方捕获错误而未回滚时，此前分配时钟或已完成的 Run SQL 可能仍在事务中，本票不改变这一低层行为。
- **Verification**：真实 SQLite + 公开写函数，在 with conn 或显式事务中检查三张表。复用 test_run_terminal 的公开非法转移/终态不改及 test_schema_v3 的行数检查；现有重复 finalizer 测试调用私有 _finalize，只作补充，不能宣称为公开入口验证。通过真实 Host.submit/wait 与 RecordingStore 检查正常 Run 录制结果；Run 更新成功但 Session 缺失的确定性反例由公开 update_run_phase + 调用方事务新增或补齐。模型边界可用 ScriptedModelFactory。
- **Decision status**：confirmed（A01）；不能以函数自身回滚代替调用方事务验证。

### CE-06：拆分引发循环导入或安装包漏文件

- **Basis**：R02/R06；D01 依赖规则；v13 已查明的静态间接回边。
- **Sequence**：独立新进程分别从 schema、database、runtime.host 入口导入，然后真实 migrate 新库及进入 v13 的旧库；在 wheel/sdist × base/MCP 四种独立安装中走现有 host smoke。
- **Expected behavior**：无需测试预热导入也能加载；没有半初始化模块引用；迁移次序不变；分发包包含所需私有模块，从源码树外安装运行成功。
- **Verification**：真实 Python 包导入、SQLite、构建及隔离安装；不替换内部模块。模型用 ScriptedModelFactory，不调用外部付费模型。
- **Decision status**：confirmed（A01）；静态回边不是当前 ImportError 的证明。

### CE-07：重构缩小受管对象清单或放过非法版本账

- **Basis**：R01/R03/R06；迁移版本账词条与现有准入合同。
- **Sequence**：无版本账但存在受管对象/退役 events；另分别构造空账、缺号、非法号、新于当前代码的版本；再用未知 v1 形状或无法转换的历史行尝试升级。
- **Expected behavior**：保留原拒绝条件、异常与可见原因；拒绝前不修改业务数据或执行破坏性 DDL；无静默盖章、无猜测修复。既有库的版本账不被改写。不额外宣称每次启动全量校验所有已迁移结构。
- **Verification**：真实 SQLite + schema.migrate；固定非法初态、比较拒绝前后 sqlite_master/业务行/版本账。FTS 缺失保留现有受控 PRAGMA 返回替身及异常测试，正常迁移必须用真实 FTS5。
- **Decision status**：confirmed（A01）。

### CE-08：异常或模块身份变化破坏数据库打开与清理

- **Basis**：R01/R06；database.open_database 现有资源所有权合同。
- **Sequence**：经 database_module.schema.migrate 测试接缝，在真实受管数据库已连接后抛出受控错误/中断；另触发实际 SchemaVersionError、RunPhaseError，及既有 FTS 缺失路径。
- **Expected behavior**：数据库入口实际调用被替换的同一 schema 模块；迁移失败不会交付半初始化连接；连接/文件租约按原 owner 清理；公开异常类身份、属性和捕获方式保持。既有清理失败/控制异常优先级不改变。
- **Verification**：复用 test_schema.py、test_managed_state_ownership.py 的公开 open_database 测试；真实 ManagedStateDirectory、SQLite 与受管文件，按原测试只替换迁移回调/连接失败点以控制异常顺序，观察文件描述符、资源所有者和原异常。
- **Decision status**：confirmed（A01）。

## Readiness and Open Decisions

- **readiness = ready-for-agent**：整体设计、所有 R/CE、D01 模块接口与 V01 测试接缝均已确认，未决决定和验收合同阻塞为 none；没有被偷偷排除或延期的必需 CE。
- **publication 与 readiness 分离**：本地规范已完成，远端正文/标签是否发布以 LOOP.md 及发布回读证据为准；未发布不改变技术就绪结论。应用 ready-for-agent 标签仍需发布授权。
- **执行状态**：IMPLEMENT / NOT STARTED；产品代码未改，产品检查与 CE 执行均 NOT RUN。规范就绪、设计事实核查、文档检查均不是独立双轴代码评审或产品通过证据。
- **修改边界**：文件名可按 D01 允许的范围微调；改变公开合同、DDL/数据行为、替换入口或必需验收须记录为新规范候选，不从“重构”推导新的产品规则。

## Out of Scope

- 不新增 schema v19；不改变 FTS、历史 SQL 或迁移修复策略；不新增自动修复损坏库功能。
- 不引入 PruneRecord，不新增生产裁剪调度器；不统一全项目时间类型/解析器。
- 不重构 Host/Memory/Graph 业务，不更改 Run 状态机或事务所有权；已有业务迁移保持原位置。
- 不扩成数据库框架/插件迁移系统，不开放注册表热更新给用户。
- 不以规范综合授权产品实施、提交、推送、合并或关闭 Issue。规范的 GitHub 发布单独按已授权范围执行；当前发布状态见 LOOP.md。

## Further Notes

### 对旧 Issue 的生效规则

本规范的「旧票六项的最终处置」替代旧 Issue 中与之冲突的实施要求：不新增 PruneRecord；不删除有调用内一致性用途的 helper 参数；必要测试导入/夹具组织允许调整，但历史 DDL、原断言意义及覆盖不削弱；不再要求撤回纯重命名或搬文件就导致行为测试失败。这些替换分别获 Q2/Q3/Q5/Q6/Q7 和 A01 批准，没有未授权删除需求。

发布目标沿用当前已讨论的 Issue，不另建重复任务。待获发布授权，以完整规范更新正文，并添加 ready-for-agent、保留 wayfinder:task 及现有其他标签；保持标题、OPEN 状态、assignees、地图归属与依赖。操作前重读 Issue，若发现并发修改先合并事实；操作后逐字校验正文及标签。发布不等于实施、提交、合并或关闭 Issue。

### 实施交接

1. 先读取同一入口 `/Users/nineofour/Agent-Alfred-issue-35-design/tmp/agent-work/issue-35/LOOP.md`，再核对本规范 revision/hash、最新基线和工作树归属。
2. 设计树 `/Users/nineofour/Agent-Alfred-issue-35-design` 仅承载设计/spec。产品实施待用户另行授权并使用隔离工作树；不改原主树已有 .gitignore 工作。
3. 对每个 R 和 CE 给出候选绑定的实现位置/验证证据；新增测试只补缺口，复用测试必须指出真实入口和观察点。保留相同候选的完整 manifest/diff，不拼接不同候选的 PASS。
4. 按 V01 完成当前项目门禁与两轴评审；只有另获交付授权后才进行 Git 提交/推送/PR/合并/关闭。Linux 未获豁免；付费模型评估不属于本票默认门禁。
5. 当前文档核验、批准和发布准备证据索引：同目录 `design-approval.json`、`confirmed-source-r4.md`、`spec-validation.json`、`issue-before-spec.json`、`publication-plan.json`；它们是证据，不增加产品合同。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/35#issuecomment-5683859766

## Issue #35 验收完成

- 最终候选：`schema-c3-31043dbb6a1f`，16 个产品文件；本地提交 `e803fa0278618e6054a8919bbb440f6538167399`，提交 tree `20d28a6481bcbaacab5d0c24dc32e7a2208ddb02` 与冻结候选逐路径、逐文件 hash 一致。
- [PR #64](https://github.com/nineofoursyrup/Agent-Alfred/pull/64) 已 squash 合并；实际 merge SHA `ccdd1f699aa5e5db34ec4d656fb1a6f791a25881`，其 tree 仍为 `20d28a6481bcbaacab5d0c24dc32e7a2208ddb02`，`origin/main` 已回读包含该提交。
- 规范身份：`SCHEMA-SPEC-r1 + ACCEPTANCE-AMENDMENT-r1`。独立 Standards / Spec 均 PASS；两轴产品 blocker 0、optional suggestion 0；旧 `STD-01` / `SPEC-01` 因用户明确改变 Linux 专项验收范围而 resolved，非产品修复、非 Linux PASS。
- 实现将公开 `schema.py` 保留为兼容 facade，把迁移定义与执行、Run/Session 写入、裁剪及时间解析拆入 `_schema` 私有模块；公开接口、异常身份、历史 DDL/数据、注册表替换接缝和调用方事务行为保持。
- 本地 macOS 门禁：Python `4557 passed, 1 deselected`；Dashboard browser `206 passed`；Ruff、Skill、`.env.example`、typecheck、build、wheel/sdist × base/MCP 四种安装通过。结构专项 `28 passed`，21 库固定基线差分通过，5/5 合同破坏变异被检出。
- CE-01–CE-08 已逐项复核：历史 DDL/数据/幂等、失败迁移事务、裁剪时间/首写/优先级、registry 调用内快照、Run/Host/事务、冷导入与分发包、非法库拒绝无写、模块/异常身份与资源清理均有充分证据。
- Linux 专项兼容矩阵依用户要求标记为 `NOT RUN — adapted, not independently tested`，没有另行启动 Linux 专项验证；仓库既有 `ubuntu-latest` 发布 CI 则按实际运行如实记录为两阶段成功。付费模型语义评估非默认门禁，本次 `NOT RUN`。
- PR-head CI [34990589683](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34990589683) `SUCCESS`，绑定 `e803fa0278618e6054a8919bbb440f6538167399`：Python `4557 passed, 1 deselected`、browser `206 passed`，其余必需步骤通过。
- merge-SHA CI [34992421031](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34992421031) `SUCCESS`，绑定 `ccdd1f699aa5e5db34ec4d656fb1a6f791a25881`：Python `4557 passed, 1 deselected`、browser `206 passed`，其余必需步骤通过。

验收矩阵、双轴、PR-head CI、merge-SHA CI、main 包含关系及限制均已核验；本票满足完成条件，按用户授权以 `completed` 关闭。
