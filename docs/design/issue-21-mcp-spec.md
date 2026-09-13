## Problem Statement

用户希望 Alfred 能调用自己配置的本地 MCP 服务器工具，并清楚知道哪些服务器被允许启动、哪些工具被允许执行、连接是否真实可用，以及结果和费用能否确认。当前内置工具与 Tavily 已有统一执行、授权和账目基础，但尚无 MCP 接入；仅启动进程、收到取消回执或保留旧目录，都不能证明工具已就绪、动作未发生或资源已清理。

## Solution

交付多个本地 stdio MCP 服务器到同一个 ToolRegistry 的桥接。Connections 提供配置来源、连接观测、诊断、显式应用/重连与清理进度；Tools 提供来源、别名、逐工具授权和不可用原因；MainBar 发起调用，运行详情与 Ops 保留真实执行及计量证据。核心能力不依赖可选 MCP 的安装或运行成功。

本规范为 **MCP-SPEC-r2**（用户 D5 批准的本地方言扩展，远端发布仍为 r1），由已整体确认的设计 r5 等义整理；仅修订 Q8/Q15 与 CE-08 的双方言范围；其余 Q1–Q20、IC-01 与 CE-01–CE-15 保留。正文为完整实施/验收合同，不依赖本地聊天或未发布附件才能解释关键行为。

## User Stories

1. As a 用户, I want 仅在明确启用本地服务器后启动它，且另行授权工具, so that 掌握进程启动和业务调用两个独立决定。（Q1）
2. As a 用户, I want 在 Connections 管理连接、在 Tools 查看来源和授权, so that 从 MainBar 调用后能在运行详情与 Ops 核对证据。（Q2）
3. As a 用户, I want 同名工具换来源时重新确认权限, so that 不让新程序继承旧允许。（Q3）
4. As a 用户, I want 执行结果未知时停止后续动作并显式恢复, so that 避免不确定动作被自动重复。（Q4）
5. As a 用户, I want 服务器描述、提示和返回内容被按外部数据处理, so that 它们不能提升权限或执行页面脚本。（Q5）
6. As a 用户, I want 看到真实的退出和清理状态及保证边界, so that 不把放弃等待理解为进程已消失。（Q6）
7. As a 维护者, I want 通过真实宿主链路和实际第三方服务分别验收, so that 区分确定性合同与互操作证据。（Q7）
8. As a 用户, I want 缺可选依赖或工具 Schema 不支持时获得准确原因, so that 核心能力仍可用且工具不会伪装就绪。（Q8）
9. As a 用户, I want 使用明确校验的配置格式和环境引用, so that 无效或误拼的配置不会部分生效。（Q9）
10. As a 用户, I want 实质替换后的旧允许权限退役而历史仍保留, so that 恢复配置不会悄悄恢复危险权限。（Q10）
11. As a 用户, I want 配置应用和重连处理过期页面与重复提交, so that 一个操作不会启动两套进程或发布半份目录。（Q11）
12. As a 用户, I want 区分成功无文本、已知失败、结果未知和费用未知, so that 后续行动依据真实结果。（Q12）
13. As a 用户, I want 发现、调用、日志与清理有明确的容量和等待上限, so that 服务器不能无限拖住助手。（Q13）
14. As a 维护者, I want 用固定 Everything 版本只调用 echo 做互操作验收, so that 验收可复现且不依赖私人密钥或外部业务写入。（Q14）
15. As a 用户, I want 复杂 Schema 校验能实际停止并明确展示不支持范围, so that 外部声明不会留下失控后台校验。（Q15）
16. As a 用户, I want 有效环境变化时旧 MCP 立即停用并等待重连, so that 下一次业务调用不会继续使用旧凭据。（Q16）
17. As a 用户, I want 不同协议结果按固定规则分类, so that 未知结果不会被美化为失败、成功或免费。（Q17）
18. As a 用户, I want 看到零工具、不可用工具、旧目录和别名变更的区别, so that 连接观测和真正可调用能力保持一致。（Q18）
19. As a 用户, I want 通过操作进度和继续清理恢复未完成的连接操作, so that 重复点击不会重启服务，残留也不被掩盖。（Q19）
20. As a 维护者, I want 分别验证基础安装、完整 extra 安装和真实互操作, so that 本机偶然依赖和夹具不会冒充发布验收。（Q20）

## Implementation Decisions

### 来源、术语与公共归属

以下合同 confirmed，来源 D1–D4；实现可以沿现有惯例选择内部文件、存储布局和新增路由，但不得改变可观察合同。服务器启动许可与工具授权分开；连接四态与应用/清理原因分开；工具别名不是能力身份；允许权限退役不删除历史。

复用 Host 唯一生命周期/MutationGate、不可变 ToolRegistry、ToolAuthorization、Loop、工具账与独立计量、Connections/Tools/运行详情/Ops。既有 ADR-0005/0006/0032 继续有效。新增持久身份/退役数据如需 SQLite 变更必须新迁移，不改已发布 DDL；没有指定必须采用某一存储形式。

公开接缝基线为 `35a053e5e793257187aea0a66a93fbc8de252dd0`：`runtime/host.py` 的 `close`、`execute_mutation`、`.env` 发布与连接快照，`tools/__init__.py` 的声明/执行及模型投影、`tools/authorization.py` 的权限发布，`tools/cost.py` 的计量投影；Dashboard 页不能继续用 Tavily 专属 `/usage` 文案呈现 MCP。

现有本地工具 Schema 子集校验需要为 MCP 分出已确认的声明校验/参数透传接缝。现有 `NotSentCost` 持久原因是 `http_not_sent`，MCP 需传输中性的未发送事实且兼容 Tavily 历史。现有授权表会按身份自动恢复历史 allowed，MCP 需落实退役规则。这些是实施适配边界，不是本会话已修复的缺陷。

### Q1 配置与启动意图

采用状态目录 mcp.json，JSON mcpServers 与 command/args/env/cwd/enabled；enabled 必须显式 true 才自动启动，缺省不启动。启动进程的允许与工具调用授权分开，配置启动可执行任意本地程序，不是沙箱。只允许参数数组执行，不另经 shell 展开。配置精确校验、变量引用及重读失败按 Q9/Q11/Q16。

### Q2 页面与用户路径

 Connections 负责服务器配置来源、状态、诊断与显式重读/重连；Tools 保留服务器状态摘要及工具来源、alias、授权；MainBar 调用，运行详情/Ops 看真实结果与计量。不做网页命令/密钥编辑器，不做服务器市场。失败服务器局部隔离，普通聊天、内置工具和健康服务器可用。

### Q3 能力身份

重连与展示 alias 改名不丢授权；server_key 不足以证明同一来源。启动定义或工具合同实质变化要重新授权。身份字段、凭据变化及旧权限回读按 Q10/Q18 和整合规则。

### Q4 失去结果后的恢复

沿用 unknown 并停止本 Run 后续工具/模型；失效服务器退出可调用状态，取消后执行有界清理，不自动重连/重试。确认资源回收后才能显式重连；迟到结果不恢复旧 Run、不写第二笔结果。健康服务器保留，后续新用户请求可用。

### Q5 内容信任与投影

只回喂文本；混合返回保留文本并明确标示未支持的非文本内容，不取链接、不执行脚本。server instructions 不自动进入系统提示；annotations 不授予本地读/写权限，MCP 工具统一按 external 授权。structuredContent、纯非文本、截断与协议损坏按 Q12/Q15/Q17。

### Q6 进程清理承诺

正常关闭、Ctrl-C、SIGTERM、可捕获异常及初始化半失败必须有真实进程组回收证据；Host 被 SIGKILL/掉电或服务自行逃逸进程组，不承诺无条件清理任意进程，记录 EOF 协作与限制。清理未确认不得显示已关闭或启动替代进程。

### Q7 验收边界

真实 Host/Registry/Loop/SQLite/HTTP/浏览器 + 本地真实 stdio 子进程夹具作为必需确定性验收；另选一个版本固定、无需秘密与外部写入的实际第三方 MCP server 做互操作验收。真实付费模型、用户私有 MCP/外部副作用不属必需验收；执行实际第三方服务器前需届时明确授权。

### Q8 Schema 声明的验证与可选依赖

新证据区分三件事：schema 声明本身、调用 arguments、返回 structuredContent。2025-11-25 要求按方言验证声明，不要求客户端验证每次 arguments。旧研究把前两者混同，不能把既定“参数由服务端校验”当作声明免验证依据。

