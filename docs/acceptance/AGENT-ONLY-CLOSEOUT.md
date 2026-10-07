# V1-AGENT-ONLY-CLOSEOUT-20261007-r1

本文件是 v1 当前工程关闭合同，承接用户于 2026-10-07 明确要求的“修改 issue 关闭条件，不进行真实测试”，以及将必要代码、文档交付 main 后关闭[总地图 #1](https://github.com/nineofoursyrup/Agent-Alfred/issues/1)的授权。它替代旧规格、正文和评论中要求新增真实校准或真实环境验证才能关闭 #1、#83、#98、#105、#106 的条款；不声称那些旧条款已经通过。

## 当前工程关闭条件

- 必要本地适配、Flash 输入修订及文档进入 main；固定候选完成独立 Spec / Standards 评审，无未解阻塞。
- 六组能力的必要工程义务有要求、公共路径、离线证据和结果／缺口映射。既有核心、CLI、Web、文件、SQLite、记忆、Skill、分流、聚合、权限、来源、限额、停止与恢复机制继续有效。
- 适用机械检查通过。既有确定性结果只有在源码、输入、依赖、配置、环境和交互影响仍适用时才能复用；变化、失败或未覆盖风险补充验证。PR 的测试 merge SHA 与实际 main merge SHA 的 CI 分别记录，不能互相冒充。
- 总地图两项遗留内容按[义务补遗](agent-only-obligations.md)核对并收口；完成验收回写后由 REST 与 GraphQL 读回 CLOSED / COMPLETED、最终正文和相关交付状态。

允许的证据是 Agent 可执行、可复核的本地确定性测试、ScriptedModel、MockTransport、synthetic fixture、临时文件与 SQLite、mock 驱动的公共 CLI / Web / 执行路径、静态和打包检查。计数不能代替义务映射，复用不是本轮重新执行。

## 工程关闭与真实守卫

| 轴 | 当前含义 |
| --- | --- |
| Agent 工程关闭 | 上述限定工程范围已交付、检查和评审可复核；Issue 的 completed 使用这个含义。 |
| `offline_engineering` | 某个证据包及候选的机械／公共路径结论，仍按对应 schema 校验；不能仅因 Issue 关闭改写包内状态。 |
| `quality` / `v1_release` | 既有版本化质量与发布判定。真实材料、批准、阈值、校准或有效证据不足时继续如实 FAIL / BLOCKED / UNKNOWN；不自动转为工程关闭结论。 |
| 真实运行准入 | 精确来源、候选／安装物、身份、时效、运行 grant、隔离和预算机制独立成立。工程关闭、聊天授权、mock PASS 或 CI 均不产生这些事实。 |

本轮不新增真实产品、judge、盲复核、metadata、认证、账户／计费、部署或实际隔离探针，不恢复真实 job，不补造 handoff，不签发运行或质量许可，不改真实账本。Dashboard 设计迁移属于独立地图；公共 PyPI 发布不在范围内。

18+18 补齐、30 案真实校准、120 案正式验收、通用数值门槛、新鲜在线身份、真实三域／本机隔离、金额 hard cap 证明和实际账单结清均不是当前工程关闭前提。默认拒绝、来源核验、停止、恢复和责任保留仍须有效；不得删除守卫或更改历史结果来取得通过。

## 历史合同与替代关系

历史文件的 OPEN、NOT STARTED、DRAFT、未授权或“必须真实验证才能关闭”等文字是对应版本的快照。下表说明其现行适用范围；原文和首次失败继续保留。

| 历史版本／来源 | 当前适用范围 |
| --- | --- |
| [设计 r4](../design/v1-acceptance-design-r4.md)、[#83 Q16–Q25](../design/issue-83-decision-frontier.md)、[ADR 0044](../adr/0044-critical-obligations-and-judge-evidence.md) | 原真实质量决策及术语来源；关键义务不可抵消、固定分母、依据／标签分开、争议保留仍适用于证据解释。真实校准和数值批准不再阻塞本次工程关闭。 |
| [阶段 A 规格](../design/v1-acceptance-evidence-phase-a-spec.md)、[离线补充规格](../design/v1-acceptance-offline-supplement-spec.md) | #84 / #95 已交付的离线机制合同及版本兼容性继续有效。旧文中的实施状态不代替后续交付；schema1–4 的原解释不变。 |
| [本地合同记录](../design/local-calibration/README.md)、原冻结草案和 `confirmation.json` | 原确认记录保留；Seatbelt、本地计划预算及 Flash 分别有明示修订。当前已停止真实测试，这些文件不能作为继续安装、认证、探针或运行的操作指令。 |
| [Flash V3 修订](LOCAL-FLASH-CALIBRATION.md) | 仅 judge／盲复核输入上限 32,000，固定官方 tokenizer 完整 canonical wire 计数加 256；产品／辅助 20,000 和输出 8,192 保持。相关来源、停止、负债和同模型独立实例限制继续有效；不是运行许可。 |
| [历史 coverage 盘点](coverage-inventory.json)及[来源审计](coverage-sources.md) | 保留 INCOMPLETE、NOT RUN、source gaps 和原行号／哈希。未映射不直接等于功能缺陷，也不直接改成 PASS；当前仍适用的义务见补遗。 |

当前验收回写入口：[六组能力／#83](https://github.com/nineofoursyrup/Agent-Alfred/issues/83#issuecomment-6039049820)、[#98](https://github.com/nineofoursyrup/Agent-Alfred/issues/98#issuecomment-6039044871)、[#105](https://github.com/nineofoursyrup/Agent-Alfred/issues/105#issuecomment-6039034809)、[#106](https://github.com/nineofoursyrup/Agent-Alfred/issues/106#issuecomment-6039039219)。这些记录验收了先前本地候选，不冒称其当时已经进入 main；main 交付及双阶段 CI 由总地图本轮记录独立绑定。

## 证据复用与保全

交付起点为 main `da395e20390890e03b0a742c3b957c903b69157a`，本地已验收候选为 `01682ec98da0e16516123658e559331fa781b1cf44acb8ee4b1039f9cc7db7fa`（851 文件）。本轮迁入必要增量，另补当前合同和仍适用的离线断言；最终候选、PR head、测试 merge SHA、实际 merge SHA 和日志以总地图验收记录为准，不能用起点候选替代。

既有结果按精确字节与作用范围复用：核心 6185 基线、Dashboard 351、本地控制层 365、诊断后继 5、Flash 输入 59、公共 mock 流程 2、到期恢复 synthetic 1、旧 schema 兼容 32，以及 Ruff／构建／四种安装组合。它们存在重叠，不相加为唯一测试总数；既有环境首错和后继修复保持可追溯。新增测试及必需 CI 独立记录，不宣称本机重跑全量。

机主本地证据索引为 `/Users/nineofour/.codex/outputs/issue-agent-only-closeout-20261007-r1/CLOSEOUT.md` 与 `EVIDENCE-REUSE.md`；本轮输出另存 `/Users/nineofour/.codex/outputs/v1-main-closeout-20261007-r1/`。原输出只读保留，运行证据不放入产品 Git 树。公开读者使用上述 Issue 验收链接；本地路径仅供机主定位。

Flash r3 原裁判 18/18、盲复核 11/18、本地比较 10/18；7 项 blind 和 8 项 comparison 缺失，原偏差 4、盲复核偏差 2、派生诊断与旧材料争议仍保留。真实 job 保持 `SUSPENDED / actual_model_identity_expired`，无有效 clean handoff／质量 checkpoint，30 案未启动。这些是历史事实，不能改为 PASS。

计划占额 USD 0.292996、含历史 USD 2.867245 仍不是核定实扣账单；未结费用责任继续保留。工程关闭不释放负债、不认可实际 hard cap、不证明模型质量或实际隔离，也不产生部署、运行或发布许可。
