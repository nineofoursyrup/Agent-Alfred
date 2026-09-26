# Issue #84 阶段 A 整票验收摘要

本页是安全、可独立阅读的交付摘要。合同为 [Issue #84 的阶段 A 规格快照](sources/issue-84.md)，逐项公共测试入口在 [phase-a-matrix.md](phase-a-matrix.md)（下称 M），当前操作在 [README.md](README.md)，授权边界在 [AUTHORIZATION-ADMISSION.md](AUTHORIZATION-ADMISSION.md)。此页的“通过”只表示阶段 A **离线机制**在所列证据范围内通过；不表示产品真实语义质量、`v1_release`、可信线上授权或发行通过。

## 候选、证据与复用规则

- 整票基线：`da5a9e3fee6cf6b9e9254ba9c53d73b1ab63d7f0`（原 main）。本次交付范围为相对该基线的**全部本票代码、测试、文档、来源快照和覆盖清单**；c31→c34 的 14 路径只是末段 P1 增量。整票 Git/PR 身份和 CI 链接由 #84 验收评论回写，不能用 c34 哈希冒充整票提交。
- 最后一次产品代码候选 c34：`89f6fe5ab29b344e7766612243933a0acc96ab170cca2c1694dbed9539f871ba`，703 文件，c34 acceptance **816 passed**、Ruff/build PASS、wheel/sdist 包文件各 501 匹配、隔离安装 wheel/sdist × base/MCP **4/4 PASS**。均为 macOS/CPython 3.14、合成授权与本机离线 wheelhouse 条件；实际日志、命令、时间和首次失败在原独立临时证据包 `trusted-authorization-p1-c34-r1`，未提交原始运行输出。
- c32 全量 Python **5799 passed、1 requires_key deselected** 只复用到 c33/c34 未改的非 acceptance 范围；c34 受影响 acceptance 全集已重跑。c31 浏览器 **351 passed** 及 sync/skills/env/typecheck 仅复用到未改代码、锁文件、前端和设置。文档收尾须核对代码字节与 c34 一致、所有引用测试名存在，并以新临时库检查实际离线示例。PR CI 在最终提交上另行运行；这两组旧结果不伪称最终候选全量重跑。
- `coverage-inventory.json` 的 1614 行、27 票、321 个定位到的 test selector 和 1293 个仍未精确绑定的义务，是 **2026-09-20 来源审计快照**。其 `current_status=NOT RUN` 是当时审计状态，不作当前测试结论；逐票产品缺口仍在清单，不能以 #84 离线机制完成消除。缺原始 standalone brief/“8 大类 30+”不编造。快照来源文件不因本次验收改写。
- 本地 Ubuntu 验收沿用用户此前“Ubuntu 忽略”决定，记为 **NOT RUN**，不填 PASS；PR 触发的仓库必需 Ubuntu CI 仍必须实际通过，并在 #84 评论记录 PR head 与合并 SHA 两阶段结果。C2C 仍 **NOT REVIEWED**，原票未把它列为新增关票门槛。

## R01–R10

表中 `M:ACxx` 指 M 的同编号公共测试和反例；除另注外，结果来自 c34 acceptance 816 passed，机械检查来自上面的 c34/c32/c31 适用范围。剩余质量与真实执行项归 #83 后续 B/C，可信生产环境与硬金额上限归独立 P2–P4；它们均不反向算作阶段 A 未实施。

| 要求 | 实现/公共入口与适用证据 | 实际结果；缺口与归属 |
| --- | --- | --- |
| R01 | `report.py`、`gates.py`、`coverage.py`；M:AC01/02/09/17 | 离线 PASS/FAIL/BLOCKED 与发布轴分离、失败和 blockers 同存；真实发布仍 FAIL/BLOCKED，#83 B/C。 |
| R02 | `candidate.py`、`schema.py`、`safety.py`、实际加载包检查；M:AC03/12/21 | 身份、内容和模型边界离线通过；哈希不证明外部来源，真实 issuer/worker 归 P2–P4。 |
| R03 | `schema.py`、`store.py`、`coverage-inventory.json`；M:AC04/05/15/23 | 版本、批次、30/120 分母与来源清单机制通过；真实批准金标/完整跨票断言仍是 #83 B/C 与各原票缺口。 |
| R04 | `runner.py`、`judge.py`、`online_judge.py`、真实 Host/SQLite；M:AC06/07/12 | 六组合成案例走产品公共路径，执行事实与 judge 分轴；真实模型语义和可信在线 Dispatch 未验证，归 #83/P2–P4。 |
| R05 | `report.py`、`specialist.py`、追加裁决与争议入口；M:AC08–11 | 分母、专项公式、失败/unknown、人工裁决机制通过；通用数值阈值和真实校准仍归 #83。 |
| R06 | `admission.py`、`simulation_authority.py`、`budget.py`、请求 journal；M:AC13/14/15 | 生产 `approval_source_unverifiable`，合成 MockTransport 账/撤回/SEND_INTENT 通过；真实 broker 和可验证硬金额上限归 P2–P4。 |
| R07 | `collect.py`、`pytest_evidence.py`、安装脚本；M:AC17/18，c34 4/4 安装 | macOS 机械证据按候选范围通过；本地 Ubuntu NOT RUN，最终 PR/merge CI 必须另记。 |
| R08 | `candidate.py`、`report.py`、校准父链与原采样时间；M:AC03/15/16 | 七天、变更、重评不刷新时效的离线拒绝通过；旧真实质量证据不自动变新鲜，#83 B/C。 |
| R09 | `EvidenceStore`、追加报告、恢复与删除；M:AC19–21 | 半写、引用、敏感数据和删除失效离线通过；原封存库只读，历史 4,518 类文件内容未验。 |
| R10 | 公共 CLI、[README.md](README.md)、M:AC22/23 | 离线顺序、格式和 BLOCKED 路径已说明；真实在线命令当前不可执行。缺来源/产品质量归 #83/各原票。 |

## AC-01–23

| AC | 实现/公共入口和证据 | 实际结果；剩余缺口/归属 |
| --- | --- | --- |
| 01 | CLI `report/verify`；M:AC01 | 离线轴独立，缺质量/阈值发布 BLOCKED；#83。 |
| 02 | `report.py` 模拟聚合；M:AC02 | PASS/FAIL/BLOCKED、FAIL+blockers 和 simulation 标签通过；真实发布 #83。 |
| 03 | `candidate.verify/verify_runtime`、store；M:AC03 | 候选/包/证据漂移拒绝，原身份不变。 |
| 04 | `schema.validate`、导入；M:AC04 | 未知版本、重复身份和断链拒绝。 |
| 05 | case set/校准资格；M:AC05 | 30/120 分组及实质隔离规则通过；批准材料未形成，#83 B/C。 |
| 06 | `dry-run`→Host/Session/SQLite；M:AC06 | 六组真实公共业务路径和重启读取通过，模型输出标 simulation。 |
| 07 | ToolRegistry/执行元数据→report；M:AC07 | 声称成功不能覆盖失败/缺持久证明。 |
| 08 | `report.py`/`specialist.py`；M:AC08 | 无默认通用阈值，提炼比率、N/A、禁止项通过；通用批准 #83。 |
| 09 | 分组与 profile 聚合；M:AC09 | 失败不被平均或跨 profile 拼接。 |
| 10 | runner/judge/中断账；M:AC10 | 产品失败、判分错误、余案与 unknown 分轴保留。 |
| 11 | `adjudicate`、review disputes；M:AC11 | 原判分不覆写，未决不放行；真实人工裁决不由 agent 代签。 |
| 12 | judge 请求/模型身份；M:AC12 | 同模型拒绝、无工具能力与非法判分处理通过；真实语义抗诱导 #83。 |
| 13 | `require_source`、模拟 Authority；M:AC13 和 admission 测试 | 生产在凭据/工厂前拒绝，只有 `SimulationSession`+`MockTransport` 合成正向；真实硬 cap P2–P4。 |
| 14 | 共用预算/journal/MockTransport；M:AC14 | 产品、辅助、judge 与重试计账及到限停止的离线路径通过；真实外部账 P2–P4。 |
| 15 | store 原批/重评父链；M:AC15 | 首次结果、已花额度和原采样时间不被重跑/重评覆盖。 |
| 16 | 原采样时间/校准引用；M:AC16 | 七天边界、未来/矛盾时间及配置变化拒绝；真实新样本 #83。 |
| 17 | `collect` 的 collection/outcomes；M:AC17 | 漏收集、skip、旧 CI 不能填通过；本地 Ubuntu NOT RUN，PR/merge CI 待远端读回。 |
| 18 | 安装脚本；M:AC18，c34 隔离安装 4/4 | 产物与源码污染检查通过；该结果限定本机离线 wheelhouse。 |
| 19 | `EvidenceStore`/report publication；M:AC19 | 半写与旧 latest 不伪造新报告。 |
| 20 | store 删除/依赖重读；M:AC20 | 已删/损坏不可复用，trace/CI 清理不级联删包。 |
| 21 | `safety.py`、空 `CredentialOverlay`；M:AC21 | 显式/ambient 凭据与私人状态诱饵拒绝；未读真实凭据。 |
| 22 | CLI 离线示例/恢复；M:AC22、[README.md](README.md) | 命令和恢复接口齐备；当前生产 execute/judge 阻断，未来操作 P2–P4。 |
| 23 | `coverage-inventory.json`、`coverage.assess`；M:AC23 | 来源→义务→路径→缺口可核对；1614 行仍有 1293 个精确绑定缺口，归原能力票/#83 发布证据。 |

## CE-01–12

| CE | 实现/公共反例证据 | 实际结果；剩余缺口/归属 |
| --- | --- | --- |
| 01 | `report` 缺证据；M:CE01/AC01–02 | BLOCKED 不被模拟 PASS 覆盖；真实质量 #83。 |
| 02 | `collect` 完整收集；M:CE02/AC17/23 | skip/漏收集拒绝；本地 Ubuntu NOT RUN，PR CI 另读回。 |
| 03 | 分组/专项/禁止项；M:CE03/AC08–09 | 已知失败仍 FAIL，N/A 不算通过；通用阈值 #83。 |
| 04 | Host 超时/空输出/工具失败；M:CE04/AC07/10 | 原案和余案保留，无输出不推定安全。 |
| 05 | judge 非法输出/同模型/裁决；M:CE05/AC11–12 | 错误与争议保留，人工裁决须显式导入；真实 judge 语义 #83。 |
| 06 | store 单次采样/父链；M:CE06/AC05/15 | 替换首次结果、混批/复用样本拒绝；正式材料 #83。 |
| 07 | `require_source`/MockTransport 预算；M:CE07/AC13–14 | 生产零请求，模拟边界通过；真实 broker/硬 cap P2–P4。 |
| 08 | 候选与 judge 校准绑定；M:CE08/AC03/12/16 | 变化/过期阻断，不把哈希当签名；新校准 #83。 |
| 09 | 重评父链和七天时效；M:CE09/AC15–16 | 补判不刷新产品样本；新采样须新授权，#83。 |
| 10 | 源码污染/安装产物；M:CE10/AC18，c34 4/4 | 离线安装公共路径通过；PR CI 另读回。 |
| 11 | 报告半写/丢失/删除/脱敏；M:CE11/AC19–21 | 原始包不可变、失效可见；旧封存材料保持原样。 |
| 12 | 旧 CI/手填 PASS/覆盖缺口；M:CE12/AC02/17/22/23 | 不被升级为阶段 A 或发布通过；跨票缺口留 #83/原票。 |

## 不改变的历史与后续

c25 的 90 次请求和派生意见持续 `QUARANTINED_AUDIT_ONLY`，实际费用 `unknown`；c21 原 `v1_release=FAIL`、r3 四条 pending、revision64 历史保护 FAIL 与首次失败/seal 均不改写。四张恢复截图当前哈希匹配原件，但不倒改历史 FAIL；4,518 项历史凭据/密钥类文件内容未读取，历史全量保护不声称 PASS。原 store 不运行会写报告的 `verify`。

#84 完成的判据是本页所述阶段 A 机制、最终同候选 Standards/Spec 双轴、必需 PR/merge CI、合并及 #84 `CLOSED/COMPLETED` 远端读回。#83 保持 OPEN；真实 broker/OS/egress、可验证硬金额上限、真实校准、正式 120 案、通用阈值和 v1 发布判定仍由后续单独处理。
