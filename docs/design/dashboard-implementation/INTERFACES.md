# 公共接口合同

对应 `DASHBOARD-IMPLEMENTATION-SPEC-r1`。以下标为“新增／扩展”的接口均是**后续实现要求，当前未实现、NOT RUN**。既有 HTTP、SSE、命令回执和保护协议继续有效；仅在本文指定的接缝增加能力。实现位置与源码差距见 [来源核对](SOURCE-AUDIT.md)，责任见 [切片规格](SLICES.md)。

本文 I00–I09 是本规格的接口章节号，不重编号上游 `issue-89:I01`–`issue-89:I06`；原始责任 ID 始终以来源命名空间引用。

## I00：读取信封、身份、错误与所有权

新增 GET 读取统一要求 `process_instance_id`，来自壳层完成同步的当前实例；Session／Run ID 保持不透明，区分参数缺失与合法空字符串，沿现有编码往返特殊字符。请求不携带消息、SQL、令牌或 Memory 正文到 URL。参数使用现有同源请求入口；生产继续回环监听、Host／Origin／CSRF 与 CSP 边界，读取不能借此引入动作端点。

| 公共字段 | 类型与含义 |
| --- | --- |
| `schema_version` | 新端点为整数 `1`；旧端点扩展字段兼容原形状，不要求旧客户端改用包裹层。 |
| `process_instance_id` | 实际服务实例；不匹配请求时返回冲突，不能带另一实例的成功结果。 |
| `observed_at` | 服务端完成该来源观察的 UTC 时间字符串；期间使用自己的 `computed_at`。不是所有页面共用的事务时刻。 |
| `code` | 错误时的机器可读代码，沿既有顶层 `code` 风格；安全文案由客户端映射，不返回异常堆栈、路径或敏感原值。 |
| 来源专用身份 | 期间 observation／价格、记忆 revision、Run／Session／cursor、Host `state_revision` 等分别使用，不互相比大小。 |

新读取缺少必需参数为 `400 missing_<field>`，不支持字段／值为 `400 invalid_input` 或下列专用代码；实例不符为 `409 process_context_expired`；无法证明读取完整为 `503 read_unavailable`，资源预算不足为 `429 read_quota`，不返回截断的“成功总量”。旧端点原有错误码及状态保持，包括 `/api/reply` 的 `reply_context_expired`。适配器可把不同旧错误映射成页面状态，但不能改变其事实。

每次客户端读取持有本地 `ReadContext`：实例、页面 generation、目标键、来源 generation、必要 revision 和 AbortSignal。它不需要服务端回显随机 request ID。仅全部身份仍匹配且所有者未退休时可写回；取消／离页、连接中断、实例变化和目标替换按源规则退休请求。abort 是撤销客户端接收资格，不是宣告服务端动作取消成功。

HTTP 读取不能更改 Session、创建 Run、重试保存、发起模型或工具调用。所有数据库读取、游标和临时计价内容由服务端在成功／异常／请求取消的退出路径释放；客户端不持有数据库事务。页面获取的 opaque cursor 只是读取位置，不能作为授权、生命周期或正文保护凭证。

## I01：Overview 期间摘要

**新增** `GET /api/overview/period`。必需 query：`process_instance_id`、`range`（`today | 7d | 30d`）、`timezone`（有效 IANA 标识）。不接受 Session、purpose、Run、tool 筛选；自定义／全部历史留在 Ops。无效时区 `400 invalid_timezone`，无效期间 `400 invalid_range`。

成功载荷在 I00 基础上返回：

