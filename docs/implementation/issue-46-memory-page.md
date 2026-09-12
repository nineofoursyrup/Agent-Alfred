# Issue 46：Memory 页与浏览器集成

权威行为见 [#46](https://github.com/nineofoursyrup/Agent-Alfred/issues/46)、
[#31 最终规范](https://github.com/nineofoursyrup/Agent-Alfred/issues/31#issuecomment-5588135839)
R01、R06–R14 与 [#18 冻结规范](https://github.com/nineofoursyrup/Agent-Alfred/issues/18) D09。
起点为 `c8434da508994e6dc2d2515edb98716278a4f6ec`。本票只做 HTTP/页面接入，
业务仍由既有 `RuntimeHost.memory_service`、遗忘服务、提炼服务、Markdown 镜像与
启动时 SkillCatalog 承担；适配层不写业务 SQL。

## HTTP 接口

同源、Host/Origin、CSRF、JSON 与请求大小防护沿用现有 Dashboard。写入经共享命令服务
自身的准入（Host MutationGate），忙时 409、不排队；手动写入不创建 Run。

| 接口 | 说明 |
| --- | --- |
| `GET /api/memory/records` | kind、mode=list/search、text、subject、since、until、cursor、page_size（1–100，默认 25）。返回 records、next_cursor、read_revision；游标失效 409 `cursor_stale`。列表不带来源组。 |
| `GET /api/memory/record` | kind、id。记录与来源组两次读取前后比较持久 revision，变化则 409 `memory_changed`；不存在 404，并带读取时的 memory_revision。 |
| `GET /api/memory/state` | 当前持久 memory_revision 与进程身份，不含正文。 |
| `POST /api/memory/commands` | 命令 envelope 原样交给共享服务，来源固定为 web 手动。成功 200 带不可变回执；delete 另附当时的遗忘进度。 |
| `GET /api/memory/operations` | 回执、当前遗忘进度与范围快照；未找到 404 仍表示未确认。 |
| `GET /api/memory/statistics` | since、until（须带 offset）、可选 session_id；结果置于 `statistics`。 |
| `GET /api/memory/skills`、`GET /api/memory/skill` | 读取启动时建立的目录快照；名称只查索引。没有目录时 503 `unavailable`，不冒充空目录。 |
| `POST /api/memory/forget/actions` | `retry_cleanup`（operation_id）或 `resolve_scope`（operation_id、独立 action_id、scopes=[{scope_id, expected_revision}]）。成员只来自服务端快照。 |

读与写分成两张路由表：写处理只经受 CSRF 防护的 POST 到达，GET 不能进入写路由。

队列与镜像沿用 #18 的 `GET /api/memory/consolidation|mirrors` 与
`POST /api/memory/consolidation/actions`。唯一增补：镜像状态每项额外带 `name` 与
`confirmation_token`（confirmation 的 JSON 文本），`mirror_confirm` 的 observation
同时接受原对象或该文本。原因是 confirmation 内的文件身份含 inode 与 ctime 纳秒等
超过 2^53 的整数，浏览器解析后回传会被舍入而永远 `stale_confirmation`；原对象接口保持不变。
这是对 #18 已冻结接口的加法式修复，需调度与用户知悉。

### 命令服务码到 HTTP 状态

| 服务码 | HTTP |
| --- | --- |
| invalid_input | 400（适配层参数错误另带 `field`） |
| not_found | 404 |
| busy、version_conflict、duplicate_conflict、operation_mismatch、operation_unverifiable、scope_stale | 409 |
| unavailable、storage_read_failed、storage_write_failed | 503 |
| 其余服务码（trace_barrier_failed、transaction_required、deadline_exceeded 等） | 503 `unavailable`，原码放在 `reason` |

新接口的请求体解析错误同样返回 `{error:{code}}`。读取期间持久 revision 变化时沿用
#18 s6b 的 409 `memory_changed`。

### 页面读取的错误形状

| 来源 | 形状 | 例 |
| --- | --- | --- |
| #46 新接口 | `{error:{code,…}}` | version_conflict + current_version、cursor_stale |
| #18 动作 | 信封 + `result.error.code` | busy、batch_invalidated、stale_confirmation |
| #18 读取、守卫与传输 | 顶层 `code` | memory_changed、invalid_limit、csrf 拒绝 |

`failureCode` 依次取 `error.code`、`result.error.code`、`code`，都没有才用 `http_<status>`。
非 200 不一律显示为保存失败：400/404/409 是明确拒绝；带 `reason` 的 503 是写入前的
拒绝（如追踪屏障未确认），显示为未执行；无响应或其余 5xx 显示“结果待确认”，保留同一
operation_id 供查询或原样重送；同一未确认的保存、编辑或删除再次提交也沿用该 ID。
HTTP 成功状态本身不证明提交：正文断流、截断 JSON、必要结果或身份字段缺失均保持待确认。
命令回执核对 operation_id、action、kind、闭合状态、MemoryId、版本、影响数与提交时间；
队列/镜像按 #18 的实际成功或持久失败形状核验，POST 与查询使用相同判定。
#18 动作在 5xx 中带回自身持久失败结果时，重送同 ID 只会重放失败，因此视为明确结果，
下一次点击是新动作；查询读到持久失败也显示明确失败，不展示 undefined 成功。

## 页面与失效模型

`/memory` 有语义记忆、情景记忆、Skill 目录三个标签，其下为操作回执、检索统计、
提炼队列和 Markdown 镜像。MainBar 与标签页 Session 仍属壳层。

`MemorySync` 是本页与运行详情共享的失效源，状态为 offline / verifying / online /
unverified，只有 online 时显示正文：

- 首个 `state_patch` 表示流已连上，先读 `/api/memory/state` 再显示任何正文；
  断线立即隐藏记录正文、编辑草稿、候选差异与镜像预览。核验失败时保持隐藏，
  由用户手动重新核验。
- `memory_patch` 的 change 目前恒为 null，页面按整页失效处理：断线后到达的帧、
  其他进程的帧和不前进的 revision 都被忽略；前进则隐藏正文并重新读取。
- 每次读取记下失效计数，期间发生失效或响应 revision 低于已知值即丢弃；
  遇 `memory_changed` 最多共读取三次。
- 页面实例以自身元素是否仍在文档中决定退订；壳层的 `#page` 会被复用。回执列表按
  标签页共享，由壳层持续恢复；导航立即解除当前页回调，不会被旧实例的迟到完成覆盖。
- 已删除记录的编辑草稿在核验 404 后清除；读取故障不当作不存在，草稿保留；
  版本冲突保留草稿，只有用户选择才改为基于当前版本。
- 删除确认绑定当时显示的版本；记录变化后需重新确认。
- 浏览器在发出命令前将操作 ID 与原文写入 sessionStorage，发送中刷新也能查询或同 ID 重送；
  已确认历史只保留最近 20 项，未确认命令与遗忘未完成的删除均保留恢复入口。
  已提交回执与删除后的进度都不含正文。
- 在线失效同时核验 sending 与 pending 操作，按原 ID 自动回读提交事实；确认后移除请求正文。
  同一读请求期间发生的新失效会触发后续核验，旧 404 不能吞掉后来的提交/删除通知。
  未提交修改没有操作回执时，以请求内的精确记录 ID 核验当前记录；只有确定的不存在事实才移除请求正文。
  保存草稿通过已确认的 MemoryId 绑定当前条目，断线或修订变化先隐藏，核验存在才恢复；
  核验 404 清草稿，读取故障保留。修改草稿仍按当前记录核验。迟到发送失败不能撤销已知提交。
- 队列/镜像动作在发送前以最少身份和版本/确认信息写入 sessionStorage，不保存候选正文或预览。
  刷新和切页恢复全部待确认动作，各自提供查询/同 ID 重送；自动恢复只查询，不重新授权旧动作。
  迟到结果必须仍属于该 operation_id，不能删除或覆盖新页面中同一按钮发起的新动作身份。

运行详情的“本次输入”展示 gate 结果、各库状态、召回数与选入数，并逐 Attempt 列出实际
携带的记忆引用。“查看当前内容”总是回查当前记录：同版本、已修改、已不存在、读取失败分别
显示，不展示任何相关度分数。

## 验收对应

浏览器测试位于 `tests/browser/memory.spec.js`，由 `tests/browser/memory_server.py`
启动独立状态目录的真实 Dashboard、临时 SQLite/FTS、真实遗忘/提炼/镜像与离线脚本模型。
stdin 只控制下一次提炼输出、Host 自身的写入闸门与下文的测试边界故障。HTTP 测试位于
`src/agent_alfred/evals/deterministic/test_memory_page_http.py`。

| 验收 | 主要证据 |
| --- | --- |
| A35 | 浏览器 A35：两个真实 SSE 标签页，旧读取用受控请求延迟，另一标签删除后迟到响应不恢复正文；手动写入不产生 Run |
| A36 | 浏览器 A36/A55：受控 EventSource 断线即隐藏正文/草稿/镜像预览，离线期间删除，重连核验 404 并清草稿；核验失败需手动重新核验 |
| A37 | 浏览器 A37（修改后版本提示、读取故障不冒充不存在、删除后无旧文）与 A52 |
| A38 | 浏览器 A38（相同创建时刻 30 条按内部次序分页、无 patch 时游标拒绝续页并清空）；HTTP `test_list_and_search_cursors_bind_query_revision_and_internal_order` |
| A40 | 浏览器 A40：空库、无结果、读取失败不显示旧列表、忙时 409 保留草稿、冲突保草稿、他处删除清草稿 |
| A41 | HTTP `test_skill_catalog_reads_the_startup_index_only`、`test_duplicate_skill_names_fail_startup_instead_of_listing_one`、`test_invalid_skill_name_fails_startup`、`test_missing_catalog_is_unavailable_not_an_empty_directory`（无状态目录的真实 Host）；浏览器 A41/A42 |
| A42 | 浏览器 A41/A42：无匹配/注入/编辑控件，文件修改后刷新仍是启动快照，重启后才加载 |
| A47 | 浏览器 A47（待批准、忙时批准与重试均 409 不排队、拒绝、失效无动作、失败重新生成 202、提交失败只重试提交且不新增系统 Run、批准成功、无绕阈值入口）与 R13 超限跳过 |
| A52 | 浏览器 A52：对话明确保存（真实 save_fact）→ 下一 Run 门模型决定检索并 FTS 命中 → 引用与来源 Run → 编辑冲突 → 页面删除 → 再查不命中、受管镜像与文件无正文、下一 Run 工作窗口排除来源与读者两组、原始会话仍在、统计 1/3 |
| A55 | 浏览器 A55：真实 SSE 上令有界入口拒收下一帧，删除后连接被关闭，浏览器自动重连并核验，正文未复现且无 Run；A36/A55 覆盖旧进程帧、revision 倒退与断线后到达的帧；HTTP `test_real_stream_memory_patch_has_no_seq_checkpoint_run_or_body` 读取真实流帧 |

规范中的页面责任另有：R08 响应丢失后查询与同 ID 重送（包括发送中刷新、超过 20 项待确认）；R09 旧库缺关联的范围确认；
R10/R14 受管镜像被外部改动时清理保持未完成、确认重新生成后遗忘完成；R14 非冲突的
镜像写失败与数据库结果分开显示并可同步重试，动作响应丢失后按同一操作查询结果；R11 情景
的本地时区输入、半开区间显示与边界搜索；R13 超限来源跳过。

`memory_server.py` 的故障只注入在测试进程：episodes 插入触发器（提交失败）、受管镜像
写入抛错、以及 broker 有界入口拒收一帧。其中镜像写入故障由测试进程在导入期把
`ManagedDirectoryLease.replace_bytes` 重绑为包装函数实现（`memory_server.py:31-40`），
`MIRROR_FAILURE` 未置位时原样委托原方法；产品代码本身不含故障注入点。

## generation 2 修复验收

沿用同一真实浏览器与 HTTP 边界，新增测试仍在 `tests/browser/memory.spec.js`：

| 修复要求 | 正向证据 |
| --- | --- |
| R09/R10 受管请求/草稿遗忘 | `G2 R10: forgotten ...` 覆盖保存/修改、响应丢失/仍在途、另一客户端删除及刷新；断线重连用例先证明读失败不清草稿，再证明核验已删后正文消失 |
| R10 迟到查询 | `G2 R10: a late missing-operation read ...` 用受控旧 404、真实提交和删除验证后续失效不会被在途查询吞掉 |
| R08 成功头不等于回执 | `G2 R08: ... success headers` 覆盖 save/update/delete 的截断 JSON、成功 Response 体中断和必要字段缺失；保留原请求与同 ID 查询，查询确认后去除正文 |
| D07/D09 动作恢复 | `G2 D09: ...` 覆盖两个待确认镜像动作、切页/刷新、队列和镜像已提交/未提交的发送中重载、旧页面迟到查询不得清新身份 |
| D09 回执及授权边界 | 镜像/队列成功及查询缺结果字段保持未确认；真实持久镜像失败显示明确失败；恢复旧文件确认后同 ID 重送仍被新文件版本拒绝 |

新增故障均在测试的 HTTP/浏览器 Response 边界施加；没有新增产品故障开关。
原始会话、既有 trace 与外部副本仍不追溯擦除；不会按文本相似度识别被删条目。

## 当前边界

- 队列复用现有 `limit=50/offset/session_id` 读契约，提供上一页、下一页与会话筛选；每次有界读取最多 50 个会话与 50 个批次，不累积拉取全部结果。
- 未使用真实付费模型；门、回答与提炼输出均来自离线 ScriptedModel。

### g2-v2 评审修复

- 记忆命令恢复由应用壳层持有唯一回执控制器，Memory 页只附接当前视图回调；导航时解除回调，壳层仍随 `memory_patch`、重连核验查询待确认操作。直接重载到 `/runs` 也恢复，已确认回执移除可复用请求正文，读失败继续保留身份与请求。
- 队列可翻页并按会话 ID 筛选。切页/筛选清除候选与旧列表，读取带请求代次，旧页迟到结果不能附加回新页；记忆失效先清除已展示正文，再核验候选。较早阻塞来源可实际跳过，较早批次可实际批准、拒绝或重试。
- `memory_revision` 与 `activity_revision` 是独立的持久时钟，默认后端在同一 SQLite 文件内以不同表保存，沿用 ADR-0009 的同一连接契约；本轮仅纠正术语，不改变存储架构。
- `G2 v2` 浏览器回归覆盖离页/重载后的保存与修改请求清理、读失败保留、跨客户端删除、较早阻塞 Session、较早批次动作以及翻页/筛选/失效后的候选读取归属。

### g2-v3 历史 Session ID 筛选

按 ADR-0027 逐字传递会话筛选值，不 trim、不规范化。只有空输入省略 `session_id`、表示全部会话；纯空白值仍是精确的历史 ID。`G2 v3` 浏览器回归在真实库中区分 `" s "`、`"s"` 与 `" "`，逐项核对 HTTP 参数、实际返回会话和页面行；清空输入后恢复全部会话。Python 及其测试字节保持 g2-v2 不变。

### g2-v5 范围选择与确认回读

历史范围选择按 operation_id、scope_id、revision 绑定，仅在同一范围修订重绘时保留；范围修订或状态改变即清除，不将旧选择自动授权给新范围。该选择仅在当前页面会话控制器内保留，不作为持久确认。确认成功立即保存并显示服务端进度，确认前启动的回执查询不能覆盖确认后的事实；后续查询仍读取最新范围。浏览器回归控制同修订重绘、修订改变以及旧查询晚于确认返回的时序，保持明确人工确认与最终遗忘完成断言。

### g2-v6 当前清理进度核验

确认代次过期的回执读取无论正常返回、请求中断还是 JSON 解码失败，都不覆盖确认后的进度或提示。读取在途时已合并的请求在结束后执行一次后续核验，不受本地旧 complete 状态阻断；没有合并请求就不自发重查。保留的已完成删除在新失效或重连时仍查询当前清理进度，已完成历史最多20项的界限不变。新增回归覆盖迟到异常、确认后实际发出后续GET，以及 complete 在失效/重连时的回读与无新事件时静止；reopened进度仅在HTTP读边界构造合法形状，不宣称构造了真实晚到来源关联。
