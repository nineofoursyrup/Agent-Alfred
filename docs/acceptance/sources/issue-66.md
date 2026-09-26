# #66 实现：Database 只读 SQL 控制台

来源：https://github.com/nineofoursyrup/Agent-Alfred/issues/66

# 实现：Database 只读 SQL 控制台

**状态：规范已冻结，等待独立执行。** 本票标签为 `wayfinder:task`、`ready-for-agent`，是总地图原生子票，原生依赖见下文；创建时保持未认领，不启动执行 agent。

## 问题与交付结果

当前 Dashboard 没有 Database 控制台。交付一条完整人工诊断路径：用户进入 Database，在固定当前实例的受管数据库上选择已说明的诊断对象，显式提交 SQL，得到有界、带类型与完整性说明、可失效的只读结果；能够取消，且不妨碍聊天保存与真实遗忘。

规范源：[决定：Database 只读 SQL 控制台的用户路径与验收 — Resolution r1](https://github.com/nineofoursyrup/Agent-Alfred/issues/65#issuecomment-5686473812)。批准源 SHA256：`689368504a8d1ae5143c36efdba8a8f5191685f5521afe36527d3ae0afb72585`；已发布 resolution 正文 SHA256：`b08f28c7be5458730d6a283fc07b2acbd5bda334268078e5f3192ff9c25d86e3`。**整份 resolution，包括附录A–D，均为本票验收依据。** 本票组织实现工作，不修改其中产品取舍。

总地图：[地图：Agent-Alfred v1 — 本地优先、无框架、可追踪的私人 AI 助手](https://github.com/nineofoursyrup/Agent-Alfred/issues/1)。规划核查基线：`ccdd1f699aa5e5db34ec4d656fb1a6f791a25881`。开始执行前须重新核对远端 main、原生依赖、相关规范和工作树；基线漂移不能静默覆盖已确认合同。

## 实现范围

- Database 导航／页面、MainBar 并存、对象目录、SQL编辑器、显式执行／取消、结果类型和100行本地分页；空、错、截断、失效、结果未收到与清理失败均有诚实呈现。
- 当前受管源库的独立只读一致事务提取；实现 resolution 附录A 的21个 `diag_*` 投影及覆盖元数据。逐次完整准备被引用对象，保护先于SQL，不开放底层原值／生产schema。
- 独立可监管执行进程和SQLite连接；附录B闭合SQL／函数边界，内存数据集与排序中间内容；源库只允许必要读取及锁／-shm协调。
- SQL、列／行／输入／输出／SQLite堆上限，五秒总执行预算、一秒IO期限及一秒实际取消回收；单runtime单任务、不排队，与Run admission分离。
- 预签一次性句柄、同步execute和独立cancel/status、30秒待用／60秒无正文终态／64条有限记录；实例／修订／保护版本核验，无结果重取或自动重试。
- 独立保护规则版本及页面失效；将运行数据集与发送缓冲纳入实际受管清理，遗忘完成不能早于旧诊断副本退出。正常业务写入优先；关停先取消诊断再排空HTTP。
- 完整浏览器→正式HTTP→真实worker→真实SQLite验收及仓库兼容性检查；静态资源／worker入口应进入发行包。
- 整合中文文档：根CONTEXT新增六个Database词条；ADR“Database SQL只读取受保护的临时数据集”；必要Dashboard/API说明。最终规范驻决策票，文档不得另起冲突版本。

## 非目标

任意DB路径／连接、写库或迁移／修复、管理PRAGMA、文件／网络查询、raw schema／trace／内部恢复载荷、脱敏绕过入口、模型生成／执行SQL、长期快照／查询历史／服务端结果重取、导出、自动COUNT／现价估值、数据库调优、全进程RSS硬上限、其他Behaviour范围扩张。本票创建不等于实现已启动，也不构成未经另行授权的提交／推送／合并。

## 验收标准

逐项完成 resolution **AC-01–AC-30**，报告每项证据；不得只写“总体通过”。最低交付证据分组：

1. **公共路径与数据准确**（AC-01–AC-06、AC-11、AC-16）：真实已保存对话和各诊断对象逐字段对照；覆盖／NULL／精度／同名列／坏数据行为准确，无伪关系与零补全。
2. **结果及资源边界**（AC-07–AC-10、AC-18–AC-20、AC-29）：EOF和1001探查、实际2MiB正文、有界完整输入、真实SQLite堆限制、五秒预算、一秒实际回收、有界状态与资源归还。
3. **只读及保护**（AC-12–AC-15、AC-23–AC-24、AC-26–AC-27）：写入／PRAGMA／文件／CTE／函数绕过反例，真实遗忘和保护版本竞态，源库及临时文件副作用核验。
4. **并发与生命周期**（AC-17、AC-21–AC-22、AC-25、AC-28）：多标签忙、取消顺序／丢响应、聊天保存优先、页面断连及恢复、故障诊断和关停顺序。
5. **发行与兼容**（AC-30及C.2）：不持久化SQL／结果，无隐式模型调用；wheel/sdist安装后的真实入口可用；全部仓库门禁。

测试必须用隔离状态目录和真实SQLite；ScriptedModel只替换业务模型依赖，不能替代HTTP、SQL执行或资源释放。按真实身份／保存／阶段屏障等待；不通过增加sleep、盲目重试、放宽期限或删反例达标。部分条件未验证则写NOT RUN／受阻，不能记PASS。

当前全部Database验收 **NOT RUN**。规划期间的基线CI／合成SQLite实验不算产品验收。

## 原生依赖

GitHub 原生 blocked_by 指向：

- 本次[决定：Database 只读 SQL 控制台的用户路径与验收](https://github.com/nineofoursyrup/Agent-Alfred/issues/65)，已写入最终resolution并closed/completed。
- 已交付[实现：切片④c — 调用工具并看账（Tools 页 + Ops 账本页）](https://github.com/nineofoursyrup/Agent-Alfred/issues/55)。其原生传递依赖覆盖会话、Memory、遗忘、提炼／镜像、Dashboard骨架及schema基础；不重复为每个祖先加边。

上述依赖是规范和基础已完成的可见关系，不以祖先通过替代本票验收。

## 上下文与实现注意点

- 读取项目 `docs/agents/issue-tracker.md`、`docs/agents/domain.md`、根 `CONTEXT.md`，以及最终resolution附录D列出的ADR。
- 原 `open_database` 会建库／迁移，`RecordingStore.reading()` 借共享writer且持锁；它们不是现成的诊断只读连接工厂。
- `memory_revision`不是全库提交版本；实例身份来自runtime。源数据一致性必须来自同一读事务，保护版本还要独立核验。
- 现有ProjectionParticipant事务回调禁止IO；现有镜像cleanup与提交后notifier不能直接证明SQL worker已退出。需扩展生命周期协作，不能在事务回调里等进程或以广播成功代替清理完成。
- 规划文档位于本机决策工作树且未提交，不能假设执行agent能读取。以最终resolution可携带摘要及词汇定义整合到执行工作树；ADR编号如与新main冲突则顺延并更新引用，内容取舍不变。
- 当前要求的精确CI命令见resolution C.2及 `.github/workflows/ci.yml`；开始时重核环境，完整测试需保留既有固定祖先历史。

本票用于后续独立执行session的实现与验收；创建本票本身不启动实现。交付与Git动作服从用户在执行session中的明确授权。不得自行续开或解决另一个决策范围。

## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/66#issuecomment-5741333678

<!-- issue-66-delivery:issue66-successor-5828ca8ff1db -->

# Database 实现验收与交付

按 [Resolution r1（附录 A–D）](https://github.com/nineofoursyrup/Agent-Alfred/issues/65#issuecomment-5686473812) 验收。规范 SHA256 `b08f28c7be5458730d6a283fc07b2acbd5bda334268078e5f3192ff9c25d86e3`。

[PR #67](https://github.com/nineofoursyrup/Agent-Alfred/pull/67) 实现受保护只读 SQL 诊断、取消和安全生命周期。候选 `issue66-successor-39949080793a`，48 文件完整变更、542 文件冻结；完整 diff SHA256 `39949080793a9693bf1cd29628776753d56fecd00484f0e6d7fc8cb15e936be2`。独立 Standards / Spec 均 PASS，STD-01–06 / STD-E01 / SPEC-01–12 全部 resolved，未关闭阻塞 0。

后续 [PR #68](https://github.com/nineofoursyrup/Agent-Alfred/pull/68) 修复实际验收中暴露的操作观测、请求身份和清理阶段核验问题。最终候选 `issue66-successor-5828ca8ff1db`，base `fe9713308d415c511e252090b8f559afbe95df02`，三个测试文件变更、542 文件冻结；独立双轴重新 PASS，SPEC-14/15 resolved。完整修复 diff SHA256 `5828ca8ff1db92da6652b97348a94dadd4ec3bc162b2e8e976a36b28954ac06f`。自最初规范基线累计 49 文件变更。

最终交付绑定：

- PR #68 head：`621945359db5d457f23cf673fd4117d657521ed4`；受审/提交/PR 测试树：`f9e233bcd38097965c332e164c08b0df240cefb9`。
- PR CI：[run 35437771971](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/35437771971)，绑定上述 head，SUCCESS；Python **4733 passed / 1 deselected**、浏览器 **238 passed**。
- 实际 merge SHA：`2533d6818c4b6a896f460b20d5e0fe40142d335e`（PR #68）。
- Merge CI：[run 35438569257](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/35438569257)，绑定上述实际 merge SHA，push SUCCESS；Python **4733 passed / 1 deselected**、浏览器 **238 passed**；构建、四组隔离安装及其余门禁成功。
- Main 包含关系与内容：合并后远端 `main` 为 `2533d6818c4b6a896f460b20d5e0fe40142d335e`，包含 PR head；实际 merge tree 仍为 `f9e233bcd38097965c332e164c08b0df240cefb9`，与受审/测试树一致。

最终候选本地完整浏览器 **238 passed**（4.7m），七项相称检查及四组安装通过。Memory 原生409及目标错误敏感性、Connections真实旧响应门闩、Database真实SQLite清理提交屏障分别有确定性红绿证据。原 native hidden/Safari BFCache 脚本与产品字节未变；Database删除用例有delta并重新验收。

前驱候选的历史验证保留真实过程：完整浏览器 237 passed / 1 项因本机 Git 环境失败，指定已有 Command Line Tools 后原用例单独 1 passed；19 项定点回归通过。Python 相同输入 Linux 4733 passed / 1 deselected，当前 build 与 wheel/sdist×base/mcp 四组隔离安装通过；独立两轴额外 24 + 6 项以及能力失败/FIFO 实验通过。后续 PR 和 merge 的完整 Linux 结果单独列在上方。

真实 Safari BFCache 与隐藏标签验收已通过：同一旧文档 trusted persisted 恢复，清空结果及草稿、重新核验、不自动执行；旧结果在新结果后抵达仍被拒绝。隐藏时保留草稿/结果且 5 秒预算不重置；临时 Safari 权限恢复关闭。相关产品及 native hidden/BFCache 脚本未在 CI 修复中改变，并经当前两轴核对；database.spec.js 的删除验收时序已修复并实际重验。

原 PR #67 的 [PR CI](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/35433463094) 通过，[实际 merge CI](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/35434357940) 失败（237 passed / 1 failed），如实保留；该失败由 #68 修复，最终两阶段证据见上。

CI 修复保留所有产品合同：选用可验证同引擎 SQLite C API 的 CPython 构建，不支持时继续 fail-closed；HTTP 夹具先验实际状态/身份、FIFO 有界退出、超大参数只缩短名称；聚合和提炼用同一 Run 持久终态作屏障。没有扩大产品期限、盲重试或弱化业务断言。

非阻塞观察 SPEC-OBS-13（P3）：既有 Memory 首次核验前提交且响应丢失后，草稿会保留但隐藏。当前 R08 仍证明 21 个未确认回执可查询、同身份重送且剩余 20 个；本票没有修改基线 `memory.js`，没有宣称修复早写交互，也不扩大为相邻功能全部交付。付费模型、发布到 PyPI、部署不在本票门禁，本轮未执行。

以下逐项列出已执行的公共路径及证据边界。测试文件可在本次提交中复核；局部故障注入或未额外复演的组合均明确保留，不把“存在测试”当作穷尽验收。

证据缩写：H=`src/agent_alfred/evals/deterministic/test_database_http.py`；S=`test_database_console_sql.py`；O=`test_database_console_objects.py`；B=`test_database_boundaries.py`；L=`test_database_lifecycle.py`；R=`test_database_review_repair.py`（同目录）。UI=`tests/browser/database.spec.js`；CANCEL=`database_cancel.spec.js`；LIFE=`database_lifecycle.spec.js`；STATUS=`database_status.spec.js`；NATIVE=`database_native_lifecycle.mjs`（同目录）。repair-1/2日志来自本地不可变验收包；Safari原始证据经两个独立轴核查。

| AC | 实际公共行为/实现 | 可复核证据 | 结论与限制 |
|---|---|---|---|
| 01 | `/database`、catalog 固定21对象/精确列、MainBar；示例仅写入编辑器 | H `catalog_lists_approved_objects_without_schema_or_path`、`each_object_returns_declared_columns`；UI第1例；R catalog保护和版本 | 已覆盖；SPEC-02 当前关闭。目录保护不再把源 migration 字符串当静态常量。 |
| 02 | ScriptedModel聊天提交→源SQLite→正式JOIN；不产生诊断Run/模型调用 | H `scripted_chat_then_join_matches_sqlite`；UI `chat then Database JOIN...`；安装包model.requests断言 | 已覆盖所列路径，跨Session内容由源查询与无隐式Session筛选核对。 |
| 03 | 21对象显式投影，当前记忆版本、来源/成本/错误/镜像派生 | O `twenty_one_objects_field_rules_from_real_fixtures`；H `facts_episodes_calendar_and_mirrors_are_derived`、`tool_metering_projection_fields` | 逐字段夹具与真实HTTP已覆盖；部分源记录由可信测试夹具准备，未把当前磁盘镜像状态伪称ready。 |
| 04 | 空结果保留列，语法错误安全，坏schema不可用 | H `empty_select_keeps_columns`、`missing_source_column_makes_console_unavailable`；UI空/语法例 | 正常收到响应分支保持；STATUS与本轮独立实际丢400响应已正确显示执行失败，SPEC-12关闭。 |
| 05 | cell动态类型、有序columns/rows、纯文本DOM、整数十进制 | H `duplicate_columns_and_int64`、`allowed_functions_and_blob_null`；S `cell_types_and_non_finite_real`；UI HTML | 已覆盖类型/重复列/HTML路径；不使用JS Number承载int64。 |
| 06 | 坏源JSON/UTF8/块类型/非有限/探查错误整次失败 | H `bad_message_json...`、`duplicate_attempt_id...`；B排除块；R known_excluded_blocks；L尾部SQL错误 | 已覆盖已知反例；前驱独立R及http-after原反例通过，相关字节未改，SPEC-03关闭状态保持。 |
| 07 | 1000行EOF/1001行探查，页面100行分页不发网络 | H `one_thousand_one_sessions_truncate_without_recount`；S row-limit/EOF；database.js renderRows | 行边界有真实HTTP，分页仅slice源码核对；本轮反复翻页+请求数的独立浏览器验收 NOT RUN。 |
| 08 | encode_rows按最终UTF8正文，完整行前缀与截断信封重新核实 | H `http_success_body_can_be_exactly_two_mib`、`final_success_json_never_exceeds_two_mib_on_the_wire`；S bytes/first-row/flags | 正式HTTP精确2MiB有证据；首行与列元数据独立辅助层验证，未把它称作全部浏览器验收。 |
| 09 | C API逐step，无DB-API预取；探查取消/超时/SQL错误失败 | L `real_worker_row_limit_probe...`、`real_byte_probe...`、`real_sql_error_in_tail...`、`confirmed_byte_limit_never_steps...` | 真实worker+HTTP/SQL屏障已覆盖；确认截断后不扫描后续错误。 |
| 10 | SQL64KiB/64列、原值原行2MiB、输入64MiB；WHERE/LIMIT不缩准备 | H size_and_column/source_value/protected_input_over_64mib；B mapped_error；budget.py固定framing | 公开边界与固定记账有证据；不声称穷尽所有类型排列。 |
| 11 | worker BEGIN同源事务提取全部被引用对象，再COMMIT | H `snapshot_does_not_mix_objects_when_write_interleaves`，mid_extract期间真实源写入 | 已覆盖双对象真实事务快照，不用revision相等冒充。 |
| 12 | 用户SQL只到内存库；真实authorizer；源ro无迁移写入 | H `sql_rejected_does_not_echo_sql_or_write_source`、journal/WAL；S分号注释 | 拒绝与持久内容对照已覆盖。 |
| 13 | SELECT/非递归WITH，拒绝递归/PRAGMA/EQP/占位符 | S拒绝矩阵；H `nested_sql_and_cte_boundaries_over_http`；B sql_operator；R CAST正反例 | 已覆盖；SPEC-04当前关闭，普通CAST参数语法不再错作函数。 |
| 14 | diag显式表列，函数closed list，独立连接无source attachment | H `allowed_functions_have_http_positive_examples`、nested/replace；S schema/unknown | 函数正反例/越界拒绝已覆盖；无文件网络能力另由连接初始化检查。 |
| 15 | project先保护全部选中对象字符串，再SQL；身份变化失败 | H `secret_is_protected_before_sql`、`short_credential...`；R catalog保护；repair-1/http-after.log | SELECT/hex保护和身份失败有证据；WHERE/GROUP/JOIN/JSON各组合本轮未逐一额外复演，不扩张声明。 |
| 16 | Attempt已记录/空/未知与当前Run覆盖，成本NULL/损坏严格分开 | O逐字段；H duplicate_attempt/negative、attempts_null、tool_metering | 已覆盖必要分支；不假设计量数等于业务账数。 |
| 17 | runtime单任务无排队、句柄一次执行、先cancel不能晚启动 | H second_execute/barrier busy/cancel_unused；UI双击；CANCEL前后queryId两个签发门闩 | 真实HTTP/UI已覆盖；SPEC-11关闭。 |
| 18 | clock TTL30/60、容量64、独立active owner与无正文状态 | H unused_handle_expire/capacity/expired_handles_occupy_capacity；service _prune/_require | TTL/容量实际公共请求已覆盖；跨实例重启×活动缓存压力完整组合本轮 NOT RUN。 |
| 19 | 准入到准备/SQL/编码5秒、子进程SQLite128MiB、实际kill/reap | H extract timeout/cancel、later_stages；L row/byte peek；S heap_limit；独立status-repro真实5秒 | 真实worker取消超时路径已覆盖；阶段屏障非逐指令覆盖，128MiB不是RSS。本轮独立超时回读已正确显示超时；NATIVE真实隐藏期间5057.9ms后504，原预算未重置。 |
| 20 | cleanup_failed保留owner、暂停新查询，释放+能力核验才恢复 | H kill_failure；L cleanup_failure_retains_worker/partial_process_construction | 实际进程+stop故障注入已覆盖；不把发信号视作已释放。 |
| 21 | 服务端完成/取消串行，客户端只查无正文状态，execution/page世代防迟到 | H completed_then_cancel/lost_execute_body；CANCEL各晚到回执；STATUS与独立status-repro | **SPEC-12 resolved**：本轴实际failed/released→执行失败、timed_out/released→超时；STATUS实际completed/released→结果未收到、stop故障→清理失败。CANCEL旧回执/页面世代回归通过。 |
| 22 | RuntimeStore transaction前note_writer中止诊断释放源锁 | H `writer_priority_stops_source_extract`；LIFE reload/close源写锁readback | 真实writer/worker释放有证据；聊天保存、日程、记忆每项完整并发组合本轮未另外复演。 |
| 23 | DiagnosticProjection注册清理目标，ConsoleCleanupPort实际释放屏障 | L `real_forgetting_waits_for_worker_cleanup`四阶段；H forget_waits_until_send_copy_released；UI真实删除/迟到响应 | 真实遗忘+worker/发送/页面已覆盖；CI-03c在实际清理写入503反例后，以删除complete、正式目录同实例/最终revision及页面自身核验为屏障，旧响应仍被拒绝，手动重查0行；最终补录：既有FTS/镜像完整兼容门禁已结束，pytest exit0（4733 passed/1 deselected），不再PENDING。 |
| 24 | Redactor remember推进版本，发布/发送再次核对，SSE只发版本 | H stale/change_during_extract、remember_notify_failure、begin_send_rejects；R catalog_drops_old_version | 已覆盖当前版本反例；配置准备失败由remember后通知故障接缝验证，保持实际保护版本。 |
| 25 | 路由清草稿，pagehide取消，offline/SSE清结果留SQL，恢复核验 | UI/LIFE真实reload、close、offline；NATIVE真实隐藏；Safari W3C原生BFCache证据 | 已覆盖，SPEC-10关闭：真实隐藏保留草稿与结果、期限未重置；同一Database文档两次trusted persisted恢复，清空并重新核验、不自动执行；旧42在新2之后交付仍被拒绝。 |
| 26 | 既有guard，SQL仅POST/CSRF，1秒读写期限，安全错误/no-store | H csrf/unknown_fields/slow_body/slow_drip；R slow_tcp_receiver；repair-1/slow-tcp-after.log；handler检查 | 真实1.64MiB慢TCP在前驱已补且独立重跑通过；handler/worker/测试字节不变，证据保留；Host/Origin公共闸继承既有路径，非另造数据库例外。 |
| 27 | 源mode=ro/current managed身份，TEMP_STORE/heap能力fail-closed | H rollback/WAL；B real hot journal；repair-1/sqlite-forced/successor-probe.log | 真TEMP_STORE=0构建/真实hot journal/正常模式均有证据；共享内存协调单独于内容写入。 |
| 28 | recorder故障不封死安全诊断；shutdown先停诊断再排空HTTP | H recording_pause/store_poison；L shutdown_cannot_succeed_while_worker_termination_failed | 已覆盖真实Dashboard/worker故障关停路径。 |
| 29 | response_owner覆盖HTTP发送，lease结束释放，终态有界 | H resource_counts_return；L handoffs/recovery；R slowTCP；LIFE真实PID/缓冲核验 | 连续生命周期资源有证据；没有长期压测或OS内存字节擦除承诺。 |
| 30 | SQL/结果不持久化；打包静态资源和worker真实可运行 | check_mcp_installations.py；mcp-artifacts.json wheel/sdist×base/mcp；broker/handler无SQL日志检查 | 四组隔离安装真实HTTP/worker PASS，无隐式模型调用；付费模型NOT RUN且不要求。 |