| 字段 | 必须返回的形状／规则 |
| --- | --- |
| `observation_id` | 本次摘要的 opaque 身份；不供 Ops 翻页／明细使用，也不是客户端要 DELETE 的句柄。 |
| `computed_at`, `expires_at` | 同次计算及最迟失效时刻；观察最多十五分钟，来源更早失效优先。 |
| `filters` | `range`、认可的 `timezone`、UTC `start`／`end`，半开区间。 |
| `ops_filters` | `range=custom`、同一 `timezone`、当地日期 `start`／`end`；**end 为排他日期**，可以原样交给现有 Ops 范围规范化。 |
| `price_version` | 本次冻结价格解释的 opaque 身份；跟随该观察，不在价格设置变化后原地替换。 |
| `summary.run_count` | 归属可判定且落在期间的全部持久 Run 数 C；全 Session、purpose、准入及结局，旧无 Run 消息不计入。 |
| `unresolved_membership_count` | 缺失／损坏归属时间、无法判定是否属于期间的 Run 数 U；不返回正文，不把 C＋U 称为确定期间总量。 |
| `summary.attempt_count` | 本次完整成员集合内可读的已记录 Attempt 数；不冒称全部实际调用数。 |
| `summary.exact_usd`, `summary.estimated_usd` | 十进制字符串，继承账目定价及作废 Attempt 仍计账的规则；不用二进制浮点累计或合成“总费用”。 |
| `summary.exact_attempts`, `summary.estimated_attempts`, `summary.unknown_cost_attempts` | 对上述已记录 Attempt 的互斥费用分类数量；新增前两项用于判别“没有可确认金额”和零，不凭累加器初始值判断。 |
| `coverage.incomplete_runs`, `coverage.reasons` | 全部账目覆盖不足 Run 数及原因到 Run 数的映射；同一 Run 可有多个原因，原因数不能直接求和当分母。保留既有原因语义。 |
| `model_coverage.complete`, `model_coverage.incomplete_runs`, `model_coverage.reasons` | 单独声明模型账目证明范围；缺失／损坏 telemetry、Attempt 损坏、调用数量未确认、记录未落定等据实归类。工具历史计量缺口不能直接变成模型账目缺口。 |
| `history_boundary` | 安全说明：持久 Run 集合，未关联 Run 的历史消息不计入。 |

实现采用既有账目完整读取与价格冻结的公共构造能力，供 Ops 保留快照模式和 Overview 请求内摘要模式共用；不在 Overview 客户端遍历 `/api/runs` 或 Ops 分页。选择的技术方式是提取请求内构造／聚合边界，**不采用 create 后等待 TTL 的保留快照方案**。归属、计价与覆盖逻辑只有一份；不能另写一套简化公式。

完整成员、计量、telemetry 与裁剪事实沿既有一致读取边界取得，价格一次冻结。结束事务后生成小型摘要；中间资源限额沿现有账目 64 MiB 上界控制，超限失败，不能只取前 N 条。读取在现有账目并发控制下执行，扫描和投影阶段检查可观察到的所属取消信号；正常返回、失败和取消都在 finally 释放。客户端 abort 不保证服务端立刻收到取消，但事务不得延续到响应展示或十五分钟 TTL；当前有界读取退出后没有悬挂事务或保留快照槽。既存 Ops 快照的身份、字节和到期时间不受影响。

消费者 S04 同批换入期间两卡；U＞0 必须显示期间无法判定。C=U=0 且读取完整才是“暂无运行记录”。可确认模型金额全无时显示费用未知；已知分项可展示且并列未知／覆盖说明。只有期间归属无缺口、模型账目完整且费用分类无未知，才能表达可证明的零费用；微小正数不得格式化成确定零。不可将 `incomplete_runs` 改名为“模型费用未知 Run 数”。

跳 Ops 使用 `ops_filters` 创建**新的**账目快照并说明按原期间重新读取；新来源、计算时间和价格可改变数值。Overview 摘要不是 Ops snapshot ID，不调用旧明细接口读取它。

## I02：完整记忆计数

