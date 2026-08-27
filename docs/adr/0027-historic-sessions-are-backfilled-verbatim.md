# 历史 Session 逐字回填，旧消息不伪造 Run

v1 的 `agent_log.session_id` 是一个没有权威表、没有外键的自由文本列。
v3 新增 `sessions` 之后，它突然有了权威表——升级前的会话必须有个交代。

## 回填，而不是丢弃或规范化

为 `agent_log` 里**每一个不同的既有 `session_id` 原值**插入一条 `sessions` 行，一对一。
关键是**原值**：「Session 标识仅由服务端生成」是**创建 API 的规则**，
**不是数据库的格式约束**。把它写成 CHECK，等于让新规则去否决已经发生过的历史。

`created_at` **逐字复制**该 Session **最小 `agent_log.id`** 那一行的时间文本。
不对时间文本取 `MIN`、不做字典序比较——`agent_log.created_at` 从未受过严格格式约束
（这一点在 [#12](https://github.com/nineofoursyrup/Agent-Alfred/issues/12) 就已经写死：
CHECK 是形状提示不是保证），按文本取最小值取到的可能根本不是最早那条。
`id` 是唯一真正单调的东西，就用它。

`activity_revision` 按各 Session 的**最大消息 id** 从旧到新依次分配，
于是最近写过消息的历史会话排在更前，而不是全挤在列表末尾。
重跑迁移不再分配——否则每次升级都会把历史会话重新洗一遍顺序。

## 旧消息走第二段游标

回填出 `sessions` 还不够：旧消息没有 `run_id`，而收件箱与 MainBar 的分页都是**按 Run** 的，
旧会话会回填出来、点开却是空的。

诱人的修法是**给旧消息造一个合成 Run**，一切分页代码就都不用动了。
否掉：那会往 Run 索引里写进一批从未发生过的 Run，
而 Run 索引正是 [ADR-0023](0023-interrupted-means-the-index-cannot-prove-a-terminal-outcome.md)
里唯一有资格证明终态的东西。往唯一的权威证人嘴里塞话，代价远超省下的那点分页代码。

改为**分段游标**：先按 `(activity_revision, run_id)` 返回新数据的消息对，
该段耗尽后按 `agent_log.id` 返回 `run_id IS NULL` 的旧消息。
无 Run 的历史 Session，标题从首条旧用户消息的用户可见文本脱敏限长派生。

## Consequences

分页 API 与消费它的界面都要能表达「现在在第几段」，跨段续页不得重复或漏行。
这是这条决定的全部成本，且它买到的是一条硬边界：
**数据逐字保留**与**旧会话可见**同时成立，谁都不用向对方让步。