规定明确修订“mcp extra 零依赖”：stdlib 仍拥有 MCP 传输/会话，不引官方 MCP SDK；mcp extra 仅引入 JSON Schema 校验库。只支持协商版本 2025-11-25，schema 方言接受默认 2020-12，或显式 Draft-07 / 2020-12。声明必须通过 metaschema 检查；inputSchema/outputSchema 为 JSON object，根 type 为 object，不接受 boolean 根 Schema。inputSchema 原样给模型，arguments 仍只做可序列化 object/资源边界检查后透传服务端，不运行本地业务 schema 校验。

依赖缺失时显示 dependency_missing，零进程启动，核心聊天与其他能力可用，不自动安装。schema 无效/方言不支持的工具在 Tools 标示不可用及原因，不把它的空 Schema 当替代能力；完整发现可有不可用工具，状态显示总数/可用数，不伪装都能用。

只允许 schema 内部引用，不获取远程或文件引用；所有校验器显式绑定拒绝外部资源检索的 registry。metaschema 合法不证明引用可解析，引用检查需独立处理。outputSchema 声明也校验；服务器成功返回 structuredContent 若有 outputSchema 则本地验证（有效 error result 见 IC-01），不宣称无 schema 的数据已验证。供应商拒绝原样 schema 时保留模型错误且零 tools/call，不删字段重试，不改变 MCP 连接证据。

替代方案是继续零依赖，但明确缩小为受限 schema profile 并拒绝其余工具，不能声称完整支持 2020-12；采用前者以保住通用 MCP 接入价值。D2 已确认此项依赖修订；本次 spec 发布不修改产品依赖文件，由实施者落实。

D5 方言扩展：根 `$schema` 缺省按 2020-12；显式接受 `https://json-schema.org/draft/2020-12/schema` 或 `http://json-schema.org/draft-07/schema`（两者均允许末尾 `#`）。按声明选择相应 metaschema、实例 validator 和引用资源语义，保留 Schema 原文透传，不改写成另一方言。支持同方言的子资源；嵌套显式不同方言仍不可用。Draft-07 的 `definitions`、tuple `items` / `additionalItems` 与 `$ref` sibling 语义按 Draft-07 执行。原有外部检索、循环/动态引用、自定义 vocabulary 禁用及可中止进程边界不变。

### Q9 配置语言与环境

规定只接受根 mcpServers，服务器字段 command、args、env、cwd、enabled、timeout_s；不兼容 servers/disabled/alwaysAllow 等其他客户端开关，不静默忽略未知字段。布尔值/整数严格区分；enabled 缺省 false。command 为绝对路径或 PATH 上可解析的裸可执行名，args 为字符串数组，无隐式 shell、变量/波浪线/通配展开；cwd 为绝对目录，省略使用状态目录，不取当前 shell 的偶然目录。

env 显式值只接受完整 ${VAR} 引用，无默认值与拼接；从已生效进程环境/.env 快照解析，进程环境优先。普通非秘密常量也先放环境/.env，避免配置文件承载秘密。继承仅 HOME/PATH/TMPDIR/LANG/LC_ALL/LOGNAME/USER/SHELL/TERM 中已有的值；不把整个 Host 环境透传。缺失引用、不可解析命令等清楚报配置问题，零启动该候选；公开快照只显示变量名/安全状态，不返回展开后的值，合成秘密验证需覆盖短值及旧值。

配置缺失/空 mcpServers 是合法零服务器配置。根结构、重复 JSON key、未知字段、无效字段或超限使整次配置应用在替换前失败，保留旧生效配置并明确“未应用”；不悄悄应用其中一半。配置本身有效、但实际服务器握手或列工具失败，才按 Q2 局部隔离。

### Q10 能力身份与授权退役

规定来源身份涵盖 server_key、解析后的可执行绝对路径、原始 args、实际 cwd、子进程有效环境、握手 serverInfo 与协商版本；能力身份再含原始工具名、description、inputSchema/outputSchema 的规范化声明。秘密只进入本地加密指纹，不出现在身份明文/日志；HMAC 密钥不可用或变化时不继承旧权限。

JSON 对象键顺序、展示 alias、title、annotations、连接时间、预算不改变身份。无法识别密钥轮换与账户切换，因此有效环境值变化均要求重新授权。该承诺不证明同路径程序内容、npx 远程包或服务器内部后端没变；不将配置指纹表述为代码签名。

实质替换/删除某能力时，旧 allowed 退役；把旧配置改回来也不自动复活旧 allowed。保留审计与历史计量。明确 denied 不因移除/恢复被静默解除：同一退役来源/能力回来仍保留拒绝，用户可再显式更改；新身份默认 unset。单纯 disabled→enabled、重连及进程重启且身份未变不是移除，不撤销权限。身份与退役索引持久失败则暂停相应 MCP 调用，不能启动未记清的新授权世代。

### Q11 重读/重连的版本与失败发布

规定页面读到配置指纹、进程身份与状态版本后，显式操作只应用该版本；服务端先获取 mutation gate 再重读文件核对，改变或过期返回冲突且零进程动作。重复请求带同一个 operation_id：受理中的同请求只回状态，完成的同请求回读结果，不重复启动；进程变更后拒绝旧 token/operation 身份。不同操作忙时拒绝不排队，沿既有 Run/recording_pending gate。

GET/页面打开不启动服务、不探活。重连只用已生效配置，不偷偷读取未应用的磁盘变更。应用先完成全配置预检；仅变更/移除的服务器需要退出与重建，未变服务器保留。先清理旧资源再启动替代，不同时运行两个版本。新启动失败则该服务器进入错误、能力不可执行，不宣称旧进程已回滚恢复；不自动重新启动旧命令。

每个服务器的新目录与授权检查作为一致快照发布；跨服务器允许明确的分别成功/失败结果，不发布某服务器的半分页目录。清理未确认则该服务器保持隔离并禁止替代；健康能力继续可用。若全局 Registry/授权/Host 发布一致性无法确认，暂停 MCP 范围并提供恢复动作，不伪造整体成功。旧 HTTP 响应/通知不能覆盖新版本；重复响应不改变状态。

### Q12 返回结果、错误与计量

规定 content 必须是可识别的块数组，isError 缺省 false、出现必须 bool。保留安全文本，非文本只显示不支持标记，structuredContent 保留有界 JSON 审计而不作为文字答案自动替代；含纯非文本或 content=[] 且带 structuredContent 的成功响应显示“服务器已返回，但没有可展示的文本”，不可谎称空查询或完整文字结果，避免因此自动再次调用。有效 content=[] 同样明确空返回，不猜业务含义。

isError=true 为已知 execution_error、可安全回喂且不降低连接状态。JSON-RPC 明确 method/params 错误作为已知协议失败；发出后得到不明确是否执行的 internal/custom error、坏帧、丢失/超限响应或 outputSchema 不符，保守记业务 unknown 并按 Q4 隔离/停止。完整有效文本只在模型回喂长度限制处截断时显式标记、审计保留有界完整内容，不把展示截断当传输失败。精确 JSON-RPC code/outcome 表与服务器不支持请求响应码见本节后续合同表，不靠错误字符串猜测。

不从工具正文/自定义 _meta 猜通用价格；本票 MCP 成本无标准受信报告即 unknown，成功结果仍可使用。只有可信 stdio 边界证明 tools/call 零字节发出才有未发送事实；进入 callable、管道部分写入和完整响应分别记录，不复用 http_not_sent 文案。模型回喂 not_sent 与传输未发送是不同证据，不互推。

### Q13 容量、时限与平台

规定明确以下首版可观察上限：配置 256 KiB、最多 8 个服务器；单协议帧 1 MiB、深度 64；每服务器完整发现最多 4 MiB/20 页/100 工具，全部服务器总计最多 400 工具；单工具 schema 64 KiB。所有容量以实际 UTF-8 字节计，界限 exact/+1 都验；重复 cursor、重复原始工具名、无法唯一化 alias 明确报错，不静默丢条。别名算法沿既定方案，但必须验证最终唯一性，短 hash 不是无碰撞证明，碰撞与顺序反例进入 CE。

