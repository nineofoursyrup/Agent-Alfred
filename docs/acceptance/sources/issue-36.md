# #36 实现：切片④a — 看见对话

来源：https://github.com/nineofoursyrup/Agent-Alfred/issues/36

## Question

落地 [决定：切片④a — 看见对话（MainBar 壳层 + Gateway 收件箱 + Loop 逐轮页）](https://github.com/nineofoursyrup/Agent-Alfred/issues/30) 已冻结的三个界面层，让用户可以在任意页面发送消息、观看同一个 Run 的过程证据，并在断线、多标签与记录失败时仍得到不重复、不伪造的结果。

本票只消费运行层和 HTTP+SSE 层已经冻结的数据契约，不重新定义 Run/Session schema、准入、重放或分页 API。

### MainBar

- 导航用「运行」，页标题只用「运行详情」；用户文案停用「逐轮」和「Loop」。
- 底部常驻输入栏 + 可展开抽屉；窄窗为全屏对话框。抽屉是当前 Session 唯一主对话，跨页保留草稿、展开态和进行中 Run，切页不重连 SSE。
- Session 按标签页隔离并存 `sessionStorage`；只在用户点击「继续此会话」时切换，失效时不静默回落。
- 当前 Attempt 只有一个临时助手槽；作废或非终结 Step 提交即清除，run.finished 才转为唯一正式回复，recorded 后按 run_id 与服务端消息归并。
- 已知忙和 409 使用同一张普通忙态卡，保留草稿，不插消息、不建失败 Run；支持查看当前运行。

### Gateway 收件箱

- 分页 Session 分组，组内另行分页已获准 chat Run；运行中不伪造回复摘要。
- 显示唯一最终回复摘要、状态、时间、Gateway 来源；entry_surface_id 仅在具有独立语义时进入详情。
- 点击只读查看，「继续此会话」才切换 MainBar。

### 运行详情

- 筛选固定为「对话 / 系统运行 / 全部」；未知 purpose 归系统运行并展示转义后的原值，深链先切筛选再定位高亮。
- Run → Step → Attempt 主从层级；Attempt 按 attempt.started.seq 保持事件发布顺序，同一 Attempt 状态迁移按 seq 展示。
- committed 是 Step 摘要主结果；失败 Step 明示无已提交尝试；aborted 原位默认折叠、撤回样式且不进主对话。
- 展示文本块快照与非文本块类型/数量，不展示 thinking 正文、工具参数或工具结果。
- Attempt 级费用严格呈现 exact/estimated/unknown；unknown 无金额，estimated 紧凑携带实际计费维度的 price_source 与 stale。

### 重连、多标签与可访问性

- 所有标签页按 run_id 折叠全局状态；仅 Session 精确相等时向 MainBar 投影临时助手槽，其他 Run 只投影忙态。
- 任何断线或 deltas_dropped 都清除受影响未收尾 Attempt 的临时文字并抑制旧 Attempt 后续 delta，直到 committed/aborted；新 Attempt 才恢复流式。
- replay_gap 四因与 current_run_state 三态按决策票文案渲染；传输提示只进壳层连接状态栏并注明仅影响本标签页，不进入 Run 时间线。
- 支持 unrecorded_terminal_projection：pending 显示正在保存，failed 显示回复已收到但未保存；终态不得被旧 pending 覆盖。
- 增量文本不设 aria-live；状态播报节流去重，保存失败一次性警报；键盘、中文输入法、焦点归还、非颜色状态提示与 prefers-reduced-motion 全部按裁决落地。

### 端到端验收

- Dashboard 发送 → 202 → Step/Attempt/增量 → 唯一正式回复；刷新或跨页无重复。
- CLI 忙时 Web 发送被预防或 409 拒绝，草稿保留且不产生失败 Run。
- aborted Attempt 在原发布位置可见、照常显示用量事实，但不与最终回复并列。
- 断线、deltas_dropped、四类 replay_gap、recoverable/unrecoverable/absent、同进程未记录终态重连均有确定性浏览器测试。
- 两标签页同/不同 Session、显式继续会话、空 Session 重启、多页 keyset 续页和深链定位均无串线。
- completed/max_steps/failed/interrupted、trace_incomplete 与 recording_state 的视觉和辅助技术语义互不混用。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/36#issuecomment-5444212208

## 回写修订（来自 [决定：切片④a — 看见对话](https://github.com/nineofoursyrup/Agent-Alfred/issues/30) 的补正裁决）

完整裁决以决策票的[补正裁决评论](https://github.com/nineofoursyrup/Agent-Alfred/issues/30#issuecomment-5444200695)为唯一权威。本票（界面层）接住两条：

- **忙态卡新增一个阶段文案**：Run 已产出回复、记录事务尚未落定时，提交返回 409，`active_run_summary` 的安全阶段显示**「正在保存」**。它仍走既有的那张普通忙态卡与同一个渲染器——保留草稿、不插消息、不建失败 Run。记录失败后才转为 503 的不可用态。
- **旧会话必须看得见**。升级前的历史 Session 没有任何 Run，分页 API 以第二段游标按 `agent_log.id` 返回这些消息（归 [#28](https://github.com/nineofoursyrup/Agent-Alfred/issues/28)）。界面据此渲染，且**绝不因为没有 Run 就显示空会话**，也不得渲染出伪造的 Run 行。

### 端到端验收补充

- 从一个含历史消息的 v2 数据库升级后启动：收件箱列出旧 Session（标题由首条旧用户消息派生），点开后 MainBar 显示完整旧对话，运行详情里**没有**对应的 Run 行。
- 同一 Session 里旧消息与升级后的新 Run 并存时，MainBar 上下滚动跨越两段游标边界**不重复、不跳行**。
- 记录未落定期间从第二个标签页发送：显示「正在保存」忙态卡、草稿保留、不产生失败 Run。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/36#issuecomment-5559572106

## Issue #36 完成说明

实现已通过 [PR #41](https://github.com/nineofoursyrup/Agent-Alfred/pull/41) 以 merge commit 合并到 main，保留原提交历史。本次完成 Dashboard 对话呈现、MainBar／Session／收件箱／Run 详情，以及既定恢复和分页契约的界面验收；实施期间经用户明确授权的只读呈现接口也包含在验收范围内。

### 完整提交与候选

- 已验收 HEAD：`b0c184f0b548c93fe9d9e0dccfb25e5f5ce6f963`。
- 合并提交：`4aa34f22d16ec5c1d58c191c5179ace0cc22fdd1`。
- 固定审查基线：`ba5ccc2e8765fb06e903d13c643d59cfc857f949`。
- 完整候选：44路径，196752字节；SHA-256：`67b9314c4fe5c2aa1bdf5545fd3074137eb0245d21475b4fd88c1a48ecfb73d5`。
- 摘要为固定基线到候选的逐路径原始Git binary patch按UTF-8路径排序、无分隔及换行归一化拼接；提交后与 `git diff --binary BASE...HEAD` 一致。
- 本次行政收尾前再次只读确认：main 包含已验收提交；合并结果树与已验收候选完全一致，tree SHA 为 `a50ed5a4da400f800262d389575fc442fda7b426`。

### 本地 exact HEAD 机械门禁

以下均在已验收HEAD上重新执行，退出码均为0：

- Python非keyed全量：**2402通过，1项requires_key按仓库配置排除**；0失败／错误／跳过。
- Chromium浏览器全量：**52通过**；0失败／跳过，retries=0。
- Ruff、JS typecheck、仓库技能检查、环境示例一致性检查全部通过。
- working／cached／固定基线cumulative diff-check全部通过。
- 离线wheel构建及资产一致性通过：18个包内候选文件与提交内容逐字节匹配。

测试使用离线模型与专用临时状态目录。上述为本地门禁结果，不将本地临时路径作为远端可访问证据，也不冒称真实模型或人工读屏器验收。

### 独立双轴与远端CI

- Standards：**整体PASS，44/44覆盖，0未解决阻断**。
- Spec：**整体PASS，44/44覆盖，0未解决阻断**。

两轴分别使用全新独立只读上下文，核验内容哈希、原始覆盖记录、跨模块影响及finding关闭记录，对同一已验收HEAD和完整摘要明确负责。原审30＋限定修复12＋CLI异常收尾2＝44的完整覆盖经核验复用，不是直接拼接旧PASS。

- [PR当前HEAD的CI](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34035821537)：success。
- [合并提交的main CI](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34036131202)：**completed / success**；绑定 `4aa34f22d16ec5c1d58c191c5179ace0cc22fdd1`，全部检查1/1成功，无其他待完成或失败检查。

### 保留的非阻断建议

**“静态响应与 _send 收尾重复”**完整保留在 [PR #41正文](https://github.com/nineofoursyrup/Agent-Alfred/pull/41) 中。这是非阻断的Duplicated Code结构建议，尚未修复，**未冒称已修复**；不在本次行政收尾中顺手重构。其他既有范围外建议未处理。

实现、验收和合并均已完成；依据本次授权，以 completed 原因关闭本Issue。分支继续保留，本地工作树未切换或更新。
