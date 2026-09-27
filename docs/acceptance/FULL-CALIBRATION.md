# 同一 job 的首次产品采样与封存

`controlled_calibration.CalibrationDriver` 接续 #103 的诊断检查点，使用
`PersistentControlledAuthority` 的原始 grant、时钟、计数与金额账。
`calibration_operations(batch)` 在激活前冻结 18 + 18 个诊断操作及 30 个产品、
30 个原判、30 个独立盲评操作。产品操作最多 16 次，主模型、gate、Skill selector、
routing classifier 的全部调用共同消耗该额度、Flash 480 和全批 576。

产品顺序按六组交错，每组原始顺序不变；原 r7 即 C01/M01/T01/S01/R01/A01 至 05。
每案使用原生 runner 的 `_run_case` 接缝，重建独立文件、SQLite、persona、Skill 和记忆，
只装载声明种子。准备/重开阶段使用显式 seed-only ScriptedModel；不会执行未登记的
产品语义工作。正常阶段全部 Host ModelRequest 经受控工厂；`replace` 只设置批准的
thinking mode，保留消息、工具、实例及两个 Attempt callback。InputCapture 记录所有
Host 输入 Attempt，账本中的 prepared object 保存实际完整出站 payload。

原 profile 的 max_steps 12、输入/gate 64000 字符、工作记忆 8 轮、每 store 8 项及
8000 字符保持不变。工具 mask 只能缩小五个批准本地工具。模型客户端只能使用固定
DeepSeek Flash/Pro 单次派发，不启用自动重试、流式、网络工具、MCP 或业务外发。
该 Python controller/Host 组合是工程接缝；真实进程权限、出口、主体隔离仍由 #105 验证。

## 首次结果与中断

每个产品、原判和盲评槽在任何发送前写 `CALIBRATION_SLOT_START`。已封存槽只读回，
未封存但已开始的槽永久保留缺口并停止；新 handler 或新目录不能重新创建首次结果。
产品业务失败仍按计划继续，基础设施、控制或边界失败关闭新派发。异常原文和 Attempt
引用并存；停机后的已观察结果允许追加封存，不能改变停止事实或释放未决费用。

Host 构造或关闭失败的 rollback owner 由同一 `CalibrationDriver` 保持可达，
即使异常被写入报告或传播为进程控制异常也不丢失。调用方保留该 driver，并调用
`retry_cleanup()` 继续关闭原资源；清理重试不创建 Host、不重跑案例或模型请求，
不改变已经记录的停止事实、计数和金额。重试遭中断时同一 owner 继续保留。
公开 `replay.demonstrate()` 在运行前接管其局部 driver 的清理责任，普通异常已被
写入报告时也会清理；若清理仍未完成，传播的异常保留可重试的 `IncompleteRollback`。
每个 handler 的 store、anchor 和 runtime 由构造前建立的聚合 owner 接管，
工厂返回与调用方保存之间的中断也不会丢失所有权。清理先确认 runtime 资源已释放，
再关闭 B/C 的 SQLite 连接；替换 handler 和演示入口的失败沿用同一清理责任。

健康 handler 替换使用原 Authority 的 `handoff` capability，由 replacement 的
`recover(handoff=...)` 一次消费，保留原始起点、计数和金额。终态新 handler 只审计
读回；未经 clean handoff 的 replacement 不获得继续采样权限。

原判复用 `judge_result` 和严格 parser。盲评仅取 `blind_input`，原标签/理由/引用不进入
盲评请求。`GRADE_BLIND_RAW_SEAL` 后才生成本地比较，比较不调用模型。标签一致、引用
可解析只证明这些局部事实，无法证明的语义支持继续 `unknown`，原 raw opinion 保留。
无产品输出、无原判、格式错误、无盲评和未运行槽各自列出，不补采或编造 review。
Authority 的 phase validator 同时核对首次封存集合、固定案例集合和已关闭操作，
不能用一个任意 phase evidence 跳过产品或盲评阶段。

新 EvidenceStore child 使用现有 schema4 的 `retry`（新采样 child）关系连接已封存的
`<job>-diagnostics`；这里只接受尚无产品结果的原材料，因此该关系不表示重跑旧样本。
其余 review/summary/decision 历史及完整祖先链原样保留。每个结果的
`evidence.controlled_execution` 绑定 job、plan、runtime candidate 和不可变
Attempt ID/digest/prepared/response 引用；模型判分不会增加产品分母。
材料缓存恢复到新目录时，已封存的诊断与产品 EvidenceStore 继续使用原稳定路径，
保留检查点绑定的路径、摘要和完整父链。恢复先验证这些派生证据再切换材料指针；
原派生证据缺失或损坏时保持停止，不以重建材料缓存代替首次结果。

## Mock provenance 与报告

`synthetic_replay` 原样接收 r7 的 `simulation=False`、原候选和完整 parent。
每个新 Mock 产品结果使用 `source="simulation"`、`sampled_at=None`，fixture 观察时刻
单列于 `evidence.execution_provenance`。运行候选单独绑定于 execution plan，
不能使用原材料批准来批准新代码。`synthetic_capacity` 不得进入该 driver。

既有 schema4 报告对空采样时间保留 `actual_sample_time_missing`，对真实材料中的
模拟结果继续保留 `real_sample_missing`。真实 online 结果缺时间也保持 BLOCKED，
不会跳过七天时效。原材料、来源争议、126 家族排除及正式 120 案的后续隔离要求保留。

完整报告包含 30 案 manifest/2-2-1 分布、18 个诊断槽、48 个应有盲评槽、双方 raw、
比较、争议、所有原失败/缺口、Run outcome/recording_state、原时钟与实际/未决金额。
`offline_engineering` 等待最终候选检查绑定；真实就绪、校准质量、阈值和 v1_release
分列。fixture 的全部完成不产生真实质量决定，aggregation_policy 仍为 None。

## 新输出目录中的完整或部分演示

```sh
.venv/bin/python -m agent_alfred.evals.acceptance.controlled.replay \
  --material-reference /absolute/protected-transfer/reference.json \
  --vault /absolute/protected-transfer/vault \
  --output /absolute/new-full-output \
  --protected-original /absolute/original-r7-root \
  --protected-original /absolute/original-approval-root
```

附加 `--stop-after-cases 2` 在已有首次失败与后续结果后执行不可逆 fixture stop。
每次必须使用新输出目录。源材料完整读回；原始文件树 before/after 校验一致。
输出包含 candidate、plan、全部实际 wire、诊断 checkpoint、完整报告、所有事件/锚和
容量读回，并验证新 handler 重复命令新增发送为 0。两个演示应在最终源码/文档冻结后
使用同一候选。运行产物只写外部输出根。

容量上界以该业务 driver 的实际事件/对象数为基数，补齐剩余至 576 个 Attempt 的
14 events/6 objects 上界，再加入 576 个控制接收/回执及保守生命周期余量。
这复用 #102 的完整容量证据，不等同真实云吞吐或账单结清证明。

## 批准 profile 所触发的原生输入预留回归

原 `RunMemory.prepare` 对已经受 JSON 编码字数预算约束的检索文本再预留六倍，
使每 store 8000 在输入 64000 下提前拒绝。修复按已编码文本的第二层 quotes/backslash
最坏两倍计算，包含固定标题余量；实际完整请求仍在每次发送前按 input_characters
检查。公开 Host 回归覆盖两 store 的 4000/8000 满预算、引号、反斜杠、控制字符和
非 ASCII；旧的超限拒绝与工作记忆裁剪不变量继续验证。
