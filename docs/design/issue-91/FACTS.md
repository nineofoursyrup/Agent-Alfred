# #91 四页现状核验

日期：2026-10-08（Asia/Shanghai）。产品基线 `22c8720e1ec874fd012cc79b086cd8aace4c79b6`，文档父提交 `7d175d6a65a93aec725cf86e6be3df167d3ca74c`。

本文是源码、既有文档与测试入口的静态调查，**不是本轮运行测试所得的 PASS，也不是用户已批准的设计**。已确认的推荐与依据见 [DECISIONS.md](DECISIONS.md)。表中源码路径相对 `src/agent_alfred/`；测试路径相对仓库根。

## 来源与既有决定

- 已读取 #91 正文和父地图 #85 当前正文；#91 没有既有 resolution。原生前置 #87 已 CLOSED/completed，`blocked_by=0`；认领后回读 assignee 为 `nineofoursyrup`，状态仍 OPEN。
- #86、#87、#90 的远端 resolution 已读取。当前工作区包含固定归档的 [#86](../issue-86/DECISIONS.md)、[#87](../issue-87/SPEC.md)、[#88](../issue-88/DECISIONS.md) 与 [#90](../issue-90/DECISIONS.md)，不从未完成的兄弟票猜测决定。
- 参考副本 [syrup-original.html](../issue-86/reference/syrup-original.html) SHA-256 本轮重算为 `54807adb38541fa2966e5b0c334b9d0e8d71c60b814245601bc4f35b8ec54b84`，与父地图一致；仅作为文本读取，未执行其运行时。
- 原稿 Ops 498–547 行包含静态测试／裁判／发布卡片、慢运行与费用图；原稿 Database 573–609 行为对象侧栏、SQL 示例和固定结果。没有 Models、Connections 完整参考页。原稿不提供真实数据合同。
- 项目用 [CONTEXT.md](../../../CONTEXT.md) 承担词汇表，由 [领域文档约定](../../agents/domain.md) 指定；不另建重复词汇表。
- #87 的中文导航、三栏壳层和未保存输入守卫是已确认设计，尚未实施于当前产品；当前导航仍有 Ops／Database 等旧文案，路由直接释放页面。后续采用这些名称与守卫是迁移目标，不是本轮观察到的运行事实。

## Models

