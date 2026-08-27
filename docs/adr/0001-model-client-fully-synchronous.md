# 模型客户端全同步，v1 不引入 async

Dashboard 是一个本地 Web 服务，常规做法会让模型调用走 async——但 `async` 会传染：
一旦 Adapter 是协程，业务循环、工具执行、记忆后端、事件汇全部得跟着变色。
在**单用户**假设下（地图前提，已写死）这换不来任何吞吐收益，却要付出三笔代价：
每次调用起停一个 event loop、CLI 的 Ctrl-C 信号处理复杂化、以及将来 Dashboard 走 ASGI 时
在已有 loop 内嵌套调用直接抛 `RuntimeError`。

因此 v1 **彻底同步**，`async` 一词不出现在代码库里；SSE 流式读取用同步逐行读即可，不需要 event loop。

## Consequences

需要并发的地方改用线程池，且**准入条件严格**：任务必须显式声明 `parallel_safe`、
无共享可变状态、无顺序依赖、失败可独立归因。v1 里唯一符合的实例是**一批并行工具调用**。
数据库写入、事件发射、记忆读写以及其他带副作用的操作**一律串行**。
（本 ADR 落笔时事件消费端叫 Observer，已由 [ADR-0003](0003-central-fail-closed-redactor.md) 一带的事件协议改名为 `EventSink`。）

同步还有一个被动收益：离线确定性测试可以直接调用，不必管理 event loop，
也让 `ScriptedModel` 驱动整条业务循环这件事保持廉价——而这是每张施工票的强制验收项。