每服务器初始化+全部发现共享 10s；全体初始化共享 30s，未开始者明确尚未尝试，不伪造握手失败。业务调用默认 60s，timeout_s 可为 1–120 的严格整数且始终受 Run 剩余预算限制，日志/进度不延长 deadline。清理 EOF/TERM/KILL 各至多 2s，每轮有界且可继续核验未完成资源；同一服务器回收未证实不能重启。stderr 持续排空，只保留末 50 行且总计至多 64 KiB，先安全脱敏再公开。

MCP 进程生命周期必需支持 macOS/Linux（实际环境与 Linux CI）；其他平台明确不可用且不破坏核心启动，不把 POSIX 进程组承诺外推到 Windows。真实部署命令可能自行执行包下载；Alfred 不额外自动安装任何运行时依赖。整体启动/关闭与 UI 响应契约须通过真实进程屏障验证，不能只靠 sleep 或 ResourceWarning。

### Q14 真实互操作验收对象

规定官方 @modelcontextprotocol/server-everything@2026.8.31，固定发布包与完整依赖 lock/integrity；使用隔离环境已安装的 node 和入口绝对路径启动 stdio，只授权并调用 echo，期望 Echo: alfred-mcp-smoke。此 handler 无凭证、网络或外部业务写入代码；准备依赖需要网络/本地包写入，与默认离线测试分开。其 serverInfo.version=2.0.0 不能替代包版本证据。

同一链覆盖配置→发现→未授权拒绝→显式允许 echo→Host 驱动真实调用→SQLite/Ops→关闭回收；模型为 ScriptedModel。其他工具保持未授权。当前仅选择将来的验收对象，不安装/运行；实施验收时取得明确执行授权。第三方兼容失败是互操作验收阻塞，不能用夹具通过替代。

### Q15 Schema 校验的可中止边界

规定 schema 声明检查与有 outputSchema 的成功 structuredContent 验证均放在可终止的本地校验子进程中，一次校验最多 2s 且受发现/调用剩余 deadline 约束；最多一个活动校验作业，结束后回收，未回收不能启动替代。不是线程等待超时。子进程只接受 JSON 输入/返回有界结果，不继承 MCP/Host 凭据环境（被校验的业务 JSON 本身也须按敏感内容处理）；显式禁止资源检索。输入大小上限继续适用，但不宣称它单独保证 CPU/内存有界。

首版支持文档内可解析的无环 JSON Pointer $ref，拒绝外部引用、循环引用、$dynamicRef 与需动态作用域/自定义 vocabulary 的声明；不自动重写不支持 schema。允许普通 $defs/$id 作为声明内容，URI 作为标识不自动触发检索；精确引用基址处理应遵循库的资源语义，不能用字符串前缀冒充解析。拒绝 unsupported 工具不影响同服务器的已验证工具，目录显示总数/可用数及原因。此范围是明确受限的 Draft-07 / 2020-12 工具支持，不声称完整 MCP/JSON Schema 兼容。

发现阶段校验失败/超时，该工具不可用并可诊断；业务调用发出后 outputSchema 验证失败/超时/校验进程故障，按 Q12 保守 unknown 停止并隔离，不能用未验证内容继续。默认 format 仅按库默认注解，不承诺 format assertion。完整 structuredContent 仅入受限、脱敏审计（有效 error result 的优先级见 IC-01），不自动转成文字答案；纯非文本继续成功无可展示文本。验收见 CE-13。

### Q16 有效 .env 变化与 MCP 旧进程

规定复用全局 .env 重读，只有实际传给子进程的有效环境改变才影响该服务器；进程环境优先，磁盘变化但有效值不变不触发。新 env 发布、affected MCP unavailable/restart_required 和模型能力不可调用必须在同一 mutation 内一致生效，禁止旧进程带旧凭据继续业务。

.env 重读不启动/重启 MCP；新 env 与停用发布后，对受影响旧进程执行有界清理，未确认回收则保持隔离与“清理未完成”；不把已发布新 env 伪称回滚，不执行新命令。页面说明“新环境已生效，MCP 尚待重连”，历史 connected 只作过去证据；连接主状态回 configured_untested，restart_required 是应用原因而非第五态。重连使用已生效 mcp 配置与最新有效 env，按先清理后替代执行，重新授权依 Q10。未受影响 MCP/Tavily/模型按既有规则工作。

prepare/publish 失败可回滚时保留旧 env 及一致旧权限视图；不能确认一致恢复则暂停相应外部能力，沿现有 Host fail-closed 处理，不假装成功。旧秘密继续进入安全脱敏集合。CE-14 用双页面、文件及管道屏障证明没有新 env/旧可调用能力窗口。

### Q17 精确业务结果表

规定：完整匹配 tools/call request id 的有效 result、isError 缺省 false/严格 bool、content 为块数组；无 outputSchema 时 structuredContent 必须为 JSON object 且不宣称 schema 已验证。isError=true 记录已知失败 execution_error，安全回喂，连接保持。

匹配请求的 JSON-RPC -32601(method not found)/-32602(invalid params) 可确认为请求被拒绝，记录已知失败并安全回喂；费用仍 unknown，不证明没传输。其他 internal/custom code、无有效匹配响应、丢帧、无效 JSON/UTF-8/结构、响应容量或验证失败均不能确认业务结果，unknown/停止/隔离。不通过错误文字猜“其实成功/未执行”。精确 tool error 与 ledger/metering 两账映射属于实施规范表。

发送取消不等待取消确认也不据其判失败；停止后的合法迟到/重复响应只作受限诊断，不补写结果。缺标准成本报告 unknown；可信 tools/call 零字节发出才为 transport_not_sent，部分写入 unknown。发送前本地校验失败不隔离健康服务器。展示截断沿 Registry 现有 8000 字符+明确标记规则（原始 UTF-8 大小与摘要保留）；外部二进制 payload 不落为巨量审计正文，只留类型/大小与未支持说明。

### Q18 发现、状态和命名的完整规则

规定没有配置/没有成功发现的工具时不编造每个工具的引导 Schema，也不新增 MCP 管理工具给模型；Connections 给真实配置引导。支持 tools 但返回空目录、或服务器没有 tools capability 是有效连接且零工具；含不可用声明可 connected 但明确“可用 n/总 m”。整体分页损坏、重复原始工具名、协议结构错误不能发布半目录，服务器 error。运行中 list_changed 仅显示目录可能变化、需显式重连，同一 Run 目录不漂移。

alias 沿既定 sanitize/长度/hash 方案，但统一按 server_key/原始工具名确定排序、先保留内置名称；碰撞时有界递增后缀且最终验证唯一性，不靠 hash 概率。display alias 变化不改变身份，映射只能查表，notice/Tools 展示原名与别名。新增碰撞者可能引起 alias 变化，但不能串用另一能力的授权或路由。

能力身份字段补精确：实际可执行路径/args/cwd/显式和继承的有效 env、协商版本与 serverInfo、原始工具名/description/inputSchema/outputSchema。env 即使仅 HOME/PATH/TMPDIR/LANG 改变也保守视为来源变化；JSON 对象键顺序、title、annotations、timeout、alias 不变更身份。仅配置检测不是程序内容认证，需在用户说明中直述。

### Q19 重复操作、等待和不完整清理

规定 Connections 控制操作异步受理，回 operation_id 并只读查询进度；mutation 所有权覆盖实际准备/清理/发布，不能因 HTTP 提前回执而放开 Run 准入。GET/查询不触发工作。同 operation_id 同请求回读；同 id 不同 payload 明确冲突。已完成记录进程内有界保留 10 分钟/256 条，达到容量按最旧完成记录淘汰；过期后拒绝旧 token，用户重新读取页面发起新操作，不静默重执行。活动操作不能淘汰。

Host 重启后旧 token 拒绝；启动按 enabled 的当前文件配置恢复，不重放旧控制 operation，也不重放旧 tools/call。新启动本身按 Q1 允许，但以旧 Host 的受管资源锁/进程清理合同为前置，不绕过未清理所有权。

清理超时显示“清理未完成”并持续隔离，提供显式“继续清理”来再次有界核验/完成已拥有资源清理；禁止杀未知/不再可确认归属的进程。它不是重复启动服务或重新执行工具。全局发布失败时暂停 MCP 而非伪造一致，恢复操作必须先确认资源与当前配置；Host.close 仍仅真实全部资源关闭才返回 true。CE-15。

### Q20 测试与可选依赖的交付边界

