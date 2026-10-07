> 历史操作与证据索引。当前 [Agent-only 合同](../../acceptance/AGENT-ONLY-CLOSEOUT.md)已替代本页要求新增真实环境、认证、18+18／30／120 案后才能关闭工程票的条款。本轮不执行下列真实操作，也不继承页内旧时态授权为新的 run grant；所有真实准入、停止、质量与费用守卫保持。V2 之后的 Flash V3 修订见[独立记录](../../acceptance/LOCAL-FLASH-CALIBRATION.md)。

# 本地工程与实际验收的衔接

适用合同为 [已确认记录](README.md)、`V1-LOCAL-REAL-CALIBRATION-SPEC-r1`、[custom Seatbelt 补充](BOUNDARY-SEATBELT-r1.md) 和 [本地计划预算补充](BOUNDARY-ADVISORY-BUDGET-r1.md)。后者明确替换 USD25 最终账单硬保证及实际计费输入 token 事前硬保证，当前执行对象必须绑定 `V2-LOCAL-ADVISORY-BUDGET`。工程基线是 PR #107 的 `da395e20390890e03b0a742c3b957c903b69157a`。本文是实现后的操作与证据索引，不是认证、run grant 或阈值许可；原冻结草案保留原字节。已授予的具体操作权限直接沿用，不能因本文历史“获得授权后”措辞重复索要。

## 代码边界

| 部分 | 入口 | 实際保证与限制 |
|---|---|---|
| 本地控制面 | `controlled/local_runtime.py`、`local_installation.py` | 显式安装配置；完整安装与账户证据不齐则拒绝 dispatch；普通入口不会自动取 key |
| 来源与撤回 | `controlled/local_source.py`、native owner helper | 由可信宿主发起本次 nonce 与精确对象认证，直接读取本次回执并持久化；不是远端独立批准，也不证明历史原事件 |
| 本地状态 | `controlled/local_persistence.py` | 复用持久账算法，新生产 SQLite 适配与独立文件追加见证；原 fixture 保持 synthetic-only；不防机主共同回滚 |
| 执行桥 | `controlled/local_runner.py`、`local_ipc.py`、`local_projection.py`、`local_readback.py` | 单案 DTO、64 KiB 控制框、限定大对象引用；可信 Host 重构完整请求，实际 runner 的 SQLite/trace/文件由宿主独立读取 |
| macOS 包 | `native/macos/`、`scripts/build_local_macos.py`、`controlled/native.py`、`local_sandbox.py` | 固定 Python 与生产依赖、逐案 custom Seatbelt 策略、OS build/loader 与字节清单；弃用接口风险已确认，编译/签名检查不能证明沙箱实际生效 |
| 阶段与金额 | 既有 `PersistentControlledAuthority`、`DiagnosticDriver`、`CalibrationDriver` | 复用阶段、首次结果、固定分母、并发 1、原 10800 秒、撤回/未知责任及统一预算；没有复制另一套流程 |

产品在沙箱中的 Host 处理真实单案本地文件和 SQLite；可信宿主内的第二个 Host 只复算下一次请求与可核对的业务状态，不作为另一次产品样本，不产生额外模型调用。只对已经独立核对的结构化文件回执做根路径对应；不一般化地忽略请求差异。worker 提交的记录保留为不可信旁证，不能自报批准、身份、金额或正式判分。

完整候选有 30 个单次产品容器和一个独立 probe 容器。消耗标记先于准备/启动持久化，失败也不重用；新 job 不能复用同一产品 slot。原因是终止一个进程组不能证明所有后代进程已退出，同一个容器不能据此交给下一案。它们不增加用户账户或云服务。新包只能成为新的候选，不能续用旧 job 的批准。安装目录需实际证明 runner 不可写；校验缓存不为更改后的字节重新背书。

## 当前安全入口

```sh
python -m agent_alfred.evals.acceptance.controlled.local inspect
```

无配置返回 `BLOCKED / local_installation_required`，不取 key、不打开认证 UI、不创建 grant、不发请求。`inspect --config <installation.json>` 只检查配置与证据，成功读取也仍缺实时 owner decision 和 run admission。`owner-decision`、`revoke` 是未来显式机主操作：它们调用本次 pinned native helper，不接受 CLI 伪造的认证事实，也不启动模型运行。

新配置字段以 `local_installation.load_installation` 为闭合契约；生产配置不随源码附送。保护根、ledger、witness、decision log、证据及 credential 是不同文件。机主选择的目录与 0700/0600 权限只构成本地可信宿主规则，必须结合 #105 实际 sandbox 拒绝验证。`capture_host_environment()` 仅在最终干净安装的 host interpreter（通常 `python -I`）导出 stdlib、依赖、Alfred、已加载 native libraries 与 import 路径，不读取整个 home 或凭据。

## 金额与结算

当前 V2 按完整实际 wire 做输入预检估计、响应 usage 后验及原子计划占额检查。USD25 是计划派发软目标；未结算计划占额继续保留并计入原子算式。未知发送、缺 usage、超预计或无效的迟到终账继续停止新发，不清除原结果或未知责任。终账按原 Attempt、payload、response、quote 与账户核销，实际 CNY 扣费和计划 USD 换算分列。最终账单可能超过 USD25，最高超支额未获证明；机主精确审批界面必须展示这项风险与输入超估计风险。完整当前规则见 [计划预算补充](BOUNDARY-ADVISORY-BUDGET-r1.md)，不得继续以旧硬金额证明作为 V2 的默认准入门槛。

