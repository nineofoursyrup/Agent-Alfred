# #20 实现：可选集成注册表与 Tavily web_search

来源：https://github.com/nineofoursyrup/Agent-Alfred/issues/20

## Problem Statement

用户需要在与 Alfred 对话时获取可追溯的网页搜索结果，并能看清外部服务是否配好、是否连接、是否被授权，以及实际消耗了多少搜索 credits。当前已有工具与账目基础，但尚无 Tavily 真实接入，不能把密钥存在、HTTP 成功、工具已启动或缺少费用报告混成“搜索成功且免费”。

## Solution

交付可选集成注册表及首个真实样例 Tavily `web_search`：用户在 Connections 查看配置和手动测试连接，在 Tools 管理既有外部授权，通过 MainBar 请求搜索，在 Ops 核对服务报告的 credits。默认未配置；缺配置时模型获得诚实的配置引导。错误、限流、旧响应、重载失败与结果未知均有明确解释和恢复边界。

本 spec 为 **r3 发布定稿**，是已获整体确认的 r2 按 `to-spec` 模板进行的等义整理。Q1–Q12 和 CE-01～CE-12 的合同保持不变，未开始实施或验收。

## User Stories

1. As a 私人助手用户, I want 直接在 MainBar 请求网页搜索, so that 在同一段对话中得到有来源的外部信息。
2. As a 私人助手用户, I want 只在自己配置 Tavily 后使用真实搜索, so that 保留是否接入外部服务的选择。
3. As a 尚未配置服务的用户, I want 得到准确的配置引导, so that 理解助手为何不能搜索并知道下一步。
4. As a 用户, I want 分别看到配置状态、连接观测和授权状态, so that 不把配好了误读为已经允许调用。
5. As a 用户, I want 在 Tools 显式允许或拒绝搜索工具, so that 控制外部行动权限。
6. As a 用户, I want 拒绝授权后连直接工具调用也被拒绝, so that 避免绕过模型可见性限制。
7. As a 用户, I want 在 Connections 主动测试连接, so that 在自己需要时获得真实服务观测。
8. As a 用户, I want 打开页面或切换标签时不自动探活, so that 避免无意消耗限频额度。
9. As a 多标签页用户, I want 多个页面共享探活缓存和请求额度, so that 避免重复点击突破限频。
10. As a 用户, I want 看到观测时间和下次可重试时刻, so that 分辨历史连接证据与刚完成的测试。
11. As a 用户, I want 服务限流后不被系统自动重试, so that 掌握后续请求和可能的费用。
12. As a 用户, I want 看到服务实际报告的用量字段, so that 不被虚构余额或搜索次数误导。
13. As a 用户, I want 更换或移除密钥后获得一致的新配置, so that 让下一次请求按已生效配置执行。
14. As a 用户, I want 重读配置失败时保留旧有效配置或明确暂停, so that 不在混合配置下继续调用。
15. As a 多标签页用户, I want 旧页面响应不能覆盖新配置或新错误, so that 始终理解当前可确认的状态。
16. As a 用户, I want 重启后连接状态重新表示尚未测试, so that 不把上次进程的成功当成此刻仍连通。
17. As a 用户, I want 所有页面与记录只显示安全的密钥遮罩, so that 避免凭据通过回执或错误内容泄漏。
18. As a 用户, I want 短密钥也被完整遮罩, so that 避免末四位显示整个秘密。
19. As a 用户, I want 搜索返回标题、链接和相关片段, so that 能自行核对来源并形成判断。
20. As a 用户, I want 有效空结果被明确说明, so that 知道本次没有找到结果而不是服务崩溃。
21. As a 用户, I want 损坏或过大的响应被明确报告, so that 不把部分或不可信结果误当完整成功。
22. As a 用户, I want 外部来源内容被安全呈现, so that 不因结果中的 HTML 或链接触发额外动作。
23. As a 用户, I want 搜索执行受时间与容量限制, so that 避免请求和响应无界占用资源。
24. As a 用户, I want 超时后的所有权与资源状态被如实处理, so that 不在旧请求仍运行时启动另一笔请求。
25. As a 用户, I want 结果无法确认时停止本次后续调用并解释原因, so that 避免重复搜索或基于未知结果继续行动。
26. As a 用户, I want 仍可用新的明确请求再次搜索相同内容, so that 让幂等保护不妨碍自己的新意图。
27. As a 用户, I want 同一调用身份回读不产生第二笔搜索, so that 避免重复执行和重复计量。
28. As a 用户, I want 在 Ops 单独看到 Tavily credits, so that 不把工具计量与模型金额错误相加。
29. As a 用户, I want 区分零消耗、未发送、费用未知和历史未记录, so that 准确理解费用证据的覆盖范围。
30. As a 用户, I want 搜索成功但费用未知时仍能使用结果, so that 不因缺失计量报告丢弃有效来源。
31. As a 用户, I want 详细 trace 被裁剪后仍能核对已记录 credits, so that 保留费用事实而不长期保存完整正文。
32. As a 用户, I want 计量写入失败时系统停止扩大未记录范围, so that 避免后续动作失去账目依据。
33. As a 集成维护者, I want 用已有 Host 整链执行离线验收, so that 证明实际授权、执行、收尾和计量行为一致。
34. As a 集成维护者, I want 每个关键反例都有稳定编号和可定位证据, so that 在独立实施会话中准确复现验收边界。
35. As a 用户, I want 真实服务验证与离线测试分别报告, so that 不把替身测试通过误认作 Tavily 已验收。
36. As a 用户, I want 探活计费未核实时不被承诺免费, so that 在显式授权真实验证前了解证据限制。

## Implementation Decisions

以下决定均 confirmed；内部模块与接口组织可按现有惯例选择，不能改变可观察合同。注册表、凭据/观测服务、Tools 授权、Registry、Loop、计量和 Dashboard 使用现有归属，不另建执行内核。

### 既有约束