规定默认无 mcp extra 安装路径必须在干净环境独立验证：未配置或缺 extra、存在 enabled 配置均零 MCP 进程、不影响核心闭环，公开状态区分未配置/缺依赖/不支持平台。全功能离线门禁明确安装 mcp extra 并执行真实子进程和校验器测试，不能因本机偶然有依赖而跳过测试；wheel/sdist 两种制品都验证基础与 mcp 安装形态。

第三方 Everything 及完整依赖只在独立互操作环境安装，默认 CI 不下载或运行它；固定包、lock/integrity、Node/SDK 版本，真实 echo 验收是 Q7/Q14 的单独必需 evidence。缺执行授权或准备条件时该项保持未验收，不能整票关闭。默认离线 CI 中受控 MCP 夹具、ScriptedModel 是声明的外部边界，不代替真实互操作。

以上全部是实施验收门禁；本次规范发布不等于已获产品实施或真实服务执行授权。

### IC-01 已知工具错误优先于成功结果 Schema

先验证 JSON-RPC 信封、匹配 ID、result/content 的必需结构和 isError 类型。结构有效且 isError=true 时，按 Q17 的“已知工具失败”处理，不要求它提供符合成功 outputSchema 的 structuredContent；错误文本安全回喂，不因此隔离服务器。错误结果里可选 structuredContent 若存在仍须为 JSON object，但不宣称满足成功 Schema，按有界不可信审计处理。

仅 isError 缺省/false 的成功结果应用 outputSchema：有 outputSchema 时 structuredContent 必须存在并通过验证，否则 unknown/停止/隔离；无 outputSchema 时可省略 structuredContent，出现则必须为 JSON object，不声称已做实例 Schema 校验。即使有有效 structuredContent，content 仍是必填数组；content=[] 加结构化数据是成功无文本，缺少 content 是协议损坏。

| tools/call 观测 | 工具回执/工具账 | 模型与服务器 | 成本 |
|---|---|---|---|
| callable 前被权限/配置/预算拒绝 | not_executed；不产生执行事实 | 安全拒绝/引导，零请求字节 | not_billable，明确没启动 |
| entered，但受信传输证明业务请求零字节发出 | 本次已启动的本地失败；不记 external unknown | 安全错误，原服务器是否健康依独立连接事实 | not_billable，transport_not_sent |
| 有效成功文本/空/非文本 | succeeded | 有文本安全回喂；无文本明确说明；可继续 | 无受信标准报告为 unknown |
| 有效 isError=true | failed / execution_error | 错误回喂，连接保持 | unknown |
| 匹配 -32601 / -32602 | failed / execution_error，保留安全 protocol code | 已知拒绝可回喂，连接保持 | unknown，不称未发送 |
| 已发后的 internal/custom error 或无法验证结果 | unknown；tool_result_unverified | 停止本 Run 后续工具和模型，服务器隔离清理 | unknown |
| 管道部分写入后失去结果、控制中断、响应超限/坏结构/超时 | unknown；控制异常沿既有路径传播 | 无自动重发；迟到不能恢复；诊断说明 | unknown |
| 请求登记失败 | 不启动；不得伪造执行账 | 沿 ADR-0032 停止扩大未记录范围 | 不伪造费用记录 |
| 执行后计量落定失败 | 保留可证明的执行事实与未落定诊断 | 停止本 Run，沿既有存储准入恢复规则 | 未知不能填零 |

工具回执类型沿现有闭合 code 集，transport 原因/connection 原因是分别解释的事实，不增加伪造的模型工具结果。模型 model_delivery=not_sent 与 transport_not_sent 分字段表达。历史 Tavily http_not_sent 读回仍兼容，新 MCP 不写 HTTP 事实；跨来源计量不可合并。


### 协议处理表

| 观测 | 处理 |
|---|---|
| enabled 且依赖/平台/配置就绪 | 构造期启动；initialize 请求 2025-11-25、client capabilities={} |
| 服务返回不支持版本或无效握手 | 断开、清理、错误；不猜测另一世代兼容，不降级重试 |
| 有效 initialize 完成 | 发送 notifications/initialized；请求 ID 在会话内永不复用 |
| 无 tools capability | connected，0 tools，不发送 tools/list |
| 有 tools capability | 拉完所有分页，无 nextCursor 为完成；cursor 仅用于本会话，不解析其业务意义 |
| ping request | 原 ID 及时 result={}，即使正在等 tools/call；不等于后台主动探活 |
| roots/list、sampling/createMessage、未知 request | -32601；不读取本地 roots、不发模型请求、不触发人机交互 |
| elicitation/create 的 mode 未支持 | 按目标版专门规则 -32602，不弹交互 |
| 未知 notification | 不回复，可忽略/有界诊断；不能按未知 request 处理 |
| tools/list_changed | 仅标记目录可能变化，保持冻结目录与权限；不自动发现/退役 |
| initialize deadline 到达 | 不发送取消 initialize；清理服务器、显示初始化超时 |
| 业务 deadline 到达 | best effort 有界 cancellation，不能把取消写入成功当作动作停止证据 |
| 合法迟到/重复响应 | 不再次结账/唤醒旧调用；有界诊断，不能被误匹配新 ID |

无主动后台探活、自动重连、业务重试、动态工具集合。接收器处理合法双向请求不能绕过权限；请求/通知处理也受帧容量和总 deadline 约束。上下游 ID 与模型 call_id 分离，不能根据 alias 解析 server/tool。


### 身份持久化与可观察范围

持久保存最近已应用的来源/能力身份、拒绝与允许退役信息，先一致对账再开放调用。重启发现 B 时与持久 A 对账、退役 A 的允许，以后恢复 A 仍需重新允许。Host 停机期间磁盘 A→B→A，若从未应用或启动 B，不能声称观察到变化；A 依“身份未变重启”保留授权。这是可观察证据限制，不是文件变更监控。

握手失败或未完成目录不是“工具已被删除”的证据；显式成功应用移除，或相同来源成功完整发现后的缺项，才触发对应删除/退役。临时 disabled 与清理隔离不等于删除。明确 denied 不被状态恢复解除。HMAC/授权/退役记录不可确认时暂停 MCP 调用并诊断，不用显示名恢复权限。

身份包含有效继承环境，因此跨启动 TMPDIR/PATH 等变化也可能要求重新授权。公开身份是有版本/用途区分的本地不可逆指纹，不能用脱敏后的星号当身份输入；不把任意凭据摘要暴露成可离线字典猜测的裸 hash。标准化对象键排序，数组顺序不丢，输入必须有界。具体模块/持久布局由实施选择，不要求复用 memory 私有密钥字段。


### 控制工作结束与残留资源归属

一次有界清理尝试结束后，必须确认该控制工作不会再发布状态，将残留进程/管道/校验作业交给 Host 仍持有的清理记录，再发布隔离状态并释放 mutation。这样健康的已验证能力可继续，新“继续清理”操作可获得门禁；未证实旧控制工作已停下时不能放开门禁。不能用清理超时无限占住门禁，又要求用户通过同一门禁继续清理。

Host.close 只有控制工作、Run/收尾、服务器和校验进程、管道/后台读写工作及清理记录全部完成才 true；日志/两账/安全诊断资源的释放按既有 Host 所有权次序，不能提前拆掉在途执行需要的记录路径。清理 owner 可重复核验已完成步骤，不重复启动进程或调用业务。

SIGKILL、掉电与逃逸进程组的限制按 Q6；不得凭一个旧 PID 杀不明归属进程。新 Host 可以继续核心能力，但对应 MCP 资源归属无法确认时必须保持隔离，不把“主进程已死”当旧业务已停止。只清理可证明归属资源，不提供全机扫杀能力。计时上限限制尝试的等待，不承诺 OS 必在期限内终止不可中断进程。

### 连接四态、目录与配置应用

| 事实 | 当前连接/应用表示 | 能力 |
|---|---|---|
| 无配置 | unconfigured；引导配置 | 无虚构工具 |
| 配置有效但未 enabled/未尝试 | configured_untested；附停用/未尝试原因 | 不可调用 |
| 缺 extra/不支持平台/启动失败/协议中断 | error；明确 dependency_missing/unsupported_platform 等原因 | 不可调用，不混成未配置 |
| 发现完成，含零工具或部分声明不可用 | connected；完整目录总数/可用数 | 仅已验证、可用且 allowed 的能力 |
| env 成功变更 | configured_untested + restart_required；保留历史观测时间 | 旧目录可见但不可调用，清理/待重连 |
| 失去业务结果、资源清理未完成 | error + 隔离/清理事实 | 不可调用，不称已关闭 |
| 配置预检失败 | 保留旧有效配置与其真实连接观测；新文件未应用 | 旧有效能力仍按既定授权 |