**新增** `GET /api/overview/memory-counts`，必需 query 为 `process_instance_id`、非负整数 `expected_memory_revision`。成功返回 I00 信封及 `memory_revision`、`counts.semantic`、`counts.episodic`、`counts.total`、`expires_at`。计数均为非负整数，total 是同次两类之和，不含 Skill 文件、镜像副本或历史已遗忘内容。

MemoryQueryService 增加完整计数读取能力。两个 Store 可提供可选的只读 count 能力；SQLite 实现在注入的同一 reading context 内直接聚合，绑定同一持久 revision。不能要求所有替代 Store 通过枚举正文假装支持 count；任一不支持或无法保证一致修订时返回 `503 counts_unavailable`，没有部分成功 total。

读前、读内及响应资格核验遵守现有 Memory 修订边界。预期修订过时／读取中修订变化为 `409 memory_changed`；同一显式用户读取可以复用 MemorySync 的最多三次修订竞争重读，耗尽后显示不可用并等待用户重试，不转成后台轮询。客户端收到新 revision 立即撤销旧“当前计数”，隐藏时也如此；旧响应不得用新修订重新贴标。计数没有正文，断连旧副本可按 #88 标离线，但已被明确修订撤销的值不能恢复。

## I03：Run 安全摘要与最近终态

**扩展** `/api/runs`、现有 Run locate、`/api/sessions/runs` 的摘要投影；新迁移客户端发送当前 `process_instance_id`，旧调用不带该参数仍可读取原形状。新增响应实例／观察字段和下列元数据采用增量字段，不删旧 keys，不让现有 MainBar 普通历史／正文恢复改用总列表。

| 字段／规则 | 投影合同 |
| --- | --- |
| 原身份、时间、阶段、结局 | 保留 `run_id`、`session_id`、`purpose`、`purpose_known`、`filter`、gateway／entry surface、三种时间、`activity_revision`、`admission_state`。无法证明的字段为 null／原未知形态，不从 ID、缺文本或 finished 推断。 |
| `filter` | 服务端统一 `chat`＝chat＋aggregation，`system`＝其余，`all` 为查询范围。记录的实际分组和请求的来源筛选分开。未知 purpose 保留安全显示值并 `purpose_known=false`。 |
| `prompt_preview` | 仅 `purpose=chat AND admission_state=admitted` 有资格，仍经中央 Redactor；其余为 null。aggregation 的输入及非获准 chat 不因字段已有而披露。Overview 响应完全不带输入预览。 |
| `recording_state` | `pending | recorded | failed | null`；null 表示展示时未知，不新增持久第四状态。 |
| `recording_source` | `durable_finalization | host_state | unknown`；Host 来源附 `state_revision`，持久来源依照既有收尾提交事实。 |
| `aggregation` | 非 aggregation 为 null；aggregation 返回 `graph_result`、`reply_disposition`、`reason_code` 及 `evidence_state=known|unknown`。只采用已证明字段，值沿现有领域枚举，缺失为 null；不把 completed 推断成有草稿。 |

持久记录状态从既有 Run evidence 收尾证据中提取不读正文／trace 的共同判据，普通 evidence 也复用该判据。缺失、坏 JSON、非对象、字段矛盾或不可识别证据不能仅因原字符串 truthy 就变成 recorded；兼容现有已支持的历史收尾格式，不要求旧记录补写新 schema。重启 interrupted＋无收尾证据继续为未知。记录来源判定不以“成功取到正文”代替。

活动／同进程未保存终态沿 Host immutable snapshot 和原可定位性判据；无法交接、不能提供 Web 详情的失败仍由全局记录服务说明。合并必须先核验实例与 Run，再在 Host 内比较 `state_revision`；持久已证明 recorded 不被较早 pending 降级。`activity_revision` 排持久行、SSE seq 排事件、`state_revision` 排宿主状态，三者不互比。