1. 可选集成注册表与 ToolRegistry 分工：前者拥有配置/依赖/健康观测，后者拥有工具声明、Schema 暴露与执行。注册表不保存授权或独立 enabled 开关。
2. 静态五项：字段、是否密钥、依赖 extra、健康检查、字段级重载范围；配置完整性由字段声明推导。Tavily extra=None，采用标准库 urllib。
3. 密钥只从环境/.env 读取；未配置本地短路，任何 API、日志、trace 不返回完整密钥。
4. 授权矩阵：未配置且 unset/allowed 为引导 Schema；denied 隐藏；已配置且 unset 隐藏、allowed 为真实能力。未配置调用返回 configuration_required 并回喂模型。
5. 工具计量和工具账分工沿用现有实现；credits 与模型金额分列，不能凭搜索深度猜实付。工具账不是按参数去重锁。
6. 改 key 值热更新；配置完整性或授权导致 Schema 变化时重建 Agent 能力视图；不要求重启 Gateway。mutation 忙时 409，不排队。

### Q1 交付范围

Connections 展示可选集成的密钥状态/四态/测试连接，Tools 复用授权，Ops 展示 Tavily credits；生产只注册 Tavily，不迁移模型端点、不接 MCP/OTel。注册表的有 extra 形态用离线夹具验证，不伪装生产集成。

### Q2 搜索合同

模型 Schema 仅 query、max_results（1–10，默认 5）；固定 basic/general，include_usage=true，关闭 answer/raw_content/images/auto_parameters。输出来源 title/url/content 与可用 score/request_id，保留供应商顺序，不额外抓链接。空 results 是成功空结果；必需结构损坏返回协议错误，可选字段缺失不杜撰。工具层沿用统一 model/audit 双投影；响应资源上限及缺字段策略由 Q7/Q8 精确定义。

### Q3 探活策略

仅显式测试发 GET /usage，打开/切页/焦点/重连仅读缓存。共享成功 60s、失败 30s 缓存之外，另加宿主 Tavily 探活滑动窗口上限 10次/600s；换 key 不清请求额度。忙时 409、无后台队列/自动重试；429 尊重 Retry-After 并明确下次可试时刻。缓存过期保留带时间的历史观测，不伪装刚测试过。本地限流不改既有连接观测。

### Q4 四态与错误

只有当前凭据的有效 /usage 或真实 search 成功才生成 connected。401、search 429/432/433、网络/5xx 为 error 并给安全原因码；400/422 是调用参数/协议问题，不证明认证成功或失败，不覆盖既有连接结论。usage 429 沿上游研究呈 configured_untested + 探活限流说明，历史成功可作为过去观测单独解释。异常 body 对象/数组/纯文本/坏 JSON 均不得泄漏原始秘密或导致错误处理崩溃。余额仅展示已报告字段，不能凭成功 HTTP 填零。

### Q5 配置生效

复用显式 .env 重读，进程环境优先；磁盘编辑本身不等于生效。无变化不清 Tavily 观测；有效 key 变化才换代并清观测，下一次调用读新快照。重读失败保留旧有效状态并可重试。Run/探活期间重读被 gate 拒绝；过期 HTTP/UI 回包不能覆盖新配置/新观测。授权继续走既有持久配置和保存/生效协议。

### Q6 执行与证据

一次已获准工具调用至多发一个 search POST，无客户端自动重试；模型新 call 可再查且单独记账。已发出但失去结果时保留 unknown，不能记未执行/免费，并停止本 Run 后续工具和模型，避免模型自动另开 call 重试；下一次明确用户请求可重新搜索。业务成功但仅成本 unknown 不停止。迟到回包不能补写成第二次执行或恢复自动调用。离线验证同一真实业务链，替换模型及外部 HTTP 边界；真实 Tavily 验收按 Q12 另取显式授权；真实模型演示不属于本票必需门禁，不将离线成功称为 live 成功。探活免费未证实保持证据限制，不以实际计量=0 掩盖未知。

### Q7 搜索输入与响应容错

合同：
- query 为 1–2000 个 Unicode code point 的字符串，拒绝纯空白与额外参数；有效输入保留原文本，不静默裁短、改写查询或把 bool 当整数。max_results 严格为整数 1–10，省略才取 5。校验在 HTTP 之前完成。
- 200 的顶层必须是 JSON object，results 必须是数组；每条必须有非空 title/url、字符串 content（允许空片段，不能填造文字）。url 只接受无用户凭据、无控制字符且有主机的 HTTP(S) URL；无法作为安全来源链接的一条视为协议错误，不静默丢弃。数组超出请求 max_results 视为协议错误，不悄悄截短。
- 任一必需字段损坏即整次返回明确协议问题，不把部分来源包装成完整成功。有效 results=[] 才是成功空结果。可选 score 仅保留 [0,1] 有限数（不含 bool），request_id 仅保留非空字符串；缺失或无效则省略，不让它们否定有效来源。供应商未请求的 answer/raw_content/images 和未知字段不进入正文投影。
- credits 接受 JSON 原生非负有限数，解码即使用 Decimal；bool、字符串、负数、非有限数均 unknown。合法 fractional credit 保留精度，不先转 float。service=tavily，unit=credits，source=reported 必须与声明一致。不凭 search_depth 猜成本。
- 搜索片段在模型工具结果中明确标为外部来源数据；UI 以安全文本呈现、链接仅由用户点击打开，不执行其中 HTML，也不替用户访问来源。完整 audit 指完整“选定字段的脱敏工具结果”，不是原始 HTTP body；model 沿现有截断及显式标记，不另建摘要模型。


### Q8 时间、响应容量与关闭

合同：
- search 的服务 IO deadline 默认 10s，与剩余 ToolContext.deadline 取较早值；usage 为 5s。使用单调时钟，进入每个可控阻塞阶段前检查剩余量、设置不超过剩余量的 IO timeout，分块读取时持续检查。预算已耗尽即不再发请求。
- 这是协作式 deadline，不承诺 urllib 对 DNS 等系统不可中断阶段具备严格墙钟上限。IO 未退出时保持所有权与 mutation gate；不靠遗留后台线程返回“已取消”后让下一次搜索并行发出。普通超时/控制中断都关闭已取得的响应/HTTPError/socket；宿主 shutdown 沿既有生命周期如实报告未完成，不另建强杀机制。
- 响应体（含 HTTP 错误体）硬上限：search 1 MiB、usage 64 KiB，恰好上限允许，多一字节拒绝；请求 identity encoding，未支持的 content encoding 直接失败。无 Content-Length 或声明虚假也按实际读取字节限额。不会为显示错误而无界 drain body。
- 200 未形成完整有效结果时不冒充成功；已发出的业务结果不能确认则按 Q6 停止。资源超限/截断未读完不能假装获取完整 usage；若独立可信计量已经取得，保留该事实而非强制改 unknown。
- 网络沿 urllib 的默认环境/系统代理选择规则；不支持或无效代理配置明确失败，不自动绕过直连。使用实例级 opener，不修改进程全局 opener；离线 HTTP 测试显式隔离代理并单独验证代理错误分支，不能修改用户环境来让测试通过。
- 不增加用户可选的 hard-timeout/强杀选项；若用户需要强制总时限，须另行选择可中止隔离机制，这会扩大本票。