旧完整目录仅以旧来源/发现版本作为历史展示，不挂到新来源，不将它当新成功。新目录与授权视图一致发布后替换；成功空目录、发现失败保留旧目录是不同事实。list_changed 的可能过时提示不撤权、不暗中替换 Schema。

外部编辑文件本身不生效；有效配置预检失败整次保留旧配置，启动时无旧配置则保持核心可用、展示配置错误并零 MCP spawn。禁跟随不明符号链接/无限读入，错误日志不回显配置正文或有效 env。进程环境优先，实际删除配置时只在显式应用/下一启动采用；常规页面 GET 不对文件变动执行动作。

Connections 提供配置来源、当前/历史连接、应用状态、可用工具数、脱敏诊断、操作进度与恢复入口；Tools 同步来源/别名/授权/不可用原因。操作按钮只提交用户明确选中的行为。页面预览的文件指纹与应用版本必须在 mutation 内核对，不将前端预检当服务端授权。

MCP 控制操作可用 202 受理与只读进度；忙/过期/重放冲突 409、关闭准入 503、无效配置 400，现有接口错误形状沿仓库惯例。`.env` 既有入口可保留同步回执，成功返回完整新快照；失败后页面读取当前快照确认实际生效，不凭 HTTP 错误推断旧状态必然还在。跨服务 env 发布一致性失败沿现有全 external fail-closed；MCP 局部发布错误在可证明隔离时只影响 MCP。

## Testing Decisions

| 合同 | 必须真实执行的入口/组件 | 允许替代 |
|---|---|---|
| 配置、发现、授权、调用、账目 | build_default_host / Host.submit / ToolRegistry / ToolAuthorization / SQLite / Ops | ScriptedModel、受控外部 stdio server |
| 重读、重连、版本与浏览器 | DashboardApi、真实 HTTP、MutationGate、Host、Connections/Tools 双浏览器 | 外部 server；响应屏障/单调时钟/明确失败点 |
| Schema 与引用 | 真实 jsonschema/referencing、真实可终止校验进程 | 输入 schema/result、受控检索探针，不 mock 校验成功 |
| 生命周期与崩溃边界 | 子进程化 Host、真实 OS 进程组/管道/孙进程/退出证据 | 受控服务行为、信号及清理故障点 |
| 真实互操作 | 固定 Everything 发布包/lock/integrity、真实 Host 与 echo | 只替换模型服务；不替换实际 MCP 服务 |

具体新增 URL/内部类名可由实施按仓库惯例选择，不构成未决产品范围；公共行为与上述路径必须存在。每例提供命令、公开入口、外部替代、确定性排序和可定位证据，不只贴测试条数。默认基础安装和完整 extra 安装分别在干净环境验证，制品不得依赖工作树 import。全部默认离线门禁、浏览器/类型检查、Ruff、skills/env、wheel/sdist 由实施执行，本次规范发布未运行产品验证。

### 测试原则、先例与门禁

测试按上述公共入口观察状态、实际进程/管道请求、模型输入、持久两账与页面；依赖 private helper 的单测不能替代端到端合同。时序用时钟、控制管道、事件屏障和明确失败点控制，不能靠 sleep 猜序；ResourceWarning 可补充，但不独自证明进程回收。覆盖容量 exact/+1、预算发送前/部分发送/发送后三界、旧页面和进程重启。

可借鉴基线 `src/agent_alfred/evals/deterministic/test_integrations.py` 的真实 Host/授权六格、原子 env 重读、未知结果和持久计量；`tests/browser/integrations_server.py` 与 `tests/browser/integrations.spec.js` 的真实 API/双页面；`test_web_rollback.py`、`test_web_rollback_close_progress.py`、`test_dashboard_request_shutdown.py` 的资源所有权与重复关闭。这些路径是先例，不是 MCP 已有验收证据。

完整功能验证环境明确安装 `mcp` extra，基础环境明确不安装：Ruff、全离线确定性测试、`scripts/check_skills.py`、`scripts/check_env_example.py`、`npm run typecheck`、`npm run test:browser`、wheel 与 sdist 在干净临时环境的基础/完整 extra 安装验证均需通过。现有 CI 使用 Linux，MCP 生命周期还需 macOS 证据；不扩展为 Windows MCP 支持。锁定运行依赖，产品代码与制品必须一致，不能从工作树偷 import。

实际第三方 Everything 的下载/安装/执行不放入默认离线 CI；在取得相应授权后，固定 `@modelcontextprotocol/server-everything@2026.8.31` 的完整 lock/integrity、实际 Node/npm/SDK 版本和入口，按 Q14 验证 echo，并保留基础“未授权零调用”与最终进程回收证据。不依赖 serverInfo 的 `2.0.0` 代替包版本。未运行或缺条件即保持未验收，不能用 ScriptedModel/受控 server 通过替代。

## Critical Counterexamples

以下均为已确认的验收合同，不是观察到的产品缺陷，也不是已通过测试。每项来源 D1–D4 与 Q/IC/ADR 均保留，验证状态全部 NOT RUN。协议及身份补充序列已归入对应 CE，没有另建竞争验收附件。

### CE-01 配了服务器但未允许启动
- Basis: Q1；ADR-0005，服务器进程启动与工具调用是不同动作。
- Sequence: 文件含 command/args，分别省略 enabled、设 false、显式 true；启动 Host，之后才授予工具调用权限。
- Expected behavior: 前两组零子进程；第三组允许启动/握手/列工具，但未获工具授权时零 tools/call；页面分别展示启动设置、连接观测、工具授权。
- Verification: build_default_host、真实配置读取、Python stdio 子进程与调用日志、Tools/Connections HTTP；ScriptedModel 替换模型；通过进程握手标志和管道屏障控制顺序。
- Decision status: confirmed（D1 Q1/Q2；D2 Q9）；行为及验证入口/确定性方法已获 D4 整体确认；验证 NOT RUN。


### CE-02 同名服务器换成另一程序
- Basis: Q3；CONTEXT 能力身份、ADR-0005。
- Sequence: server_key 与工具名不变且已有 allowed，修改启动定义/能力合同；另测仅重连与 alias 改名。 补测首次重启发现 B 与未观察的停机 A→B→A。
- Expected behavior: 来源/能力改变不能继承旧 allowed，显示重新授权；确认身份未变的重连/alias 改名保持授权且路由正确。精确身份字段按 Q10/Q18 与Implementation Decisions 中的整合规则。 前者按持久 A 对账退役；后者不能假装观察到变化，身份未变可保留权限。
- Verification: 真实授权 store、重建与 Registry 直接调用；两个不同本地子进程记录收到的请求；检查两次能力目录与历史账身份。
- Decision status: confirmed（D1 Q3；D2 Q10；D3 Q18）；行为及验证入口/确定性方法已获 D4 整体确认；验证 NOT RUN。



### CE-03 服务已执行但取消后迟到成功
- Basis: ADR-0006/0032；Q4。
- Sequence: 子进程记录 tools/call 并完成标记写入后阻塞响应；deadline 到达，客户端发取消；此后送达旧成功并尝试后续工具与重连。
- Expected behavior: 业务 unknown；本 Run 后续执行停止且账目不重复；不能称取消成功/动作撤回；隔离并清理服务器，旧结果不恢复调用，回收未确认不能启动替代进程。
- Verification: Host.submit → 真实 Registry/Loop/两账/stdio，本地夹具仅模拟外部服务器；用控制管道/屏障精确安排执行、deadline、迟到结果及清理失败；读取实际请求数和 SQLite。
- Decision status: confirmed（D1 Q4；D3 Q17/Q19）；行为及验证入口/确定性方法已获 D4 整体确认；验证 NOT RUN。