| 现有事实 | 证据与迁移含义 |
| --- | --- |
| 按端点列候选，支持目录展开／刷新、钉选／取消、主模型／辅助位置指派、显示名、线路形状和四维覆盖价 | `ops/static/pages.js:454–709`；`candidates.py:78–174`。现 UI 的辅助位置文案为“检索门”，实际同时服务 Skill 自动选择、记忆检索门与消息分类 |
| 指派、钉选、支持结论、支持依据、线路出处各有字段 | `candidates.py:118–174`；不能把候选来源 `sources` 当作账目逐维 `price_source` |
| Models DTO 只给用户覆盖价，没有四维有效报价预览 | `candidates.py:133–155`；覆盖价单位和留空／显式 0 的区别来自 [#29 决定](../../acceptance/sources/issue-29.md) 第 13 节 |
| 连接四态、支持三态、目录健康四态正交 | [ADR-0021](../../adr/0021-assignment-and-evidence-semantics.md)；连接失败不阻止合法指派，用户手选线路仍为未验证 |
| 端点头已有连接状态／观测时间／方式，以及目录的上次成功／错误／重试时刻 | `pages.js:509–551`。连接的 observation.reason 当前漏渲染；候选来源、支持依据和当前指派亦可由现有 DTO 补足可见表达，不需靠样例填值 |
| 初次目录只读取主模型所在端点，其他端点懒加载 | `runtime/host.py:1258` 起、`catalog_schedule.py:45` 起；无凭据零请求，同端点在途合并。`catalog.py:104` 起允许 catalog_url 为空时回落到 base_url 的目录路径，空值不等于无目录能力 |
| 设置写入携带 expected_revision，并另有磁盘指纹冲突检查 | [ADR-0022](../../adr/0022-dual-conflict-check-for-settings-writes.md)；陈旧标签页与外部改动不得静默覆盖 |
| 外部改动后仍使用内存 last-known-good，当前模型设置没有通用重载入口 | `runtime/model_settings.py:273–347`、`runtime/host.py:1358–1420`；“重新读取 .env”重载凭据，不解决模型设置文件的 external_change |
| 当前提交粒度不同 | `pages.js:618–673`：显示名显式保存，线路形状和各维覆盖价在 change 时提交。不能把“整行保存”写成已实现事实 |
| 已有覆盖价清空最后一维存在实现落差 | `pages.js:661–666` 空输入 → `host.py:1401–1409` 的 None → `settings_commands.py:147–157` 全空 override=None → 同文件 `65–69` 的 pin 将 None 解释为保留旧覆盖。留空的既定合同仍有效，本轮仅静态发现，未执行复现或修复 |
| 已有显示名清空存在同根实现落差 | `pages.js:641–645` 空输入 → `host.py:1394–1400` → `settings_commands.py:109–123` → 同文件 `60–64` 的 pin 保留旧名。本次检索未找到这两条“已有值→清空”回归；`test_settings_commands.py:44` 起的初始缺省／显式 0 用例不覆盖清空 |
| 当前错误反馈存在缺口 | `pages.js:462–479`：mutation 只有 response.ok 时 render，非 2xx 没有展示分支；`561–565,593–596`：部分禁用原因只放 data-*。已批准的错误可见要求仍需迁移实现补齐 |
| 真实推理探针属于具体已指派模型行，可能产生费用 | `pages.js:674–702`；当前函数发 POST 后未解释回执。回执、忙态与结果反馈已由 Q7 确认，产品未实现，不能自动重试付费动作 |
| 推理探针成功受理返回 202 和 Run 身份；拒绝／不可用另有明确结果 | `gateway/web/api.py:608–695`；可信 run_id 可进入原有 `/runs/<run_id>` 读取，无回执不能猜测哪条 Run 就是本次测试 |
| 非 202 不统一等于“没有任何持久记录” | `gateway/web/api.py:649–695` 的 admission_failed 可覆盖已持久受理但移交失败路径且不返回不可达 Run 身份；会话记录只由 chat／aggregation 写，推理探针仍保存自身 telemetry／usage（`runtime/recording.py:580` 起） |
| 不应擅自补写不存在的操作 | 当前 style 下拉选回空值不会发 mutation（`628–635`）；不能声称已有“恢复内置线路”路径。手工新增任意模型和清空辅助指派也不能凭视觉推定为既有 UI |

回归入口：`tests/browser/settings.spec.js`；模型设置与候选的确定性用例。旧实现的反馈缺口不降低 #86、#91 要求，当前仅记录，未修改产品。

## Connections

| 现有事实 | 证据与迁移含义 |
| --- | --- |
| 一个页面包含模型端点、可选集成与 MCP；有重新读取 .env 的全局动作 | `ops/static/pages.js:160–450`。三类对象动作不同，不能统一成“连接全部” |
| 端点地址、目录地址与密钥配置状态只读 | `connections.py:153–175` 已返回 base_url、catalog_url、api_key_env；当前页面 `275–330` 尚未完整展示这些只读详情。密钥从环境与启动时固定 .env 路径读取，页面只有显式重读，没有密钥编辑框 |
| 连接观测与目录健康分开，目录读取不证明认证成功 | [CONTEXT.md 模型接入](../../../CONTEXT.md#模型接入)；无免费认证探针时明确原因，不临时调用付费模型代替 |
| 模型推理测试在 Models 的具体模型行，凭据测试属于 Connections | `pages.js:275–330,674–702`；开页不自动探测 |
| 生产六端点目前没有声明免费认证探针 | `endpoints.py:130` 起；通用能力和注入测试存在，不代表当前真实端点可探测。可选集成测试另遵其原有费用说明；不得一概称免费 |
| 可选集成有独立配置、凭据探测与可获得的用量信息 | `pages.js:333–357`；现有 Tavily 路径不代表未来所有集成都有同样字段 |
| MCP 有配置状态、服务器启动许可、连接／历史观测、工具发现与维护操作 | `pages.js:358–423`；启动许可不等于逐工具授权；业务授权归 Tools |
| 重读、恢复焦点、跨标签通知与迟到响应各有身份约束 | `pages.js:160–450`；新布局需保留原读取／mutation 的归属，反馈不能覆盖较新的事实 |
| 各服务的操作完成不等于当前就绪 | `.env` 失败及回滚失败可为 not_applied；受影响 MCP 为 restart_required。Tavily 400／422 保留旧观测但更新 last_attempt，探测／搜索 429 分开；缓存回执不证明新探测。MCP completed 不保证每服务器连接或清理成功；同操作重试复用 operation_id/token/payload（`host.py:1103` 起、`integrations.py:308` 起、`runtime/mcp_control.py:60,166` 起） |

回归入口：`tests/browser/settings.spec.js`、`integrations.spec.js`、`mcp.spec.js`。这些测试名称是静态找到的入口，未在本轮运行。

## Ops：用量账本

| 现有事实 | 证据与迁移含义 |
| --- | --- |
| 筛选 → 固定账目快照 → 汇总与 Run 分页 → 单 Run 明细 | `ops/static/accounting.js:23–140`；时间、IANA 时区、Session、用途、工具身份及精确 Run 均保留 |
| 精确 USD、估算 USD、费用未知 Attempt、覆盖不足 Run 与 Token 缺失分开 | `accounting.js:68–79`；已知部分不是完整总额，未知不补零 |
| 当前宿主已接价格链和冻结分项 | `runtime/host.py:2026–2064`、`runtime/accounting.py:82–122,350`、`pricing.py:130–204`；已保存用户覆盖价 → 可获得的目录价 → 包内静态规则，估算给出逐维来源和元数据。`docs/dashboard.md` 末尾“当前未安装价格解析器”为旧描述，与当前调用链不符；本票依据现行实现，不修改该共享旧文档 |
| 工具计量按服务／单位独立显示；请求、已确认启动、明确未启动、启动未确认分开 | 同上；模型金额不能与 credits 相加。工具筛选包含完整 Run，模型费用仍属于整个 Run |
| 快照绑定规范化期间、价格、计量和实例；刷新才更新 | `accounting.js:5–20,92–134`；服务端到期拒读，当前前端收到 410 或实例变化后保留已加载财务内容并标旧，停止续页／未加载明细，刷新失败保留筛选；没有前端到期计时器 |
| 明细含 Attempt 费用、工具当次结果、独立当前核验和历史正文三种投影 | `accounting.js:134–170`：模型结果投影、完整脱敏审计、模型提交参数分别读取；当前核验不重写当次结果和快照金额 |
| 历史正文按段读取，只保留当前段；Run 入口携带账目快照与时间上下文 | `accounting.js:130–194`；断连保留并标离线，重连用 revalidate_cursor 核验，不可读时清正文；离开详情清正文，不能同财务快照统一缓存 |
| Run 列表没有 recording_state 或每 Run 金额；detail 顶层 summary 仍为全快照汇总 | `runtime/accounting.py:403–420`；单 Run 数据来自 run.attempts／tools，不能复用顶层金额冒充本 Run 费用。`runtime/host.py:2120` 起补充的 currently_registered 是当前目录读数而非冻结历史注册状态 |
| 没有原稿那种发布验收仪表盘 | 当前入口为真实账目；原稿 judge、threshold、68/68、release pass 不作为产品事实。新增发布证据区需要独立数据与合同决定，本票范围不含该新增能力 |

现行语义对照：[Tools/Ops 设计](../issue-32-tools-ops-spec.md) 的账目与历史部分、[工具计量 ADR](../../adr/0032-tool-metering-survives-trace-pruning.md)。旧文档当时“不接 MCP”等范围不覆盖后续已经实现的接入。

回归入口：`tests/browser/accounting.spec.js`，特别是旧分页迟到、快照过期、离线历史正文、从 Ops 到 Run 保留筛选和显式刷新。

## Database

| 现有事实 | 证据与迁移含义 |
| --- | --- |
| 目录和示例查询只填入 SQL，用户显式执行 | `ops/static/database.js:110–161`；对象来自真实批准目录，不使用原稿 facts/episodes 的示例行数。当前 `119–122` 直接覆盖编辑器；替换前保护已由 Q10 确认，尚未实现 |
| 用户 SQL 只查询预先保护的完整短命诊断数据集 | [ADR-0040](../../adr/0040-protected-diagnostic-dataset.md)；WHERE/LIMIT 不缩小准备范围，结果上限不等于数据库总量 |
| 编辑器、执行、取消、复制 SQL、复制结果是独立操作 | `database.js:31–58`；SQL 草稿和受管结果在页面内分别持有 |
| 执行已经取得当次 SQL，编辑器随后仍可编辑 | `database.js:233` 起的局部 sql 与 input 监听 `404–407`；结果 DTO 有 query_id 没有已执行 SQL，明确提交关联与草稿变化提示已由 Q10 确认，尚未实现 |
| 当前复制结果只复制已渲染当前页及区内说明文本 | `database.js:398–405` 的 result.innerText 不等于全部返回行或数据库导出；截断状态在结果区外，目前不会复制，且 rows.length 是本次返回总数而非当前页行数。Q10 要求本页复制连同说明，须补足准确行段和已有截断提示 |
| 结果有读取时刻、数据源、保护版本、覆盖和截断信息，类型保真 | `database.js:164–215`；每页 100 行为本地分页，NULL、BLOB 和整数不能渲染为同一类字符串或空值 |
| 句柄先签发，执行与取消绑定同一查询及页面身份 | `database.js:217–298,345–395`；迟到句柄先取消不执行，旧状态／取消不能覆盖新查询 |
| 执行结局、是否收到结果正文与实际清理是独立事实 | `database.js:300–331`；“已请求取消”不等于“已实际停止”，仅 cleanup=failed 表示清理失败 |
| 记忆／保护变化与断连清结果、留 SQL；真正离页清两者 | `database.js:410–458`；隐藏页面仍受保护失效规则约束，重新连接／显示不自动重跑 |
| pagehide／BFCache 恢复另有清理与重验规则 | 同上；壳层面板显隐不触发真实离页，也不暂停这些原有责任 |
| 资源限制由后端执行，目录已有 limits，但现 UI 未完整呈现 | `database_console/budget.py:28` 起、`service.py:291` 起；SQL 64 KiB、保护后输入 64 MiB、SQLite heap 128 MiB、结果 1000 行／64 列／2 MiB JSON，执行预算 5 秒，同实例一个活动查询。另有源单值／行、句柄期限与容量、取消清理和 HTTP IO 限制；摘要不能使完整约束失去入口 |
| 结果没有固定 TTL，句柄及终态记录期限与页面结果寿命分开 | `database_console/service.py:424–445,521–545,846–901`；30 秒仅未执行句柄，60 秒仅无正文终态记录。发送后不保留供重取的 SQL／正文，GET 状态只回查询身份／结局／cleanup，不能补取丢失正文 |
| limits 不包含所有内部预算 | `database_console/catalog.py:1121–1134` 已含主要大小、行列、分页、句柄和执行预算；取消清理及 HTTP IO 的 1 秒预算是后端合同常量，非当前目录字段 |

实现与资源／验证入口：[issue-66-database-console.md](../../implementation/issue-66-database-console.md)。该文档记录的历史测试或历史 BFCache 限制不能表述成本轮新验证结果。

回归入口：`tests/browser/database.spec.js`、`database_cancel.spec.js`、`database_status.spec.js`、`database_lifecycle.spec.js`、`database_native_lifecycle.mjs`，以及 `src/agent_alfred/evals/deterministic/test_database_*.py`。

## 本轮证据边界

只执行源码／文档／GitHub 读取、工作区隔离、认领与本地设计文档编写。没有调用业务连接、模型、真实工具、SQL 查询或启动 Dashboard。产品与浏览器测试为 **NOT RUN**；后续文档检查只验证本轮文件的链接、格式和范围，不证明功能已经实现。
