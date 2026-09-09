# 检索门与本地记忆后端

依据 Issue 17 及 Issue 31 的最终 Memory resolution（5588135839），代码基线为
`55418dad266eed4df8a3e40670a069e3dcddc65d`。本票提供共享后端和普通聊天 Run 的
检索，不实现 Memory 页面、通用 ToolRegistry 或完整遗忘隔离。

## 公共入口

- `RuntimeHost.memory_service`：`execute(command, CommandContext)`、
  `get_operation(operation_id)`、`get(kind, id)`、`get_records(...)`、
  `memory_revision`、`get_provenance(kind, id, version)`、`statistics(...)`。
- 命令只接受 `save/update/delete`；来源、来源组和准入凭证只从可信
  `CommandContext` 传入。工具适配器必须传入当前 `WorkItem.memory_permission`，
  不能根据 Run ID 自行领取权限。手动操作使用宿主同一个准入门。
- SQLite Store 的构造依赖是连接、时钟及 HMAC 签名函数。Store 不提交或回滚；
  共享命令服务拥有事务。提炼器后续可在自己拥有的同连接事务中组合 Store 操作，
  提炼批次账及审批仍由 Issue 18 实现，不能用单条命令回执代替批次提交。
- 生产状态目录使用 `audit.key`，父目录 0700、文件 0600；权限或加载异常拒绝使用。
  操作回执只保存参数 HMAC、key_id 和安全提交结果。删除回执沿用被删记录的
  内容 HMAC 与其 key_id，不复制正文。旧 key 不可比时拒绝重做旧 operation。

前向迁移 v5 增加内容版本、修改来源、人工保护、全局 memory_revision、操作回执和
来源完整性元数据。旧记录从版本 1 开始观测；旧来源关联明确为 unknown，不补造 Run
关联。新明确手动输入且没有历史来源时为 known_none；有已知来源组时为 known。

## Run 与输入证据

普通 chat 在首次回答前评估一次门；系统 probe 不检索。门与回答共享 RunBudget，
模型重试共享门的 Step 和 deadline。门未指派时用 primary；显式指派无法构造时走
确定性回退，不悄悄改指派。门模型的真实 Attempt 与费用进入同一份 Run 账。

当流式回退、普通重试或退避阶段在下一请求发送前抛异常时，策略通过
`ModelCallInterrupted` 携带此前真实 `ModelResult`；嵌套策略只合并各自已经发生的
Attempt。Run 账本先记录该结果并补齐请求的模型归属，再传播原始异常；中断仍按原
类型收束。此路径不从事件回放计费、不为未发送请求创建 Attempt，也不重置期限。
Adapter 在每次解码用量后发布当前 Attempt 的进度，因此流读取或回调中的控制异常
同样能携带已收到的用量。记账观察器再次失败时，已保存的账不回退；原控制异常保持
优先级，原始原因及次生错误仍可达。门异常发生时已耗尽预算，则记录 `model_deadline`。

实际请求身份从 SDK 完成本地编码后的 HTTP 分派开始。由策略层注入绝对期限，
底层 connect/read/write/TLS 每次使用剩余预算，流内也检查期限并关闭响应；不创建
后台模型调用线程来模拟取消。HTTP 包装集中在独立模块，并显式固定其依赖版本。

带预算的默认网络后端通过短生命周期子进程解析 DNS，以剩余期限等待；超期或中断
会终止并回收子进程、关闭管道后传播原错误。子进程只接收目标主机与端口，不接收
模型正文或凭据；返回值按大小、地址族和字段类型验证。字面 IP 直接连接，多个地址
逐个使用同一剩余预算。IPv6 只支持数值 scope；local_address 必须是字面地址，
不支持的形式明确拒绝。资源清理失败保留原错误、可达 owner 和可重试清理进度。

TCP 构造失败的清理 owner 在进入 httpcore/SDK 异常转换前，由网络后端移交到
HTTP 客户端持有的 `RollbackSlot`。即使异常随后被转换为普通 `ModelError`，
待清理资源也不会只依靠异常链存活；关闭客户端会重试清理。关闭再次失败时，
`IncompleteRollback` 保留整个关闭进度，重试不会重复关闭已成功释放的资源。

