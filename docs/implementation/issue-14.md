# Issue #14 实施与验收计划

基线：`4aa34f22d16ec5c1d58c191c5179ace0cc22fdd1`。本计划是实施输入，
不证明任何测试或审查已经通过。原始规范为 #2/#3/#4/#14/#29 与相关 ADR。
2026-09-06 用户最终补充裁决优先，见 ADR-0030。

1. 六行不可变端点表、per-model style/path、auth_probe 五项齐备校验（生产零声明）。
2. 支持三态与 wire_style_source 分离；未知 style、用户 override、responses 边界逐项测试。
3. ToolSpec 三字段、ToolChoice、StepStarted.tool_choice；原始参数片段三字段。
4. 改形前生产序列化 golden 固定；旧 trace、新 payload 走真实证据读端。
5. OpenAI/Anthropic 非流式：同一消息投影、工具定义与选择、工具块解析、usage 双口径与未知停止原因。
6. 双流式：多块保序、signature、工具参数累积、累计 usage、收尾信号与 incomplete_stream；
   断流参数只作为原始片段，不构造可执行工具调用，不猜补 JSON。
7. frozen 支持覆盖先脱敏再完整插入；三元组隔离、幂等、改形状与密钥轮换、重启。
8. 测试专用规则验证 F1–F7：record 失败、单/全部 Sink 禁用、发布 fatal、插入前后中断、
   flush 失败、重复通知抑制、脱敏失败；不变式“发起通知 ⇒ 覆盖已生效”。
9. 复用 recording_pending 闩锁证明覆盖生效早于准入释放；结构扫描只补充行为测试。
10. 既有 /api/run-evidence 增加本 Run 的 support_overrides；trace 不可用仍可查内存记录；
    已落盘 notice 在重启后仍可查，不承诺发布或 flush 失败时所有 Sink 未落盘。
11. 表驱动工厂、正确路径与单一鉴权头、无密钥零 transport、注入假端点即可扩展；
    指派保留，覆盖命中后下次发送明确失败 model_unsupported。

**调整的验收边界**：六个端点生产白名单为空，启用条目 0；所有生产失败不翻转。
测试专用规则只证明机制，不替代真实供应商识别验收、不进入生产配置。
原计划要求供应商翻转正例的部分按用户裁决调整，不标为已通过或静默跳过。
完整结构化错误观测通道、通用规则框架留待首条真实规则获批。
OpenCode messages=x-api-key、chat/completions=Bearer；源码依据与未实测限制见 ADR-0030。

不做工具执行/ToolRegistry、目录价格链、Models/Connections 页、responses Adapter、CLI 流式呈现。
CLI 后续票仅草案：实现 block 多索引呈现与作废撤回，依赖 #14；本轮不创建 Issue。

验证：逐项 red → 最小实现 → targeted green → 相关回归；不用 sleep 或概率竞态。
最终按 CI 运行 ruff、skills、env、非 keyed 全量、Dashboard typecheck/browser，并检查打包。
不安装升级依赖，不使用模型凭据；冻结候选路径哈希后独立 Standards/Spec 对同一摘要审查。
主 Agent 唯一 writer，不暂存、提交、push、创建 PR 或合并。

实施细节：保留 `ModelRequest` 既有位置参数顺序。Anthropic 未给 `max_tokens`
时使用 4096；其明确报告的 `input_tokens` 仍为已知 uncached 值，缺缓存字段时
不推算 total。OpenAI SDK 3.3.1 内部吞掉 `[DONE]`，路由 transport 在 SDK 已解析的
SSE 边界保留收尾标志；真实 SDK 离线字节流验收覆盖“已有 finish_reason 但缺 DONE”。
这些是协议机制证据，不是任何供应商线上实测。

flush 失败时 `trace_incomplete=true` 是整次 Run 的遥测事实；若另一个 trace Sink
已经成功写入完整包，读端的 `trace_status` 仍可为 `available`。这两个字段不互相覆盖。
