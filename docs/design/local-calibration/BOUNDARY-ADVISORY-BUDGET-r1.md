# 已选择的本地计划预算边界（实现与实测仍待验收）

2026-09-29，机主明确决定 USD25 **不作为最终账单硬标准**，并对独立提问明确选择“完整请求本地预检＋响应 usage 事后核验”，替换原 Flash20,000 / Pro24,000 **实际计费输入 token 事前硬保证**。这是两项明确的合同保证变化；原 2026-09-28 严格合同、`V1-LOCAL-BILLING-BOUND`、首次失败和历史证据原样保存，不能在新合同下改报旧保证 PASS。本文件记录选择，不是 owner helper 的认证事件、#105 实测、计费凭据或 run grant。

新 `V2-LOCAL-ADVISORY-BUDGET` 要求可信宿主在每次可能发送前，针对**完整实际 wire**（含 system/messages/tools）计算本地输入预检估计，Flash≤20,000、Pro≤24,000 才准继续。原模型、端点、参数、Flash输出8192、Pro输出16384不变。响应 `usage.prompt_tokens` 是事后真实计量；若超过本地估计或原阈值，保存首次结果与原值并停止新派发。不能称预检估计是供应商认证上界，也不能承诺该次实际计费输入绝不超限。

USD25 继续作为同一 job 的计划派发阈值，统一覆盖产品、辅助、18+18诊断、30原判、30盲复核、失败和可能发送。可信宿主以冻结的官方价格/账户币种/换算快照、上述输入估计与既定最大输出形成计划占额，并在原子账本中执行 `已核实本批费用 + 未结算计划占额 + 下一次计划占额 ≤ USD25`；超过则不发下一次。未知发送、超时、缺 usage 与未结清费用不释放占额或自动重发。响应实际计量、账户最终扣费与发送前估计分别留存；已发现模型、价格、账户/币种或换算快照变化、无法继续核实，或最终扣费超过预计时，停止新派发并保留原数值与未知责任。本地快照校验不能主动保证发现供应商未通知的实时变价。

**这个本地控制不保证最终账单≤USD25。** 公开价格、余额、402、token/请求/时间限制、人工确认和本地账本都不能证明供应商最终金额上界；当前也没有已证明的最高超支额。最终收费可能大于计划估计。计划审批界面必须直接展示此风险；旧 `hard_cap_required`、`hard_cap_pass`、`complete_final_liability=true` 不适用于新合同。

已批 r7 材料包中的旧 `proposal.version=2` 及 `fee_condition.mode=hard_cap_required` 仅是原合同的历史提案字节；材料摄取只核对其 batch binding，不把该提案当成本次运行许可。新 `execution_plan.version=2`、readiness、预算文档与机主精确 run grant 必须共同绑定新合同；旧提案、旧 grant 均不能替代。状态/停机报告中的未结算数额标作“计划占额”，不得沿用旧合同“最大责任”字段。

V2 的机主 run request 自身须包含上述两项中文风险和计划阈值；只给 plan/budget 摘要或自由填写 reason 不足以完成披露。已经存入受保护目录、尚未核销的原 Attempt 终账凭证会在下一控制动作前由可信宿主按原 quote/response/账户核销；核销有效且未超计划时，原 job 可在原时钟和额度内继续。无效或超计划终账停机，首次响应保持原样。终账若在这次核销之后、派发前最后检查时才出现，也保守停机，不自动恢复。这个检查不保证发现供应商尚未出账、尚未写入本机或在最后检查之后到达的费用；这些仍是计划预算的残余风险。

V2 终账读回独立记录实际 CNY 扣费（每元 10¹² 整数单位）、原 quote 的 CNY→USD 换算分数及向上取整后的 USD 计划账本单位；旧 `V1-LOCAL-FINAL-CHARGE` 不可用于 V2。受保护的账户原始记录与机主审阅须绑定同一 Attempt、payload、response、quote 和账户。换算结果是计划账本对比值，不表示供应商以 USD 实际扣费或证明最终总金额封顶。

仍保留总576 Attempt（Flash480/Pro96）、每案主辅16、并发1、原10800秒、每 Attempt/gate120秒、每 Run900秒、模型/阶段顺序、synthetic 守卫、精确批准对象、首次结果与固定分母、争议和历史失败、撤回/停止、未知发送责任、独立盲复核以及诊断后机主审阅精确质量 checkpoint。没有 AWS、新管理员、自动充值、正式120案、通用阈值或发布授权。原 #105 custom Seatbelt 隔离强度不随本变更降低；真实安装、凭据/出口/故障实测、账户与模型核对、独立复审和新合同 owner grant 仍是首条付费请求的前置。

完整条款文字、差异和最小验收顺序见独立本机草案：

- `/Users/nineofour/.codex/outputs/local-calibration-soft-budget-20260929-r1/CONTRACT-DELTA.md`
- `/Users/nineofour/.codex/outputs/local-calibration-soft-budget-20260929-r1/ISSUE-98-106-REVISION.md`
- `/Users/nineofour/.codex/outputs/local-calibration-soft-budget-20260929-r1/IMPLEMENTATION-ORDER.md`
