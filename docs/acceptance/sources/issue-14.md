# #14 实现：ModelEndpoint 配置表与两个适配器

来源：https://github.com/nineofoursyrup/Agent-Alfred/issues/14

## Question

**ModelEndpoint 配置表** + 两个适配器类。

- 配置表每行：`{name, base_url, api_key_env, catalog_url?, models: {<model_id>: {style, path}}}`。至少收录 `anthropic`、`openai`、`xai`、`deepseek`、`opencode`（`https://opencode.ai/zen/v1`）、`opencode-go`（`https://opencode.ai/zen/go/v1`）。注意后两者 **base 不同但共用 `OPENCODE_API_KEY`**。
- ⚠️ **`style` 挂在 model 上，不挂在 provider 上**（[#2](https://github.com/nineofoursyrup/Agent-Alfred/issues/2) 裁定，推翻本票原先的 per-provider `style` 字段）。调研 §10.1 实测 OpenCode Go 同一个 base 下按模型分派三种 wire 形状。`style` 取值 v1 只有 `anthropic` 与 `openai`；`responses` 的模型标为 `unsupported`，携带机器可读原因 `unsupported_wire_style: responses`。
- **支持三态 `supported / unsupported / unknown`（默认 unknown）与 provider 连接四态正交**，两个字段分别渲染。目录里出现的、判断不出 style 的模型落 `unknown`，要求用户手选 style 并持久化后方可使用。自动翻转 `unknown → unsupported` 仅限「端点不存在的 404」与「白名单 400」，且必须把证据写进 trace。
- `AnthropicAdapter`：原生 messages API，content blocks + `tool_use`。
- `OpenAICompatibleAdapter`：**一个类覆盖全部 OpenAI 兼容端点**（`ModelEndpoint`；`Gateway` 一词已归入口适配器）。
- 两者都实现 `决定：内部消息、工具调用与模型响应协议的形状` 定下的协议；**业务循环不得出现任何供应商分支**。
- 密钥只从环境 / `.env` 读；对外只暴露"是否已配置 + 末四位"。

### 验收

- 一个测试断言 `loop/` 目录下不出现任何供应商名字符串。
- 两个适配器对同一份内部消息产生各自正确的 wire 格式（用录制的 fixture 断言，不打真实网络）。
- 加一家新供应商 = 配置表加一行，有测试证明。





---

⚠️ **由 [#29](https://github.com/nineofoursyrup/Agent-Alfred/issues/29) 修订，四处：**

- **复合键统一为 `(endpoint_id, model_id)`**。`provider_id` 这个叫法退出词汇表——`provider` 本就在 `CONTEXT.md` 里 `ModelEndpoint` 的 _Avoid_ 列表上，而调研 [#8](https://github.com/nineofoursyrup/Agent-Alfred/issues/8) 实测同一供应商的两个 base（`opencode` / `opencode-go`）有 6 处价格不同，这个心智模型已被证伪。
- **配置表行增加可选 `auth_probe` 声明**，只给**确有「需认证且无副作用」端点**的行填，且必须冻结成**可执行契约**：`method` / URL 或 path / 成功状态码 / 超时 / 错误映射**五项齐全**，缺任一项即视为未声明。没有声明的端点，界面如实显示「无免费认证探针」，**不得拿 `{base_url}/models` 的 200 冒充凭据有效**——该路径并非每个端点都存在，且有些目录允许匿名读，200 只证明公共目录可达。
- **支持三态的「依据」与「形状出处」分成两个字段**：`support_basis`（`builtin_table` / `probe_evidence`，只记**系统**依据）与 `wire_style_source`（`builtin_table` / `user_declared`）。用户手选 style 后支持三态**仍是 `unknown`**（ADR-0021），因此本票原文「要求用户手选 style 并持久化后方可使用」的落地方式是：`assignable` 放行、支持三态不动、界面说「未验证，按你选择的形状尝试」。
- **自动翻转 `unknown → unsupported`** 照旧（端点不存在的 404 + 白名单 400），证据写进 trace 并记为 `support_basis=probe_evidence`。那一格从未被用户声明占据，所以翻转空间仍在。


---

## 2026-09-06 用户批准的 Issue #14 实施边界修订

六端点生产形状错误白名单为空，启用条目为 0；生产失败不触发支持态翻转。测试专用规则只验收覆盖、Run 证据读取、租约与异常收尾机制，不代表供应商识别验收，绝不进入生产配置。本次明确调整原供应商翻转正例要求，不将缺失证据标为通过。

覆盖键为 `(endpoint_id, model_id, wire_style)`；机制允许 supported 或 unknown 被有效证据覆盖为 unsupported/probe_evidence，不改内置表或指派；无成功调用升级。重启清空内存覆盖，已持久化 trace 保留；空覆盖不意味从未翻转或已恢复。发布中断、flush 失败不能推定所有 Sink 未落盘；既有 /api/run-evidence 仅增加本 Run 的 support_overrides，不抬高 trace 完整性。

OpenCode messages 使用 x-api-key、chat/completions 使用 Bearer，仅单套头，不自动切换。依据官方源码 337fd144d2ba144743368f78d9579a99cce175bd，未使用真实凭据实测，不保证线上部署版本一致。auth_probe 六行均未声明。完整结构化错误观测通道及通用规则框架暂缓，待首条真实规则获批。

本地实施文档：docs/implementation/issue-14.md、docs/adr/0030-shape-evidence-does-not-change-assignment.md（当前尚未提交，不构成验收证据）。

## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/14#issuecomment-5565705668

## Issue #14 完成说明

本票已完成已批准范围内的实现、验收与合并，关联 [PR #42](https://github.com/nineofoursyrup/Agent-Alfred/pull/42)。本说明保留范围调整及验证限制，不把未完成的供应商识别或漏洞复现计为已验收。

### 实现与合并身份

实现内置 ModelEndpoint 配置表、按模型选择 wire style 的表驱动工厂、OpenAI-compatible 与 Anthropic messages 适配器，以及工具协议、流式/非流式内容块和运行期支持覆盖证据。支持覆盖按三元组隔离、先脱敏记录再通知，不改变用户指派；既有 Run evidence 读端提供覆盖证据。

已知凭据从既定入口读取并显式登记，涵盖短凭据及轮换值；普通脱敏阈值保持、空值不登记，不新增凭据最小长度限制。

- 已验收 HEAD：`fca685624a237a389a63e1a976cb6b5e1c74778a`
- 固定基线：`4aa34f22d16ec5c1d58c191c5179ace0cc22fdd1`
- merge commit：[`ea9e4b8e1a1581995c94bf26a893f7dc86194019`](https://github.com/nineofoursyrup/Agent-Alfred/commit/ea9e4b8e1a1581995c94bf26a893f7dc86194019)
- 内容摘要：`7fc116a40895b8ba25d6511a7c5b68943753756a9d077cc1401868e50fcf1437`
- 合并结果树：`edcdfd54b79aba933e1a26bd84677b618a5213c5`

行政收尾前已重新确认 PR #42 为 merged，main 包含上述已验收 HEAD。合并提交的两个父提交分别为固定基线和已验收 HEAD；合并树全部243份内容匹配验收清单，34条候选变动路径没有扩大。内容摘要为按路径排序、包含 path/kind/sha256 的 JSON 清单（两空格缩进、末尾换行）的 SHA-256。

### 本地验收与独立审查

- 本地 Python 离线确定性全量：**2579 passed，1项 requires_key 排除**。
- 目标回归：**356 passed**，其中包含新增18项合成凭据保护用例，不重复累加计数。
- ruff、技能检查（空技能树）、`.env.example` 一致性、wheel/sdist 打包及 staged/committed diff whitespace 检查均通过。
- 打包归档哈希已复核；wheel中168份源码资源及sdist中全部34条候选文件与验收内容一致。
- 针对完整已验收 HEAD，独立 **Standards 整体 PASS（non-blocking）**、**Spec 整体 PASS**。两轴分别连接此前34路径完整实质审查与四文件凭据修复，评估跨模块契约影响；本地提交后又各自读取 Git 树、父提交、路径和内容，明确确认 exact HEAD，而非未经核验搬用提交前 PASS。

本地提交收尾阶段，typecheck通过／browser **52 passed** 是历史结果经核验复用，不是该阶段对新 HEAD 的重新实跑；32份前端源码、浏览器测试与配置输入一致，后端契约影响由当前 Python 回归与保护测试覆盖。历史浏览器记录没有完整环境变量快照，该限制仍然保留。

### 合并后实际 CI

[合并后 CI：run 34087920544](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34087920544) 对应 merge commit `ea9e4b8e1a1581995c94bf26a893f7dc86194019`，push事件，第1次运行，已完成且 **SUCCESS**。

重新核验的全部工作流、检查与步骤均成功，包括 lint、技能检查、环境示例一致性、离线 Python 测试、Dashboard typecheck 和离线浏览器测试。这是合并提交的实际 CI 执行结果，与上段本地历史前端门禁复用明确区分；未通过重跑或绕过检查取得结论。

### 保留的三项非阻断建议

1. `wiring.py` 的 `OpenCodeGoFactory` 旧厂商名称继续作为通用工厂兼容别名。
2. `endpoints.py` 中 opencode 的 base URL 字面量与 opencode-go 常量写法视觉不对称；不构成双真源。
3. `anthropic_native.py` 的类型标注及 docstring 与 OpenAI 实现不对称。

以上建议保留，不处理、不升级严重度，也不因行政收尾创建维护票。

### 原范围调整与验证限制

- 六端点生产形状错误白名单为空，启用条目为0；生产失败不触发支持态翻转。测试专用规则仅验证机制，**真实供应商形状识别及翻转正例未验收**。完整结构化错误观测通道和通用规则框架仍按批准范围暂缓，auth_probe均未声明。
- 密钥状态／末四位及短值全掩码的呈现归 #34；本票不实现该UI，不豁免既有凭据保密责任。
- 短凭据修复采用获批的**静态链路核验与修复后合成保护测试**；**未运行修复前红灯或完整泄漏链路复现，未独立端到端证明持久化保密**，不声称完成严格红绿循环。测试使用隔离环境、合成凭据、内存数据库、FakeClock、ScriptedModel与内存事件捕获，不使用真实凭据或模型服务。两轴分别判断上述证据足以验收明确范围。
- 接受极短凭据可能使普通正文额外被掩码的代价，普通非凭据匹配阈值未全局降低。
- OpenCode messages使用x-api-key、chat/completions使用Bearer单套鉴权，依据源码提交 `337fd144d2ba144743368f78d9579a99cce175bd`，不是线上实测，也不保证实际部署版本与该源码完全一致。

上述保留项不改变当前已批准范围的完成结论；本票按 completed 原因行政关闭。