依据：Python urllib 文档 https://docs.python.org/3.14/library/urllib.request.html 将 timeout 定义为阻塞操作的等待上限，并非整次调用硬时限；ADR-0006 已定预算不等于可抢占取消。

### Q9 完整连接观测与调用结局表

健康观测是最近一次有效尝试的结论，不是是否允许模型执行的第三个授权开关。configured+allowed 即使尚未测试或存在错误，仍保留真实能力；客户端主动冷却拒绝可以阻止发出，但不能改写授权。Connections 与 Tools 使用同一份观测；缓存 TTL 只决定能否再测试，不给旧观测刷新 checked_at。

| 输入/结果 | 当前连接观测 | search 业务结局与是否继续本 Run |
|---|---|---|
| 本地无 key | unconfigured，无网络观测时间 | configuration_required，不发 HTTP，可回喂 |
| 有 key，首次/换代/重启 | configured_untested | 不要求先探活才能搜索 |
| 本地输入错误/忙/缓存命中/本地冷却 | 不覆盖旧网络观测 | 本地拒绝或读缓存，零 HTTP；不伪造真实执行 |
| search 200 且来源合同有效 | connected，checked_via=real_run | succeeded；仅成本 unknown 仍可继续 |
| usage 200 且顶层 object 至少有 key 或 account 的 object | connected，checked_via=auth_probe | 非搜索，不产生搜索结果/搜索 credits |
| 任意 200 坏 JSON/无必需结构/资源超限 | error，protocol_error/response_too_large | search 结果 unknown + 停止；usage 只报告探活失败 |
| 400/422 | 保留原连接观测，另显本次 request_invalid | search failed + invalid_input，回喂后可继续 |
| 401/403 | error，authentication_failed/forbidden | search failed + unavailable，可回喂 |
| search 429 | error，rate_limited + retry_at | failed + unavailable；到期前同当前 key 的新调用本地拒绝，零 HTTP，不自动调度 |
| usage 429 | configured_untested，probe_rate_limited + retry_at | 不自动调度；历史成功只显示为过去证据 |
| 432/433 | error，plan_limit/paygo_limit | search failed + unavailable，费用无报告为 unknown |
| 其他 HTTP 4xx/5xx 或被拒绝的 3xx | error，安全 http_status/redirect_refused | search failed + execution_error；成本独立按证据 |
| 有可信证据的发送前网络失败 | error，安全网络原因 | failed/未发送，成本不计费，见 Q11 |
| 已发出或不能排除发出后丢失结果 | error，network_error/timeout | unknown + tool_result_unverified，停止本 Run |

补充合同：
- usage 的余额数据只是当前响应报告的只读快照。plan_usage/plan_limit 只有合法非负有限值才显示；字段缺失明确未报告，不合并 PAYGO 与套餐、不计算“可继续搜索次数”、不把已连接读成有可用额度。
- 探活计数在准备发请求时消耗，失败也占额；拒绝、缓存不占额。滑动窗口用单调时钟，恰好满 600s 的旧记录过期；进程重启窗口清空，不承诺跨进程或跨其他客户端控制 Tavily 配额。换 key 不清窗口。
- Retry-After 解析合法非负秒值或 HTTP-date，取与本地窗口/缓存约束中最晚可发时刻；无效/缺失时 usage 默认冷却 600s、search 60s。不等待后自动发请求，也不提供绕过限流的强制刷新。
- 缓存只提供同凭据、同观测代次的结果。真实搜索的新观测要使较旧探活缓存失效，防止“刚 search 401，又读探活缓存变 connected”。未变化 key 不清缓存；配置变化同时清该 key 的服务观测与冷却，宿主探活窗口不清。


### Q10 配置发布失败与注册表边界

合同：
- 环境覆盖按现有规则：启动环境优先，重读只加载启动时确定的 .env 文件；不重新寻找文件或改变进程环境。文件不存在/被删除视为空 overlay，可显式移除文件来源 key；权限/IO/解码/语法错误则整次重读失败，保留旧有效快照，不能吞成“配置已删除”。
- 只以空/纯空白判未配置；非空 key 保留原值，含 CR/LF/NUL 或首尾空白判配置错误，禁止发送，不隐式 trim 修好。错误配置不伪装未配置或已连接；授权保留，Tools 解释配置无效、能力不可执行。
- 重读先准备有效的新配置、脱敏输入和能力视图，全部可应用才发布；任何准备/应用失败都不能对外展示新 key 已生效。若无法恢复一致状态，暂停 external 能力并给明确修复/重读指引，不能继续旧 allowed 配新 Schema 的混合状态。凭据变化不修改持久授权文件。
- 在发送前将本轮新旧凭据加入共享 Redactor（短凭据走 credential=True），避免旧在途内容与历史读取漏脱敏。只返回 configured/last4/masked 与安全状态，不返回密钥摘要或可复原 identity。少于 8 字符全遮罩。
- 所有 Tavily 3xx 均不跟随，HTTP 只走固定 Tavily HTTPS endpoint；不开 URL override 用户设置。测试 URL 注入属于外部边界替身，不进入生产配置。
- 注册表身份稳定，重复 integration/field/tool 能力身份是开发配置错误，装配失败而非择一覆盖。额外依赖缺失的夹具报告安全 dependency_missing，其他集成/本地工具仍可用；Tavily extra=None 无此依赖门槛。缺依赖不能冒充 key 缺失，不能触发偷偷安装。具体模块命名与工厂形状由实施选择。
- schema 暴露与执行检查必须在同一生效版本；已定授权保存/重新应用故障策略继续生效。配置无效/缺依赖时隐藏真实模型能力，直接调用返回 unavailable，不发送 HTTP；未配置引导仅用于可解释的缺字段场景。


### Q11 工具启动、HTTP 发送与结果未知的计量

