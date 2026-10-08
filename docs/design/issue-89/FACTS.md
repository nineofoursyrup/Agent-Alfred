# #89 收件箱与运行迁移：事实依据

核对日期：2026-10-08（Asia/Shanghai）。本文件记录只读调查；不是产品验收结果。未运行产品／浏览器测试，未读取个人会话正文，未调用模型。

## 候选与来源

- 议题：[决定：收件箱与运行页面的设计迁移及证据呈现 / #89](https://github.com/nineofoursyrup/Agent-Alfred/issues/89)，父地图 [#85](https://github.com/nineofoursyrup/Agent-Alfred/issues/85)。实时读取时 #89 为 OPEN、无评论；本会话已按仓库约定认领为 `nineofoursyrup`，认领后读回一致。
- 原生阻塞依赖为 [#87](https://github.com/nineofoursyrup/Agent-Alfred/issues/87)，现已 CLOSED / completed；#86、#88 的已确认合同亦须继承。
- 本文档工作区基于 #88 固定归档 `0e9915b5f020a756776f3e67fe13f352da3bb983`，分支 `codex/issue-89-inbox-runs-design`。其产品源码、测试、脚本及依赖配置与主工作区 `22c8720e1ec874fd012cc79b086cd8aace4c79b6` 无差异。
- 主工作区原有 `CONTEXT.md` 的 Overview 词条和 `tmp/` 保留；本会话在独立工作区写设计文档。
- 领域词汇位于 [CONTEXT.md](../../../CONTEXT.md)，遵循 [仓库领域文档约定](../../agents/domain.md)，不另建平行 GLOSSARY。

## 已确认约束

| 来源 | 对本票的约束 |
| --- | --- |
| [#86 视觉决定](../issue-86/DECISIONS.md) | 中文深色；14 / 13 / 12px 字级；紧凑表格、清楚焦点、独立状态轴；缺失／未知／失败不能变成成功；普通文字对比度至少 4.5:1 |
| [#87 壳层规格](../issue-87/SPEC.md) | ≥1100 CSS px 为左 232px、中央弹性、右 MainBar 320px；窄屏面板互斥；一份 MainBar、一份 Stream；原页面地址和 Run 深链接保留 |
| #87 R03、R07、R08 | 浏览／预览／导航不切 Session；新建和继续须显式操作；Run 详情不复制正式回答；正文恢复只补内容，不重跑、不重试保存、不改记录状态 |
| #87 R04–R06、R10 | 返回、面板、输入保护和焦点遵守壳层；只保留非正文呈现身份，不把页面正文放入 history state 或通用缓存 |
| [#88 D06、D09](../issue-88/DESIGN.md) | Overview 最近运行固定为全历史五条终态＋至多一个当前槽；摘要不展示请求正文；详情重新读取，导航不切 Session；全部运行入口为 `/runs?filter=all` |
| [ADR-0027](../../adr/0027-historic-sessions-are-backfilled-verbatim.md)、[ADR-0028](../../adr/0028-admission-is-a-durable-handoff-not-a-response-receipt.md) | 旧消息不伪造 Run；准入未知不能当作已获准聊天；分段游标和旧会话保留 |
| [ADR-0041](../../adr/0041-trace-export-is-a-sanitized-manual-artifact.md) | trace 导出为净化后的人工工件；保留正文需明确选择；失效、取消、清理与浏览器下载边界不被换肤删去 |
| [ADR-0043](../../adr/0043-run-path-evidence-shares-trace-retention.md) | 单次路径及当时拓扑共享 trace 保留边界；裁剪后不拿当前图或结果补画历史；手动快照不冒充完整实时执行证据 |

## 已核对的现有入口

| 能力 | 代码证据 | 迁移义务／已知缺口 |
| --- | --- | --- |
| 收件箱会话列表 | [pages.js](../../../src/agent_alfred/ops/static/pages.js) 55–88；[sessions.py](../../../src/agent_alfred/runtime/sessions.py) 137–196 | 默认每页 25 条；活动会话与稳定历史分开读取；按活动序号排序，不按标题或显示时间重新排序 |
| 只读会话预览 | pages.js 91–145 | 现有消息预览、会话 Run 列表各有分页；点击会话标题不改变 MainBar Session；消息与 Run 不是同一集合 |
| 显式继续／新建 | pages.js 94–95；[app.js](../../../src/agent_alfred/ops/static/app.js) 649–674、764–790 | 继续才切 Session 并恢复相应草稿；发送中及当前聊天 Run 尚未收尾的限制须保留 |
| Run 列表与筛选 | [runs.js](../../../src/agent_alfred/ops/static/runs.js) 24–67、109–189；[api.py](../../../src/agent_alfred/gateway/web/api.py) 715–733 | 当前有 `all` / `chat` / `system`、分页、活动槽与 Run 定位；显示 purpose、结局、入口／受理时间及准入标记 |
| 预览摘要披露 | api.py 905–930；[runs.py](../../../src/agent_alfred/runtime/runs.py) 348、451–464 | Run 总列表 API 提供再次脱敏的 `prompt_preview`，但没有 admitted-only 门；现有列表 UI 未显示它。若增加摘要，须先冻结准入披露边界，不能因为字段存在就全部展示 |
| Session 标题来源 | sessions.py 198–220 | 已有 admitted-chat 筛选；不得用总运行列表的任意输入替代 Session 标题来源 |
| 普通列表的记录状态 | api.py 905–931 | 普通 Run JSON 不含 `recording_state`。独立持久记录状态的展示需要延续 #88 已确认的无正文证据补充，不能由 finished 推断 recorded |
| 正式回复定位 | app.js 67–110、649–674；runs.js 的页面参数 | 现有正文恢复由 MainBar 当前 Session 驱动；继续只接受 Session，运行详情没有精确 Session/Run 回复定位入口。这是本票已确认的补齐项，尚未实现 |
| Step / Attempt 过程 | runs.js 312–459 | 按事件 seq 与 step_index 分组；committed、aborted、运行中、结果未知、发送待确认分开；作废内容不成为正式回答，费用仍保留 |
| 输入及辅助调用 | runs.js 268、590–638 | 检索门、Skill 选择、准备失败／未确认、调用用途、字符限额、实际来源、历史 Run 和工具账引用均有真实入口 |
| 消息分流与聚合 | runs.js 639 起；[aggregation.js](../../../src/agent_alfred/ops/static/aggregation.js) | 分类模型、分流决议、上下文恢复、普通回退以及聚合来源／结果事实不能因参考稿未画出而删除 |
| 过程与账目分离 | runs.js 459 起；[evidence.py](../../../src/agent_alfred/runtime/evidence.py) 136、183 | live / available / partial / pruned / unavailable / too_large 区分；历史上限 32 MiB；缺少 trace 时仍显示可证明账目，不推断过程顺序；普通详情不展开 thinking 正文或工具参数 |
| Graph 单次路径 | [topology.js](../../../src/agent_alfred/ops/static/topology.js) 103、145、252、271 | 默认收起，展开／重读才请求；保留当时拓扑、图文对照、wave、节点与 Step/Attempt 锚点、缩放／平移和键盘入口；网络失败可标旧，权威 pruned 等结果须清旧图 |
| trace 导出 | [trace_export.js](../../../src/agent_alfred/ops/static/trace_export.js)；[service.py](../../../src/agent_alfred/trace_export/service.py) 161、194 | 生成／取消／下载／清理／失效／过期完整流程；pending 拒绝生成；保存失败与包完整性分开；下载接收不等于用户文件保存成功 |
| 当前记忆引用 | runs.js 67–108、508 起 | 展开重新读取当前条目；断连隐藏正文，删除或版本变化不能把历史证据中的旧正文冒充当前内容 |

## 已核对的设计缺口

1. 请求摘要的披露门和持久记录状态来源见上表；有字段不等于有完整展示合同。
2. 详情没有精确 Session/Run 正式回复定位入口；当前 `resume(session_id)` 仅加载普通会话历史。
3. [app.js](../../../src/agent_alfred/ops/static/app.js) 581 起重建页面；尚无完整的详情来源、原列表分页／滚动／焦点恢复机制。
4. committed 文本在 Step 和 Attempt 中可能重复呈现；缺少完整的长内容摘要／展开规则。普通运行列表没有独立空／加载状态，过程读取失败没有独立重试按钮；不能把 Graph 已有的恢复样式算作全页能力。
5. `aggregation` 筛选口径不一致：runs.py 401–404 的列表 SQL 将聚合纳入 `chat`；同文件 210–222 的 `classify_purpose` 及 runs.js 111、134 仍仅将 `chat` 归对话，其余归系统。这是静态发现，尚未通过执行复现。

针对第 5 项，已回查 [AGGREGATION-SPEC-r1](../issue-27-manual-aggregation-spec.md) 79、151、231 行和 [实施记录](../../implementation/issue-27-aggregation.md) 37 行：聚合有独立 purpose、关联 Session、进入人工会话展示与精确正文恢复，同时仍被排除在自动工作窗口／聚合窗口／提炼来源之外。合同未明确冻结三个列表筛选的中文分组标签；本票可以确认一致的呈现归属，但不得把聚合 purpose 改成 chat 或改变自动消费边界。

后续行为验收可复用 [inbox.spec.js](../../../tests/browser/inbox.spec.js)、[runs.spec.js](../../../tests/browser/runs.spec.js)、[run-path.spec.js](../../../tests/browser/run-path.spec.js)、[trace_export.spec.js](../../../tests/browser/trace_export.spec.js)、[aggregation.spec.js](../../../tests/browser/aggregation.spec.js) 及对应确定性读侧测试。源码／测试阅读不代表这些用例在本轮运行或已通过。

## 参考身份与边界

已检查 #86 封存的 [原始参考](../issue-86/baseline/r1/reference/syrup-original.html) 中 Loop 区，以及 [获批运行页边界截图](../issue-86/baseline/r1/screenshots/A-runs-edge-1280.png)。参考中的 `TURNS` / `ITERATIONS`、样例检索／费用／耗时与搜索控件不能替代本项目 Run / Step / Attempt 定义或自行扩充业务能力。正式字级、状态和控件规则以 #86 冻结基线为准。

## 第一轮确认后的补充核对

- [replies.py](../../../src/agent_alfred/runtime/replies.py) 44–139 的精确正文读取支持 chat／aggregation、同进程未记录投影及精确已记录行；no_reply、withheld、实例失效与不可读是不同边界。[api.py](../../../src/agent_alfred/gateway/web/api.py) 751–788 已映射相应响应。
- api.py 790–804 的 MainBar 历史只接受 Session、limit 和游标；[runs.py](../../../src/agent_alfred/runtime/runs.py) 570 起提供分段历史，没有按 Run 返回历史位置的读取。故 Q5 的精确定位需要有界的定位补充，不能把正文接口声称为已有完整定位能力。
- [app.js](../../../src/agent_alfred/ops/static/app.js) 220–294 已处理 Run 对按身份合并、无 Run 旧消息分段、保存等待与阅读锚点；迁移须保留，不能按文本相同去重。
- [topology.js](../../../src/agent_alfred/ops/static/topology.js) 145–181 保留手动快照时间、上一实例／读取失败旧图标记及显式 Step/Attempt 锚点；[trace_export.js](../../../src/agent_alfred/ops/static/trace_export.js) 的生成任务状态查询、记忆／连接失效和离页清理均为既有资源协议。Q6 的列表显式刷新不能顺带取消这些机制。

## 整合合同的继承补充

- [AGGREGATION-SPEC-r1](../issue-27-manual-aggregation-spec.md) 231 行已经要求 Run 列表同时保留 purpose 与 Graph 结局，不能只凭 completed 推断有草稿；本次整合在 D05／D06 和 I02 显式补齐，不作为新的产品问题。
- [archive.py](../../../src/agent_alfred/trace_export/archive.py) 200–206 与 [trace_export.spec.js](../../../tests/browser/trace_export.spec.js) 的历史未知场景明确保留 verified_complete／known_incomplete／unknown；记录 unknown 不单独证明源已知不完整。D10 和 CE-21 已按该既有合同澄清。

两轮 Q1–Q11 均已由用户「全按建议」确认；用户随后回复「确认」，整合合同已定稿。未发布 resolution、关闭 #89 或修改父地图。
