# Overview 数据来源核验

核验日期：2026-10-08（Asia/Shanghai）。产品基线 `22c8720e1ec874fd012cc79b086cd8aace4c79b6`；本工作树继承的 #86/#87 归档只增加设计材料，产品源码相同。本文记录观察，不代替用户的设计决定。

## 候选指标

| 内容 | 已有事实来源 | 缺口及边界 |
| --- | --- | --- |
| 运行记录数 | `AccountingSnapshots.create()` 对固定范围的全部 `runs` 行生成 `summary.run_count` | 包含所有准入状态与运行用途；不等于成功数或已执行数。排除未关联 Run 的旧消息。时间损坏时有 `unresolved_membership` |
| 模型费用 | 持久 `runs.telemetry` 的 Attempt 账目；`summary()` 产生精确、估算、未知及覆盖计数 | 已作废 Attempt 也计账；缺失用量与无价格依据不能当零；工具服务计量分列，不能与模型 USD 相加 |
| 端到端平均延迟 | `run.finished.duration_ms` 是事件字段，可能存在于 retained trace | 持久 Run telemetry 没有统一整体时长；图时长不含图前的记忆准备与检索门；`finished_at` 还包含收尾落库时序，不能直接代替端到端性能事实 |
| 检索统计 | `/api/memory/statistics` 对持久 chat Run 的检索证据计算比例 | skip=S/(S+H+M)、hit=H/(H+M)、error=E/(S+H+M+E)；零分母为 null；排除未记录／未评估／不完整／旧或不支持数据，不代表 gate 模型调用比例 |
| 当前记忆条目数 | semantic 的 `facts` 与 episodic 的 `episodes`；既有查询返回分页及 `memory_revision` | 没有 total 聚合。跨页修订变化可使游标失效；不能用首屏长度替代全量。程序性记忆来自 Skill 文件，不属于这两个 Store |

源码：[账目](../../../src/agent_alfred/runtime/accounting.py)、[用量序列化](../../../src/agent_alfred/runtime/telemetry.py)、[Run 收尾](../../../src/agent_alfred/runtime/recording.py)、[聊天图前置处理](../../../src/agent_alfred/runtime/chat_graph.py)、[图时钟](../../../src/agent_alfred/graph/context.py)、[检索统计](../../../src/agent_alfred/memory/statistics.py)、[记忆分页](../../../src/agent_alfred/memory/storage.py)、[Memory HTTP](../../../src/agent_alfred/gateway/web/memory_api.py)。

## 时间与集合

- Ops 支持 `today`、`7d`、`30d`、`all` 与自定义日期半开区间，显式 IANA timezone；`7d` 是包括当天在内的七个自然日。记录归属按 `started_at`，缺失时回退 `accepted_at`。
- Memory 检索统计默认滚动七天 UTC，按 `accepted_at` 选取 `[since, until)`；不能把两个默认值同标「最近 7 天」并当作相同集合。
- 账目在一个 SQLite 读取事务中固定成员、用量、工具计量与裁剪事实，退出事务后冻结价格并汇总；响应含 `computed_at`、`process_instance_id`、`snapshot_id`、`expires_at`、规范化范围、价格身份和 `unresolved_membership`。
- `summary.run_count` 来源为完整集合；`page()` 每页最多 50 条只限制明细。超出快照资源配额时失败，没有成功返回截取统计的承诺。
- 账目快照默认 TTL 为 900 秒、最多 8 份、总配额 64 MiB；总览若复用，须考虑反复进入产生快照的生命周期，但这不是另建新计费事实的理由。

## 最近运行与记录状态

[`runtime/runs.py`](../../../src/agent_alfred/runtime/runs.py) 的 `list_runs()` 按 `(activity_revision, run_id)` 降序给出终态 Run；非终态另列，避免分页重复。默认 25 条，可设置条数；`/api/runs` 没有时间窗口或 total。`chat` 筛选包括 chat 与 aggregation，`all` 包含系统和未知用途。