合同：
- 保留既有“进入工具 callable”的启动口径；不把预先登记 intent 或 ToolStarted 事件当成 HTTP 已发出。只在受信 transport 能证明未发送时记录搜索未发送/不计费；callable 已进入仍显示工具已启动。没有证据时保守 unknown。
- 未进入工具即预算耗尽沿既有 not_executed；进入后、请求发出前预算耗尽须零 HTTP，并保存真实启动事实与受信未发送证据。不得用外部工具的 cost=None 冒充已确认不计费，因为当前投影会把它变 unknown。实现可补齐最小通用结果表达，不开新一套账。
- 丢失业务结果时统一 external ledger state=unknown、metering result=unknown、可信成本或 unknown；stop_reason=tool_result_unverified，Run outcome=failed，recording_state 独立。控制异常保留 interrupted 语义与原异常传播。
- MainBar/CLI 固定收尾明确说明“搜索结果无法确认，可能已执行；本次后续调用已停止，未自动重试”，附可用 operation_id；不得再调用模型编写这段解释。同批剩余工具标明确未启动，已生成 model_content 的 delivery=not_sent，不能声称已回喂。
- 只有成本 unknown 的成功结果仍是 succeeded，可回喂；有效 HTTP 错误响应是已知 failed，成本未知独立，不强行变业务 unknown。恢复读取不重新发请求，不由用户同参数新调用覆盖旧账。
- 同调用身份重放 receipt 的验证在真实 ledger/registry seam 完成；整 Run 重复身份若被上游协议拒绝，不要求穿透拒绝再次执行，用两层证据证明至多一次。



## Testing Decisions

优先使用一个最高业务入口：`build_default_host` + `ScriptedModelFactory`，通过 `host.submit(SubmitRequest)` / `host.wait` 驱动实际业务链。现有公共读口 `host.tools_catalog`、`host.tool_requests`、`host.read_tool_history` 与 Dashboard/Ops API 提供观测。直接 Registry/ledger 测试只补足上层会提前拒绝的同身份重放；真实回环 HTTP 与双浏览器标签补足模型替身无法证明的传输、资源和 UI 顺序。

好测试从本合同独立推导期望，只断言外部行为与持久事实，不镜像实现；不为省事替换整个 Registry、配置发布者或收尾状态机。依照既有授权矩阵测试、工具收尾/恢复测试、ScriptedAuthTransport/FakeClock、真实 HTTPServer 重定向测试和浏览器 settings/tools 测试惯例扩展。

### Q12 验收交付与 live 证据限制


合同：
- 必需离线验收：每个确认 CE 给同一固定候选的测试证据；真实 Host/Registry/配置/SQLite/计量/HTTP 编解码/浏览器，替换 ScriptedModel 和远端 HTTP，IO 故障与时序可控。另按项目适用 Ruff、全离线 Python、skills/env 校验、Web 类型、浏览器、wheel+sdist 隔离安装执行；不得运行 requires_key 默认测试或偷偷调用真实模型。
- 真实服务验收单列：实施完成后，另取授权执行 1 次 /usage 和 1 次 basic search；搜索优先由 ScriptedModel 驱动真实 Host + Tavily，核对来源与服务报告 credits，使 Tavily 验收不依赖付费模型。真实模型是否会选用工具不是本票 mandatory live gate，离线 ScriptedModel 已覆盖选择后的回喂链。
- live 没有授权/凭据时交付“离线验收通过、真实 Tavily 未验收”，不称整票服务接入已验收、不关闭 #20。真实失败不自动循环到通过；另记真实请求数、结果与计量。
- /usage 免费未获官方明确证据：删除“免费测试”承诺，UI 称“测试连接（查询用量）”，帮助文案注明计费未核实；这是明确收窄承诺，不阻止交付有真实观测的探活。探活不是模型工具调用，不造 Run/搜索 credits 或塞入 Ops 工具账；本票不新增所有控制面 HTTP 请求的费用账。若需免费保证，须新证据后再作结论。
- 本次 `to-spec` 请求已授权发布本 spec 并添加 ready-for-agent；真实服务调用、代码提交/推送、关闭或发布仍需各自授权。交接保留所有尚未运行的实施验收项目及依赖条件。

### 验收矩阵

| 合同 | 必须行使的公共入口与真实组件 | 可替换边界 | 必须观察的证据 |
|---|---|---|---|
| CE-01/05/06/07/10 | build_default_host + ScriptedModelFactory；host.submit(SubmitRequest)/wait；真实 Registry/Loop/授权 store/临时 SQLite 双账 | 模型、Tavily HTTP、时钟与指定 IO 失败点 | Schema、模型输入及是否继续、HTTP 次数、host.tools_catalog/tool_requests/read_tool_history、Run outcome/recording_state、Ops 计量与 not_sent |
| CE-02/03/11 | 真实 Connections API、MutationGate、凭据与观测服务、临时 .env、双真实浏览器标签 | 外部 HTTP、响应交付顺序、文件读取/应用故障点、时钟 | 实际请求计数、409、旧回包不能覆盖当前证据、重读失败的有效配置、授权文件不受凭据变化修改 |
| CE-04/08/09 | 真实 urllib 编解码/关闭/投影/Redactor、回环 HTTP fixture、真实页面渲染；上层 Host 整链 | 远端地址与响应、分块/断流/重定向目标、合成 key | 原始 HTTP code、必需结构校验、各输出面无完整秘密、零重定向、大小与资源释放、正文无执行 |
| CE-12 | 真实注册装配/目录/执行检查 | 独立测试 extra 存在性夹具 | 稳定身份校验、错误原因不混用、无偷偷安装、生产只增加 Tavily |

测试约束：
- 假时钟/屏障用于决定性的时序控制；真实 HTTP 测试证明标准库边界，不能仅测替身。高层测试不得替换 Registry、配置发布者或计量状态机后声称覆盖它们。
- 期望值从本文合同推导，不能把当前实现输出存成快照作为正确性定义。
- 为每个 CE 记录测试路径/用例、命令、候选 commit 与含未提交文件的内容清单、结果及证据位置。一个测试可覆盖多个 CE，但每个 CE 均须有可定位的公共路径证据。
- 离线四道门禁、相关浏览器与类型检查以及隔离安装见 Q12；设计阶段仅执行文档结构/一致性检查，不报告实现测试已通过。
- 真实服务验收需要授权与凭据，严格按 Q12 单列。默认模型与 Tavily 均不真实联网；这里的“零真实调用”不包括资料浏览或 GitHub 只读核查。

### Critical counterexamples