### CE-04 启动多个服务器时一个半途失败
- Basis: Q2/Q6；核心闭环不依赖可选服务。
- Sequence: A 正常，B 握手成功后列工具失败并留下孙进程；尝试聊天、A 的已授权调用、关闭/重连 B。 补测无 tools capability、有效零工具与分页失败；等待中接收合法 ping。
- Expected behavior: B 错误有安全原因并回收其资源，A 与内置能力仍可用；不得把 B 的部分列表发布为完整连接成功；未确认清理不得隐藏残留状态。 无 tools capability 不发 tools/list；零工具成功与失败保留旧目录分开；ping 正确响应而非等待业务结束。
- Verification: 真实 Host 启动/关闭、两个真实 stdio 进程组、HTTP/浏览器快照；ScriptedModel；故障点在外部夹具协议响应与进程行为，生命周期实现不 mock。
- Decision status: confirmed（D1 Q2/Q6；D2 Q11；D3 Q18/Q19）；行为及验证入口/确定性方法已获 D4 整体确认；验证 NOT RUN。



### CE-05 服务描述自称只读并夹带指令
- Basis: Q5；ADR-0005、中央脱敏及安全文本呈现约定。
- Sequence: initialize instructions 要求跳过权限；工具 annotations 自称只读；结果混有文本、图片和危险链接；未授权直接调用。 调用等待期间服务器发送 roots/list、sampling/createMessage、elicitation/create 及未知 request/notification。
- Expected behavior: instructions 不提升为系统指令、annotations 不绕过授权；未授权零 tools/call；文本安全呈现，非文本明确不支持，不自动获取来源，不执行脚本。 按协议表回应请求，零模型/roots 文件读取/用户交互；notification 不回错误。
- Verification: 真实能力目录/Registry/模型请求投影/HTTP/浏览器，外部 MCP 夹具 + ScriptedModel；合成秘密检查日志/trace/页面，不读取真实凭据。
- Decision status: confirmed（D1 Q5；D2 Q12；D3 Q15/Q17）；行为及验证入口/确定性方法已获 D4 整体确认；验证 NOT RUN。



### CE-06 Host 被强杀且服务忽略 EOF
- Basis: Q6；清理证据不得由退出意图推断。
- Sequence: 受控 Host 子进程启动带孙进程的服务；分别正常退出、SIGINT、SIGTERM、可捕获异常、SIGKILL；服务分别合作 EOF 和忽略 EOF。
- Expected behavior: 可控关闭路径以资源真实回收为完成条件；SIGKILL + 不合作/逃逸服务不承诺必然清理；不宣称未验收的崩溃全清理。实验夹具自身拥有最终清理，避免污染用户环境。
- Verification: 子进程化 Host 与真实 OS 进程组/管道；父测试进程记录 PID/进程组及退出事实，使用同步握手后发送信号；不以 mock Popen 或 ResourceWarning 独自证明回收。
- Decision status: confirmed（D1 Q6；D3 Q19）；行为及验证入口/确定性方法已获 D4 整体确认；验证 NOT RUN。


### CE-07 已进入 callable 但尚未写入 stdio
- Basis: ADR-0006/0032，Tavily 已区分 entered 与发送；Q7。
- Sequence: callable 前耗尽、进入 callable 后写管道前耗尽、部分/完整写入后失去响应三组；另测调用成功但无计量报告。
- Expected behavior: 前两组只有可信边界证明未发送才可记未发送；部分写入不得推断未执行；成功无费用报告继续结果、成本 unknown，不伪造零费用或 HTTP 请求证据。
- Verification: 真实 Registry/ToolContext/stdio 写入/SQLite/Ops；单调时钟与外部子进程屏障；验证字节写入、执行、账目各自证据。
- Decision status: confirmed（既有 ADR-0006/0032；D2 Q12；D3 Q17）；行为及验证入口/确定性方法已获 D4 整体确认；验证 NOT RUN。


### CE-08 声明校验触发外部读取或无界计算
- Basis: Q8/Q13；MCP 2025-11-25 schema 规则与本地资源边界。
- Sequence: 对照 Draft-07 与默认/显式 2020-12 的原文发现、参数透传与成功输出校验（含 tuple items 和 $ref sibling 的方言差异）；tools/list 含无效 schema、不支持方言、远程/文件引用、内部引用、循环引用或高代价正则；对照缺失可选依赖的启动。
- Expected behavior: 缺依赖零进程；声明无效或不支持的工具可见但不可调用；零自动 HTTP/文件读取。内部合法引用不因 URI 外观被误判为网络请求。时间/计算限制按 Q15 子进程时限；字节上限不单独证明计算有界。
- Verification: 真实发现/Registry/模型适配器/校验器，真实 stdio 夹具提供 schema；回环请求计数与受控文件访问观测证明禁取外部引用；专用子进程与时钟控制验证计算边界，校验子进程方法按 Q15。
- Decision status: confirmed（D2 Q8/Q13；D3 Q15）；行为及验证入口/确定性方法已获 D4 整体确认；验证 NOT RUN。


### CE-09 配置预览后文件又变、重试同一操作
- Basis: Q9/Q11；旧版本不执行新动作，受理与发送事实分开。
- Sequence: 页面读取 A，磁盘改成 B 后提交 A 的应用；有效受理后丢失回执并重发同 operation_id；另一标签并发发不同操作；重启 Host 后重放旧 token。
- Expected behavior: 过期提交冲突且零进程动作；已受理同操作只回读，不重启第二次；其他操作忙拒绝不排队；新进程拒绝旧身份；旧响应不覆盖新快照。文件根结构或字段无效时保留旧配置，不先杀旧服务器。
- Verification: Connections HTTP → 真实 MutationGate/配置文件/Host/子进程；双浏览器与响应屏障，统计 spawn/terminate 与快照版本；无副本 gate 或 mock 发布状态。
- Decision status: confirmed（D2 Q9/Q11；D3 Q19）；行为及验证入口/确定性方法已获 D4 整体确认；验证 NOT RUN。


### CE-10 已允许 A 换成 B 再恢复 A
- Basis: Q10；能力身份与历史授权退役。
- Sequence: A allowed→替换为 B→恢复 A；对照 A denied 同样过程、仅 disable/enable、仅重连、有效环境值变化、JSON key 顺序变化以及 HMAC key 失效。 补测启动对账、目录失败、完整目录删除与退役写失败；停机期间未观察的 A→B→A。
- Expected behavior: 实质替换退役 allowed，恢复 A 需重新允许；相同身份 denied 保留，新身份 unset；普通停启不退役；无法区分的凭据变化重新授权；键顺序不改变身份；身份不可确认不能继承 allowed，历史账不重写。 先持久一致对账才开放调用；目录失败不退役、完整缺项/显式移除才退役；持久失败零新能力调用，旧账不改写。
- Verification: 真实配置应用/授权存储/Registry/SQLite 重启读取，用两台受控程序与合成 env；不读真实 key，用注入隔离目录和 key 故障；验证源身份、实际调用数和历史记录。
- Decision status: confirmed（D2 Q10；D3 Q18）；行为及验证入口/确定性方法已获 D4 整体确认；验证 NOT RUN。



### CE-11 响应是非文本、坏结构或未报告费用
- Basis: Q12；Q5 文本投影与 ADR-0006/0032。
- Sequence: 已发 tools/call 依次返回混合 content、纯非文本、空数组、content=[] 且带 structuredContent、isError=true、非法 isError、method/params 错误与 internal/custom error；有效成功但无费用字段。 工具有 outputSchema 时，分别返回合法 isError=true 文本且无 structuredContent、相同正文 isError=false；另测 content 缺失与 content=[]。
- Expected behavior: 可确认成功且无文本时明确未展示文本，不能伪造失败重试；已知业务错误安全回喂、不冒充断连；发出后不能确认结果的坏响应/错误 unknown 并停止；费用缺失 unknown 不否定有效业务结果。outputSchema 相关验证按 Q8/Q15，错误码矩阵见Implementation Decisions 中的整合规则。 有效 error result 优先按已知工具失败，不套成功 Schema；成功缺必需 structuredContent 则 unknown；content 必填，缺少不能当空数组；合法迟到响应不重复结账。
- Verification: Host→真实 Loop/Registry/stdio/两账/Ops 与浏览器；仅替换 MCP 服务和模型，用 ScriptedModel 断言收到投影或未发生后续模型调用。
- Decision status: confirmed 基础行为（D2 Q12；D3 Q15/Q17）；有效 error result 与成功 Schema 的优先级 IC-01 confirmed @ D4；验证 NOT RUN。



