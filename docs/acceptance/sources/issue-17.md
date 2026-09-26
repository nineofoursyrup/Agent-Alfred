# #17 实现：切片② — 检索门与语义/情景记忆 FTS 后端

来源：https://github.com/nineofoursyrup/Agent-Alfred/issues/17

## Question

切片②：检索门 + 语义 / 情景记忆的 SQLite FTS5 后端。

**检索门**：

- 优先用小模型输出结构化决定 `{retrieve, query, reason_code}`（最终闭合形状见下述 Memory 规范）。
- 小模型**由用户指定；不指定时回落到主模型**（建图已定）。
- 解析失败或模型不可用 → **确定性回退**（本票要定死回退规则并测试它）。
- 普通寒暄、简单计算不该检索；涉及用户偏好、人物、历史安排与"之前"语义时应检索。
- 检索决定必须进 Observer 事件与遥测（Dashboard 要显示 skip/hit 比例）。

**记忆后端**：

- 按 `决定：三类记忆的后端 Protocol` 实现语义（facts + FTS5）与情景（episodes + FTS5）两个默认后端。
- 事实的增、删、改、查；检索命中要能追溯到来源。

### 验收

- 检索门 skip / retrieve 两条路径的离线测试。
- 解析失败回退的离线测试（**回退函数不能只返回常量**，否则测试无信息量）。
- FTS 检索测试；一次真实的"记忆命中"演示。

## Memory 裁决规范（权威补充）

