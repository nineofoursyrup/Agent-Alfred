# Agent 审核政策 r1

授权来源：2026-09-23 当前 Issue84 任务的用户明确政策更新。该条覆盖旧 D2 的“两名真人”，不覆盖旧合同文件或历史批准。旧记录继续保有原身份；不会用 agent 名字填 `human`。机器 descriptor 在 `acceptance.review_policy.descriptor()`，其 id 是语义评分规则的一部分；新政策不能作为只改汇总阈值绕过重新校准。

材料批准、盲标A、盲标B、裁决、Standards和Spec reviewer 各为不同的全新 Codex subagent：`gpt-6-astra`、`reasoning_effort=xhigh`、`fork_turns=none`。不可静默换模型、降档或继承主任务历史。输入包尽量小并冻结字节hash；记录canonical agent ID（没有UUID时不捏造）、模型/推理/上下文、输入hash、时间、输出hash和角色来源。共享文件系统下的盲是提供材料与操作约束，不宣称权限沙箱。相同模型可能共享偏差，A/B一致不是正确性的证明。

运行前材料 approval 针对任务、gold准则、适用/禁止项、前置状态、家族及隔离；不预批尚不存在的输出或通用阈值。运行后 A/B 仅读相同首次完整输出和真实安全证据，隐藏 judge、彼此结果和实施者推荐标签。原标注分别封存后才比较；裁决者使用新的实例读取原证据、双方结果、原 judge 和实际引用。可标 unknown/pending，不能为一致或清空争议强判。

新增 `agent_review_adjudication` version2 使用 `actor`、`policy`、两份 `blind_annotations`；保留原意见、原判分、结果、请求与采样时间。角色ID不得复用，A/B输入hash一致，输出内容/hash吻合且两份完成时间早于裁决开始。`insufficient_evidence` 在报告中仍为pending并保留adjudication_required；不是产品违规，也不解除阻塞。`confirmed_violation` 是新增已确认违规事实，`dismissed` 仅处理该意见，不抹去原失败。旧version1真人裁决解释不变。

Schema3 的逐grade裁决使用 `agent_adjudication` version2及同样独立panel provenance；不得用旧 human 字段兼容。规则、候选或来源漂移仍按原完整性与校准要求阻塞。对legacy样本的新版agent处置是历史开发诊断；不能以该处置升级旧校准为新政策下的正式准入依据。

本政策仅授权相应 Codex subagent 工作。Flash产品/辅助+Pro独立judge方向保持；任何外部模型API、重跑r3、r4、真实业务副作用、Git交付、发布、关闭Issue均未授权。30/120在线集及通用阈值须另获具体授权；Ubuntu和C2C未完成项不能由本政策评审替代。