### CE-12 多页目录不完整与别名碰撞
- Basis: Q13；完整发现、alias 映射唯一、可用状态如实。
- Sequence: 第一页正常、第二页损坏或超时；重复 cursor、重复原名；不同原名 sanitize 后碰撞、长名短 hash 碰撞、顺序重排；实际 UTF-8 大小 exact/+1、深度 exact/+1、stderr 写满。 补测不同发现顺序和 list_changed 通知。
- Expected behavior: 不把半目录发布为完整连接；最终 alias 必须唯一且正确路由，改名可见、不继承错误授权；明确超限/原因而非静默截条；stderr 持续排空并有界保留，进度不延长 deadline。 保持最终 alias 唯一且正确路由；list_changed 仅提示需显式重连，不自动改目录或退役授权。
- Verification: 真实分页解析/alias 构建/Registry 调用、真实管道与子进程；外部夹具提供不同顺序/分块/屏障，观察工具目录、原始服务实际收到的名称与环境资源；不靠单一 mock TimeoutError 覆盖全部边界。
- Decision status: confirmed（D2 Q13；D3 Q18）；行为及验证入口/确定性方法已获 D4 整体确认；验证 NOT RUN。



### CE-13 恶意正则拖住校验子进程
- Basis: Q15；Q13 时限、ADR-0006 不伪造线程可中止。
- Sequence: 合法有界 schema 含高代价模式，发现校验或结果验证进入高 CPU；达到校验/调用截止，子进程延迟退出；另测合法无环内部引用和禁止循环引用。
- Expected behavior: 到界停止等待并实际终止/回收校验作业，不留继续运算的线程；发现阶段工具不可用，调用后无法完成验证则 unknown 停止；回收未确认不得启动替代；其他健康服务器不被挂死。
- Verification: 实际 JSON Schema validator、真实校验进程、Host/stdio/Registry/两账；注入时间与受控 IPC 屏障，真实问题 schema 作为辅助，确定性超时不依赖机器速度；观测进程退出与后续请求数。
- Decision status: confirmed（D3 Q15）；行为及验证入口/确定性方法已获 D4 整体确认；验证 NOT RUN。


### CE-14 新凭据发布后旧进程仍可调用
- Basis: Q16；Q10 来源变更重新授权，现有 .env 一致发布合同。
- Sequence: MCP A 持有效 env A；磁盘改 B 后全局重读，与旧 Tools 页授权/调用请求交错；另测无效 .env、进程 env 覆盖、同值重读、失败回滚和迟到 HTTP。
- Expected behavior: B 与 restart_required/旧能力不可调用一致发布，零旧凭据业务调用、零隐式 spawn；同有效值不触发；失败保留一致 A 或明确暂停；显式重连后只有新 env 且身份需重授权，旧页面不覆盖。
- Verification: Host.reread_dotenv 经真实 HTTP MutationGate→CredentialOverlay/真实 mcp 快照/授权 store；真实文件与双浏览器，合成 env 和本地子进程记录实际值，仅内部使用指纹，公开输出全脱敏。
- Decision status: confirmed（D3 Q16）；行为及验证入口/确定性方法已获 D4 整体确认；验证 NOT RUN。


### CE-15 清理未完成时重复重连或关闭
- Basis: Q19；Q4/Q6 真实资源所有权与不重复启动。
- Sequence: 控制请求受理并开始清理后回执丢失，客户端同 id 重试；清理期限耗尽，用户点重连与继续清理；另一标签重复提交；最终 Host.close 被再次调用。 清理尝试结束但资源残留，确认控制工作已停止发布后再发继续清理/健康能力调用。
- Expected behavior: 同 id 只回读，未确认旧资源回收不能 spawn；继续清理只处理已拥有资源，最终结果可核验；关停未完成返回 false，不能提前释放仍需两账/诊断的资源；过期/旧进程 token 不重执行。 残留转交 Host 清理记录并发布隔离后方释放 mutation；继续清理能重新入门，旧控制工作未停不得放门禁；Host.close 等全部资源真实结束才 true。
- Verification: 真实 Connections API/operation store/MutationGate/Host.close 与子进程组；管道和响应屏障、控制故障注入；观测总 spawn、信号、worker/资源终止及快照，不 mock 生命周期。
- Decision status: confirmed（D3 Q19）；行为及验证入口/确定性方法已获 D4 整体确认；验证 NOT RUN。

## Readiness and Open Decisions

- **Design confirmation**：D1=Q1–Q7 后用户“全按建议”；D2=Q8–Q14 后“全按建议”；D3=Q15–Q20 后“全按建议”；D4=整体确认 r4（含 IC-01）时用户“是”。批准稿 SHA256 `5afaff0714c0635d4a32d9679014a406f9fcacc76450d23da628bfac7c2a91ba`；r5 仅补确认，SHA256 `67cb6e365b829c9eaf5c153fa5ea4df021751b2098098c35b7a153fa3ad20df6`。
- **D5（2026-09-13）**：真实固定 Everything 包的 13 项工具均声明 Draft-07，r1 按合同全部拒绝。用户对“是否将规范扩展为同时支持 Draft-07，再修改实现并重跑验收？”回复“可以”，明确批准本 r2；仅本地修订，尚未发布远端正文。
- **Readiness**：ready-for-agent；20 项决定、IC-01、15 个反例的行为/公共验证边界均确认；unresolved design/acceptance blockers=0，deferred=0，无删除或替换必需案例。
- **Publication 与执行分开**：用户调用 to-spec 授权规范发布及就绪标签；就绪不表示已实施、测试通过或授权真实服务调用。本次不创建实施任务、不提交代码、不推送/合并/关闭 Issue。
- **Implementation/validation**：NOT STARTED / NOT RUN；全部 15 个 CE、制品安装和真实 Everything 互操作均待实施者逐项举证。缺少届时真实服务执行授权/准备条件时，互操作验收仍是必需交付项，不能以就绪标签豁免。
- **Dependencies**：发布前原生依赖“调研：MCP stdio 服务器接入与子进程生命周期”和“实现：切片③ — ToolRegistry 与内置工具”均已 closed；main 仍为上述基线。实施开工时重新核验分支/工作树/远端，不依据该日期快照推断当前候选。
- **User-approved clarifications**：D2 明确修订旧“零新依赖”为 Schema 可选依赖；D1/D3 区分可控清理和 SIGKILL/逃逸限制；D3 明确受限 Schema profile；D4 确认正常工具错误优先于成功 Schema。均已纳入正文，不保留竞争的旧口径。

## Out of Scope

不做 modern 协议、remote HTTP/SSE/WebSocket MCP、OAuth、服务市场、网页命令/密钥编辑器、roots/sampling/elicitation 的实际能力、后台主动探活、业务自动重试/恢复、Run 中动态目录、二进制显示与来源自动抓取、Windows MCP 进程生命周期、任意 Schema 全兼容、未知进程扫杀、SIGKILL 后无条件清理、收费报告推测或跨服务 credits 相加。

接入用户指定命令不是沙箱；命令或 wrapper 可以自行下载程序，Alfred 不额外代装依赖。受限 Schema profile/验证超时可能拒绝合法复杂工具，必须可见；真实包版本不等于握手 serverInfo.version，互操作证据必须记录二者。原研究 v2.0.0 与“零新依赖”不再作为当前安装合同。

## Further Notes

### 权威位置与接续

本 r2 以已发布 GitHub Issue 正文 **MCP-SPEC-r1** 加用户 D5 批准的方言扩展为实施权威；本地实施版为 `/Users/nineofour/Agent-Alfred-issue-21/docs/design/issue-21-mcp-spec.md`。远端 r1 保留原发布记录；本地逐字镜像为 `/Users/nineofour/Agent-Alfred-issue-21-design/docs/design/issue-21-mcp-spec.md`。验收定义完整内嵌，设计稿是有 hash 的历史确认来源，不是第二份可变验收合同。

同一状态入口为 `/Users/nineofour/Agent-Alfred-issue-21-design/tmp/agent-work/issue-21/LOOP.md`。实施者先读最新 revision，再读取本规范和冻结 manifest；20 个 Q、IC-01 和 15 个 CE 的源映射保存在该目录 `spec-r1-coverage.json`。复核分支 `codex/21-mcp-design` 与基线；设计工作树只有本票文档，主工作树原有 `.gitignore` 改动不得携入。实际产品写入者尚未指派。

可携带的设计文档为 CONTEXT.md 与 ADR-0033/0034/0035；未发布到 main，因此这里直接保留其词汇与架构含义，不能假设 GitHub 上已有这些文件。后续独立实施工作树应按 manifest 携带这些文档，或以此正文等义建立；不得把其他工作树未提交文件当作已发布主线。

