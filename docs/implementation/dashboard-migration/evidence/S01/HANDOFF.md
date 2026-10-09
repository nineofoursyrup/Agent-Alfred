# S01 / #109 共享读取交接

片内实现和检查 **PASS**；独立 Standards / Spec review 由协调器安排，当前 **NOT RUN**。整个 source / AC / G 不在本片提升为 PASS：UI 组合责任仍属于 S02 / S03 / S04 / S11。无 push、远端修改、真实模型、服务切换或业务数据修改。

## 固定候选

- worktree：`/Users/nineofour/.codex/worktrees/dashboard-s01/Agent-Alfred`
- branch：`codex/dashboard-s01`
- base / 最后核验的 integration tip：`7d87243ff715eaecc42159f35aa9f223c4ef8ce7`（已基于该 tip，无待合入的新 integration 提交）
- r1 head：`5f4cd757f9a6a82e97eee9e663a9aad94f6eeeaf`
- r2 head：`93c87bc5bf2a38cf92e2df27c743f50f59df7620`（证据保存在 `HANDOFF-r2.md` / `source-coverage-r2.json` / `candidate-files-r2.json`）
- r3 head：`e2862d927fdc846d07e6f20bad1a5a3b583189d5`（证据保存在 `HANDOFF-r3.md` / `source-coverage-r3.json` / `candidate-files-r3.json`）
- r4 head：`5672fbb9e9aa13224bc1d9f07dcd736c033ae0df`（证据保存在 `HANDOFF-r4.md` / `source-coverage-r4.json` / `candidate-files-r4.json`）
- r5 head：`3c0cd57edfb9f67e78130ca8ed94c6dbb24fda5a`
- r5 tree：`0472b276c7d7520c006ebb2d3f4249a4df66a7a3`
- 18 个实际改动文件及 SHA-256 / 字节数：`candidate-files.json`；精确 diff：`git diff 7d87243 3c0cd57e -- src/agent_alfred`。
- 完整责任映射：`source-coverage.json`，含 72 个 source ID、19 个 AC、5 条组合链。它引用固定源位置及原 hash，不改写冻结映射或设计证据。

## 实际变更

I00：新增七个 GET 入口统一必需 `process_instance_id`、错误码和观察信封。特殊字符与合法空 Session / Run ID 正常往返，参数缺失与空值区分。使用原 Host / Origin / CSRF / CSP / no-store 边界；读取不创建 Run、切换 Session 或调用模型。HTTP 对端断开可触发期间读取取消，事务和临时内容释放。

I01：抽取 accounting 共用成员与定价投影，Overview 在请求内计算完整集合而不占用 / 过期 / 清除 Ops 留存槽。保留 C / U、全用途及准入、Run 时间归属、半开自然日期、Decimal exact / estimate、模型证明与工具缺口分开，暴露固定 `ops_filters`、`price_version`、TTL。为 #88 D04 增量提供 `summary.price_sources`、`stale_price_attempts`、`tiered_price_attempts`；协调器已确认此源责任，不修改冻结 INTERFACES。

I02：SQLite Store 可选 count；MemoryQueryService 在同一 reading context 检查 revision 并统计 semantic + episodic，缺能力整体 503，修订竞争 409。Host 和 HTTP 输出前再次检查响应资格，不枚举正文。

I03：安全 Run 元数据统一用途分组，chat + aggregation 属于 chat；prompt preview 仅 admitted chat。持久记录判据与 Run evidence 共用，不再把任意 truthy telemetry 当 saved。无正文摘要分别表达 aggregation Graph / disposition 与记录状态，Host 同身份 merge 不降级 recorded。Overview 返回至多六个终态候选并完全移除 prompt_preview，供当前槽去重后取五条。

I04：r5 修复独立 Spec r4 P2：Session Run 定位取额外一个最近较新邻项作为真实排他复合 key 边界，保留 `(activity_revision, run_id)`，不再用 revision + 1。较新邻项不足时缩小返回量至目标，防止旧记录补位；普通 cursor 仍从目标之后继续。r4 修复独立 Standards r2 P2：anchor ID 绑定 SQLite 前复用 `parse_cursor_position_int`，超域、布尔及零返回 `400 invalid_anchor`，合法最大整数对应目标不存在仍为 404。新 Session / message / session Run 定位复用旧页形状、keyset 顺序与 opaque cursor；旧 Run locate 可保留 `filter=all`，排除目标的指定 filter 返回 409。持久消息行生成独立版本锚点，同文旧消息不同身份；historic 前存在可记录 Run 时返回 waiting / runs_pending；活动目标仅返回 pinned 槽，不制造历史游标。

