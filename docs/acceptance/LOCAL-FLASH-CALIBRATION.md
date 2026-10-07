> 当前工程交付按 [Agent-only 关闭合同](AGENT-ONLY-CLOSEOUT.md)完成，已停止新增真实测试。下文保留 2026-10-07 Flash V3 的已确认运行合同及限制；其中真实 18+18、30 案和认证／运行要求不再是本轮 Issue 关闭前提，也不授权执行它们。原结果、缺口、争议、首次失败、job 停止事实和未结负债保持；输入 32,000 仅限 judge／盲复核，产品限制不变。

# 本地 Flash 校准修订

2026-10-07 用户确认新独立批次，并明确“确认接受，不使用pro，改使用flash”。
本修订只适用于新 `V3-LOCAL-FLASH-CALIBRATION`；V1 严格合同和 V2 本地软预算合同、
既有 job、首次结果、争议、批准回执、原始时钟及未结算负债均保留。

## 精确对象与保证变化

- 产品、辅助、18 次诊断、18 次独立盲复核、30 次判分及 30 次独立盲复核均使用
  `deepseek-flash`。机主随后明确接受输入修订 A：仅裁判/盲复核输入阈值改为32,000，
  用固定官方V4 tokenizer数据与`tokenizers==0.22.2`计数完整canonical wire再加256；
  产品/辅助保留原字节估计方法与20,000。所有输出上限8,192，
  `thinking=disabled`、非流式、无重试、无 fallback。每个裁判操作只调用一次。
- 新批总 Attempt 上限仍为 576，按模型计 Flash 576 / Pro 0；用途上产品及辅助最多
  480，裁判及复核最多 96。产品每案最多 16 次，单案 900 秒，单次 120 秒，
  同 job 总计 10,800 秒，包含等待质量批准时间。不重置既有批次时钟。
- USD25 是本地计划派发软目标；完整请求预检及响应 usage 事后核验不构成实际计费
  token 或最终账单硬上限。未知费用不退还预留，最终账单超计划仍原额记录并停止新发。
  输入阈值和估计方法绑定V3 plan、budget、run disclosure以及原Attempt；usage高于
  原估计或该用途阈值即停新发。32,000与256余量不保证所有真实请求或usage均合规。
  tokenizer缺失、版本/数据散列不符或计数失败均拒绝，不能退回更低估计。
- 独立性保留不同实例、最小盲输入、原结果及盲输出先封存再本地比较；**取消跨模型
  独立性保证**。schema4 仅在全部产品和裁判为 `deepseek/deepseek-flash` 且 profile
  显式含 `independence_policy=distinct_instances_same_model` 时接受同模型。
  未声明的旧合同、其他模型或其他 schema 版本仍拒绝。

## 材料与授权

V3 plan 绑定完整 `judge_profile_change.before/after`。只允许旧 Pro profile 的
`model_id` 改为 Flash、增加上述 independence policy、重算 profile id；协议、端点、
材料、gold、rubric、固定分母不变。可信宿主检查 before 与原 material binding 的
profile 完全一致。所有请求、producer、判分和新 schema4 child 使用同一 after。

原材料包和原 material binding 继续用于来源认证、恢复和每次发送前检查。
原 7 个来源/争议回执只有在同一安装、同一主体、同一对象、当前未撤销时才可复用；
原 3 条来源独立性 unknown 从原材料保留，不能因换 profile 后 inactive 而消失。

新预算/身份/就绪证据使用 V3 单 Flash 对象；不要求 Pro 身份或不同有效模型的证据。
原 V1/V2 两模型证据不能自动充当 V3。真实 owner helper 必须认证新精确预算、就绪、
运行对象；V3 run disclosure 明确模型差异、限制、金额风险和保证变化。
聊天中的操作授权不是原生回执，也不替代诊断质量批准。

## 验收边界

精确 V3 运行批准授权本批 runtime profile amendment，不伪造新 profile 的 schema4
材料批准。原 review 和 summary 保持历史对象；新的最终 schema4 材料轴仍如实保留
`user_materials_approval_missing`、`independent_material_review_missing`。
不能在采样后补签、过滤缺口或将运行批准改称材料批准。

既有受控路径可先完成 18+18，汇总原始错误与全部争议；只有实际有效诊断和用户对
精确 checkpoint 的质量决定通过后，才可继续同 job 真实 30 案、判分和独立盲复核。
首输出缺失或判分无效仍会留下槽位缺口；完成流程不保证 90 次有效调用。
这些实际观测可交付，但不等于完整 calibration quality PASS、通用阈值批准、正式
120 案或发布。要取得新材料轴 PASS，必须另在采样前完成新目标的独立材料复核和
精确 summary 批准；不能将本次新模型运行默认为该批准。

所有 synthetic 守卫、本地真实运行专属入口、精确候选/安装物校验、操作系统隔离、
凭据与出口约束、停止/撤回及未知发送责任保持。离线测试不冒充实际安装或真实校准。
