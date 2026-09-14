# 手动聚合

验收合同：[AGGREGATION-SPEC-r1](../design/issue-27-manual-aggregation-spec.md)。

## 使用

Dashboard 的 Behaviour 页面提供「手动聚合」表单：选择既有目标会话，填写聚合目标和关键词，再勾选语义记忆、情景记忆、会话窗口。关键词初始随目标填入，可独立编辑；选中记忆来源时不能为空。全不选合法，会记录目标及「未选择来源」，不调用模型。

CLI 使用相同入口与校验：

```sh
agent-alfred --aggregate '整理本周项目进展' --session SESSION_ID \
  --keywords '项目 进展' --sources semantic,episodic,history
agent-alfred --aggregate '空来源对照' --session SESSION_ID --sources ''
```

CLI 逐源显示实际提供数量、来源容量排除、请求裁剪、许可排除及未取完状态；会话窗口另列轮数排除，Web 的 Behaviour、MainBar 和 Run 详情也显示该事实。轮数与字符排除分开计数；因轮数上限排除全部历史时归为 `capacity_excluded_all`，已有来源故障仍优先归因。无法证明的数量保留 `unknown`，局部故障显示降级及原因。

`SESSION_ID` 必须是已有会话；CLI 聚合不自动创建会话。每次明确提交产生新 Run。提交后切换 MainBar 会话不改变目标；响应丢失时先查看已有运行，页面不会自动重投。

聚合草稿及「本次提供的资料」清单保存在目标会话，运行详情展示逐源读取、排除、实际发送和恢复事实。清单仅表示本次发送了哪些资料，不证明逐句引用正确。可从清单查看原记忆或历史 Run；删除、更新或隔离后原身份显示不可用。展开的原资料通过现有 MemorySync 核验版本；离线、资料变更或无法核验时立即隐藏正文，过期读取响应不能恢复正文，重新查看需要再次读取。

## 运行边界

启动时编译固定 `manual_aggregation` DAG。三个来源各有独立 `local_read` 工具节点和普通错误恢复边；只执行选中的来源，每个最多采集一次。`join` 在完整来源裁剪后决定唯一的综合节点或五种无草稿终点。聚合专用工具不加入普通聊天的工具目录。

每库仅查询既有检索顺序的前 `per_store_limit` 个候选；先检查自动消费许可，再以 `per_store_character_budget` 整条裁剪。单库计量采用同一中央脱敏器投影后的资料数组编码，包含来源身份、连续编号、完整内容及数组分隔符；替换文本变长或缩短都按实际投影计算。来源节点提交前再次核验 Registry 返回的实际脱敏结构与容量。超长项跳过后继续当前候选内的后项，不补页。窗口只取当前目标会话中完整、获准、成功的普通聊天问答，受 `working_memory_rounds` 限制；总请求超限时从最旧问答起整组移除。基本目标、指令、人格和固定记忆仍超限则明确失败。即使 `join` 超限，已发生的来源结构校验失败仍保留失败状态和原因，不会被工具执行成功计量覆盖。

综合使用一个 Step，共享原 Run 的 Attempt、重试和总期限。发送前及每次重试前复核冻结身份、版本和消费许可，登记可对账的输入；仅准备但未发送的来源不计作使用。请求只包含目标、固定指令、人格和最终资料，不进入分类、检索 gate、Skill 选择或普通 Agent 回退。

模型流中的候选不会进入正式 MainBar 或 CLI。非空文本经过引用格式及身份检查后才能交付；未知、损坏引用或工具请求使运行失败，不追加修复生成。合法文本可不含引用。局部来源失败可保留其它资料，总截止、取消、输入证据或计量失败则强制终止。

唯一 Recorder 保存目标和合法草稿。保存中保持准入占用；保存失败沿用单槽未保存投影，刷新后仍可见草稿及来源事实。进程重启将未完成 Run 记为 interrupted，不续跑或补投。

## 持久化与遗忘

前向迁移 v18 为 `runs.purpose` 加入 `aggregation`，保留旧数据、索引及触发器；系统 probe/consolidation 仍不关联 Session。人工会话和 Run 读取包含聚合，自动聊天窗口、后续聚合窗口及提炼候选排除聚合。

持久资料清单仅含 kind、id/version 或原 Run/Session 身份与编号；不复制主题或正文。真实输入沿 `memory_uses`、`history_reads` 传播遗忘隔离。原始会话草稿及 trace 保留供人工阅读，受管摘要不缓存聚合来源正文。

## 验证入口

- `src/agent_alfred/evals/deterministic/test_aggregation.py`：Host/HTTP/CLI、真实 DAG/SQLite、容量、来源组合、持久化及遗忘。
- `src/agent_alfred/evals/deterministic/test_aggregation_safety.py`：真实 SDK/受控 HTTP、每 Attempt 复核、未发送对账、来源故障和真实计量写入失败。
- `src/agent_alfred/evals/deterministic/test_aggregation_repair.py`：来源类型校验、真实提炼准备/自动提炼、终端传输失败、删除/隔离与未选资料对照、保留记忆的窗口裁剪及局部超时。
- `tests/browser/aggregation.spec.js`：Behaviour、HTTP/SSE、正式显示、提交丢包、保存故障、刷新/重启、遗忘及所有来源组合。

离线模型只验证协议与执行边界，不衡量草稿语义质量。具体候选、门禁和 CE 运行结果见实现工作树的 `tmp/agent-work/issue-27` 证据，独立评审结论另行绑定候选。