I05：r3 补齐独立 `aggregation` 元数据，投影复用 `aggregation_metadata`，持久路径复用 `finalization_metadata`；返回 Graph 结局 / 回复处置 / 无草稿原因 / evidence_state，并走中央脱敏；普通 chat 为 null。完整合同未改写。精确 Session + Run 记录读取先命中 immutable 未保存投影，不等待记录数据库写锁；保存后读取原 pair，完整回复不截短。用户正文 / 仅预览如实区分，`item_key` 为版本化 opaque Session + Run 身份，`history_contiguous=false`，没有普通 cursor。正文可读与 pending / failed 分开；用户侧及回复侧脱敏失败安全映射为 unavailable。旧 `/api/reply` 与普通历史形状兼容，新增锚点为增量字段。

## 片内证据

| 检查 | 结果 | 实际范围 / 日志 |
| --- | --- | --- |
| 定向读侧 r1 | PASS | 57 项，`candidate-r1-targeted.log` |
| 额外真实 HTTP / 活动定位 | PASS | 10 项，`additional-boundaries-successor.log`；包含 Memory 响应前修订变化，真实 TCP 断开期间事务释放，保留他人的 Ops snapshot 身份 / 字节 / TTL |
| 正文及用户脱敏故障 | PASS | 27 项，`user-redaction-successor.log`；含持久 / 未保存两种安全失败和真实保存锁屏障 |
| r5 受影响定位 / Run / HTTP 回归 | PASS | 264 项，`composite-location-r5-successor.log`；同 revision 和不同 revision 三目标、limit 1 / 2、完整普通 cursor 后续、只读状态 / 模型数量不变 |
| r4 受影响定位 / HTTP 回归 | PASS | 306 项，`anchor-domain-r4-successor.log`；包含真实超域 HTTP、合法整数上界、零 / 布尔与读侧无模型 / 状态变化 |
| r3 受影响回归 | PASS | 425 项，`aggregation-locate-r3-successor.log`；真实 NoAction / 产出草稿在 pending 和 recorded 两路径保持独立事实；reply / HTTP / aggregation 回归 |
| r2 广后端回归 | PASS | 678 项，`backend-r2.log`；Ops、Session、Run evidence、Memory revision、HTTP、记录、聚合及以上新增读取 |
| 旧源码浏览器消费者 | PASS | 39 项，`browser-r1.log`；Runs、分页、accounting、recording、restart；r2 只改变新增端点边界，无旧路由 / JS / 输入变化，证据仍适用 |
| Ruff | PASS | `ruff-r5.log` |
| TypeScript | PASS | `typecheck-r3.log`；已执行 |
| check_skills / check_env_example | PASS | empty skill tree / `.env.example` consistency；已执行公开脚本 |
| git diff --check | PASS | r5 提交前执行，工作树干净 |
| 全 Python r1 扩测 | NOT RUN / 不完整 | `full-python-r1.log`；824 passed、1 deselected，266.52 秒后主动中断于无关 acceptance safety 校验；不称为全量 PASS。最终完整门禁由 S11 最终候选执行 |
| wheel / sdist 安装后 HTTP | NOT RUN | 归 S11 最终产物候选 |
| 原生 200% / 真实中文 IME | NOT RUN | 归 S11；本片没有 UI 交付 |
| iPhone / iOS Safari / 真实移动软键盘 | NOT RUN | `excluded_by_user` |

环境：macOS Darwin 27.2.0，CPython 3.14.7（共享 SQLite C API），锁定依赖 + dev / mcp，Node 22.23.2，Chromium Playwright，专用端口 17810；HTTP 用例使用独立随机回环端口与临时状态。