### 术语与架构取舍

**MCP 服务器（MCP server）**：
由用户本地配置标识的一项工具来源；服务器的连接事实与其各项工具的调用授权分别表达。
_Avoid_: 工具、授权集合

**服务器启动许可（server launch permission）**：
用户允许 Alfred 启动指定 MCP 服务器并发现其工具的意图，不包含对发现工具的业务调用授权。
_Avoid_: 工具授权、已连接

**工具别名（tool alias）**：
Alfred 向模型展示并用于分派的一项工具名称；它不独自证明服务器来源或能力身份相同。
_Avoid_: 能力身份、服务器自报名称

**服务器隔离（server quarantine）**：
Alfred 暂停向某个 MCP 服务器发出新业务调用、等待资源清理与显式恢复的状态；不证明其既有动作已撤销。
_Avoid_: 已取消、已回滚

**允许权限退役（allowance retirement）**：
某项 MCP 能力被实质替换或移除后，既有允许记录不再有资格自动成为当前权限；历史审计仍保留。
_Avoid_: 删除历史、临时停用、拒绝

**MCP 环境待重连（MCP environment awaiting reconnect）**：
Host 已采用新有效环境、而服务器尚未以该环境重新建立能力的应用状态；此时旧服务器工具不可调用。
_Avoid_: 用户拒绝、凭据无效、已连接

#### ADR-0033 MCP 服务器启动许可与工具调用授权分开

Alfred 必须先启动 MCP 服务器、握手并发现工具，才能展示逐工具授权，因此不能用尚不存在的工具授权作为进程启动条件。用户在本地配置显式启用服务器，表示允许执行其启动定义和发现能力；省略启用则不启动。发现的工具仍按既有 external 三态授权执行，连接成功不授予业务调用权限。

直接把“存在配置”解释为允许执行，虽便于导入其他客户端的配置，却可能在用户尚未表达启动意图时执行本地程序；反过来，要求逐工具授权后才发现工具会形成循环依赖。本设计接受一个独立的启动许可概念，同时保留 ADR-0005 的可用性与工具授权分工。启动许可不承诺进程沙箱或约束服务器自身的行为。

来源：MCP 桥接设计访谈 Q1–Q2，用户确认 D1“全按建议”。精确配置校验、重读和身份规则归设计/实施规范，不在本 ADR 固化。

#### ADR-0034 被替换的 MCP 能力不能自动复活旧允许权限

MCP 服务器可以在同名配置下更换启动程序或工具合同。实质替换或删除能力后，其旧 allowed 权限退役，即使后来恢复原配置也必须重新允许；同一身份明确的 denied 保留。普通停启、重连及展示别名变化不构成权限退役，历史计量与审计始终保留。

当前授权存储按来源与能力身份保留历史记录，直接沿用会使旧配置回来时自动恢复 allowed。接受“回滚配置也可能要重新授权”的成本，是为了让过去允许过某程序不变成恢复它的永久启动业务许可。密钥变化无法可靠区别同账户轮换与账户切换，因此同样不能静默继承允许。

来源：MCP 桥接访谈 Q3/Q10，用户确认 D1、D2“全按建议”。身份字段、指纹与退役记录的一致持久化合同放在设计规范中；本决定不承诺配置身份能够验证任意服务器代码内容或内部远端行为。

#### ADR-0035 MCP Schema 校验使用可终止进程

工具提供的 Schema 与结构化结果属于外部输入，字节容量有限不代表正则、引用和组合校验会及时结束。Schema 声明及成功结构化结果的校验因此在可终止子进程执行，受校验时限和所属发现/调用剩余预算共同约束；暂停等待不等于校验已经停止，回收未确认时不启动替代作业。

同进程或线程校验更简单，却不能落实已承诺的中止行为；接受进程与资源所有权成本，以避免主循环被外部声明拖住。校验依赖通过 mcp extra 提供，传输仍是 stdlib；只支持明确的引用范围并禁止外部检索，不宣称覆盖所有合法 JSON Schema。具体时限、结果分类和测试接缝归设计规范。

来源：MCP 桥接访谈 Q8/Q15，用户确认 D2/D3“全按建议”；沿用 ADR-0006 对真实中止与放弃等待的区分。

### 核验来源与既有接口

- Schema 声明和 arguments 是两层义务：[MCP JSON Schema usage](https://modelcontextprotocol.io/specification/2025-11-25/basic#json-schema-usage)、[Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)。
- check_schema 只完成 metaschema 验证，不证明引用全部可解析或计算有界。验证器必须显式传拒绝检索的 registry，不能依赖未传 registry 的历史默认行为：[Validator API](https://python-jsonschema.readthedocs.io/en/stable/validate/)、[Referencing](https://python-jsonschema.readthedocs.io/en/stable/referencing/)。format 默认不作断言，不能宣称全部字符串格式已校验。
- 实际包版本与 serverInfo.version 分开：[Everything 固定 manifest](https://github.com/modelcontextprotocol/servers/blob/a40bc270fb5ece62673f8a1196f57116d885c5eb/src/everything/package.json)、[发布版本 2026.8.31](https://registry.npmjs.org/@modelcontextprotocol%2fserver-everything/2026.8.31)、[echo 实现](https://github.com/modelcontextprotocol/servers/blob/a40bc270fb5ece62673f8a1196f57116d885c5eb/src/everything/tools/echo.ts)。调查仅只读包内容，未安装/执行。
- 当前授权事实：tools/authorization.py prepare 直接按 identity 回读历史值，save 保留消失能力；暂无退役机制。tools/__init__.py capability_identity 是 source/capability 二元组。Q10 是新增设计，不将它表述成当前已实现行为。
- CredentialOverlay 负责有效 env，Redactor 不提供稳定身份；memory/audit.py 的 AuditKey 提供可复用的持久 HMAC 模式。只读代码未读密钥。模型适配器原样发送 schema，不能保证供应商接受任意声明。

设计阶段的两个只读事实调查均已完成，当时未运行 MCP 服务、未修改 Issue。公共接口依据固定基线；协议依据 2025-11-25 官方文档。

- legacy 允许双向 request/response；同一 requester 的 request ID 在同一 session 内不可复用。旧研究“禁止 server request/client response”与“仅 pending 不重复”不适用。[Base](https://modelcontextprotocol.io/specification/2025-11-25/basic)、[Transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)。
- 收到 ping 必须及时回复空 result；这不要求客户端后台主动探活。未知 notification 与不支持的 request 不能同样静默丢弃。[Ping](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/ping)。
- 取消允许因已完成/不可取消等原因被忽略；initialize 不可取消。客户端应忽略取消后的迟到结果；本地业务 unknown 推导仍依 ADR-0006。[Cancellation](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation)。
- instructions 注入 system 是协议可选建议，本项目选择不注入，annotations 不构成可信行为证据。[InitializeResult](https://modelcontextprotocol.io/specification/2025-11-25/schema#initializeresult)、[Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)。
- 声明空客户端能力不支持 roots/sampling/elicitation；不支持请求须按目标版本规则回应，不能概括为所有情况同一码。[Lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)、[Roots](https://modelcontextprotocol.io/specification/2025-11-25/client/roots)、[Elicitation](https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation)。
- 完整工具目录需处理分页，不透明 cursor 不跨 session 复用。总量/循环游标/半目录发布已由本规范冻结。[Pagination](https://modelcontextprotocol.io/specification/2025-11-25/server/utilities/pagination)。
- Schema 声明、调用参数和结果验证已分别按 Q8/Q15 冻结；不重新打开“调用参数由服务器校验”的既定裁决，不承诺兼容任意 MCP schema。[JSON Schema usage](https://modelcontextprotocol.io/specification/2025-11-25/basic#json-schema-usage)。

现有公共接缝：`runtime/host.py:625` 的 close 返回真实关闭状态，未完成时可重试；`:814` 起 mutation gate 串行控制变更，`:997` 起 Connections 快照版本，`:2013` 起 Tools 投影；`tools/__init__.py:109` 起 source/capability 与 Tool，`:158` 起现有 schema 子集校验需为 MCP 透传适配；`tools/cost.py:35` 起计量投影目前将未发送原因写为 http_not_sent，不能直接当 stdio 事实。`ops/static/pages.js:329` 起集成卡片含 Tavily 专属展示，需要 MCP 对应呈现。以上为实施适配边界，非已确认实现缺陷。