全部 case 的行为、Verification seam 及整体 r2 均已获用户确认（最后确认原文：“[$to-spec] 确认”）。各 case 引用上述具体条款，不能只按标题测试。没有 deferred case；这些是待实现合同，不是已观察到的 Tavily 缺陷，也不是测试 PASS。

#### CE-01 缺配置与授权不能混成一个开关
- Basis: #20 评论、ADR-0005；Q1/Q10。
- Sequence: 无 key，分别 unset/allowed/denied；再填 key，遍历三种授权；尝试模型可见与不可见工具请求。
- Expected behavior: 完整六格矩阵；引导返回 configuration_required 并被下一 Step 读到；拒绝/引导零 HTTP；不能因为测试连接成功而自动授权。
- Verification: RuntimeHost.submit + ScriptedModel + 真实 ToolRegistry/临时 SQLite/授权存储；用 host.tools_catalog/tool_requests/read_tool_history 观测；观测模型收到的 Schema、工具结果、HTTP 调用计数、Ops 计量。仅替换模型和外部 HTTP。
- Decision status: confirmed（Q1/Q10，对应轮次用户“全按建议”）；r2 已整体确认，r3 等义发布。

#### CE-02 多标签与失败缓存突破探活配额
- Basis: 官方 rate-limits、#10、ADR-0016；Q3/Q9。
- Sequence: 两标签同时点击；失败后每 30s 重试直到第 11 次；间插切页/重连/换 key；服务端返回 429 + Retry-After。
- Expected behavior: 忙请求 409 不排队；缓存命中不发 HTTP；任何宿主滑动 600s 内最多 10 次探活；冷却期不发请求，显示真实历史时间/可重试时刻，页面读取从不探活。
- Verification: 真实 Web API/MutationGate/集成服务，注入单调时钟与可阻塞 HTTP transport；双真实浏览器标签验证渲染及调用数量。不得 mock 掉限流服务。
- Decision status: confirmed（Q3/Q9/Q12，对应轮次用户“全按建议”）；r2 已整体确认，r3 等义发布。

#### CE-03 旧密钥的成功回包覆盖新配置
- Basis: 连接观测必须对应当前凭据及最新观测代次、字段级重载；Q5/Q9/Q10。
- Sequence: key A 请求发出后尝试重读 B（应忙拒绝）；A 完成，再成功重读 B；把旧页面 A 的响应最后交付。另测删除 key、相同值重读、进程重启；同 key 探活成功缓存后 search 返回 401，再交付旧探活缓存/旧页面回包。
- Expected behavior: B 回到 configured_untested，删除为 unconfigured，相同值不冒充变化；旧观测不能覆盖 B；新请求只带 B；重启不恢复旧 connected；同 key 的最新 search 错误不能被旧 probe 成功覆盖。没有凭据明文进入版本身份或 DTO。
- Verification: 真实 host/API/临时 .env/浏览器，阻塞 HTTP 与浏览器响应；固定事件屏障，不靠 sleep 猜时序。
- Decision status: confirmed（Q5/Q9/Q10，对应轮次用户“全按建议”）；r2 已整体确认，r3 等义发布。

#### CE-04 200、422 与配额错误不是同一类证据
- Basis: #20 真实四态及 #10 的 detail 双形状；Q4。
- Sequence: 首次调用或已有成功观测后分别收到 200 有效/200 损坏、400、422 数组、401 对象、429、432、433、5xx、非 JSON。
- Expected behavior: 严格按 Q9 完整状态表分类；坏结构不伪造成功；参数错误不冒充鉴权结果。已知 HTTP 失败在允许继续时安全回喂；业务 unknown 按 Q11 停止且标 not_sent，不再调用模型。usage 限频与 search 限频分别表达；HTTP code 与安全原因可解释，原始 detail/input 不回传。
- Verification: 真实 urllib transport + 回环 HTTP fixture 测协议解析；真实 registry/loop/Connections 投影验证结果。外部地址仅测试注入，生产固定 Tavily HTTPS。
- Decision status: confirmed（Q4/Q9/Q11，对应轮次用户“全按建议”）；r2 已整体确认，r3 等义发布。

#### CE-05 配额未知不等于零
- Basis: #20 ToolCost、ADR-0032；Q6。
- Sequence: 搜索返回有效 results，但 usage 缺失、credits=0、分数、负数、bool、非有限数或错误类型；再裁剪 trace 并重启读取 Ops。
- Expected behavior: 有效非负有限报告才进入 Decimal 计量；缺失/非法为 unknown，0 与未知分开。结果成功与费用未知可以并存；Tavily credits 单独归属，不与模型金额或其他服务相加，trace 裁剪不删计量。
- Verification: Host.submit -> 真实工具/计量/SQLite -> Ops 查询；外部响应替身，真实持久账与 trace 裁剪/重开读取。
- Decision status: confirmed（Q6/Q7/Q11，对应轮次用户“全按建议”）；r2 已整体确认，r3 等义发布。

#### CE-06 POST 已发出后超时、控制中断或计量失败
- Basis: ADR-0006/0032、external 工具账 unknown；Q6。
- Sequence: transport 确认收到 POST 后阻塞/断连接；或返回后计量落盘失败；在释放迟到结果前尝试后续工具/下一 Run。
- Expected behavior: 无自动重发 POST；失去业务结果如实 unknown，不伪造免费/未执行，工具账与计量结局一致，并通过明确 stop_reason 停止本 Run 后续执行；仅成本缺失不停止；计量失败沿既有合同停止后续并保留诊断；控制异常不吞为普通工具错误。资源最终关闭；迟到结果不追加第二笔结果。deadline、资源关闭和系统不可中断限制按 Q8；统一账目及固定回复按 Q11。
- Verification: 真实 registry/ledger/metering/host；HTTP 屏障、注入存储失败、进程控制测试。替换外部 IO，不替换收尾状态机。
- Decision status: confirmed（Q6/Q8/Q11，对应轮次用户“全按建议”）；r2 已整体确认，r3 等义发布。

#### CE-07 相同查询与新调用身份
- Basis: 工具账是证据不是锁，#32 唯一请求身份；Q6。
- Sequence: 同一 query 使用两个合法不同 call_id，再重放同一已接受调用身份。
- Expected behavior: 新调用各自执行、各自计量；重复身份沿既有协议拒绝/回读，不能重复计费；不引入按 query 去重或结果缓存来冒充真实搜索。
- Verification: ScriptedModel 驱动真实多 Step 执行及持久身份校验，观测 POST 数、请求数、credits。
- Decision status: confirmed（Q6/Q11，对应轮次用户“全按建议”）；r2 已整体确认，r3 等义发布。

