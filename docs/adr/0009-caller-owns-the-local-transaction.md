# 本地事务归调用方所有，Store 不得自行提交

记忆提炼要求「事务成功后才标记原聊天为已提炼，失败时不得丢失原始记录」。
默认 FTS5 后端与 `agent_log` 同在一个 SQLite 文件里，做得到；
托管或向量后端**不可能**与本地 SQLite 原子提交。这与 [#4](https://github.com/nineofoursyrup/Agent-Alfred/issues/4) §7 里
`local_write` 与 `external` 的分野是同一个问题的另一个尺度，因此沿用同一套形状：
**Store 声明能力，调用方选路。**

Store 上有一个静态声明 `transaction_mode: "local_atomic" | "external"`，
构造后不变。**Protocol 的方法签名里不出现任何 txn / conn 参数**——那会把 SQLite
钉进接口，托管后端根本不该认识本地数据库。同事务性来自一个**构造期事实**：
`local_atomic` 的 Store 在构造时被注入了提炼器手里的**同一个连接**
（与 [#4](https://github.com/nineofoursyrup/Agent-Alfred/issues/4) §4「静态依赖按最小权限注入」同构）。

由此得出本 ADR 最容易被善意重构掉的那条约束：
**`local_atomic` 的 Store 绝不调用 `commit()` / `rollback()`。**
提交时机唯一属于调用方。「Store 自己提交更内聚」是个很自然的想法，
但踩掉它之后的失败模式是**「提炼标记落了、事实没落」**——
用户的对话被标成已提炼、再也不会被重新提炼，而提炼出的事实根本没进库。
这是本项目最难查的一类丢数据。

`external` 模式走 [#4](https://github.com/nineofoursyrup/Agent-Alfred/issues/4) §7 已定的状态机：提炼器**先**持久化一张
带确定性批次 ID 的 `consolidation_batches` 任务表并为每个写项生成稳定操作 ID，
再按 `started → succeeded / failed / unknown` 推进，**只有全部确认成功才标记已提炼**。
`unknown` 留待重试或人工核对——宁可重复提炼一次，不可丢事实。
`consolidation_batches` 归**提炼器**所有：不复用 `tool_ledger`（提炼不是工具调用，
混进去会让 Dashboard 的 Tools 页渲染出用户从没调过的条目），也不下放给各 Store
（那会逼每个后端重复实现一遍流程持久化）。

## Consequences

一致性测试按 `transaction_mode` 分档：`local_atomic` 必须通过
「调用方回滚后，`save` 的内容 `get` 与 `search` 都查不到」；
`external` 不跑这条，改跑「同一稳定操作 ID 重放两次只产生一条」。
从 Protocol 外部就能验出 Store 是否越权提交，因此**不需要**用假连接去窥探
`commit` 有没有被调用——那种测试断言的是实现而非契约。

`external` Store 写入后不保证立即可检索，所以契约测试要先过一道
**可见性屏障**（有界退避轮询，可见即返回，超时则携证据令测试失败）。
它是**测试提供者注入的夹具**，`local_atomic` 用空操作；
生产 Protocol 与提炼器**均不得引用它**。一旦它出现在 Protocol 上，
生产代码就能写出「轮询到能搜着为止」的路，而那条路在超时时会退化成伪造的成功判定：
搜不到究竟是还没同步、还是根本没写进去，轮询分不出来。
远端写入是否成功**只能**由写入回执与状态机判断，
**检索暂不可见绝不能被读作写入失败，也不能被读作成功。**
