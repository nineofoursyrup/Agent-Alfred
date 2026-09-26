# #28 实现：Dashboard HTTP+SSE 骨架与重放环

来源：https://github.com/nineofoursyrup/Agent-Alfred/issues/28

## Question

落地 [决定：Dashboard HTTP+SSE 骨架与断线续传](https://github.com/nineofoursyrup/Agent-Alfred/issues/23)：
让事件流可靠到达浏览器并可恢复。**不含九个页面的内容与布局。**

范围：

- **`RuntimeHost`**：唯一持有 `process_instance_id`、`seq` 分配、共享 `FanOutSink`、
  唯一数据库写连接、`ReplayRing` 与 `RunCoordinator`。
  启动序列**先取状态目录进程锁 → 再绑 `127.0.0.1:7717` → 成功后原子写入含
  `instance_id`/`pid`/`port` 的入口描述**；锁冲突与端口占用均如实失败，**绝不自动换端口**。
- **`ReplayRing`**：纯数据结构。只装 `replayable` 事件，元素是逻辑事件、驱逐原子，
  帧数与编码字节双重计额（2048 / 32 MiB），保留单调 `replay_floor_seq`。
- **`SSEBroker`**：两级队列（ingress 4096 / 32 MiB → 每连接 512 / 8 MiB），
  每连接独立写线程；**可重放帧必先原子入环、再投 ingress**；溢出矩阵按 #23 §9。
- **线格式**：`retry:` → re-seed(`id:`) → 可选 `replay_gap` → 原子快照/精确补发 → 实时流。
  `id:` 只出现在可重放且已完整的边界；1 MiB 硬帧上限 + UTF-8 安全切分。
  快照起点、`high_water_seq` 与连接注册在**同一临界区**内确定。
- **`SSEHandler`**：`ThreadingHTTPServer` + `BaseHTTPRequestHandler`，
  `daemon_threads=True`，仅绑回环；规范化 `Host` + 精确 `Origin` + 进程级 CSRF 令牌，
  **永不输出 `Access-Control-Allow-Origin`**；写请求限正文大小与 `Content-Type`。
- **`RunCoordinator` / `MutationGate`**：任意时刻至多一个 Run，忙时立即
  `409 run_in_progress` 不排队；接受返回 `202 + run_id`，HTTP handler 立即返回。
- **`EventSink` 两阶段改型**（准备/提交）与 `FlushResult` 闭合联合的落地
  —— 若 [#13](https://github.com/nineofoursyrup/Agent-Alfred/issues/13) 已先行完成，本票只做 SSE 侧实现。

### 强制验收项（建图时约定）

> 依赖可注入（`SSEConnection`、时钟、等待器），且同一份代码能被
> `FakeConnection` 驱动，有离线测试证明。

### 其他验收

- 断线重连后事件无重复、无静默空洞（第 2 层，`FakeConnection`）。
- 注入永不消费的连接，断言 Run 正常完成且其余连接不受影响。
- 环滚出后重连必得 `replay_gap`；四类非法游标（`malformed`/`instance_mismatch`/
  `too_old`/`ahead`）各一条。
- 快照与实时流的竞态：注册连接的同时并发发射，断言无重复无空洞。
- 分块事件：只有末帧带 `id:`；断在第 k 块后重连，整个事件被完整重发一次。
- `Host`/`Origin`/CSRF 三层各自的拒绝用例；断言响应中永不含跨域许可头。
- **一个绑随机回环端口的薄 HTTP 契约测试**：真实字节格式与 `Last-Event-ID` 回传闭环。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/28#issuecomment-5443666167

## 回写修订（来自 [决定：切片④a — 看见对话](https://github.com/nineofoursyrup/Agent-Alfred/issues/30)）

本票在既有 HTTP+SSE 骨架范围内新增以下所有权；页面内容仍归后续纵向施工票。

### HTTP 契约

- 接受聊天请求只在运行层确认「租约取得 + accepted 已提交 + 已交接唯一执行线程」后返回 `202 + run_id`。
- 已有 Run 返回 `409 run_in_progress`；记录失败闸门开启时返回 `503 recording_unavailable`；落盘或交接失败不得返回 202。
- 已知忙与竞态 409 共用服务端生成的 `active_run_summary`：仅含 purpose、Gateway、开始时间、可空当前 Step、可选脱敏限长 `prompt_preview`、经同源校验的安全导航目标。

### SSE 三类载荷

1. `domain_event` 使用既有事件 `seq`；
2. 连接局部 `transport_notice` 不占 seq、不进 trace/ReplayRing；
3. 幂等 `state_patch` 是绝对生命周期替换，携带独立 `process_instance_id + state_revision`，不占事件 seq。

- 服务端先更新权威快照再投递补丁；投递不可靠即断开连接。
- 客户端归并契约拒绝实例不符或 revision 倒退，且 pending 不得覆盖 recorded/failed。
- 只有数据库事务提交可确认 recorded；trace 刷写、事件 persist、游标和 ReplayRing 都不是确认。
- Attempt 增量只走 domain_event，不复制进 state_patch。

### 原子运行态快照

- 每次连接/重连取得有界原子快照：进程标识、state revision、可空活跃 Run 的完整生命周期投影、当前 Step/Attempt 终态摘要、recording_state、本连接 Session 有效性，以及至多一个 `unrecorded_terminal_projection`。
- 该单槽覆盖已发布 run.finished 但记录仍 pending/failed 的 Run；记录失败后服务端保留它并拒绝新 Run，不能让第二个未记录终态覆盖。
- 历史 Session、消息与 finished Run 不塞进 SSE 快照，统一走分页 API。

### 只读与创建 API

- 显式创建 Session；服务端签发 session_id，客户端不可自选，且不存在 Web 全局活动 Session。
- Gateway 收件箱：服务端分页 Session 分组，并在组内独立分页已获准 chat Run。
- 运行页：服务端按 purpose 过滤并分页 Run；唯一未终态 Run 单独返回供置顶和去重。
- MainBar：按 Run 活动 revision 分页返回每个已记录 chat Run 的唯一用户/助手消息对，默认最近 25 个。
- 深链定位、筛选切换、转义未知 purpose 均由服务端完成，不能先取固定页再由浏览器过滤。

### 验收补充

- 202/409/503 三条 HTTP 契约和同构 busy summary 的薄契约测试。
- state_patch 倒序、重复、跨实例、pending 覆盖终态均被拒；快照先更新后投递及队列不可靠时断连可确定性复现。
- run.finished 后事务 pending、recorded、failed，以及同进程重连保留未记录正式回复的测试。
- Session/Run/消息的 keyset 分页、未知 purpose、深链定位、非终态置顶去重与多标签 Session 校验测试。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/28#issuecomment-5444211896

## 回写修订（来自 [决定：切片④a — 看见对话](https://github.com/nineofoursyrup/Agent-Alfred/issues/30) 的补正裁决）

完整裁决以决策票的[补正裁决评论](https://github.com/nineofoursyrup/Agent-Alfred/issues/30#issuecomment-5444200695)为唯一权威，本评论只记录本施工票接住的闭包。

### HTTP 契约：`pending` 期间是 409，不是 503

- Run 在收尾事务落定前仍占有准入租约（归 [#13](https://github.com/nineofoursyrup/Agent-Alfred/issues/13)），本票的表面契约随之收紧：**`recording_pending` 期间的提交返回现有的 `409 run_in_progress`**，不新增错误码。
- 该 409 与已知忙态**共用同一张忙态卡与同一个渲染器**，`active_run_summary` 里的**安全阶段显示为「正在保存」**。字段白名单不变。
- `503 recording_unavailable` **只在 `recording_failed` 之后**返回，且必须在协调器状态落定之后才开始返回。
- 提交成功后是**先**广播 `recorded` 的权威快照、**再**释放租约，因此本票不会出现「快照还写着旧终态、新 Run 已被准入」的窗口。

### 分页 API：历史消息走第二段游标

回填 `sessions`（归 #13）之后仍有一个缺口：旧消息没有 `run_id`，而收件箱与 MainBar 的分页都是**按 Run** 的——旧 Session 会回填出来、点开却是空的。本票的分页 API 负责补上：

- **分段游标，两段**。第一段按 `(activity_revision, run_id)` 返回新数据的消息对；该段**耗尽后**，第二段按 `agent_log.id` 返回 `run_id IS NULL` 的旧消息。
- **绝不伪造 Run** 来包住旧消息。逐字保留数据与旧会话可见，两件事同时成立。
- 游标必须能表达「现在在第几段」，且跨段续页不得重复或漏行。
- 无 Run 的历史 Session，收件箱**标题**从**首条旧用户消息**的用户可见文本**脱敏限长**派生（脱敏仍走中央 Redactor，ADR-0003）。有 Run 的 Session 标题规则不变。

### 验收补充

- 一个只含历史消息（全部 `run_id IS NULL`）的 Session，在收件箱可见、标题非空，点开后消息**完整**且**不出现任何伪造的 Run 行**。
- 一个同时含历史消息与新 Run 的 Session，跨两段续页**不重复、不漏行**，两段边界处的游标可往返。
- `recording_pending` 期间的提交返回 409 且阶段显示「正在保存」；`recording_failed` 之后返回 503。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/28#issuecomment-5464852097

## 终审闭包（2026-08-30）：游标定义、释放进度位、revision 重复拒绝与租约/卡片原子发布

最终独立复审的六项闭包已在本分支收口（不改动既有提交）：

1. **SSEBroker.close() 的假完成**：`_closed` 只在 dispatcher 与全部 writer 确认退出后置位；stop 哨兵与队列停止成为跨尝试的独立进度，重复 `close()` 继续 join 并连续返回 False，真实完成后才 True 且幂等。上层只有拿到真实 True 才允许关库、放锁。
2. **DashboardService._release_server()**：`shutdown()`/`server_close()` 建模为可重试进度，server 引用存活到 socket 确认关闭；描述符与锁只跟随确认过的关闭。
3. **DashboardRuntime._release_locked()**：同形修复——`_released` 拆为 db_closed / entry_released 进度位，各自只在对应动作成功后推进；异常时保持 closing、保留可重试引用与进程锁。
4. **apply_state_patch() 重复 revision**：相同 `state_revision` 不论载荷同异一律 `revision_duplicate` 拒绝且不改当前状态；跨实例仍 `instance_mismatch`、更小 revision 仍 `revision_regression`。
5. **租约与 active_run_summary 竞态**：`admission_reserve(run_id, summary)` 在协调器锁内原子完成冲突判断、租约占用、pending handoff、done event、busy 卡片与权威快照替换；DB/handoff 失败原子撤销并发布 idle/中断事实。「占租约未提交」窗口内的第二请求恒得完整同构 busy 卡片的 409。
6. **游标 `:0` 规格冲突**：经用户裁决（2026-08-30）正式定义——`:0` 是不消耗领域 seq 的传输启动边界（非事件检查点），re-seed 重种「曾签发边界」而非「当前可重放」承诺；环仍拒绝一切未签发正数 seq。权威文本见 ADR-0013「修订（2026-08-30）」与 #23 的同日修订评论；代码中的 `_Contradicts ADR-0013` 掩饰注释已移除。

全部测试以 barrier/event/latch 与注入替身驱动，无 sleep；keyed 测试未在本轮运行。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/28#issuecomment-5558054986

## 完成与验收结论

本 Issue 的 Dashboard HTTP+SSE 骨架、重放／恢复与已裁决的准入及资源所有权闭包已完成，由 [PR #40](https://github.com/nineofoursyrup/Agent-Alfred/pull/40) 以 **merge commit** 合入 `main`，保留原有提交历史。页面内容与布局仍不属于本票范围。

### 精确交付对象

```text
已验收 HEAD=76824b68c8bc4190f305629ba879478342069af2
固定审查 BASE=31daa31010dc96639664da1be0767f51a7de83a0
MERGE_COMMIT=ba5ccc2e8765fb06e903d13c643d59cfc857f949
累计 DIFF_SHA256=e7eb0216b60cb8c4b593ae4c7a0c54cd874d07cb6eabb894c577309307e12fc2
累计 DIFF_BYTES=2644702
验收 PATHS=101
TREE=cc30fb78208d939dacc3a678f9c15487c91d7add
```

本次收尾前已重新只读核验：PR 状态为 MERGED，`main` 指向上述 merge commit；其两个父提交依次为固定 BASE 和已验收 HEAD，因此包含已验收提交与全部原历史。合并结果 tree 与已验收 HEAD 的 tree 完全一致，没有引入候选之外的内容变化。

累计摘要可由以下命令的完整 stdout 字节计算 SHA-256 复核：

```sh
git diff --binary --full-index 31daa31010dc96639664da1be0767f51a7de83a0...76824b68c8bc4190f305629ba879478342069af2
```

### 机械门禁与 CI

已验收 HEAD 的提交后本地机械门禁均为 **RC0**：

- `.venv/bin/ruff check --no-cache .`
- `.venv/bin/python -B scripts/check_skills.py`
- `.venv/bin/python -B scripts/check_env_example.py`
- working／cached／固定基线至 HEAD 的 `git diff --check`
- 完整非 keyed pytest：**2386 passed、1 deselected，64.22s**；JUnit 为 **0 failures、0 errors、0 skipped**。唯一 deselected 来自仓库既定 `-m not requires_key`，没有额外排除此前临时目录用例。

[PR HEAD 的 CI](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34021653541) 成功。
[合并提交的 main CI](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34021950461) 也已 **completed / success**，headSha 精确为 `ba5ccc2e8765fb06e903d13c643d59cfc857f949`；lint、skills、env 一致性和离线确定性测试步骤均成功。这里分别记录本地验收和 GitHub CI，不互相冒充。

### 独立双轴结论

- **Standards：PASS，101/101 路径及跨模块契约；partial=0、missing=0、未解决阻断=0。**
- **Spec：PASS，101/101 路径及跨模块契约；partial=0、missing=0、未解决阻断=0。**

两轴使用独立只读上下文，分别对上述已验收 HEAD 与同一累计摘要负责。复用先前完整覆盖前，均独立核验当前文件／完整 diff section、同轴证据链和契约影响；不是直接沿用旧 PASS、主 Agent 自报或以测试绿灯替代独立结论。关键闭包包括 Attempt 原账有限重试、权威 recorded 快照先于准入释放、正文身份及内存／数据库接续与生命周期事实隔离、SSE 不倒退／不抹 gap／完整事件 checkpoint，以及真实进程锁与资源关闭所有权。

### 保留建议与关闭范围

**25 项非阻断建议（Standards 21 项、Spec 4 项）完整保留在 [PR #40 正文](https://github.com/nineofoursyrup/Agent-Alfred/pull/40)，全数未修复，也未冒称已修复。** PASS 表示无未解决阻断，不表示没有改进建议；这些建议作为后续可选维护，不在本次行政收尾中扩展处理或创建维护票。

依据上述交付和验收证据，本 Issue 按 **completed** 收尾。本次仅添加此完成说明并关闭 #28，不修改代码／配置或其他 Issue，不删除分支、不切换或更新本地工作树，不触碰用户既有 `.gitignore` 修改。

<!-- issue-28-completion:ba5ccc2e8765fb06e903d13c643d59cfc857f949 -->