#### CE-08 故障路径泄密
- Basis: #20 密钥绝不回传、中央 fail-closed Redactor；Q1/Q5。
- Sequence: 短 key、正常 key、轮换后的旧 key 出现在远端错误/detail/input、异常文本、搜索响应；另测 HTTP redirect。
- Expected behavior: 短 key 不显示完整末四位；API/SSE/CLI/log/trace/持久计量不含完整新旧 key；禁止把 Authorization 随跨源重定向发送。安全错误仍可解释，不能仅依赖前端遮罩。
- Verification: 真实 transport、投影与 Redactor；使用合成 sentinel 密钥扫描全部公开/持久输出；回环第二服务记录是否收到 Authorization。不读真实凭据。
- Decision status: confirmed（Q5/Q7/Q10，对应轮次用户“全按建议”）；r2 已整体确认，r3 等义发布。

#### CE-09 空结果、坏来源与超大响应
- Basis: 真实搜索结果与有界工具回喂；Q2/Q7/Q8/Q11。
- Sequence: 有效空 results；缺 URL/content；可选 score/request_id 缺失；超大/慢速响应；来源中含 HTML 或危险 URL。
- Expected behavior: 按 Q7/Q8 校验；空数组成功，任一必需字段损坏不得伪装无结果或完整成功；可选缺字段省略。超限或截断导致业务结果未知时按 Q11 停止，说明具体原因；成本保留可信已得事实，否则 unknown。不执行来源脚本/不自动抓链接。
- Verification: 真实 urllib/结果映射/两份投影与真实浏览器文本渲染；HTTP fixture 控制分块/大小；覆盖 query 与条数边界、title/url/content 缺失、score/request_id 异常、含危险链接的单条结果、容量 exact/+1。
- Decision status: confirmed（Q2/Q7/Q8/Q11，对应轮次用户“全按建议”）；r2 已整体确认，r3 等义发布。

#### CE-10 工具启动后、发送前预算耗尽
- Basis: ADR-0006、工具启动与发送事实分离；Q8/Q11。
- Sequence: 在 ledger 意图/事件准备耗完 deadline（尚未进入 fn）；以及进入 fn 后、transport 发出前耗完 deadline；第三组在发送后耗尽。
- Expected behavior: 前两组零 HTTP；第一组明确未启动，第二组保留真实工具启动与受信未发送/不计费；第三组不能推断没发送或免费，业务未知时停止。不存在超时后自动继续的遗留请求。
- Verification: 真实 Registry/ToolContext/两账，假单调时钟与传输屏障；Host 整链验证后续调用数及收尾文案；不得用单一 mock TimeoutError 覆盖三种边界。
- Decision status: confirmed（Q8/Q11，对应轮次用户“全按建议”）；r2 已整体确认，r3 等义发布。

#### CE-11 重读失败不能发布混合配置
- Basis: Q5 已确认失败保留旧有效配置、授权版本一致；Q10。
- Sequence: .env A→B，在读取、校验、能力视图准备分别失败；另测删除文件、进程环境覆盖、同值重读；成功重读后旧标签最后回包。
- Expected behavior: 可回滚失败保留 A 与其一致视图；删除只移除文件来源字段；进程环境仍优先；无法确认一致应用时暂停 external 并显示未生效，不用半新半旧配置；旧响应不能推翻当前状态。
- Verification: 真实文件/host/授权 store，注入精确失败点；直接请求与浏览器回读核对实际生效 key 的合成指纹（仅测试内部）及 HTTP 捕获，无明文公开输出。
- Decision status: confirmed（Q5/Q10，对应轮次用户“全按建议”）；r2 已整体确认，r3 等义发布。

#### CE-12 可选依赖与密钥缺失是不同原因
- Basis: 注册表静态五项、Tavily extra=None、Q1/Q10。
- Sequence: 注册测试集成需要不存在 extra 且 key 已配置；对照 Tavily 无 extra；再注入重复身份以及无效含换行 key。
- Expected behavior: 测试集成显示 dependency_missing 而非 unconfigured，零执行、无自动安装；Tavily 不受影响；重复声明装配失败；无效 key 不能发送或伪装成缺失，授权不改变。
- Verification: 真实注册装配/能力目录/registry，用独立夹具 extra 存在性边界；生产清单断言只有本票 Tavily 新增，拒绝假集成混入。
- Decision status: confirmed（Q1/Q10，对应轮次用户“全按建议”）；r2 已整体确认，r3 等义发布。

## Out of Scope

- 不实施其他生产集成：MCP、OTel、其他搜索服务；不迁移模型端点。
- 不引入 Tavily SDK 或 Tavily 专用 optional extra；不自动安装缺失依赖。
- 不开放高级搜索、供应商生成答案、正文全文、图片、自动参数、来源自动抓取或用户可编辑 API base URL。
- 不新增按 query 去重、搜索结果缓存、后台轮询、排队、自动重试、强制刷新绕过限流或强杀隔离机制。
- 不从历史连接观测推断当前可用，不承诺 `/usage` 免费，不推算余额可换多少次搜索。
- 不把探活伪造为模型工具 Run 或搜索 credits，不新增所有控制面请求的费用账；不换汇、不跨服务合并 credits、不把工具成本加到模型金额。
- 不把付费真实模型演示设为本票必需验收；不在本设计/发布会话实现功能、真实调用服务、提交、推送、合并或关闭 #20。

## Further Notes

