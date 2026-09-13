# Graph 引擎实施接缝

唯一验收合同：[GRAPH-SPEC-r1](../design/issue-25-graph-spec.md)。本文件说明机械接口，不变更 R/Q/IC/CE。

## 构造与执行

```python
from agent_alfred.graph import (
    GraphBuilder, GraphRegistry, NodeOutcome, TerminalSpec, fn_node,
)

builder = GraphBuilder("echo").declare_input("message")
builder.add_node(
    "reply",
    fn_node(lambda state, context: NodeOutcome({"reply": state["message"]})),
    required_reads=("message",),
    writes=("reply",),
    terminal=TerminalSpec.result("reply"),
)
registry = GraphRegistry({"echo": builder})
result = registry.get("echo").invoke({"message": "你好"})
assert result.output == "你好"
```

`GraphBuilder` 是可变作者入口，`compile()` 生成冻结的 `CompiledGraph`。
`add_edge(source, target)`、`add_error_edge(source, target)` 与
`add_conditional_edges(source, path, path_map, required_reads=(), optional_reads=())`
构造边；`path_map` 的值可为单节点名或 fan-out 节点序列。路由只返回声明标签。
`describe()` 返回缓存拓扑的独立 JSON 视图；修改它不影响执行或完整 SHA-256。
输入与节点声明读写是单写者所有权；required 来源用布尔控制流分析验证，波序决定可见性。
`loop` 与控制保留名、双下划线前缀不可用作作者节点身份。

状态边界递归冻结 Mapping、列表/元组、集合及标量；不接收持有任意可变行为的业务对象。
optional 缺席保持键缺席。普通节点只读波初值，路由额外看到自身源的候选写入。
节点写集和边决议均在波末生效；已接管普通失败不使同波成功节点重跑。

## 四工厂与 Run 所有权

- `fn_node(fn)` 调用 `fn(state, PureNodeContext)`；上下文只有 `node_id/error`。
- `llm_node(prompt, client=..., model=..., assistant=..., output_key=...)` 复用真实 Assistant 单 Step。
- `agent_node(..., tool_names=())` 使用完整 Assistant 循环；静态白名单同时限制模型定义与强行调用执行。空集不提供工具定义。
- `tool_node(tool_name, arguments, output_key=...)` 只接受注册名称。prompt/arguments 可为纯 state 函数；实际工具始终经过 Registry、业务服务、账本和计量。

模型和工具图由调用方提供 `GraphRunContext(budget=同一RunBudget, run_id=..., tools=..., ...)`。
一次 budget 只允许一个 Graph invoke；后续普通循环继续使用此 budget，不重置计数。
省略 context 只适合零模型图，默认模型预算为零。Graph 不提供自动重跑工具或用户选图入口。

现有 `AttemptLedger` 同时服务普通循环和图节点，保留中断携带的真实 Attempt/费用。
`context.model_results` 为原始计量，`context.steps` 单独标记波有效性；撤销不改 Attempt outcome。
成功波才提交 `context.transcript`；同波模型节点均从此前已提交转录出发。

零 Step 工具的持久三元身份使用 `(run_id, -1, "graph:" + node_id)`。
`-1` 是现有整数存储列中的独立执行命名空间，不代表申请了模型 Step；
模型 Step 始终非负，图每节点至多执行一次；公开事件的 `step_index` 仍为 `None`。
既有账表、旧模型调用身份及已发布迁移保持兼容，无需改写或重建历史账目。
Registry 的受限视图共享原 Registry 的计量、业务账和当前授权事实。
副作用三态读取这些持久事实，已知输入拒绝不因进入工具函数而误记为发生副作用。

## 结果与唯一收尾

五态结果沿规范。`Failed.forced_stop` 是不可变的可信 Run 收尾事实，
保留原 reply/outcome/error 和固定收尾字段，消费者无需依赖事件或可变上下文来识别它。
删除、文件/MCP 未核验和计量失败都优先停止，不进入普通/error 后继。

外层取得 GraphResult 后调用 `settle_graph(recorder, item, result, context)`；
`item` 必须属于已准入、已进入运行阶段的同一 Run。此函数只适配结局与遥测，
正式消息仍由真实 `RunRecorder.settle` 独占写入。
删除成功但拓扑未完成时保留 Run completed 与固定回执；`NoAction` 不造助手回复。
既有普通循环默认记录行为不变。

离线验收位于 `src/agent_alfred/evals/deterministic/test_graph*.py`：
真实编译/执行、SDK 网络替身、SQLite/文件、受控 MCP 子进程和真实记录器。
干净安装脚本也执行 GraphRegistry 与四工厂。没有付费模型、私人服务或 Host 用户选图验收。

评审回归补充：非法非 Mapping 写集在波末触发 invalid_writes，撤销整波，不进入 error 恢复。输出 JSON 投影将集合确定排序为数组、bytes 表示为 `$bytes_hex` 对象、非字符串键映射表示为 `$map` 键值对数组；GraphResult 本身保持深冻结值。副作用聚合按 external_tool_operations 精确关联 Step/call 的计量证据，`http_not_sent` / `transport_not_sent` 不计为已发生，也不能抹掉同 Run 其他已发生副作用。