[`gateway/web/api.py`](../../../src/agent_alfred/gateway/web/api.py) 的摘要载荷含用途、准入状态、来源、脱敏请求预览、阶段、结局与时间，但没有整体费用、延迟或独立的持久 `recording_state`。当前未保存状态来自 Host 的 active Run 和 `unrecorded_terminal_projection`；摘要不能因 `phase=finished` 就新增「已保存」判断。后续需定义并复用可证明的状态来源，缺失时明确未知。

现有 [`runtime/evidence.py`](../../../src/agent_alfred/runtime/evidence.py) 的持久读取依据 Run telemetry 投影 `recorded`；该通用接口同时加载 trace，不适合为了总览徽标逐条读取。总览需提供同一事实边界的无正文元数据读取。重启恢复的 `finished/interrupted` 可以没有该证据。现有 [`runtime/snapshot.py`](../../../src/agent_alfred/runtime/snapshot.py) 还会识别未进入执行、交接与收尾失败且无法提供 Web 详情的特殊形态；总览沿用既有可定位性保护，不制造失效链接。

Run 深链接 `/runs/<run_id>` 已存在；按 #87，它不选择该 Run 所属 Session。请求预览目前经过中央 Redactor，但 Q3 已选择总览不展示请求摘要与正文。

## 记忆库存与失效

[`memory/storage.py`](../../../src/agent_alfred/memory/storage.py) 中的删除是条目删除，`_recent()` 枚举现存条目；`human_protected` 表示对修改的保护，不是另一份条目。待批准提炼候选与已保存条目是不同材料。当前保存数量并不证明每条都允许进入下一次自动输入。

[`MemorySync`](../../../src/agent_alfred/ops/static/memory.js) 在连接后核验持久修订；连接、实例、修订变化使先前读取失效。旧响应不能恢复旧内容。总览计数的失效规则需显式承接这种身份区分，即使它不展示记忆正文。

## 架构与详情入口

- RuntimeHost 是 CLI 与 Web 的共享所有者，RunCoordinator 控制同一准入租约；不是两个互不相干的执行服务。
- 普通对话可使用消息分流图，先前准备的记忆与上下文被图使用；工具、模型和记忆不是每条 Run 必经的固定串行链。
- 手动聚合是明确发起的独立 workflow，有自己的来源选择与草稿边界。
- Behaviour 的流程拓扑是声明；Run 的当时拓扑与执行路径是证据，随 trace 保留。静态架构组件存在不证明连接可用、工具已授权或特定 Run 已执行。
- 当前 Ops 页面支持通过 URL 预填时间范围与时区，但进入页面通常创建新快照；不能仅传 `snapshot_id` 就宣称打开原来的固定摘要。Runs 列表没有期间筛选。Memory 的当前列表也会晚于总览计数的观察时刻。
- 当前 Host 的 `_accounting_prices()` 已组合用户价格、可用目录和包内静态价格；应按实际计费维度判定 estimated / unknown，不能把旧版 Dashboard 说明中「未安装价格解析器」的描述当作当前代码事实。

源码：[运行宿主](../../../src/agent_alfred/runtime/host.py)、[对话处理](../../../src/agent_alfred/runtime/chat_graph.py)、[手动聚合](../../../src/agent_alfred/aggregation/graph.py)、[账目页面](../../../src/agent_alfred/ops/static/accounting.js)。保留边界见 [ADR-0032](../../adr/0032-tool-metering-survives-trace-pruning.md)、[ADR-0042](../../adr/0042-routing-statistics-use-durable-evidence-and-semantic-versions.md)、[ADR-0043](../../adr/0043-run-path-evidence-shares-trace-retention.md)。

## 所需补充与当前验证

已确认需要补充语义／情景完整计数的读取合同；范围、源身份、局部错误、覆盖信息、记录状态、跨页范围与临时资源已纳入 [DESIGN.md](DESIGN.md)。这些仍是待实施的读取能力，不是当前已经存在的 API。

此轮只读源码、核验远端议题及既有文档，未读取用户私人运行数据，未发起实际模型／工具执行；不需要以产品全量测试证明文档调查。