- **确认记录**：Q1–Q6 第一轮“全按建议”；Q7–Q12 第二轮“全按建议”；r2 整体确认由用户调用 `to-spec` 并回复“确认”。r3 仅按模板等义整理并发布，不重新开启访谈。
- **冻结基线**：`796b88c7d1e134906e25cf42f259d20381017fc2`；发布前再次核实远端 main 未变化。实施者仍须在开工时检查最新 Git/工作树与依赖状态，并保留既有改动。
- **权威位置**：本 GitHub #20 正文为发布 spec；本地交接保留逐字相同的 r3 文件与 SHA-256。后续若修改合同，必须明确形成新修订，不把旧稿和新实现混验。
- **就绪含义**：ready-for-agent 表示设计与测试边界完整，可交独立实施代理；不代表代码完成、测试通过、真实服务可用或获准付费调用。
- **验收状态**：CE-01～CE-12 全部确认，设计 blocker=0，deferred=0。全部实施测试和 live Tavily 验收均未运行；缺少真实授权/凭据时按 Q12 保持“真实服务未验收”，不能整票验收或关闭。
- **上游依据**：原生依赖 [#10](https://github.com/nineofoursyrup/Agent-Alfred/issues/10)、[#19](https://github.com/nineofoursyrup/Agent-Alfred/issues/19) 均已关闭。复用地图 [#1](https://github.com/nineofoursyrup/Agent-Alfred/issues/1)、[#4](https://github.com/nineofoursyrup/Agent-Alfred/issues/4)、[#32](https://github.com/nineofoursyrup/Agent-Alfred/issues/32) 与 ADR-0005/0006/0016/0032；不重开已定授权和计量分工。
- **资料修正**：原票“五要素却列六项”的表述已按后续确认收敛为五项；启用条件可推导，reload_scope 在字段级。原研究中 `/usage` 免费仍是未证实推断，本稿按 Q12 撤去免费承诺；422/detail 数组为 2026-08-26 既有实测记录，本次未实发 Tavily 请求。
- **当前接口提醒**：生产注册尚无 Tavily；ToolCost 已使用 Decimal。当前 entered 表示进入工具 callable，不证明 HTTP 发出；timeout、成本 unknown 与停止信号尚不能自动保证两账一致。实施必须兑现 Q11，并逐 CE 提供真实证据。
- **术语与架构**：可选集成和可选集成注册表的定义已纳入本地 CONTEXT.md；沿用现有 ADR，不为容量/缓存默认值新建 ADR。交接须携带这份词汇表变更。
- **已复核来源**：[Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search)、[Usage API](https://docs.tavily.com/documentation/api-reference/endpoint/usage)、[Rate Limits](https://docs.tavily.com/documentation/rate-limits)、[Credits](https://docs.tavily.com/documentation/api-credits)、[Python urllib](https://docs.python.org/3.14/library/urllib.request.html)。复核日期 2026-09-13；服务合同后续漂移须如实报告，不静默改写验收。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/20#issuecomment-5425931387

📌 上游调研 #10 已关闭，三条**硬约束**直接落到本票，执行时不要重新发明：

1. **不用 `tavily-python` SDK**，裸 `urllib` 直连。决定性理由不是依赖体积（虽然它拖 13 个包含 2 个编译扩展），而是**红线本身**：四态的全部信息量在原始 HTTP 状态码里（含 Tavily 自定义的 432 / 433），SDK 会把它包成自己的异常类型，拿不到 `.code` 就只能猜——那就等于伪造状态。
2. **`未配置` 必须本地判定，不能靠打 API 推断**：实测「缺密钥」与「密钥错误」都返回 `401` 且**错误文案完全相同**。另外参数非法实测返回 **422（不是文档写的 400）**，且它的 `detail` 是**数组**，与 401/432/433 的 `detail` **对象**结构不兼容——不同时处理两种形状会在 422 上直接 `TypeError`。
3. **健康检查用 `GET /usage`**（免费，顺带返回真实余额），但限 **10 次 / 10 分钟**。Dashboard 有九页，只要两个页面各自轮询就打满配额，因此必须**缓存 + 以手动触发为主**。

另外本票的验收要跟着改：**`reload_scope` 挂在字段粒度，不是集成粒度**。Tavily 一个集成同时踩中「改 key 值 → 热更新」与「启用状态变更 → 重建 Agent」两档，集成粒度表达不了。

⚠️ 本票关闭前需自行确认的两点（调研票已标注为未坐实）：`/usage` 免费是**推断**而非官方明文；六要素的清单以地图 Notes 为准（字段 / 是否密钥 / 依赖 extra / 启用条件 / 健康检查 / 重载范围）。

详见 `docs/research/tavily-integration.md`。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/20#issuecomment-5427891126

[#4](https://github.com/nineofoursyrup/Agent-Alfred/issues/4) 裁掉了本票正文里「五要素」写完却列出六项的口径出入，并给注册表加了一个正交维度：

**注册表只存不可推导的五项**：字段 / 是否密钥 / 依赖 extra / 健康检查 / 重载范围。「启用条件」完全由「字段 + 是否密钥」决定，**是可推导项，不单独存储**（调研 [#10](https://github.com/nineofoursyrup/Agent-Alfred/issues/10) §8 的建议采纳）。地图 Notes 第 5 条已同步改写。

**`reload_scope` 下沉到字段粒度**，至少区分「值变更＝热更新」与「启用状态变更＝重建 Agent」——Tavily 一个集成同时踩中两档。

**注册表管的是可用性，不管授权**（ADR-0005）。两个维度正交：

- 可用性（配齐没有）→ **连接四态**，注册表的职责。
- 授权（准不准动外部世界）→ **三态 `unset|allowed|denied`**，存用户配置，不存注册表也不存 `Tool`。

于是 `web_search` 的暴露规则是：`未配置 + 非 denied` → 暴露**引导 Schema**（工具名不变、参数置空、描述如实写明不会执行），调用返回 `configuration_required`；`已配置 + allowed` → 暴露真实能力。**本票原验收「未配置时返回结构化错误并被回喂模型」由此保住**。

另：`usage.credits` 落 `tool.finished.cost`（`ToolCost | UnknownCost | None`，`Decimal`，闭合 `unit/kind/source`）。**工具成本与模型 token 费用分列，绝不相加**；「不计费」的 `None` 与「应计费但未知」的 `UnknownCost` 必须分得开。

## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/20#issuecomment-5651692328

<!-- issue20-closeout-v6 -->
# #20 交付验收回执

已交付可选集成注册表和 Tavily `web_search`，覆盖 Connections 配置/主动探活、Tools 授权、MainBar 来源回喂与 Ops credits 独立计量。实现复用真实 Host、Registry、Loop、SQLite 双账及 urllib 边界。未配置引导、错误/未知结果、凭据同代发布和 4096 位数值上限均按已确认规范验收。

- PR：https://github.com/nineofoursyrup/Agent-Alfred/pull/58，已合并。
- 最终候选 v6：24 文件；提交 `81b268346c6e1120449f3d3f619148d5880eaff3`，包含首个实现提交 `108f8b23c06e0472f4d787ee79e53ac2a64923db`。
- 固定基线 `796b88c7d1e134906e25cf42f259d20381017fc2`；完整 diff SHA256 `89af8cc95e7c93be24627691399371a69ed4bcd6be49add933b7454e59da1e5d`。
- 合并提交 `35a053e5e793257187aea0a66a93fbc8de252dd0`；其文件树 `57f0c613247f9a25489fcae1a50f471bec00f9b6` 与独立评审候选完全一致，父提交及 main 包含关系已核验。
- 独立 Standards / Spec 均 PASS，0 未解决阻塞、0 optional。CI-01、STD-V5-01、SPEC-V5-01 已通过原始确定性反例及成功断言复验。

## 检查证据

- 本地 Python：3957 passed，1 deselected；浏览器：171 passed。
- Ruff、skills、env、typecheck 全部通过；wheel、sdist 隔离安装各 123 passed，installed-tools 检查通过；制品源码与候选哈希一致。
- [合并前 CI](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34742146111)：SUCCESS，run `34742146111`，head `81b268346c6e1120449f3d3f619148d5880eaff3`，全部适用 job 成功；Python 3957 passed / 浏览器 171 passed。新 head 未产生自动 PR run 后，通过 workflow_dispatch 执行原有同一 CI，未修改工作流。
- [合并后 CI](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34742607841)：SUCCESS，run `34742607841`，事件 `push`，head 为实际 merge SHA `35a053e5e793257187aea0a66a93fbc8de252dd0`，全部适用 job 成功。与合并前 CI 分别核验。

## 验收矩阵与逐 CE 证据

CE-01/05/06/07/10 通过 build_default_host、ScriptedModelFactory、真实 Registry/Loop/授权 store/SQLite 双账；CE-02/03/11 通过真实 Connections API、MutationGate、配置发布与双浏览器；CE-04/08/09 通过 urllib、回环 HTTP、Redactor 与真实页面；CE-12 通过真实注册装配、目录与执行检查。只替换已声明的模型、外部 HTTP、时钟及故障注入边界。

完整测试路径/公共入口/命令索引见[实现验收文档](https://github.com/nineofoursyrup/Agent-Alfred/blob/35a053e5e793257187aea0a66a93fbc8de252dd0/docs/implementation/issue-20-integrations.md)，合同见[已确认规范](https://github.com/nineofoursyrup/Agent-Alfred/blob/35a053e5e793257187aea0a66a93fbc8de252dd0/docs/design/issue-20-integrations-spec.md)。

| CE | 已验收行为与入口 |
|---|---|
| CE-01 | 六格配置/授权矩阵、零 HTTP 引导及下一 Step 读取：`unconfigured_guidance_reaches_next_step`、`six_cells` |
| CE-02 | 真实 MutationGate 409、多标签缓存、10/600s、探活/搜索独立冷却：`probe_is_explicit_cached_and_window_bounded`、`429_cooldowns_are_local_and_separate` |
| CE-03 | 凭据同代发布、同 key 新错误、旧回包/进程重启：`reread_is_atomic_and_preserves_authorization`、`same_key_probe_cache_cannot_overwrite_search_error` 及真实浏览器 |
| CE-04 | 完整 HTTP/结构结局、usage 必需结构、余额缺失/超限：`entire_search_outcome_table`、`usage_requires_real_structure_and_does_not_invent_balances` |
| CE-05 | 0/缺失/非法/精确 credits、SQLite 持久计量、Ops 精确汇总；不可表示 Decimal 和 4096 exact/4097 边界补充：`fractional_credits_and_empty_success`、`decimal_cost_survives_restart_without_trace`、`reported_number_expansion_is_bounded` |
| CE-06 | POST 后未知/控制中断/计量失败停止后续调用，真实 IO 关闭所有权：`bad_200_stops_batch_and_model`、`control_exception_keeps_both_ledgers_unknown`、`metering_failure_stops_further_io`、`host_preserves_interrupted_control_and_closes_http` |
| CE-07 | 新 call 同 query 独立执行，同身份 Registry/ledger 回读不重复发送/计量：`new_identity_searches_and_replay_never_resends` |
| CE-08 | 短/新/旧 key 在 CLI、HTTP、audit、trace、SSE、页面脱敏；零重定向、代理失败无回退：`all_projections_redact_and_redirect_never_follows`、`rotation_sends_only_new_key_and_redacts_both_in_history` 及真实浏览器 |
| CE-09 | 输入/来源/Unicode/optional 字段、真实字节容量 exact/+1、截断/编码/慢响应、安全文本展示：`invalid_inputs_send_nothing`、`actual_byte_limits`、`valid_unicode_boundaries_and_optional_fields` |
| CE-10 | callable 前、进入后发送前、发送后三种预算边界，分别验证启动/发送/计量事实：`distinguishes_start_and_send` |
| CE-11 | 凭据读取/准备/发布/回滚故障、配置暂停与授权 save/reapply 恢复：`injected_publication_failures_keep_consistent_view`、`authorization_write_failure_can_recover` |
| CE-12 | 必需配置与缺 extra 原因独立、授权保留、重复 integration/field/tool 拒绝：`invalid_key_keeps_permission_but_hides_execution`、`declarations_and_missing_extra_are_separate` |

## 真实服务与跨票边界

2026-09-13 已授权的 Tavily GET /usage 1 次与 basic POST /search 1 次均 HTTP 200；3 条来源经真实 Host 回喂 ScriptedModel，Run 完成；服务报告、工具账本和 Ops 均为 1 credit，unknown 为 0。无重试、无真实模型调用，/usage 是否免费仍未确认。

后续修复没有新增真实服务请求。Tavily transport/search、授权、Loop 与计量执行链保持相同字节；v6 Host 只读完整快照版本和 UI 时序由独立公共路径回归验证。不得将 ScriptedModel 验收表述为真实模型服务验收。

其他工作树的 HEAD、状态及原有改动哈希均保持不变；未包含凭据、临时验收目录或其他工作树改动。本次仅验收和关闭 #20；不关闭相邻 Issue，不发版本、不部署、不删除分支或工作树。

本地详细证据：实施工作树 `.scratch/issue20/review-v6/`（双轴/全部门禁）、`review-v5/`（原反例）、`live-v1/`（两次真实调用）及 `delivery/`（两阶段 CI、合并及保留检查）。以上技术与交付条件通过后，以 completed 原因关闭本 Issue。