**新增** `GET /api/overview/recent-runs`，必需 `process_instance_id`。返回 I00、`expires_at`、`display_limit=5`、最多 **6** 条可定位的全用途持久终态 `runs`，按 `(activity_revision, run_id)` 降序，且不含 prompt_preview／正文／trace。第六条仅为与至多一个当前槽去重后的五条显示候选。当前槽内容始终来自同一壳层 Stream，不由此请求重新定义。

S04 在一次成功读取时按完整 ID 排除当前槽并固定至多五条；不足如实显示。普通事件更新当前槽和新数据提示，不重新读取、重新排序或把未读新终态插入持久历史。当前槽释放后，原先未纳入历史的记录要等显式刷新。S03 普通 Run 列表仍默认 25 条及原 opaque cursor，不能拿列表长度作为全部运行数。

## I04：来源锚点与有界返回

新增定位与现有分页共享查询／脱敏／排序，而不调用“下一页直到找到”。除明确为单条精确定位外，页面大小沿现有 HTTP 规则：默认 25、最高 100，复用同一 ASCII 参数解析和夹取行为，不另建不一致解析器。

| 入口 | query／参数 | 成功返回 |
| --- | --- | --- |
| 扩展 `GET /api/runs/locate/<encoded-run-id>` | 可选 `filter` 与 `limit`；迁移客户端附实例。省略 filter 保留旧自动选择目标分组行为；指定时必须保留该来源筛选。 | 原 RunPage＋I00 增量身份＋`target`。filter=all 时不被目标分组改写。 |
| 新增 `GET /api/sessions/locate` | 实例、`session_id`、可选 `limit` | 原 SessionInboxPage 形状＋`target`，目标在稳定页或活动槽。 |
| 新增 `GET /api/sessions/messages/locate` | 实例、`session_id`、`anchor`、可选 `page_size` | 原 SessionMessagesPage＋`target`，保持 runs／runs_pending／historic 分段。 |
| 新增 `GET /api/sessions/runs/locate` | 实例、`session_id`、`run_id`、可选 `limit` | 原 SessionChatRunsPage＋`target`；仍只包含获准 chat／aggregation。 |

`target` 至少含目标 `anchor`、所属身份以及 `placement=page|pinned|waiting`；它只描述当前位置，不是凭证。返回定位页包含目标及最多 limit−1 条最近的较新邻项，按该列表原顺序输出，并签发与普通读取兼容的下一段 cursor。稳定排序取原有活动序号／行 ID，不用显示时间、标题或文本排序。活动对象没有稳定历史位置时落在原活动槽；不得生成虚假历史 cursor。

在普通 Session 消息与 MainBar 历史 DTO 上增量增加 `message_anchor`：以原持久行身份、Session 和分段编码的版本化 opaque 值。Run pair 的逻辑身份仍为 Session＋Run；旧 historic message 使用实际行身份，两个相同文本的旧消息得到不同锚点。无数据库 schema 变化，无正文写进锚点；解码后仍核对对象、Session、分段及当前存在性，不能跨 Session 使用。

历史段前仍有可记录 Run 时，定位不能穿过 runs_pending 把未知邻居冒充连续内容：返回已知目标身份及 `placement=waiting`、原等待游标／状态，遵守既有释放事实，收到记录落定后提示可继续；无密集自动重读。锚点损坏／类型或 Session 不符 `400 invalid_anchor`；目标不存在为原 `unknown_run`／`unknown_session` 或 `404 source_target_unavailable`；指定 filter 排除目标返回 `409 source_filter_mismatch`，保留用户来源筛选，不偷偷改成其他列表。

客户端 `SourceContext` 仅保存安全路由、来源列表筛选、Session／Run／anchor、分区、Ops snapshot 身份、必要非正文滚动锚点。返回先重新核验原来源，再使用一次定位读取恢复目标；允许因新记录位置变化而给出新位置，但不遍历前置页。目标失效明确说明并提供显式回列表动作，不能以另一条记录替代。游标按所属列表／对象管理，不能跨分区或 Ops 快照使用。普通进入详情不改变 MainBar 所选 Session。