以下 Route A/B 段落保留原严格合同的机制与历史适用范围；它们不能与当前 V2 混用，也不能作为本批硬封顶 PASS。

本地 Route A 支持已证明的精确最终收费函数，或完整最终责任上界；每次发送前原子核验 `已验证支出 + 未决最坏负债 + 本次最坏预留 <= USD25`。输入 token 的界、输出界、失败/在途计费、附加费、税、汇率和取整都需要适用本账户的供应商原始证明。实现中的 `provider_certified_complete_wire_utf8_bound` 不假定 UTF-8 字节与 token 的比例；缺供应商证明时拒绝。公开价格、余额、请求次数或 owner 勾选不能自行成为账单硬上限。

响应与 usage 已核实，但终账金额未定时为 `RESPONSE_VERIFIED`：`actual_units=null`，完整最坏预留保留，后续请求只能使用剩余的已证明责任空间。发送/usage 未知仍停止并占额。迟到终账只核销原 Attempt 的原 payload、response、quote 与账户，必须有新的精确终账证据及 owner 审阅；不重新发送，不重新定价，不刷新原运行时间或 grant。实际收费超过证明上界时保存真实值并报告 `billing_bound_violated`。

供应商或支付侧强制总责任封顶的 Route B 尚无适配和有效证据；当前候选不会因为账户余额或充值额度而自动采用它。D2 已确认严格 USD25，因此金额证明不足的下一步仍是 `BLOCKED`，不默认降级或增设云架构。

## 最小推进顺序

1. **完成联合工程候选。** 冻结三部分源码和确认文档；运行源码、受影响行为、分发包、Dashboard 及原生编译/签名检查。30 案输入形状与 576 次容量演示必须单列为 synthetic 工程证据，不能进入真实 30 案分母。每次修复保留初次失败，按确切候选复核。
2. **#105 的具体本机操作包。** 展示安装包摘要、所需新目录、host interpreter、代码/签名/entitlements、30+probe 容器、无价值 sentinel 与无计费目标、拟做系统检查及留存/恢复方式。获得相应操作授权后，才安装或启动 native/owner/probe。固定 probe 目前自动覆盖六种文件 read/write、IPv4/IPv6 loopback TCP 和同 helper 子进程；UDP、DNS、外部出口、代理、系统代发、FD/提权、跨案与实际 Keychain 策略等仍需明确追加实测，不能从 probe 子集推定全部通过。按 [#105 AC1–AC8](ISSUE-105-DRAFT.md) 逐项保留原始结果，拒连/缺文件/超时不算权限拒绝。
3. **#106 的账户与准入包。** 核验 Flash/Pro 的本账户有效身份和适用 route、当前价格/账户币种/换算快照、完整 wire 预检及响应 usage 后验方案；将本次来源/安装/模型/费用证据与 candidate、binding、V2 plan/readiness/预算及 run grant 精确绑定。最终内部路由与收费仍缺的事实和残余风险明示，余额不作上界。沿用 Q1–Q25、r7 材料及限定三条争议范围，新的 owner 承接事件声明 `original_event_verified=false`。旧消息缺失的身份或时刻不补造。承接批准、就绪审阅、run grant 是不同对象；必要凭据接入与已授权只读核验可继续，计费身份请求必须归属既定计划与有效 grant。
4. **单独明确启动后运行裁判诊断。** 复用 `ProtectedMaterialStore`、`EvidenceStore`、`execution_plan`、`PersistentControlledAuthority` 及 `DiagnosticDriver.run()`。原计划最多 18 次裁判诊断 + 18 次独立盲复核；归并在本地比较，不增加模型调用。坏 judge、unknown、争议或缺少人工裁决不能修成 PASS。
5. **用户检查点。** 输出该 job 的 `DiagnosticDriver.checkpoint()` 精确摘要和剩余时间/预算。owner 对该对象的新鲜批准才能 `continue_with()`；人工等待计入原 10800 秒，过期需保持停止，不能重开旧 grant 或刷新起点。
6. **真实 30 案及判分复核。** `CalibrationDriver` 接续同一个 authority/job/ledger，固定 C/M/T/S/R/A 交错 01–05；最多 Flash 480（每案 16，包含辅助）及 Pro 30 判分 + 30 独立盲复核，全程总计 576。保留首次产出、未记录终态、失败、争议和固定 30 分母；不是对坏结果重跑后择优。停止/撤回后仍保留未知负债及原响应核销通道。
7. **#83 决策收口。** 展示真实结果、成本、争议、质量与 reviewer 范围，由用户再决定通用阈值/聚合规则和最终合同。`thresholds`、`aggregation_policy` 仍未批准；旧 95% 草案不作默认。正式 120 案及 7 天时效是后续质量验收，不被本地工程 PASS 替代。

本地工程、#105 环境、#106 首请求准入、实际校准、质量与发布结论分列。不存在任何根据本次离线结果关闭 #98/#105/#106/#83 或按未满足的旧 AWS 合同勾选完成的操作。
