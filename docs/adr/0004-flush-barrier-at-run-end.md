# 事件异步派发，Run 收尾同步等待持久性关键 Sink 后再原子落库

事件派发全程非阻塞：`FanOutSink` 在一个短临界区内原子分配 `seq` 并向各 Sink 的**专属队列**
投递，锁内绝不写终端、绝不碰磁盘。动机是 [ADR-0001](0001-model-client-fully-synchronous.md) 允许的唯一并发场景——并行工具批——
会让多个工具线程同时发事件，而慢消费者绝不该拖住业务。

但异步落盘与「写盘失败必须可见」直接冲突：drain 线程发现 `ENOSPC` 时，
写回复的事务早已提交，数据库里于是躺着一条**声称追踪完好**的记录，磁盘上却什么都没有。

因此在 Run 收尾处设一个**故意的同步点**：发出 `run.finished` 后，只对
`flush_at_run_end=True` 的 Sink 同步 `flush()` 并取得明确的 `FlushResult`，
再据此把回复、Run 级 telemetry 与 `trace_incomplete` 标记**原子写入同一个事务**。
渲染类 Sink 不参与该屏障。

考虑过的两个替代都更糟。让 TraceSink 破例在锁外同步 append，会让并行工具线程
随机承担磁盘延迟，而整套队列设计的动机就是把 IO 赶出发射路径。让 telemetry 先写、
事后 `UPDATE` 补记失败，则需要一个「补写失败的补写」，无限递归。

## Consequences

`trace_incomplete` 由此获得精确定义：**在该 Run 的持久性屏障处，关键 Sink 写盘未成功**。
没有这个屏障，这个词就只能表示「某个时刻某处可能写失败了」，无法落进任何一条断言。

代价是每个 Run 收尾多等一次本地 flush，并且 `EventSink` 必须把 `flush` 与 `close` 分开——
屏障是每 Run 一次，而 Sink 的生命周期是整个进程。

关键 Sink flush 失败**绝不中断 Run**：模型钱已经花了、回复已经生成，
因为写日志失败而把用户的回答扔掉，是拿最贵的东西赔最便宜的东西。