## I05：MainBar 精确正式记录读取

**新增** `GET /api/mainbar/locate`，必需 query 为 `process_instance_id`、`session_id`、`run_id`。一次只取该目标记录及必要身份；不存在前页数量相关的网络循环。绑定严格相等的完整身份；不接受短 ID、文本搜索或“最近回复”替代。

| 成功字段 | 形状与含义 |
| --- | --- |
| I00 信封及 `session_id`, `run_id`, `purpose` | 仅可证明属于该 Session 的 chat／aggregation；不新建会话，系统 Run 无此动作。 |
| `item_key`, `activity_revision`, `created_at` | 稳定的 Session＋Run 记录身份、可证明持久位置和时间；未持久时后两项可以 null，不捏造排序。 |
| `source` | `recorded_pair | unrecorded_projection`，说明正文来自持久记录还是当前实例有界终态投影。 |
| `user` | `availability=full|preview|unavailable`，`blocks` 为已有安全 block 投影或 null，`preview` 为安全预览或 null。未保存投影只有请求预览时明确标预览，不新持久化完整输入。 |
| `reply_text`, `reply_disposition`, `skill_notice` | 复用正文恢复完整文本与 notice 合同；无回复时 text=null 且 disposition=no_reply。保留 aggregation 独立事实，不用正文非空反推整个 Run outcome。 |
| `recording_state`, `recording_source`, 可选 `state_revision` | 沿 I03 已证明元数据；正文取得不能清除 pending／failed。 |
| `history_contiguous` | 固定 false；这是一处精确定位，不证明前后历史已读。响应没有普通历史的 next_cursor。 |

读侧复用现有 `recover_reply` 能力：先读取匹配的 immutable 未保存投影，命中不阻塞在保存所持数据库锁；未命中才按确切身份读取已获准终态持久记录。用户消息与已保存 pair 的投影复用现有安全 block 规则，正文编码／脱敏不长期占用读取锁。保存与读取交错时允许投影内容和后来持久元数据在同一身份上汇合，不能声称它们是一个数据库事务；无法证明一致身份时失败。完整正式回复不因“有界”被截短；界限是目标数量、查询路径和既有正文恢复保护。

| 结果 | HTTP／显示责任 |
| --- | --- |
| 身份匹配、正文可读或权威 no_reply | 200；后者保留用户请求和 Run 事实，不制造助手气泡。 |
| 新实例 | 409 `reply_context_expired`；先同步，之后已保存目标可重新读；不承诺未保存文本跨进程恢复。 |
| Session／Run 不存在、不属于该 Session 或非会话用途 | 404 `reply_target_unavailable`；不回退到别的对象。 |
| 明确被脱敏边界 withheld | 503 `reply_withheld`，带 `reply_disposition=reply_withheld`；可呈现安全的目标元数据，不把 trace 当替代正文。 |
| 记录尚无可读正文、存储失败或正文损坏 | 503 `reply_unavailable`；显示正文未完整加载，重试仅只读。它不证明未保存、no_reply 或业务失败。 |

旧 `/api/reply` 的形状、错误和恢复用途保持；新 endpoint 不是第二份正式回复，也不让旧接口开始返回历史游标。S02 通过一个 MainBar 所有者将定位结果、普通历史、SSE 正式结果和恢复合并到同一个 `item_key`。相同文本不同身份不去重；同身份旧 pending 不能降级已确认记录。新的片段可补足尚未读取内容，不能因重复到达增加新回复提示。

MainBar 拥有独立的 `locationTarget` 和普通 `historyCursor`；有缺口时明确显示历史定位及“回到最新”。在定位处收到新回复只更新原所有者与提示，不强制滚动。回到最新清除定位呈现、保留合法普通历史与去重状态；它不声称全部历史已加载，也不重发。刷新 Run 深链接不自动重新执行跨 Session 定位。

