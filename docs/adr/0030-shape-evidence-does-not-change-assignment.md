# 运行期证据否证的是形状支持，不是用户的选择

支持态由只读内置表与进程内支持覆盖共同决定。经批准的形状证据可将
`supported` 或 `unknown` 覆盖为 `unsupported/probe_evidence`，不改指派或内置表。
覆盖键为 `(endpoint_id, model_id, wire_style)`；密钥轮换不撤销形状证据，
A→B→A 重新命中 A 的覆盖。成功调用不升级支持态；用户手选不借用其他形状的
内置支持声明；内置 `responses` 模型即使 override 也保持不支持。

覆盖是先脱敏、完整构造、一次发布的不可变记录；`model_support_flipped` 是
已生效记录的审计副本，每三元组每进程至多发起一次，不承诺各 Sink 恰好投递。
发布中断或 flush 失败不撤销覆盖，也不证明所有 Sink 都未落盘。
重启仅清空内存覆盖；已持久化 trace 可继续读取，空覆盖不意味着从未翻转或
重新验证成功。只读 Run 证据按 run_id 隔离，覆盖不能把不完整 trace 变完整。

## 本轮验收边界（2026-09-06 用户修订）

六个生产端点的形状错误白名单均为空，启用条目为 **0**。生产失败不触发支持态
翻转。测试专用规则仅证明覆盖、证据读取、租约和异常收尾机制，不代表真实供应商
识别已验收，不进入生产配置。本次明确调整 #2/#14 原先要求的真实翻转正例；
没有凭空构造供应商错误码来满足正例。完整结构化观测通道和通用规则框架暂不实施。
所有 auth_probe 均未声明，不发新增探测请求。

OpenCode 鉴权改为最小路由映射：messages 使用 x-api-key，chat/completions 使用
Bearer，不同时发送两套头，不在失败后切换。依据为官方源码提交
`337fd144d2ba144743368f78d9579a99cce175bd` 的
[Zen messages](https://github.com/anomalyco/opencode/blob/337fd144d2ba144743368f78d9579a99cce175bd/packages/console/app/src/routes/zen/v1/messages.ts)
及 [Go chat](https://github.com/anomalyco/opencode/blob/337fd144d2ba144743368f78d9579a99cce175bd/packages/console/app/src/routes/zen/go/v1/chat/completions.ts)
等四条路由；这是固定源码依据，未用真实凭据实测，也不声称线上部署版本一致。