带预算的请求采用 pool=0，连接池饱和立即失败，不排队等待；connect/read/write
扩展 timeout 同步裁剪。SDK 必须配置 max_retries=0，由 Runtime 统一控制重试；
同一 Attempt 的第二次 HTTP 分派（包括自动重定向）会被拒绝，保留首次请求证据。
未注入预算的裸 Adapter 不新增强制期限。公开注入的 NetworkBackend 负责遵守收到的
剩余 timeout；测试利用这个接口确定性模拟阻塞 IO。
调用者注入的 HTTP 客户端若已有未受预算包装的连接，限时模型请求会在分派前拒绝，
不会关闭调用者连接或伪造有界执行；新建生产客户端在首次使用前安装预算边界。

确定性规则整句识别白名单寒暄和纯算式，不执行算式；其余使用原问题检索。两库各自
选完整有序前缀，JSON 实际序列化后的 Unicode 码点计量，不能互借预算或跳过长项。
参考消息位于工作窗口之后、当前问题之前，不写会话记录。

`gate.evaluated` 和持久门证据不含正文、subject、query 或分数。实际请求引用在
Adapter 发起 Attempt 的边界单独记录，包含用途、Step、Attempt、引用版本和实际
工作历史组。事件丢失不能制造或删除这些业务事实。统计只接纳严格验证过的 v1
证据；未记录、未评估、不完整和旧/不支持证据单列，零分母返回 null。

## 验收证据入口

所有测试位于 `src/agent_alfred/evals/deterministic/`，使用临时真实 SQLite、
ScriptedModel 和可控失败端口，不需要模型密钥。

| 验收项 | 主要测试文件及行为 |
| --- | --- |
| A01、A08–A10 | `test_memory_commands.py`：事务、账/回执故障回滚、重启重送、参数冲突、旧 key、删除后旧保存不复活 |
| A05–A07、A27、A39、A53 | `test_memory_stores.py`：机械幂等、版本/重复冲突、原文与来源、保护、硬删除、UTC 半开时间；`test_schema_v5.py` 验证旧数据迁移 |
| A11、A20 | `test_runtime_memory_gate.py`：真实 FTS 保存后下一 Run 命中，检查实际请求参考顺序与来源；`test_memory_gate.py` 验证删除后再次 miss |
| A12–A15 | `test_runtime_memory_gate.py`：每 Run 评估、probe、门指派、共享 Step、重试和费用；`test_runtime_memory_io_deadline.py`：真实 SDK 与 RuntimeHost 共享门/Run 期限；`test_attempt_io_deadline.py`：真实 SDK 逐次读写剩余额度、隐藏重试/重定向；`test_bounded_connect.py`、`test_resolver_process_ownership.py`：DNS 子进程、跨地址预算、真实 localhost 与异常回收 |
| A16–A19、A24 | `test_memory_gate.py`：整句规则、非法结构化输出、两库独立前缀、零选入、故障和无正文证据 |
| A21–A22 | `test_memory_input_attempts.py`、`test_runtime_memory_gate.py`：真实 Adapter 发送/本地构造失败、失败 Attempt、无 Sink 业务证据 |
| A23 | `test_memory_statistics.py`：2/6、3/4、1/7、零分母、非法证据与真实已记录 chat 过滤 |
| A25 | `test_runtime_memory_gate.py`：手动写 busy、工具继承真实权限、伪造/过期权限拒绝、不创建假模型 Run |
| A33 | `test_memory_gate.py`：编辑剔除旧引用且不补位；运行时发送前重新核验版本。完整工具多 Step 接线由 Issue 19 消费此接口 |
| R11 读接缝 | `test_memory_queries.py`：查询绑定游标、修改失效、列表与搜索顺序、读失败不伪装空结果 |

跨层故障回归：`test_adapter_cleanup_ownership.py` 通过真实 OpenAI/Anthropic SDK
验证流式/非流式异常转换后的资源存活、关闭失败重试及完成后不重复关闭；
`test_gate_attempt_preservation.py` 通过公共 RuntimeHost 验证回退、普通/嵌套重试、
退避失败和中断时的身份、用量、模型归属、引用及无事件情况下的持久账。
`test_interrupted_attempt_accounting.py` 覆盖两种真实 SDK 的当前流中断，以及中断
与账本观察器失败同时发生的场景，校验控制异常、原因链和持久用量均保留。

缓存生命周期故障回归：`test_factory_cleanup_ownership.py` 使用真实工厂与 SDK、
内存假 socket 和 FakeClock，覆盖全量/单端点失效、版本替换、Host dotenv 重读、
模型设置变更及 Host/工厂关闭。缓存删除之前将客户端交给 RollbackSlot 中的
ResumableRollback；失败由该存续 owner 保留并传播，后续失效或 close 会重试，
成功后 slot 退休对应 owner 并释放引用。工厂自有 HTTP transport 独立保留关闭进度，
网络 stream 的 close 失败在 httpcore 清空连接池之后仍交接至 transport slot，
并在池清理后排空 pending owner；不依赖 SDK 的析构器或 httpx 已关闭标记；调用者注入的 HTTP 客户端仍由调用者关闭。
Host 在 worker/记录收尾之后、sinks 与数据库之前清理缓存，沿用 BackgroundCloseStep
保证有界等待和失败重试；Host 不永久关闭外部传入的工厂，工厂的显式 close 则拒绝新建客户端。