## I06：壳层导航与页面生命周期端口

S02 冻结下列逻辑端口。名字可用现有代码命名风格实现，但输入、结果、时序和所有权不能因私有拆分改变；页面不得绕过它们直接抢写主路由或重复建立 MainBar。

| 端口 | 输入／输出 | 责任 |
| --- | --- | --- |
| `navigate` | 目标同源 URL、意图（普通／返回来源／历史）、可选 SourceContext；结果 `applied|unchanged|cancelled|unavailable` | 校验目标→判断同一地址→准备目标→询问会丢失的输入→提交路由／历史→释放原页面→挂载并按源恢复。取消不退休原有效请求。 |
| `openPanel`／`closePanel` | `navigation|mainbar`、显式触发器与可恢复焦点身份 | 管理单层面板历史、互斥、可操作区域和关闭恢复；仅改呈现，不调用 page dispose 或 Stream connect。 |
| 页面 `getLeaveState` | 目标／原因→实际 dirty 输入范围、可读摘要及已提交动作状态 | 页面判断是否会丢输入，不把所有非空字段当 dirty；提交中动作与未提交输入分开。 |
| 页面 `setVisible` | 可见布尔、布局上下文 | 保持实例、DOM／草稿和保护订阅；隐藏区域 inert／不在焦点链。 |
| 页面 `captureSource`／`restoreSource` | 返回／接收 SourceContext，restore 返回已恢复或明确失效 | 只存安全非正文状态；经当前真实读取后恢复锚点，不能存通用结果缓存。 |
| 页面 `dispose` | 实际离页／文档卸载原因、页面 generation | 退休读取及监听并启动原服务收尾；清理在途对象移交既有服务所有者，不能把 Promise resolve 冒成取消完成。 |
| 页面保护通知 | 当前实例、Memory revision、连接／文档生命周期事实 | 原资源协议继续处理，包括隐藏时；不是统一十五分钟过期钩子。 |

首次壳层同步取得入口凭据及 Host 状态后再读页面。只有一份 page registry 决定标题、导航组、路由与 active 项；从第一片起维护真实旧九页入口。S02 适配所有现有页面的 dirty／visibility／dispose 边界，即使页面内部尚未重排；S05–S10 不能被迫各写第二套导航。

跨页链接、品牌入口、程序跳转和浏览器历史统一进入协调器；Ctrl／Cmd／新标签及非应用导航保留浏览器语义。对于 popstate 已先改变地址的情况，取消时恢复原有效历史位置并抑制仅由本次恢复引起的重入；不能追加一串重复记录或在取消后才清理原页。面板层优先消费，跨页确认后消除本壳创建的无用面板层，不篡改外部站点历史。

壳层对话阅读位置、新回复最小身份、宽屏偏好按标签页保存；history state 仅含版本化路由／呈现／非正文恢复信息。`alfred.expanded` 的旧值不直接等同新偏好；没有新宽屏偏好时默认展开，窄屏刷新中央。存储不可用保留内存草稿并提示无法保证刷新恢复。

## I07：MainBar 动作与 Stream 端口

| 动作 | 合同 |
| --- | --- |
| `selectSession`／新建／继续 | 只来自明确用户操作，经原忙态和未收尾守卫；切换前保留原标签页草稿。普通预览／Run 导航不调用。 |
| `locateReply` | 接收完整实例／Session／Run 与显式动作身份；跨 Session 按明确文案和守卫切换，绑定切换后的 generation，再调用 I05。被阻止返回原因且保留原输入。 |
| `returnLatest` | 只切 MainBar 阅读位置，不修改 Session、Run、普通 cursor 或生成提交。 |
| `subscribeState` | 页面订阅壳层的只读状态与保护通知，拿到自己的 unsubscribe；不能取得第二个 EventSource 所有权。 |
| `submit` | 沿原发送准入协议绑定当次 Session＋文本，Enter／Shift+Enter／IME 语义不变；受理未知不自动重试，202 不清除在途新增输入。 |

