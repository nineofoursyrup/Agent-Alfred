# 工具预算只在启动前生效，不伪造可取消的超时

同步 Python 函数**无法被抢占中断**：`signal.alarm` 只在主线程有效，线程杀不掉。
因此我们不设「工具超时」，只设**预算**（`budget_s`）：Registry 用单调时钟算出绝对
deadline（`min(now + budget_s, step_deadline)`）经 `ToolContext` 传给工具，
**预算耗尽时不启动该工具**。启动之后真正的中止只能由工具自己的 IO 层执行——
网络工具设 socket / HTTP 超时，MCP 发 `notifications/cancelled`，
需要硬中止的工具必须跑在可终止的子进程里。

被否掉的是 `ThreadPoolExecutor` + `future.result(timeout)`。它看起来实现了超时，
实际只是**放弃等待**：遗留线程仍在跑，仍会写库、仍会产生外部副作用，
而调用方已经把「超时」当成「没发生」回喂给了模型。这是伪造执行结果，
撞的是本项目最硬的那条红线。既然启动后无法中止，**唯一诚实的控制点就是启动前**。

deadline 必须是**绝对值**且取自 `time.monotonic()`：串行批里后面的工具要扣掉前面
已消耗的时间，传相对秒数等于让每个工具重新拿满预算；用墙钟则 NTP 回拨会让 deadline
瞬间过期或永不过期，与「事件排序只认 `seq` 不认时间戳」是同一个理由。

## Consequences

超时的回喂文案必须说「已放弃等待，该工具可能仍在后台运行，其结果未知」，
**不得说「已取消」**。相应地，工具账上 `external` 工具需要一个 `unknown` 终态——
它不得被读作失败，也不得被读作成功。

本决策同时消解 [ADR-0001](0001-model-client-fully-synchronous.md) 的一处自相矛盾：那份 ADR 把「一批并行工具调用」列为
v1 唯一获准的线程池场景，同一段又要求数据库写入一律串行，而 v1 的内置工具全在写 SQLite。
裁定为 **v1 全部工具串行执行**，`parallel_safe` 字段保留但恒为 `False`，
事件里的 `parallel_group` 恒为 `None`。并行只对 IO 密集的外部调用有价值，
本地 SQLite 写并行的收益是负的（写锁竞争）。等真出现 `parallel_safe=True` 的工具时再回写修订。

代价是每个带 IO 的工具都必须**自己**把 deadline 落实到传输层——忘记落实的工具会超预算运行，
而 Registry 帮不了它。这是用「可能超时」换掉「谎称已取消」。
