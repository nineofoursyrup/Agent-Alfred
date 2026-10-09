# #90 页面与业务路径事实核验

核验日期：2026-10-08（Asia/Shanghai）。产品源码基线 `22c8720e1ec874fd012cc79b086cd8aace4c79b6`；继承的 #86–#88 提交只增加设计材料。

本文是源码、现有文档和测试入口的静态调查，**不是本轮运行测试所得的 PASS，也不是用户已批准的布局设计**。

## 参考与已冻结设计

- 原始参考：`/Users/nineofour/Downloads/Syrup agent repository/Syrup Dashboard.dc.html`，本轮重算 SHA-256 为 `54807adb38541fa2966e5b0c334b9d0e8d71c60b814245601bc4f35b8ec54b84`，与 #85 一致。可交接副本继承自 [#86 原稿档案](../issue-86/reference/)。本轮只读文本，没有执行 `support.js`。
- 原稿 Memory 使用语义／情景／Skill／SOUL 四标签、硬编码计数、正文行与编辑文字，以及静态提炼历史（原文件 371–424、819–838 行）。其「都可以直接编辑」不符合当前 Skill 和人格入口事实。
- 原稿 Graph 是包含 `CHECK CALENDAR` 的示例拓扑和示例统计（426 行起）；不能用它替换实际发布的消息分流／手动聚合图。
- 原稿 Tools 是来源分组、紧凑工具项与硬编码调用次数（549–575、853–875 行）。示例 `update_soul`、`read_calendar`、`write_note` 等名称不等于 Agent-Alfred 的真实能力目录。
- 视觉使用 [#86 决定](../issue-86/DECISIONS.md) 与封存基线，不能重新导入原稿的低对比文字、执行动画和未知伪零。壳层使用 [#87 规格](../issue-87/SPEC.md)，包含显隐不离页、真正离页守卫和保护内容核验。

## Memory

| 真实路径／状态 | 当前事实与迁移边界 | 证据 |
| --- | --- | --- |
| 页面分区 | 语义、情景、Skill 三标签，`?tab=` 与方向键有效；下接操作回执、检索统计、提炼队列、Markdown 镜像 | `ops/static/memory.js:1884–1963` |
| 保存与搜索 | 两种记忆字段各异；FTS、主题／时间筛选、显示全部及每页 25 条；`saved` 与 `already_exists` 不混称新建 | `memory.js:691–895,1146`；[实现说明](../../implementation/issue-46-memory-page.md) |
| 真实保存路径 | 手填走共享 MemoryService，忙时 409、不排队、不另创 Run；修改绑定版本 | 同上；`memory.js:1042–1175` |
| 来源与使用 | 创建来源／最近修改来源、版本、保护可见；来源组到 Run，提炼来源到批次；明确无来源与关联未知分开 | `memory.js:219,895–976` |
| 检索证据 | Run 输入分开显示召回／选入与 Attempt 实际携带；当前正文须重读且区分同版本、已修改、不存在、读取失败 | `ops/static/runs.js:507–639` |
| 人工保护 | 手填／工具保存和修改产生保护；当前页面展示保护，没有解锁开关。提炼改写受保护记忆需批准证据 | `memory/storage.py:109,207` |
| 编辑冲突 | 保留草稿并比较当前版本，显式确认后才换基线；确认删除清草稿，读取失败不当作删除 | `memory.js:1042` 起 |
| 删除与遗忘 | 确认绑定显示版本；删除成功与 `cleaning / failed / needs_scope / complete` 分开。范围待确认时并存的清理失败仍须可见 | `memory.js:532,978`；[遗忘实现](../../implementation/issue-45-forgetting.md) |
| 删除后边界 | 不回显删前正文；受管副本清理、来源及后继自动使用隔离；原历史与独立记忆不自动抹除。旧回执不改写，当前进度可重新进入待处理 | [ADR-0007](../../adr/0007-forgetting-must-be-real.md) |
| 未确认命令 | 无响应或不可信成功响应可为结果待确认；同 `operation_id` 核对／恢复。必要未确认请求按原协议保存，不能泛化为所有正文持久缓存 | `memory.js:342` 起；实现说明「请求、回执与恢复」 |
| 断线／修订失效 | `offline / verifying / online / unverified`；旧正文、编辑内容、候选差异与镜像预览隐藏，核验后恢复；旧实例／旧修订响应不得复活被删内容 | `memory.js:38` 起；[实现说明](../../implementation/issue-46-memory-page.md) |
| 提炼队列 | 会话阈值／超限阻塞与跳过、批次、候选差异、整批批准／拒绝／重试。重试有提交恢复和新模型调用之分，不能变成含糊统一按钮 | `memory.js:1551` 起 |
| 检索统计 | 全部／当前会话范围、S/H/M/E 比例与排除数；缺证不能显示为零命中或收益 | `memory.js:1319` 起 |
| Markdown 镜像 | 单向派生；预览、同步重试、外部修改后的显式重建。同步失败不否认数据库保存 | `memory.js:1768` 起 |
| Skill | 只读启动快照、名称／描述／来源／覆盖标记／正文；文件改动或 `create_skill` 后需重启加载。没有页内直接 CRUD | `memory.js:1253–1306`；[Skill 规格](../issue-24-skill-spec.md) |
| 人格 | 没有 Persona 页面、Memory 标签或专用 HTTP 编辑器；Tools 展示 `read_persona`／`update_persona`，MainBar 普通 Run 调用。完整替换、版本冲突、下一 Run 生效与显式文件覆盖限制保留 | `tools/persona.py:36–99`；`runtime/host.py:489–524`；`ops/static/tools.js:18–46` |
| 长内容 | 当前正文直接渲染为文本／pre，非独立阅读器；公共 textarea 高度上限 180px。列表摘要与展开属于本票待决定的呈现 | `memory.js:881` 起；`ops/static/app.css:196` |

回归入口：`tests/browser/memory.spec.js` 的保存→检索→真实 Attempt→来源→冲突→遗忘整链（109 行起），以及提炼、镜像、遗忘和 Skill 对应现有用例。静态发现这些入口不等于本轮重新执行。

## Behaviour

| 真实路径／状态 | 当前事实与迁移边界 | 证据 |
| --- | --- | --- |
| 两类操作 | 消息分流是持久设置；手动聚合是显式单次请求。两区各有默认收起的只读流程，路由统计属于消息分流区 | `ops/static/pages.js:711–772`；`aggregation.js:55–154` |
| 分流设置 | 已保存值与待提交 checkbox 分列；保存失败保留选择。版本冲突、磁盘外改、配置损坏和不可读分别显示 | `pages.js:738–767`；`runtime/behaviour.py:41–93` |
| 配置恢复 | 有指纹时显式「备份原文件并恢复为关闭」，不以普通刷新自动修复 | `pages.js:725,740–744`；`runtime/behaviour.py:95–147` |
| 聚合请求 | 目标 Session、目标、关键词、三类来源；全不选来源是合法 NoAction；提交冻结当次参数，之后编辑不改变在途 Run | `aggregation.js:55–154`；[聚合规格](../issue-27-manual-aggregation-spec.md) |
| 准入与结果 | 提交中、准入未确认、读取／起草、保存、未保存分开；响应丢失不自动重投，失败不自动补搜或回退普通 Agent | `aggregation.js:107–150`；[ADR-0039](../../adr/0039-aggregation-failure-does-not-enter-agent-fallback.md) |
| 草稿交付 | 内容属于指定 Session，Behaviour 展示同一 Run 的事实；不自动进入后续工作窗口、聚合或提炼 | [ADR-0038](../../adr/0038-aggregation-drafts-stay-out-of-automatic-sources.md) |
| 原资料查看 | 保存身份而非缓存被遗忘正文；读取当前条目且核对版本，离线／失效隐藏已显示资料。历史草稿可人工查看不意味着原资料仍可用 | `aggregation.js:18–45`；`aggregation/views.py:6–30` |
| 当前拓扑 | 只读真实发布图，只有 message_routing / manual_aggregation；不表示具体 Run 实际路径。保留节点／边文字、图例、选择、缩放、平移、适应全图 | `topology.js:103–271`；[拓扑实现](../../implementation/issue-75-topology.md) |
| 拓扑刷新 | 手动／展开读取，无轮询；失败保留标旧快照，明确无图撤旧图；同结构保留视口与选择，变结构重置 | `topology.js:273–321` |
| 统计刷新 | 范围／刷新／收起／断连／换实例均清旧结果，重开单次读取，无轮询；不能套用拓扑保留旧图的规则 | `routing-statistics.js:35–69` |
| 统计语义 | 按版本组，比例保留分子、分母与覆盖；零分母不是 0%。故障回退、图内保守兜底、图前绕行及上下文恢复不混称 | [统计说明](../../routing-statistics.md)；[ADR-0042](../../adr/0042-routing-statistics-use-durable-evidence-and-semantic-versions.md) |
| 历史执行路径 | 从 Run 的当时证据查看；trace 缺失后不得用当前拓扑补齐历史 | [ADR-0043](../../adr/0043-run-path-evidence-shares-trace-retention.md) |
| 未提交表单 | 目前仅页面 DOM 内存；原导航重建会丢，不等于 MainBar 已有 sessionStorage 草稿。迁移须接 #87 已批准离页守卫 | `app.js:592–609`；[#87 R06](../issue-87/SPEC.md#r06实际离页与未保存输入) |

回归入口：`tests/browser/routing.spec.js`、`aggregation.spec.js`、`topology.spec.js`、`routing-statistics.spec.js`。特别保留延迟 202 后改表单、遗忘原资料、图／统计观察不保存表单的现有行为。

## Tools

| 真实路径／状态 | 当前事实与迁移边界 | 证据 |
| --- | --- | --- |
| 目录 | 动态真实注册能力；默认本地工具、Tavily，及配置发现的 MCP。不能按示例工具名写死目录 | `runtime/host.py:519–540,2336–2373`；`tools/__init__.py:307–346` |
| 当前字段 | 身份、来源、名称、描述、副作用、可用性、暴露及原因，外部连接观测，MCP 元数据；目录没有逐工具调用数或 schema 编辑接口 | `ops/static/tools.js:24–46`；注册表投影 |
| 页面职责 | 查看能力和授权；从 MainBar 提出工具请求；逐项链接 `/ops?tool=<identity>` 查看包含该工具的运行 | `tools.js:18,45–46` |
| 正交维度 | 本地工具不需要外部授权；外部可用性与 unset / allowed / denied 分列。配置可读、保存值与应用状态继续独立 | [ADR-0005](../../adr/0005-availability-and-authorization-are-orthogonal.md)；`tools.js:24–43` |
| 授权草稿 | 按完整能力身份保存，带基线 revision；逐项显式保存，冲突显示原基线和当前值，显式选择基于当前版本继续编辑 | `tools.js:47–65` |
| 草稿生命周期 | 草稿仅在组件内 Map，离开后重建组件不承诺恢复；迁移承接 #87 离页守卫，不误称已持久保存 | `tools.js:19,113–124`；`app.js:592` 起 |
| 保存与生效 | 回执后再次核对；可以已保存未生效，另有重新应用已保存授权；不能只因 HTTP 成功就显示已授权可用 | `tools.js:81–114` |
| 未确认／离线 | 不自动重送；读取失败、离线保留草稿并提示待核验。迟到请求不能覆盖新读取；真实离页关闭请求所有者 | `tools.js:70–124` |
| 外部修改与历史 | 磁盘版本另列，禁止覆盖；在途操作保留原政策；MCP 历史条目标明不可调用，不冒充新来源或继承旧授权 | `tools.js:27–35,55–64` |
| MCP 汇总 | 可用 N／总数 M 是连接和 Schema 可用性计数，不是已授权工具数；已连接工具仍可能对模型隐藏 | `mcp/__init__.py:552`；`tests/browser/mcp.spec.js:20` |
| 到 Ops 的身份与范围 | 按稳定工具身份查看包含该工具的完整 Run，同 Run 其他工具仍在；模型费用仍属整个 Run，不归因给单个工具 | `ops/static/accounting.js:50,134` |
| 调用信息 | 请求数与启动确认分列，调用结果与后续原操作核验分列；unknown 不等于失败。查看核验记录不会执行恢复或重放工具 | `accounting.js:4,68,144`；`runtime/host.py:2475` |
| 历史正文与返回 | Ops 保留模型投影、脱敏审计、模型提交参数的不同入口和分段读取；到 Run 携带原 snapshot 与筛选，失效需显式刷新，不静默换账 | `accounting.js:65,119,141–160` |
| Connections 边界 | 连接／凭据探测、Tavily usage、MCP 应用配置／重连／清理在 Connections；Tools 仍只管理逐工具业务授权 | `ops/static/pages.js:333,358`；[Tools/Ops 规格](../issue-32-tools-ops-spec.md) |

早期 #55 文档中「尚未接 MCP」不是当前代码事实；本轮以现行 Host 注册链和页面/API 为准。调用记录仍属于 Ops 与 Run 的真实账目、请求／启动、结果与后续核验路径，不能用目录装饰数字代替。

回归入口：`tests/browser/accounting.spec.js` 的授权、两标签冲突、掉回执与应用失败路径，以及 `mcp.spec.js` 的连接／授权分离。Ops 和 Connections 的既有路径作为本票跨页回归约束，不在 #90 重新设计那两页。

## 待设计事项与术语处理

- 调查后完成两轮访谈：首轮确定三页信息层级，第二轮确认详情、长内容、页内切换／折叠、统计图形和状态摘要；当前设计与回归矩阵见 [DESIGN.md](DESIGN.md) 和 [ACCEPTANCE.md](ACCEPTANCE.md)。本文件仍只记录调查事实。
- 「流程拓扑」「本次执行路径」「聚合草稿」「人工保护」「遗忘」「可用性」「授权」「授权生效状态」均沿现有领域定义；不将图样式、统一卡片或图标转换为新业务含义。
- 当前核验未发现必须新增产品能力才能继续本轮视觉决策的缺口。人格编辑器、工具次数摘要、直接图编辑等若被明确提出，应作为范围变化单独裁定。
