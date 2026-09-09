# OpenCode Go 默认客户端请求头

核查日期：2026-09-10。
[官方 Go 客户端要求](https://opencode.ai/docs/go/#where-can-i-use-it)要求使用客户端
自己的 User-Agent，以及每段 conversation 稳定的 `x-opencode-session`，用于路由
和 prompt caching；其适用对象是产生类似编码 agent 请求的客户端。
[官方端点表](https://opencode.ai/docs/go/#endpoints)列明本项目默认
`deepseek-v4-flash` 使用 `/zen/go/v1/chat/completions`。

## 实现与身份边界

- User-Agent 为 `agent-alfred/<已安装分发版本>`；源码未安装时版本为
  `0+unknown`，不声称是 OpenCode、Claude Code 或其他已验证客户端。
- 仅对 HTTPS 的 `opencode.ai`、默认443端口、精确 `/zen/go/v1` 基路径和
  `/messages`、`/chat/completions` 相对路由生效。拒绝相似域名、其他路径、
  非HTTPS、非默认端口或绝对路由。其他端点继续使用既有 SDK 行为。
- 根据 CONTEXT.md 的 Session 定义，Host 将服务器签发的持久 Session ID 加
  `session:` 前缀作为 conversation 身份；无 Session 的系统 Run 使用
  `run:` 加该 Run ID。主回答和辅助模型在相同 Run 执行边界得到同一身份。
  Step、Attempt、模型切换和重启不改变既有 Session 的身份。
- 出站值为 `alfred-` 加上述身份的 SHA256，避免原始本地标识直接进入请求头，
  并保证 ASCII。哈希不是凭据或授权机制，不使用聊天正文或模型产出生成标识。
- 独立使用工厂、没有 Host 上下文的调用者，每次 `factory.create()` 得到一个
  随机 UUID 的客户端会话；该客户端内复用，其他工厂客户端隔离。调用者可在
  ModelRequest 中显式携带其 conversation 身份；Host 始终覆盖为可信来源。

## 调用链

RunExecutor 的模型调用包装器 → ModelRequest.conversation_id → RetryPolicy /
StreamFallback → 两种 Adapter 的请求头回调 → RouteClient 的 SDK HTTP options。
请求头不进入 JSON body，不修改共享 SDK default_headers，也不保存在 transport
pool 中。流式回退和重试仍接收同一请求身份。没有扩展 Memory 或工具功能。

## 验证范围

`test_opencode_headers.py` 用真实默认 Host、工厂及 SDK，仅替换 HTTP 边界，
验证正式请求头、多 Step 工具 Run、同 Session 的后续 Run 与重启、不同 Session、
无 Session 探针隔离、两个 wire style、重试/流式回退、共享 transport 和其他目的地。
`test_gate_client_routing.py` 验证独立指派的辅助模型与主模型共用会话身份。

这是离线兼容性证据，尚未证明服务端已接受新候选。T49/T50 仍仅有 a0a115c
临时补头的历史 PASS。新候选真实模型复验须按单独方案授权后执行，不读取旧凭据。