SSE 连接、generation、frame 队列、重放和正文恢复继续由既有 Stream／MainBar 所有者驱动，不更换 wire protocol。跨页、显隐、列表刷新和精确定位不建立第二个连接；显式 Session 切换及真实重连仍按原替换规则释放旧连接。

`locateReply` 仅在用户动作仍有效时开面板并将焦点移到目标／标题。请求返回时如果目标、Session、页面、实例或焦点意图已改变，可按仍有效身份接收内容，但不得再切 Session、覆盖输入或夺焦；已退休读取不得写入。普通异步状态不重开手动收起的组。中文 IME 确认 Enter 不是提交。

## I08：页面保存、回执和保护交接

此接口不是新的通用命令服务。各页保留现有 command、expected revision、operation ID、准入和回执读取；壳层只询问 dirty 与离页责任。

| 所有者 | 隐藏／折叠 | 真实离页／替换 | 仍须继续的事实 |
| --- | --- | --- | --- |
| Memory 编辑／维护 | 保留有效输入；照常接收 revision 失效 | 按目标守卫；退休页面显示请求 | 已提交 operation 的回执／显式恢复仍归原身份，遗忘和清理不得丢。 |
| Behaviour 拓扑／统计／聚合 | 原来源继续失效；有效草稿保留 | 图与统计各按原关闭协议；未提交输入先守卫 | 已提交聚合固定目标，资料保护在发送前复核，迟到结果不改投。 |
| Tools 授权 | 保留草稿与异常摘要 | 目标改变经守卫 | 以完整 source identity＋revision 核验保存／生效，退休不恢复权限。 |
| Models／Connections | 保留在途后新增编辑和各组状态 | 独立设置目标守卫、退休显示请求 | 保存／探针／MCP 清理保留原目标；失去响应不自动重送。 |
| Ops | 仅按各源协议保留允许的财务旧值／正文 | 取消本页读取，来源 snapshot 身份可作为非正文返回上下文 | snapshot 过期与历史正文、当前记忆核验分开。 |
| Database | 不因开 Chat 清 SQL／结果；保护变化仍立即清 | 按既有生命周期清 SQL／结果、取消／释放执行 | worker／handle 负责实际 cleanup，未释放状态继续限制新查询。 |
| Run Graph／trace export | 不因折叠／开 Chat 取消 | 按原服务协议释放本页请求／导出任务 | 已开始原生下载按原边界运行，传输完成不冒本地文件已保存。 |

S08 修复现有设置命令内部的“未指定”与“明确清空”混用：不修改公开命令名称，显示名 null 清空，单维价格 null 移除该覆盖，最后一维移除后清掉整个覆盖；空输入不是数值零，显式 0 必须保留。内部可采用独立 UNCHANGED 哨兵或直接替换状态；既有未指定参数调用仍保留旧值。验证必须重新打开持久 store／重新 GET，而非只看表单空白或成功响应；旧已创建账目 snapshot 的价格解释不随之改变。

## I09：兼容和资源登记

- 旧九路由、Run 深链接、合法 query、已有 API 消费者、Session 草稿和正文恢复继续可用。新增路由与静态资源应同时登记服务、包资源及 HTML／import／CSS 引用闭包。
- S02 仅建立新壳；S04 注册真实 Overview 页面及 `/overview` 服务入口；S11 在最终候选以 replace 语义规范化 `/`→`/overview` 并更新品牌入口，不增加一次 Back。
- 生产没有 CDN、外部原型运行时或演示数据。资源 MIME、CSP、no-store、nosniff 及实际包内字节通过安装后 HTTP 核对。
- 不向 Ops 额外扩充逐 Run 金额或 recording_state 协议来迎合共享卡片。公共视觉只能呈现各源已有事实，来源缺少字段时不能借另一源推断。
