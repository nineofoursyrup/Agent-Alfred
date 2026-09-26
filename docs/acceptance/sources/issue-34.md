# #34 实现：Models 与 Connections 设置页（唯一模型入口）

来源：https://github.com/nineofoursyrup/Agent-Alfred/issues/34

## Question

按 [决定：Models 与 Connections 设置页（唯一模型入口）](https://github.com/nineofoursyrup/Agent-Alfred/issues/29) 的决议施工。**本票不重开任何决定**；下面每一条都在那张票的决议正文里有对应段落，出现分歧以决议为准。

### 范围

- **Connections（只读页）**：展示内置 `ModelEndpoint`、密钥状态（长度 ≥ 8 才显示末四位，短密钥全掩码，**前端永不收到完整值**）、连接四态与观测三件套（`checked_at` / `checked_via` / 机器可读原因）、目录健康（`unfetched / fresh / stale / unavailable`，**与四态并排但分别标注**）。动作只有「验证凭据」（仅当配置表行声明了 `auth_probe`）与「重新读取 `.env`」。**不增删改端点。**
- **Models 页**：候选按 `(endpoint_id, model_id)` 合并且保存 `sources` 集合；钉选 / 取消钉选；手选 wire style；四维 `price_override`；显示名覆盖；主模型与检索门模型指派；模型行上的「测试真实调用」。
- **`ModelSettingsStore`**：`schema_version` + `revision` + 钉选记录 + `assignments`，由 `RuntimeHost` 独占，一切写入过 MutationGate，落盘按 ADR-0022 的顺序。
- **`ModelClientFactory`** 与按不可变配置版本管理的传输池；Run 准入时一次性捕获 `assignments` + style + 凭据快照。

### 红线（会被评审直接打回的做法）

- 用连接四态去 gate 指派（ADR-0021）。`assignable = pinned && (supported || (unknown && style ∈ {anthropic, openai}))`。
- 用户手选 style 后把支持三态改成 `supported`（ADR-0021）。
- 目录拉取推动连接四态——**哪怕请求与 `auth_probe` 逐字相同**。
- 打开 Models 就并发拉取全部内置端点的目录（只拉**当前指派端点**，其余懒加载；无密钥不发请求）。
- 拿 `{base_url}/models` 的 200 冒充凭据有效；没有 `auth_probe` 声明就如实显示「无免费认证探针」。
- 目录失败时改动指派，或自动换模型。
- 先发布内存快照再落盘。

### 验收

- **矛盾组合的渲染用例**：端点 `已连接` + 模型 `unsupported` → 两处各说各的，禁用原因码指向支持维；端点 `未配置` + 模型 `supported` → **仍可指派**，发送时失败为 `endpoint_unconfigured`。
- 断掉 `catalog_url`：目录健康转 `unavailable`（带 `last_success_at` + 原因 + 重试时刻），Models 仍列 pinned + builtin，**聊天照常可用**。
- 移除密钥：`未配置`，两个探针各自禁用或明示无探针，且**不发任何请求**。
- `settings_conflict` 两个分因（`stale_revision` / `external_change`）各一个测试；启动期 `settings_invalid` / `settings_schema_newer` 各一个测试，断言**原文件一字节未改**。
- 四维 `price_override`：缺省（沿链下查）与显式 `0`（声明免费）的行为差异有测试。
- `unknown` + `user_declared` 的模型，界面上不得出现「支持」字样。
- 依赖可注入：`ModelClientFactory` 离线工厂返回 `ScriptedModel`，同一份页面逻辑能被它驱动。


---

## 2026-09-06 用户批准的 Issue #14 实施边界修订

六端点生产形状错误白名单为空，启用条目为 0；生产失败不触发支持态翻转。测试专用规则只验收覆盖、Run 证据读取、租约与异常收尾机制，不代表供应商识别验收，绝不进入生产配置。本次明确调整原供应商翻转正例要求，不将缺失证据标为通过。

覆盖键为 `(endpoint_id, model_id, wire_style)`；机制允许 supported 或 unknown 被有效证据覆盖为 unsupported/probe_evidence，不改内置表或指派；无成功调用升级。重启清空内存覆盖，已持久化 trace 保留；空覆盖不意味从未翻转或已恢复。发布中断、flush 失败不能推定所有 Sink 未落盘；既有 /api/run-evidence 仅增加本 Run 的 support_overrides，不抬高 trace 完整性。

OpenCode messages 使用 x-api-key、chat/completions 使用 Bearer，仅单套头，不自动切换。依据官方源码 337fd144d2ba144743368f78d9579a99cce175bd，未使用真实凭据实测，不保证线上部署版本一致。auth_probe 六行均未声明。完整结构化错误观测通道及通用规则框架暂缓，待首条真实规则获批。

本地实施文档：docs/implementation/issue-14.md、docs/adr/0030-shape-evidence-does-not-change-assignment.md（当前尚未提交，不构成验收证据）。

## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/34#issuecomment-5574405789

## 完成说明

本票由 **PR #44**（https://github.com/nineofoursyrup/Agent-Alfred/pull/44 ）实现并已合并。

### 实现范围

Models 页（模型行状态、「测试真实调用」`purpose: inference_probe`、组头 `last_success_at`/`last_error`/`retry_at`）；
Connections 页（按端点连接行、凭据掩码、「验证凭据」按钮）；模型设置存储与命令面（钉选/取消钉选/指派为检索门/价格覆盖，
区分「缺省」与「显式 0」）；`.env` 重读；目录四态 `unfetched/fresh/stale/unavailable` 与「刷新目录」；
以及**已声明 auth_probe 执行链**：点击 → 浏览器 POST `/api/connections/probe`（body 仅 `{endpoint_id}`，走既有 CSRF 写守卫）
→ handler → `DashboardApi.probe_auth`（`try_begin_mutation`/`finally end_mutation`，覆盖 `KeyboardInterrupt`）
→ Host 解析可信声明、读端点既有凭据、可注入传输 → `pool.store_observation(..., checked_via=auth_probe)`。
目标、请求头、请求体与密钥一律不来自客户端；目录取数不写连接观测。

### 合并对象

| 项 | 值 |
| --- | --- |
| merge commit | `55418dad266eed4df8a3e40670a069e3dcddc65d` |
| 第一父（固定基线） | `d3c7187c02ab6debead4fda2ce5641fb0446ba1c` |
| 第二父（已验收 HEAD） | `63cf7c14fe2efb6de0e1fd85d224096db3b0113b` |
| 合并结果 tree | `90bef9f46fbc5887afcbd2b657d40fa0a0bcc188` |
| 37 路径内容摘要 | `b0fbb13b58de89695181347999435bd1f732b497a0d10ec11d6fe868c17293ad` |
| 范围 | 37 文件 = 16 新增 + 21 修改 + 0 删除，全部 `100644` |

摘要算法：按 path 排序，拼接 `path + NUL + file-sha256-hex + LF`，再对整串取 SHA256；**mode 单独核验，不包含在摘要内**。
合并结果 tree 与已验收候选 tree 相同，`main` 同时包含该合并提交与已验收 HEAD。

### 合并后 CI

https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34147621763 — **成功**。
run `34147621763`，attempt 1，`event=push`，`head_branch=main`，`head_sha=55418dad266eed4df8a3e40670a069e3dcddc65d`；
检查 `lint / skills / env / tests` completed/success（该 run 日志：ruff 通过、skills/env PASS、
`2684 passed, 1 deselected`、Playwright `57 passed`）。

### 本地 exact HEAD 门禁（`63cf7c14…`）

Python 测试 **2684 通过 / 1 项 `requires_key` 排除**；Playwright **57 通过**；
Ruff、skills 检查、`.env.example` 一致性检查、Dashboard typecheck、
working/cached/累积三类 `git diff --check`、wheel + sdist 构建与资产检查、隔离 wheel 加载检查——**全部通过**。

### 评审

Standards 与 Spec 两轴对**同一 exact HEAD 与同一摘要**分别给出**整候选 PASS**。
37 路径的覆盖方式如实说明：**并非**在最后一轮重新逐行通读全部 37 个文件——两轴各自独立重算了逐条内容哈希与整体摘要，
确认字节与此前受审对象完全相同，据此以**内容等价／契约等价**承接先前审查，并在其上新审了本轮真正变化的部分。
这是仓库内的评审记录，**不冒称 GitHub 上的人工批准**（PR #44 的 reviews 计数为 0）。

### 已批准裁决的落地

三项均已实现：① `auth_scheme` 必填且仅 `bearer` / `x-api-key`，无默认、不轮换，静态头不得覆盖认证头或嵌入密钥；
② 探针目标必须与 `base_url` 同源且为 HTTPS，相对 path 解析后仍复验最终目标，`//host`、跨源、前缀相似主机一律拒绝，
**不跟随重定向**（含同源），3xx 不计为成功；③ 外部 POST 使用零长度请求体且不主动附 `Content-Type`。

**生产六端点仍全部未声明 `auth_probe`**，页面继续显示「无免费认证探针」，本次未联系任何真实供应商。
**空 POST 体不证明目标端点无副作用**；真实启用探针仍须另行提供「需认证且无副作用」的证据。

### 保留项与验证限制（未修复，随候选合入）

10 项非阻断建议与全部验证限制**完整保留在 PR #44 正文**，本次收尾**不冒称任何一项已修复**。其中需要特别点明：

- **`_UrllibCatalogTransport.get` 的 `HTTPError` 分支仍未显式 `close()`**。
  「未显式 close」已证明；「持续泄漏未复现」（CPython 3.14.7 下 60 次失败请求文件描述符 6 → 6，零增长）
  **不等于**无泄漏保证——换非引用计数运行期、或该异常被长期持有，关闭都会推迟。
- **该生产目录传输路径缺 pytest 覆盖**：测试注入假传输，`_UrllibCatalogTransport` 从不被测试执行，
  因此 CI 的 2684 通过**对它不构成任何验证**。
- **`DashboardFacade` 接口漂移仍在**：Protocol 为 16 声明，实际调用 21 处（`probe_auth` 等未列入协议）。
- 其余保留项：409/503 映射三处重复；`RuntimeHost._endpoint_rows` 读工厂私有属性；
  `CatalogScheduler._generation`/`bump()` 只写不读；`ModelSettingsStore` 接受 `clock=` 后即 `del clock`；
  `test_probe_requires_all_five_fields_…` 名称写 five 实为六字段；静态头嵌密钥的拒绝无专项测试；
  chat 的 `settings_invalid` 映射为 `admission_failed`。

**未做**：真实供应商目录／模型／认证探针实测；keyed 测试实跑；非 CPython 运行期实测。

以上范围内本票实现已完成，故以 completed 收尾；上述保留项与限制继续以 PR #44 正文为准。
