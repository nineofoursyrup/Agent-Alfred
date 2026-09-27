# 决定来源与执行绑定

`execution_decisions.py` 实施 #98 / #100 的离线来源协议。它使用既有
`DecisionSource.read_decision`、schema4 决定/争议合同和 `EvidenceStore.read`。
schema1/2/3/4 的内容与历史批准不变；执行绑定及 run/checkpoint 事件是独立的
`V1-REAL-CALIBRATION-DECISIONS-r1`、`version=1` 闭合合同。

## 原批准与来源缺口

`approved_materials()` 固定 r7 摘要 ID、canonical SHA256、完整材料包 manifest
SHA256 及三条来源争议的精确 ID/digest。Q1–Q25、D1/D2/D4/D5/D6 保留；D3
的有界盘点、三家族缺原 case、正式 120 案空槽仍为证据缺口。
这些常量仅表达已批准对象和已知范围，不构造原消息 ID、真实主体或原决定时刻。
本地聊天副本、记录时刻、哈希和 Agent 签名不能成为来源。

当前没有安装真实来源适配。所有真实包、任意注入的来源对象及 CLI 都保持
`approval_source_unverifiable`，不会读取凭据、构造模型客户端或赋予发送能力。
已批准产品选择与缺失来源认证分别表达；不把来源缺口描述成用户尚未作选择。

## 公共接缝

```python
binding = material_binding(
    store, batch_id,
    approved_batch_id=original_batch_id,
    package_manifest_sha256=verified_package_manifest_sha256,
)
reader = DecisionAdmission(
    store, source=source, subject=expected_subject,
    dispute_scope_refs=individually_verified_scope_references,
)
observation = reader.verify_materials(binding)
request = run_request(
    binding, job_id=job_id,
    plan_sha256=exact_plan_digest, budget_sha256=exact_initial_budget_digest,
)
event = reader.verify_execution(request, source_ref, binding)
```

`material_binding` 每次沿完整父链读回不可变包，检查 bundled deny-only 历史、父摘要、
原请求前缀及已开始预算的 scope/起点。未知祖先、c25 及后代、重置预算均被拒绝。
调用者不能删除 parent 来匹配既有绑定。c21 历史授权不会产生新运行许可。
完整包字节验证由材料准入层负责；这里只接收它核验过的完整 manifest digest。

绑定包含原/当前摘要、精确候选、全部祖先、授权历史及字段级 before/after digest。
只改变候选时，原材料事件仍只认证原摘要对象，返回明确差异，必须另有绑定新候选的
run 事件。改变材料内容则需要认证当前材料摘要；旧摘要不能被重签或冒用。
读取器没有许可缓存。重新创建读取器后仍需活的来源，撤回、失联、源数据篡改都会关闭准入。

来源独立性争议首先须通过既有 schema4 逐项用户裁决合同。除此之外，
`dispute_scope_request(binding, original_dispute, adjudication)` 将每条原意见、
精确裁决和 `judge_diagnostic_only` 范围绑定到可读回事件。它不创建 `dismissed`
记录，不替换原意见，也不把 `unknown` 写成 PASS。两种来源投影可以来自同一个已发生
人类决定，无需重新访谈；无法认证实际原决定时继续阻塞。

`checkpoint_request` 接受完整 `run_decision`，以及同一 job 的精确裁判摘要、争议和
剩余额度摘要。它固定原 run 的 ID、来源引用和 request digest；核验时再次读回原 run，
因此撤回原运行许可后，旧检查点不能继续生效。材料、run、checkpoint 与既有
results/calibration/thresholds 摘要互不替代。冻结材料绑定在同一 job 内保持不变；
产生的证据版本以额外 digest 关联，不能借新目录、绑定或 checkpoint 重置预算。

阶段执行器仍须检查真实 18+18 对象、阶段先后、实际剩余额度、同一时钟、并发、金额
预留和独立隔离。来源核验只返回事件，不返回 grant 或发送票据。

## 离线入口与验证

```sh
uv run python -m agent_alfred.evals.acceptance source-preflight \
  --store /path/to/new-evidence-store --batch candidate-batch \
  --approved-batch original-material-batch \
  --package-manifest-sha256 VERIFIED_SHA256 --subject EXPECTED_SUBJECT
```

此命令只读，不写封存库；默认无可信来源，退出码为 2。
`source_preflight` 始终返回 `online_executable=False`、`release_eligible=False`。

唯一正向 fixture 是明确合成的 `SimulationAuthority`：
`issue_decision` 复用旧合同，`issue_execution_decision` 演示新增版本协议，
`revoke_decision` 保留原事件并让后续读回拒绝。其 live anchor 仅用于离线机制测试，
同机权限、测试签名及可写文件均不是真实独立性证明。
真实身份、独立权威接入及隔离在 #105 验收；真实校准与质量决定在 #106。
本接缝不设置数值阈值、不关闭 #98/#83、不授权真实运行。
