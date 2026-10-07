# 总地图遗留义务的当前解释与证据

合同：[V1-AGENT-ONLY-CLOSEOUT-20261007-r1](AGENT-ONLY-CLOSEOUT.md)。本补遗处理总地图 #1 的“剩余离线断言缺口”及“CONTEXT.md 与首批 ADR”；保留原能力范围和结果真实性，不把缺失来源或未映射行直接认定为功能缺陷，也不把它们改成 PASS。

## 盘点与当前义务

[历史 inventory](coverage-inventory.json)保持原字节：1,614 行、321 行部分映射、1,293 行尚缺独立语义映射，原结论仍为 INCOMPLETE / NOT RUN。行中包含历史章节、复合条款、继承和重复来源，不是 1,614 个等量独立测试。1,293 行不能整体称为已被真实测试范围替代。

本次按后继已批准、自包含的功能规格合并相同工程义务，核读公共路径的关键断言，沿用各票验收和固定候选证据；对识别出的必要缺口单独补测或修复。下表及[精确选择器索引](agent-only-mappings.json)记录核读的功能组，不声称完成对旧盘点所有句子的逐行认证。`D/` 表示 `src/agent_alfred/evals/deterministic/`。

| 来源票／功能 | 当前必要工程义务与公共证据 |
| --- | --- |
| #12、#35 存储 | `schema.migrate` 幂等、原 DDL／数据／签名、caller 事务所有权、非法数据库原样保留；`test_schema.py`、`test_schema_baseline.py`、`test_schema_boundaries.py`。 |
| #13、#14、#34 模型与设置 | 同一公共循环、零 Step 不调用、协议／路径／唯一认证、缺配置发送前拒绝、目录失败保留 stale、unknown 不冒充支持；`test_loop.py`、`test_endpoint_factory.py`、`test_catalog.py`、`test_models_page.py`、`test_model_settings_host.py`。 |
| #15 费用与 trace | exact／estimated／unknown 分列，正 token 缺价不填默认，记录失败保留不可完整事实与回复；`test_cost_projection.py`、`test_trace_sink.py`。 |
| #16、#28、#36 Host／会话 | 唯一准入及记录、busy 不落新 Run、历史分页原文、首批准消息标题、CLI／Web 共用协调器；`test_runtime.py`、`test_sessions.py`、`test_web_cli_serve.py`。输入和 CLI 缺口见下一节。 |
| #17 记忆与检索 | 真正 SQLite／FTS 可检索、版本硬删、回执失败回滚、引用仅进入回答、统计已知分母；`test_memory_stores.py`、`test_memory_commands.py`、`test_runtime_memory_gate.py`、`test_memory_statistics.py`。 |
| #18 提炼与镜像 | 事实／事件／来源共同提交，中间失败回滚，不足阈值零调用，删除后批准不复活，文件清理失败禁读、晚到来源重建；`test_memory_consolidation_service.py`、`test_memory_consolidation_runtime.py`、`test_memory_mirrors.py`。真实内容质量另列历史限制。 |
| #19、#55 工具与 Ops | 外部权限、绝对 deadline、删除后无后续模型／工具、同 Run 记忆版本失效、独立文件草稿与同操作恢复、能力来源身份和未知账计量；`test_tool_registry.py`、`test_runtime_tools.py`、`test_file_tools.py`、`test_ops_acceptance.py`。 |
| #20、#21 Integration／MCP | 未配置提示到达下一 Step、非法输入零发送、子进程启动许可独立、真实临时 Host 结果矩阵、替换后旧许可退休；`test_integrations.py`、`test_mcp.py`。远端服务实际连通不由本轮证明。 |
| #24 Skill | explicit／off 仅装载所选正文、选择器只读目录和安全历史、工具往返保持冻结快照而不新增权限；`test_runtime_skills.py`。 |
| #25 Graph | wave 原子提交、路由失败整 wave 撤销、无动作恢复不凭空造输出、仅最终回复入账且保留全部 Attempt；`test_graph.py`、`test_graph_recording.py`。 |
| #26、#76 分流与统计 | 实际 classifier 输入及路径、真实本地副作用后失败不重复、决策／fallback 分母分列、未落账重启 unknown；`test_message_routing.py`、`test_routing_statistics.py`。 |
| #27 聚合 | 空来源零 Step／零动作、仅选中资料与单 Step、非法草稿不交付或自动修复；`test_aggregation.py`。 |
| #45、#46 遗忘与页面 | 固定范围、来源闭包、部分确认／重启、共享事务、迟到投影失效、HTTP 回执不泄正文；`test_forgetting.py`、`test_memory_page_http.py`、`test_memory_sse.py`；A29 详见下文。 |
| #66 Database | 只读 SQL 形状和 authorizer、尾部真实 SQL 错误不冒充截断成功、遗忘等待 worker 清理；`test_database_console_sql.py`、`test_database_lifecycle.py`。 |
| #70 导出 | 分享脱敏、缺附件不冒称完整、ZIP 存在时遗忘不能 complete；`test_trace_export.py`、`test_trace_export_http.py`。 |
| #75、#81 拓扑与路径 | 拓扑只读、实际 Run 绑定结构／决策／trace、无 wave 原子事实不能宣称提交；`test_behaviour_topology.py`、`test_run_path.py`。Dashboard 设计迁移不纳入。 |
| #84、#95、#98 验收机制 | 六组临时 Host 产出及重启读取、失败／缺判／争议不平均、首次结果不覆盖、关键 FAIL 不被 unknown 隐藏、固定分母不被拆分义务增票；`test_acceptance.py`、`test_acceptance_supplement.py`及[schema4 矩阵](schema4-matrix.md)。 |
| #98、#105、#106 本地及 Flash | 精确对象、文件／SQLite 追加见证、native 身份／IPC 边界、停止／恢复／负债保留；`test_local_*.py` 和 `test_mach_receive_boundary.py`使用 deterministic／mock／synthetic 边界。Flash canonical wire、32,000／20,000、超额停发及诊断→checkpoint→同 job 恢复流程见 `test_local_judge_input.py`、`test_local_flash_profile.py`、`test_local_flash_pipeline.py`。不启动实际 helper／probe。 |