同端点同配置版本的不同模型/协议路由分别缓存并共存；仅版本变化时整端点退休，
避免显式门模型复用回答协议，或创建门客户端时提前关闭回答客户端。
`test_gate_client_routing.py` 通过公共 RuntimeHost 与真实 SDK 检查路径、鉴权及用量。

TLS 升级回归：`test_tls_cleanup_ownership.py` 通过真实工厂、两种 SDK 与内存 DNS/socket
验证 TLS 失败/成功、连续关闭失败、已完成步骤不重复关闭，以及控制异常及原始原因链。
原生连接改用 httpcore 的 MemoryBIO TLS 实现：TLS 只借用已拥有的 stream，不再创建
或替换 socket，因而 SSLObject、BIO、TLS stream 和升级返回的任意中断仍由同一个 owner
负责关闭原资源。`_TLSIO` 将每次加密读写接回绝对期限守卫；SSLContext 的证书与主机名
验证、SNI、ALPN 保持生效，标准 SSLContext 与生产 truststore 路径均有本机真实 TLS 验证。
升级前发布 rollback 到存续 slot，失败即时清理，未完成的清理仍由后续失效/close 重试；
成功关闭从 slot 退休。IO 异常 cause/context 在 SDK 前保存、Attempt 退出时恢复，
防止 httpcore `raise exc from None` 清除原始控制异常原因。没有改变预算或权威账来源。

## 当前边界

- 默认 FTS5 沿用 unicode61 分词，按 token 匹配，不是任意子串搜索；连续中文中的短词
  不保证召回。本次演示使用固定无歧义词项，不以模型复述作为命中证据。
- 当前 Store 搜索分页会在内部获取匹配集再切页，以保持 Store 排序与 UTC 微秒语义；
  大库性能优化可保持公共契约独立进行。
- 底层删除和引用失效不等于完整遗忘。传递来源隔离、在途 trace 屏障和副本清理由
  Issue 45 接入；HTTP、SSE 页面失效与完整浏览器记忆路径由 Issue 46 接入。
- 本票没有新增通用工具循环。引用容器和发送前版本检查供后续工具 Step 使用，
  不能将其独立测试称为完整保存/编辑/删除工具产品闭环。
- 未使用真实付费模型。已有浏览器回归验证 Dashboard 未回归，不是 Memory 页交付证据。

TCP 成功返回也使用同一个存续 owner：backend 在调用 BoundedConnector 前把空
ResumableRollback 登记到 transport slot，connector 在其中登记 socket token，成功后
不退休该 token。DeadlineNetworkStream 继续借用这份进度；connector、backend 两层
返回被控制异常打断时仍能由工厂关闭重试，正常 stream.close 成功后才从 slot 退休。

部分写入回归：`test_tls_write_deadline.py` 通过公共 RuntimeHost、两 SDK 和真实
SSLObject/MemoryBIO，在握手及应用数据发送中注入遵守 timeout 的短写 socket。
原生 SyncStream 的发送循环由 DeadlineNetworkStream 执行，每次 socket.send 前重算
同一绝对 deadline 的剩余时间；耗尽后不再启动发送。gate 先到期仍能规则回退并回答，
Run 先到期不再启动回答；故障 Attempt、未知用量、实际 FTS 引用及持久证据保留。
超时与连续关闭失败组合仍由原 owner 重试，原超时原因保持可追溯。

应用数据读写的控制异常也在 IO 接缝先保存；httpcore 在异常展开期间关闭失败时，
通过同一 rollback 挂接清理失败并继续传播原控制信号。rollback沿用dominant_error的
“首个失败、控制异常优先”规则，首个控制异常不会被次生控制异常取代。两SDK回归
验证原始cause与无环异常链、已发起Attempt及最终清理退休。

#45 在共享命令上增加的来源隔离、范围确认、非终结 trace 屏障和清理协调接口，
见 [Issue 45 实现说明](issue-45-forgetting.md)。该核心端口验收不替代 #16/#18/#19/#46
的真实消费链路验收，以上 #17 原验收口径保留。