[完整独立规范](https://github.com/nineofoursyrup/Agent-Alfred/issues/31#issuecomment-5588135839)。本段明确修订前文不一致的旧表述；其余原范围保留。证据基线 `55418dad266eed4df8a3e40670a069e3dcddc65d`；实施须核查实际起点，不能用旧主工作目录的HEAD替代。

### What to build

R01–R08及R10/R11的后端接缝。默认Store、版本/保护与来源最小记录、共享命令/事务/回执、检索门和实际请求证据。完整遗忘由TF，HTTP和页面由TM。

### Acceptance criteria

- [x] A01：Given 一条明确保存，空库；When 共享命令提交；Then get与FTS可读同ID，安全回执与最小账同事务。
- [x] A05：Given 规范化相同、大小写不同、同subject异文夹具；When save及重启重送；Then 同键同ID保留首文；大小写不同不合并；同主题异文不覆盖。
- [x] A06：Given 同记录两个版本编辑者；When 后者交旧expected_version；Then 原子冲突，无覆盖；duplicate_conflict两条都不变。
- [x] A07：Given 无变化编辑、真实编辑、重复save；When 执行；Then 仅真实正文/区间变化增版本，origin不改。
- [x] A08：Given 注入事务中间故障；When save/update/delete回滚；Then 业务、FTS、账和成功回执共同未提交。
- [ ] A09：Given 写入已提交但HTTP响应丢失；When 查/重送同operation_id并重启；Then 回同一提交结果，无重复副作用；不同参数拒绝。
  - [x] #17 后端验收：共享命令的 operation_id 查询/重送、重启回执、参数冲突及无重复副作用已验证。
  - [ ] 下游集成验收：#46：HTTP 响应丢失后的端到端恢复待集成。
- [x] A10：Given 已保存后又删除、旧HMAC key不可比；When 读旧回执、重送；Then 不复活正文；不可比不被当作可重做。
- [x] A11：Given 已有一条真实FTS事实，聊天窗口无原句；When 下一Run查询固定相关词；Then Store真命中，origin和版本正确，相关/无关夹具不凑满limit。
- [x] A12：Given 主模型多Step及Attempt重试；When 完成同Run与下一Run；Then 同Run一次gate；下一Run重新评估；系统probe不检索。
- [x] A13：Given 门显式指派失败/未指派；When gate选择；Then 前者规则回退，后者primary；不暗换指派。
- [x] A14：Given 门调用重试、5秒/Run deadline可控；When 注入超时；Then 共享deadline/Step，真实Attempt全部计费用；Run耗尽不延时。
- [x] A15：Given max_steps=0及1；When 分别运行；Then 0无请求；1门用完后回答0请求，selected不冒充实际入模。
- [x] A16：Given 白名单、纯算式、混合个人句、普通知识句；When 门不可用/非法输出；Then 前两skip、后两原文查库；规则非恒量且不求值。
- [x] A17：Given 两库各5条，首条或中间超额；When 计量并筛选；Then 各自4000码点、完整前缀、不借额度、不跳长项。
- [x] A18：Given 0命中、部分可用、命中全超限；When gate完成；Then miss继续；partial注明；all_excluded停止答案但仍计hit。
- [x] A19：Given semantic失败或episodic失败；When 查询；Then 明确error，未知数量null，部分资料不进入回答。
- [x] A20：Given 已选引用及有效工作窗口；When 捕获实际Adapter请求；Then 单独参考user消息位于窗口后问题前；不入agent_log。
- [x] A21：Given 请求前失败与重试发送；When 捕获实际网络边界；Then 无发送不造Attempt；已发送每Attempt有正确引用、版本、purpose。
- [x] A22：Given gate完成、SSE丢失、Run未记录或旧数据；When 收尾与统计；Then 权威遥测不依赖SSE，旧/不完整不补0。
- [x] A23：Given S=2,H=3,M=1,E=1及全零夹具；When 查询固定7天范围；Then skip=2/6、hit=3/4、error=1/7；零分母无数据；范围可核对。
- [x] A24：Given 含独特subject/正文/query/异常标记及分数；When 序列化gate/遥测；Then 仅允许元数据，禁止字段/标记均不存在。
- [ ] A25：Given 宿主已有Run租约，页面并发写；When 工具继承权限并写、页面写；Then 工具不自锁，页面409；手动写不创建假Run。
  - [x] #17 后端验收：真实宿主租约下继承 WorkItem 权限不自锁；手动写返回 busy、伪造/过期权限拒绝，手动写不造模型 Run。
  - [ ] 下游集成验收：#19：实际工具循环接线；#46：页面并发写及 HTTP 409 映射待集成。
- [x] A27：Given 所选记录和无关记录均有索引；When delete提交；Then 所选get不存在/FTS不命中，不留正文墓碑；无关记录保留。
- [ ] A33：Given 同Run参考已选旧版本；When 工具编辑后下一Step；Then 旧引用剔除，无重新检索补位，实际引用证据一致。
  - [x] #17 后端验收：引用失效容器与发送前版本核验已交付；剔除旧引用、不重新检索补位的接缝已验证。
  - [ ] 下游集成验收：#19：真实工具编辑后的下一 Step 产品闭环待集成。
- [x] A39：Given 跨日区间、瞬时、空区间、不同offset同瞬间；When 边界查询；Then UTC半开相交正确；until边界排除；naive/逆序拒绝。
- [ ] A53：Given 既有自动提炼记录，明确重复保存同内容；When 执行后提炼尝试覆盖；Then 保留ID/正文版本并置人工保护；提炼原子重检后整批待确认。
  - [x] #17 后端验收：重复明确保存保留 ID/正文版本并置人工保护，自动写入的 Store 原子保护检查已验证。
  - [ ] 下游集成验收：#18：提炼器原子重检与整批待确认、批次账及审批待集成。

本票须阅读完整规范中上述R段与既有ADR。主责以外的接口实现由相应票验收，不能用替身PASS冒充下游已集成。原票已有的其它验收仍有效。

### Blocked by

- [施工 31](https://github.com/nineofoursyrup/Agent-Alfred/issues/31)
- [施工 5](https://github.com/nineofoursyrup/Agent-Alfred/issues/5)
- [施工 13](https://github.com/nineofoursyrup/Agent-Alfred/issues/13)

GitHub原生依赖是权威；本清单供独立阅读。阻塞清零与全票规范就绪是两件事。

### 实施与授权

独立agent、TDD、适用机械检查及固定候选Standards/Spec独立两轴评审。只做本票；先核查认领与worktree。没有自动提交、推送、合并、发布或关闭票授权。缺失且影响产品验收的选择交调度agent与用户，不猜默认。

### #17 验收收尾说明

已按合并提交 `193de2dc377a3eaaa674931ee1408e35e38d146a` 核对主责范围；详细逐项证据见本票验收总结评论。A09/A25/A33/A53 的父项保留未勾选，因为包含下游端到端集成；已完成的 #17 后端义务与待完成部分在子项明确区分。A27 仅验收本地记录硬删除，不代表 #45 完整遗忘。本票以 completed 关闭不改变 #18、#19、#45、#46 的状态或验收结论。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/17#issuecomment-5596188159

## Issue #17 验收总结：主责范围 completed

已依据本票及 [Memory 权威规范](https://github.com/nineofoursyrup/Agent-Alfred/issues/31#issuecomment-5588135839) 逐项核对。交付经 [PR #47](https://github.com/nineofoursyrup/Agent-Alfred/pull/47) 合入 main：`193de2dc377a3eaaa674931ee1408e35e38d146a`。本次关闭只确认 #17 主责，不代表跨票产品链路全部完成。

### 实现范围与固定候选

交付默认 semantic/episodic SQLite FTS5 Store、版本/保护/最小来源、共享命令事务及安全幂等回执、普通聊天 Run 检索门、实际发送引用与 Attempt/费用权威账、统计及读接口。门使用共享 Step 与绝对 deadline；两种 SDK 的 TCP/TLS、短写、异常原因和重试清理所有权均已修复验证。

[实现说明及验收映射](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/docs/implementation/issue-17-memory.md)。

v13 完整候选相对 `55418dad266eed4df8a3e40670a069e3dcddc65d` 共 **81 文件，无漂移**。PR head `cc117610bee561c110f0a70c9f3f8ed6de0c09c1`；合并树与已审 head 相同：`121872562fdeb86f68815347d9ef2a4e7e8de6e2`，不含 `.scratch` 或额外文件。

- Manifest SHA256：`fd13bd503b1230766b40b51e7e6d44dce2ef2192ee6813e24d97227d63ee2999`
- 完整 diff SHA256：`c2ec25680728132bbdd31f0ae269f59e110c46a2f19fd3f58630fe72e3c0c5e9`

### 逐项验收

原始三项验收均通过：skip/retrieve 离线路径、解析失败的非恒量规则回退、真实 SQLite 保存→下一 Run 命中→实际请求引用→删除→下一 Run miss 闭环。不是用模型复述或替身命中冒充真实 FTS。

正文主清单 **22 项勾选**；A09/A25/A33/A53 四个父项保留未勾选，各自增加已交付后端与待集成下游子项。

| 条目 | 结论及边界 | 已合并测试证据 |
|---|---|---|
| A01 | 通过：共享保存、同 ID get/FTS、回执与账同事务 | [test_memory_commands.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_memory_commands.py) |
| A05 | 通过：规范化幂等、首文、大小写与异文区分、重启重送 | [test_memory_stores.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_memory_stores.py) |
| A06 | 通过：旧版本及重复冲突不覆盖记录 | [test_memory_stores.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_memory_stores.py) |
| A07 | 通过：真实内容/区间修改才增加版本，origin 保留 | [test_memory_stores.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_memory_stores.py) |
| A08 | 通过：save/update/delete 中间故障使业务、FTS、账及回执共同回滚 | [test_memory_commands.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_memory_commands.py) |
| A09 | 后端通过；HTTP 丢响应端到端恢复留 #46，父项不勾选 | [test_memory_commands.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_memory_commands.py) |
| A10 | 通过：删除后旧回执不复活，旧 HMAC key 不可比拒绝重做 | [test_memory_commands.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_memory_commands.py) |
| A11 | 通过：真实 SQLite FTS、下一 Run 命中、来源/版本与实际请求引用；删除后下一门 miss | [test_runtime_memory_gate.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_runtime_memory_gate.py) |
| A12 | 通过：每 Run 一次门、下一 Run 重评、probe 不检索 | [test_runtime_memory_gate.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_runtime_memory_gate.py) |
| A13 | 通过：显式指派失败规则回退，未指派 primary，不暗换模型 | [test_runtime_memory_gate.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_runtime_memory_gate.py) |
| A14 | 通过：共享 Step/绝对期限，重试与失败 Attempt 保账；TLS 每次短写重算余量 | [test_tls_write_deadline.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_tls_write_deadline.py) |
| A15 | 通过：max_steps 0/1 不多发请求，selected 不冒充实际输入 | [test_runtime_memory_gate.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_runtime_memory_gate.py) |
| A16 | 通过：寒暄/纯算式 skip，其余原文查库，非法输出非恒量回退、不求值 | [test_memory_gate.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_memory_gate.py) |
| A17 | 通过：两库独立 4000 码点完整前缀，不借额度或跳过长项 | [test_memory_gate.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_memory_gate.py) |
| A18 | 通过：miss 继续、partial 注明、all_excluded 停止答案且仍计 hit | [test_memory_gate.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_memory_gate.py) |
| A19 | 通过：任一库失败为 error，未知数量 null，部分引用不入回答 | [test_memory_gate.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_memory_gate.py) |
| A20 | 通过：真实 Adapter 请求中参考 user 消息位于窗口后问题前，不入 agent_log | [test_runtime_memory_gate.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_runtime_memory_gate.py) |
| A21 | 通过：发送边界记录身份/引用/version/purpose，本地失败不造 Attempt | [test_memory_input_attempts.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_memory_input_attempts.py) |
| A22 | 通过：无 Sink 仍保留业务证据；旧/不完整证据不补零，统计不从 SSE 回放 | [test_runtime_memory_gate.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_runtime_memory_gate.py) |
| A23 | 通过：2/6、3/4、1/7、零分母和固定范围过滤 | [test_memory_statistics.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_memory_statistics.py) |
| A24 | 通过：门/遥测仅元数据，无正文、subject、query、分数及私有异常标记 | [test_memory_gate.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_memory_gate.py) |
| A25 | 后端真实租约权限/busy 通过；工具循环 #19、页面/HTTP 409 #46 待集成，父项不勾选 | [test_runtime_memory_gate.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_runtime_memory_gate.py) |
| A27 | 通过：本地硬删除 get/FTS 均消失，无正文墓碑且无关记录保留；不等于完整遗忘 | [test_memory_stores.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_memory_stores.py) |
| A33 | 后端引用失效/版本核验通过；真实工具编辑到下一 Step 留 #19，父项不勾选 | [test_memory_gate.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_memory_gate.py) |
| A39 | 通过：UTC 半开区间、边界/offset/瞬时/空区间及非法时间拒绝 | [test_memory_stores.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_memory_stores.py) |
| A53 | 后端幂等保存、人工保护及原子保护检查通过；整批提炼审批留 #18，父项不勾选 | [test_memory_stores.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_memory_stores.py) |

A14 的既有用量保留另见 [test_gate_attempt_preservation.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_gate_attempt_preservation.py)、[test_interrupted_attempt_accounting.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_interrupted_attempt_accounting.py)；A22 的非法/旧证据过滤另见 [test_memory_statistics.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/193de2dc377a3eaaa674931ee1408e35e38d146a/src/agent_alfred/evals/deterministic/test_memory_statistics.py)。这些是后端与公共 Runtime 的验收；不将工具、页面或提炼器替身当作下游已集成。

### 独立双轴评审与验证结果

最终 v13 完整候选由两个独立 agent 并行复审，未沿用旧版本 PASS：

- **Standards PASS，0 项发现**；独立故障/所有权/回滚回归 **181 passed**。
- **Spec PASS，0 项发现**；独立回归 **266 + 135 = 401 passed**。
- 两轴评审前后完整 81 文件清单、逐文件 SHA256、完整 diff 和 index 无漂移；原独立评审任务也确认双轴 PASS、0 项发现。

[合并提交 CI](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34312947524)：精确 head 为 `193de2dc377a3eaaa674931ee1408e35e38d146a`，**completed / success**。Python **2968 passed、1 deselected**；浏览器 **57 passed**；lint、skills、env consistency、typecheck、依赖等检查全部通过。[PR head CI](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34312187883) 同样成功。

最终候选本地验证：新增及既有故障回归 **148 passed**；重新构建 wheel/sdist，两种产物隔离安装各 **148 passed**，并通过 schema/命令/FTS/localhost DNS smoke；标准 SSLContext 与生产 truststore、两种 SDK 的本机 TLS 信任/拒绝不可信证书/拒绝错误 hostname 正反例通过。未调用付费模型，requires_key 测试被排除。57 项浏览器测试证明现有 Dashboard 无回归，**不证明 Memory UI 已交付**。

可追溯本地原始证据保存在 issue 工作树 `.scratch/issue17/`：`candidate-v13.json`、`candidate-v13.diff`、`DELIVERY-v13-precommit.md`、`write-deadline/review-v13.md`、`write-deadline/artifacts-v13/`；合并及 CI 原始读回在 `pr-prep-v13/`。上述独立评审报告未提交进仓库，本评论记录其候选、结论及实际运行结果，不虚构远端报告链接。

### 保留的下游边界与限制

- **#45 完整遗忘**：本票仅本地记录/FTS 硬删除及引用失效；传递来源隔离、在途 trace 屏障与副本清理仍待该票验收。
- **#19 工具循环**：已提供共享命令、真实权限继承、引用失效与发送前版本核验；通用工具循环及真实保存/编辑/删除到下一 Step 的链路仍待集成。
- **#46 Memory UI**：已提供命令、查询、回执、统计与 busy 等后端接缝；HTTP 状态映射、响应丢失恢复、SSE/页面失效及完整浏览器记忆流程仍待集成。
- **#18 提炼批次集成**：Store 支持调用者拥有的事务、人工保护及自动写入保护检查；提炼器批次原子重检、整批待确认、批次账与审批不在本票交付内。

FTS5 使用 unicode61 token 匹配，不承诺任意中文子串召回；当前搜索分页内部先获取匹配集再切页，大库性能优化未在此票扩展。

按本次明确授权，以 **completed** 关闭 #17；以上未完成跨票条目保留，不修改其他 Issue，不发布版本，不删除分支或工作树。