以上函数的准确位置及已检查断言在交付输出 `obligations-audit/FUNCTION-MAPPINGS.json` 中按原 851 文件候选封存；入仓索引保留稳定选择器。执行结果须连同下文复用和本轮检查阅读，不能仅由函数存在推出 PASS。

## 本轮补齐的必要离线证据与修复

来源为 [#16 已批准 V01–V19](sources/issue-16.md)。本轮在既有公共 `RuntimeHost.submit → wait` 边界使用临时 SQLite、生产命令／trace 和 ScriptedModel；没有把私有 helper 调用次数当作产品完成。

| 义务 | 本轮精确测试／可观察结果 |
| --- | --- |
| V01／V02／V03 | `D/test_runtime_working_memory.py::test_mixed_history_filters_before_n_complete_groups_at_gate_and_answer`：N+1 安全完整组、interrupted、旧无 Run 零散条目、probe 及真实删除隔离的最新组共存；gate／answer 先过滤再取 N，整组保留，排除原因可读，人工历史不被删。 |
| V05／V06 | `test_complete_chat_input_exact_limit_counts_persona_tools_and_result_wrapper`：汉字、组合字符、emoji、控制字符、人格、嵌套工具 schema 和工具结果包装纳入 `request-input-v1`；实际完整输入等于 limit 可发，limit−1 拒绝下一请求，已执行动作不重复。 |
| V09（兼容 V07／V08） | `test_retrieval_reservation_alone_can_stop_before_gate`：固定 answer 自身可容纳、只有 reserve 导致差 1 超额，即使脚本将 skip 检索也在 gate 前停止；actual／reserve 分列，零模型调用、零虚构 Attempt。 |
| V11 | `test_first_oversized_unknown_ledger_entry_does_not_skip_to_ordinary`：首条 unknown 单独超过 4,000，不截成半条，不跳去后续普通项；完整前缀为空，省略及 unknown 数量和提示准确。 |
| V12／V13 | `test_budget_trims_complete_history_then_ordinary_then_unknown_ledger`：同一来源集合的预算逐减，先移完整历史、再 ordinary、最后 unknown；摘要及计数同步。扩展既有 `test_zero_window_keeps_real_prior_tool_evidence_out_of_the_gate`，明确本 Run 新动作只在转录，下一 Run 才进入 prior ledger。 |
| V04：`V1-CLI-SESSION-01` | `D/test_web_cli_serve.py::test_cli_session_resumes_after_restart_without_reading_other_sessions`（one-shot／REPL）及 `test_cli_unknown_session_refuses_without_creating_or_sending`：原命令解析 `--session` 却无条件新建；修复后续接指定会话、重启仍读正确历史，其他 Session 不混入，未知 Session 明确拒绝且零请求／零新建。 |

V04 有命令级 red→green；混合历史夹具初轮误用了 probe Session、未配置 probe 目标、错误地将观察 sink 声明为持久 sink，以及漏计安全的“排除账项”摘要，这些夹具失败全部保留并明确区分于产品缺陷。修正采用既有公共契约，没有放宽生产守卫。具体运行日志、退出码及候选绑定见本轮 `implementation/` 输出和地图交付回写。

r1 独立双轴评审发现三项 P2 阻塞；r2 修复 `SPEC-001` 和 `ST-02`，但 `ST-01` 的后端修复尚未贯穿实际调用方。r3 修复运行时、安装入口与 owner CLI 的构造交接；其最终 `local.main` 错误转换仍会丢弃未完成清理的 owner。r4 在转换前使用既有 `raise_if_rollback_pending` 保留原异常及恢复入口。r1／r2／r3 候选及失败证据保留，不能改写为原轮通过。修复证据分别见 `implementation/successor-r2/`、`implementation/successor-r3/`、`implementation/successor-r4/`，关闭仍以固定候选的后继双轴评审为准。

| 发现／现行条款 | 当前行为与离线回归 |
| --- | --- |
| `SPEC-001`／V19、ADR 0016／0026 | 指定 Session 先经 `RuntimeHost.admission_observe`，实际 submit 仍权威准入；读取后的 `RecordingUnavailable` 受控拒绝。`D/test_web_cli_serve.py::test_cli_session_refuses_busy_before_reading_a_locked_store` 用 Event 保持 finalizer 的 Store 锁，one-shot／REPL 在释放前报告 busy；`test_cli_session_maps_store_failure_before_or_after_preflight` 用实际 SQLite commit／rollback 故障覆盖预检查前与其后失效，均无新 Run／Session。正常续接、未知 Session 及聚合继续回归。 |
| `ST-01`／CONTEXT 构造回滚 | `LocalExecutionStore`／`LocalExecutionWitness` 与后端共用 `ConstructionOwner`；r3 的实际 `LocalInstalledRuntime` 三资源循环、`install_local_runtime` 和 `owner_decision` 也传递并保留调用方 `_rollback`。`D/test_local_resource_ownership.py::test_local_runtime_dependency_return_keeps_actual_caller_ownership` 在真实构造的 `PY_RETURN` 验证 ledger／witness／decisions；`test_local_install_and_owner_cli_returns_keep_cleanup_reachable` 覆盖安装返回与 owner-decision／revoke 命令返回；`test_local_owner_cli_retains_first_failure_and_completed_close_progress` 验证普通首错不被 cleanup 失败替换、逆序关闭和已完成步骤不重做。普通异常／`KeyboardInterrupt` 后，临时 SQLite／append fd 已关闭或有可恢复 owner；实际安装验证和时钟使用固定离线替身，认证／发送禁用。原后端交接与父构造回归继续保留。 |
| `ST-02`／ADR 0006 | `Frames.write` 按目标 pipe 的 `PC_PIPE_BUF` 分片，不再假定 4096；循环使用同一绝对 deadline。`D/test_local_runner.py::test_frames_pipe_writes_respect_deadline_and_preserve_complete_payload` 在普通 Python 子进程用真实 pipe 背压验证及时超时、完整大帧及断管；测试进程超时必 kill 并 wait，不运行 native bundle／实际隔离探针。 |

`ST-01` 的 r4 出口回归直接执行 `local.main`：`D/test_local_resource_ownership.py::test_local_main_preserves_unfinished_cleanup_until_caller_retry` 覆盖 owner-decision／revoke、操作成功／失败和短暂／持续清理错误；持续失败的多次 retry 仍保有可达 owner，逆序释放不重做已完成步骤，业务首因保留。`test_local_main_reports_only_after_cleanup_and_preserves_process_control` 确认完整清理后的正常／受控失败 JSON 及 `KeyboardInterrupt` 行为。转换入口不自行追加一次 close 来掩盖失败。

仍适用但已有直接行为证据的条件继续复用：V08 双库完整 JSON 与二次转义、V15 新动作与同操作幂等分开、V16 实际 retry／fallback 的各 Attempt 身份、V17 同 Run 记忆 blue→green 后旧引用退出及登记故障拒发、V18 真实 HTTP／browser 的输入详情与准备失败 reload。它们不是因为旧票 CLOSED 才视为有证据。

## 找回的来源与仍未知的来源

- #45 A29：[批准补遗原文](sources/issue-45-a29/01-SPEC-SUPPLEMENT.md) SHA256 `eeaef156ebe191bd534485b34290cb4c0552408f75b92f7306be8e4c1a96bd89`；[A29-01–24 工程矩阵](sources/issue-45-a29/02-ACCEPTANCE-MATRIX.md) SHA256 `45e3f1b7a0f5326a8b74691925cedb525a963b45f273b62b7657ac85c06fbdad`。来源是本地批准任务 `01a0849e-304b-7382-a745-84ca168c2dc4`；补遗 §1 为用户批准的两项产品裁决，§2–6 和 A29 编号是工程展开，不伪称 GitHub #31 已发布的原文或额外批准。原件中的“尚未实施”是准备时点。
- A29 的逐项实现／测试对应见[遗忘实施说明](../implementation/issue-45-forgetting.md#验收对应)：固定成员／部分确认／重启，跨 Session 范围，迟到后继，不同删除相互独立，scope stale／幂等，四用途读闸，确认及清理失败回滚、重开、无正文通知。下游实际消费者由上表 #16／#19、#18、#46 各自的公共路径证明；核心读闸不能代替整条消费者链。
- #35 [ACCEPTANCE-AMENDMENT-r1 原文](sources/issue-35-acceptance-amendment-r1.md) SHA256 `bb74a13078e23af57c953502cdb0890a563465989247eead5c9389da97fc9009`。只将该票另行 Linux 专项记为 `NOT RUN — adapted, not independently tested`；R01–R07、D01、CE01–08、DDL／事务与 macOS 四安装义务不变。既有 Ubuntu CI 按实记录；该历史例外不豁免本轮两阶段 CI，也不是 Linux PASS。
- 原始 standalone brief／“8 大类、30+ 条”逐项原文仍未取得。当前地图和后继功能规格不能冒充其字节原件。本轮按现行自包含已批准功能义务续验，保留此来源限制，不编造原编号或未知要求，也不据缺原文推断新功能缺陷。

## CONTEXT 与 ADR

交付起点 main 已有 `CONTEXT.md` 及连续 ADR 0001–0043，地图“随决定逐步长出”的首批文档要求已经落实。本轮迁入主工作区未提交的验收术语和 ADR 0044／四份历史规格，以[当前合同的替代关系](AGENT-ONLY-CLOSEOUT.md#历史合同与替代关系)解释其旧状态；原文件字节保留。另删除词汇表中“日程读取层尚未落地”的过时状态句，保留 aware instant／UTC 规则；现有 `test_tool_acceptance.py::test_calendar_uses_instants_and_read_does_not_write_ledger` 已证明公共读取行为。

`Overview` 术语及 Dashboard 迁移留在原主工作区，本次不迁入。主工作区和本地适配来源树均不因本轮整理而改写；临时材料、受保护证据和真实账本不纳入提交。

## 复用边界与已替代条款

本地适配／Flash 产品增量从已验收 851 文件候选逐路径迁入；相对该候选，本轮新增生产修复位于 `gateway/cli.py`、`evals/acceptance/controlled/local_persistence.py`、`local_ipc.py`、`local_runtime.py` 和 `local.py`。CLI、聚合构造交接和 pipe 分片的旧结论不能按“全部源字节未变”继承；r2 重测命令级准入、资源所有权、存储与重启、runner 帧及失败回收、本地费用保留与 Flash synthetic 公共流程；r3 重测实际运行时构造、安装与 owner CLI、持久化及相关交接；r4 重测实际 `main` 出口与资源／local CLI 回归。每次源码变化后重新构建和验证 wheel／sdist × base／mcp，旧包不能证明新候选。原 109 项 Dashboard 相关文件完全不变的陈述也不再原封继承；HTTP／浏览器实现与输入未改，CLI 入口交互单列，本轮 Linux CI 仍实际跑浏览器检查。

未受上述变化影响的核心、控制层、Flash 输入计量、schema 和 native mocked-C 证据按[合同索引](AGENT-ONLY-CLOSEOUT.md#证据复用与保全)及本轮逐文件／依赖／环境／输入／交互核验复用；接口或共享资源交互受影响的范围使用对应 r2／r3／r4 新结果。r3 改变构造和 owner CLI 清理，r4 仅补全 `local.main` 错误出口；r2 的 Flash pipeline 夹具用 `object.__new__(LocalInstalledRuntime)` 后直接挂载 store／anchor，绕过构造和 owner CLI，输入、依赖和执行路径未变，因此复用其已完成的两项结果。不重跑无关本机全量；本机离线缓存安装和固定 Flash HOST 依赖是两种环境，不互相冒充。测试计数不相加。

真实质量、30／120 案、judge／盲复核、在线身份／metadata／认证、实际部署／隔离、hard cap 和实扣账单条款记为“历史保留，当前工程关闭不要求”；复合条款里的工程守卫仍适用。历史 FAIL／UNKNOWN／NOT RUN、首次失败、争议、原 job 状态、费用责任和原 `coverage-inventory.json` 保持，不能由新合同制造旧合同 PASS。