r2 的 678 项与旧浏览器 39 项未重复执行；r3 仅新增精确定位字段，受影响读取由 425 项 successor 检查，其余证据继续适用。r4 仅修复锚点整数域，受影响定位 / HTTP 另306项通过。r5 复合 key 定位另有 264 项受影响检查。独立 Standards / Spec 需对 r5 增量更新；r2 Standards FAIL / r4 Spec FAIL 保留，不由实施者改成 PASS。可选 P3 重复投影未修改。

完整后端命令已在 `backend-r2-command.txt`；浏览器命令和类型检查命令在各日志首部。checks 使用同一候选或明确可复用的未变化行为，不将不同日志测试数相加当作整体验收。

## 首次失败与修复

所有首次日志保留于此目录，结构化关系见 `source-coverage.json`。

- 新接口缺失的 RED：`red-period-interface.log`、`red-memory.log`、`red-summary.log`、`red-locations.log`，后由真实实现及定向检查通过。
- 工具 / 夹具：期间 seed 列数、一次脚本引号错误、坏 JSON fixture 触发既有 CHECK；保留 `first-fail-period.log` / `red-period.log` / `green-summary.log`。只修 fixture，不放松生产约束。
- 旧 DTO 精确断言：`regression-first.log` 为 MainBar 新增 message_anchor；保留原脱敏 / 完整内容断言，修正增量字段期望，`summary-regression-successor.log` 118 PASS。
- r1 活动 Session Run locate 返回了历史邻项：`additional-boundaries-first.log` 1 FAIL，r2 限定目标槽，successor 10 PASS。
- r1 用户预览 / 用户 blocks 的脱敏 RuntimeError 未映射为专用 unavailable：`user-redaction-first.log` 2 FAIL，r2 复用安全正文错误处理，successor 27 PASS。
- r2 精确定位缺少 aggregation 元数据：`aggregation-locate-r3-first.log` 4 FAIL，r3 真实 NoAction / 可读草稿在投影和持久路径均通过，受影响 425 PASS。
- 独立 Standards r2 P2 超域 anchor：`anchor-domain-r4-first.log` 真实 HTTP 1 FAIL（500 vs 400），r4 公共整数域校验修复，successor 306 PASS。独立原报告 / 复现仍在 `../reviews/S01-r2-Standards.md` / `S01-r2-anchor-repro.json`。
- 独立 Spec r4 P2 复合排序边界：`composite-location-r5-first.log` 2 FAIL（同 revision 错目标；不同 revision 最新目标混入较旧行），r5 精确边界 / 窗口修复，successor 264 PASS。独立原报告 / 复现仍在 `../reviews/S01-r4-Spec.md` / `S01-r4-spec-repro.log`。
- Ruff 首次、successor 及 r3 import 格式失败保留；r5 全部通过。

## 资源及组合交接

静态资源：本片没有 HTML / JavaScript import / CSS 引用变更，没有新增需要登记的静态 asset。该结论从实际 diff 的引用变更核对取得；不是遍历 ASSETS 代替引用闭包。两个新增 Python 模块为 `gateway/web/shared_reads.py` 与 `runtime/source_locations.py`，由现有 hatch `src/agent_alfred` 包选择包含；S11 需在四组安装后验证七个真实 HTTP 入口。

S02：精确定位消费者须保留新增 `aggregation` 字段，显示独立 Graph 结局 / 无草稿原因，不从 Run completed 或正文有无推断。将定位、普通历史、SSE 正式结果与恢复按完整 Session + Run（或等价 item_key）合并，保持历史 cursor / locationTarget 分离，真实迟到目标 / Session / 焦点 guards 与 returnLatest。

S03：连接实际来源定位与 MainBar 显式动作；所有筛选 / 分区 / 目标返回、等待段、缺失目标及焦点路径须真实组合验证；普通详情不能改变所选 Session。

S04：独立三数据区，精确 U / 金额 / 覆盖 / 来源呈现，最多五条历史加单当前槽，Memory 隐藏修订撤值、断线 / 重连 / 过期 / 刷新不得续期；真实 `/overview` 页面及固定 Ops 日期导航。

S11：负责完整 309 源项、AC / MCE / G 与最终默认入口、全门禁、wheel / sdist × base / mcp、安装后 HTTP、正常关闭 / 升级 / 支持目标回退、原生缩放与 IME。旧 BFCache BLOCKED 必须保留。S01 证据只完成共享后端片段，不能代替这些责任。
