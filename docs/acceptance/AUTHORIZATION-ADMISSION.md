# 授权来源、统一准入与历史隔离（离线机制）

生产在线路径当前不可执行：真实宿主确认/broker、可信状态、凭据和 dispatch 隔离均 **NOT AVAILABLE / NOT VERIFIED**。CLI execute/judge、product、auxiliary、judge 的公共执行路径在读取凭据和创建线上工厂前，以 `approval_source_unverifiable` 拒绝。没有开关、仓库密钥、自签回执或第二个 agent 可以提供真实许可。

`validate` 只报告 schema 有效性，并明确输出 `validation_scope=schema_only`、`online_executable=false` 和独立授权状态。`AuthorizedBatch` 构造可用于只读检查历史字段/时序；其名字不是来源认证。它的 client/start/真实 respond 仍执行来源准入。历史校验通过不产生可执行许可。

## Proposal

执行者只生成不可执行的 version 2 proposal，含随机 nonce、完整候选 manifest 与实际包摘要、不可变 batch 输入摘要、模型/profile、策略、product/auxiliary/judge 请求角色、证据 namespace 与规范绝对输出范围，以及请求/token/time 限制。已有 by/at/金额接受不复制为许可；subject/confirmed_at/cost_acceptance/receipt 和 worker_identity 始终 null。`cost` 是待审估计，不是金额上限；`fee_condition` 固定为 `hard_cap_required`，任何执行者填写的金额或强制机制只保存在 `executor_claim`，`broker_verified` 始终 false。

真实付费 grant 将来必须由独立 broker 核实币种、金额、计费口径和外部强制上限。当前没有该证明，也没有接受 unknown 费用的真实事件。当前 `evidence_namespace` 只是待审的本地范围摘要，不是 broker 分配的权威 store ID。动态请求内容目前只在合成 Dispatch 中按受限模型/profile 生成描述摘要；proposal 明确标记 `controlled_worker_required / NOT_AVAILABLE`。P2 未固定并隔离真实 worker 与运行包之前，这份摘要不能证明 agent 未改变 payload。

```sh
.venv/bin/python -m agent_alfred.evals.acceptance proposal \
  --input /absolute/path/batch.json \
  --output-scope /absolute/path/evidence \
  --operations product judge \
  --output /absolute/path/proposal.json
```

本命令不签署用户许可，不解释自然语言，不执行模型。金额字段来自待审计划，仅是费用描述。

## 离线协议接缝

`AuthorityDispatch` 定义 `submit_job(proposal_ref)`、`invoke(job_id, role, attempt_id, request_descriptor)`、`finish(job_id, role)`、`status(job_id)` 四个领域操作。生产 `UnconfiguredAuthority` 在每个操作均以 `approval_source_unverifiable` 拒绝，且公共 CLI/product/judge 在凭据和线上工厂之前调用该边界；没有本地配置回退。

`SimulationAuthority` 是专用模拟器，签发身份始终为 `simulation:*`。调用者显式给出 synthetic subject/source event/digest、激活截止与**仅合成**的费用选择；这不是实际金额接受或真实 grant。测试密钥只存在于模拟对象；生产拒绝路径不接收它。正向公共执行必须传 `simulation_session` 和 `httpx2.MockTransport`。Host/worker 只收到空的 `CredentialOverlay` 和 `RestrictedModelClient.respond/close`；原始 SDK/HTTP 对象及合成凭据留在模拟 Dispatch 内。每次请求仍重新走 Authority 准入，不向 worker 返回 send ticket。公共 execute/grade_batch 不接受任意 factory/http_client 注入分支；仅设置 simulation=True 不能读取调用者或环境凭据。CLI 没有模拟 authority/MockTransport 附着能力，所以 execute/judge 继续拒绝；dry-run 仍只运行离线脚本。内部引擎与通用 ModelClient 并非 OS 沙箱，Python 同进程对象不证明真实权限隔离。

授权状态的权威副本在模拟 authority 中，由同一锁保护 grant 消费、撤回、隔离与账目。每次操作验证文件身份/原始字节/单调时间锚；删除、损坏、回滚、替换、锚丢失或复制到另一个进程均 fail closed，不从执行输出恢复许可。该对象只支持单一拥有者进程，线程并发由同一临界区串行化；真实跨进程宿主/broker 适配尚不可用，fork 复制不可获得许可。

首次 activate 唯一；重复签发同一 proposal 也拒绝。product/judge 在同一 session 中各消费一次操作，完成后为 CONSUMED；复制输出目录、改绑定、恢复或 regrade 不会产生新起点/额度。存储输出路径是明确绑定的 scope。

请求预留是第一线性化点：在同一临界区核验来源绑定、模型/profile、角色、撤回、隔离、预算并持久预留。模拟 Dispatch 发送前再次在同锁持久化 `SEND_INTENT`；撤回或隔离先完成则把未发送请求记为 `CANCELLED_BEFORE_SEND`，不派发。`SEND_INTENT` 后可能已发送，在途仍可结算，不能撤销既有费用或重发同一 attempt。预留后崩溃保留未决状态并占额度；已结算却缺输入/输出 usage 时停止新请求，不填零、不退款。product→auxiliary→judge 共享 grant、原起点与请求账；schema3 任意 final_error 保留首次停机、零重试。

这里仅验证离线协议行为。相同执行者仍能读写模拟状态、测试密钥或运行任意 shell；没有验证真实凭据独占或外网 dispatch 隔离，不能声称不可绕过的安全边界。

## 历史事实与资格分离

`EvidenceStore.import_batch/read/revise` 保留原批次字节语义。`authorization_status(batch_id)` 与纯 `report` 单独投影资格；报告命令、CLI verify 同样投影。已知 c25 事故由只提供拒绝事实的随包索引定位 candidate、原 batch/parent digest、授权 digest、请求和 run identity；复制/改名/换候选或父链不能凭保留的原事实获得新资格。索引缺失或损坏仅增加不可验证阻塞。该索引不是生产可信 registry，也不能授权任何请求。

- 已知 c25：`QUARANTINED_AUDIT_ONLY / INVALID`，90 次请求不追认，费用 unknown；派生意见也隔离，正式校准引用拒绝。
- c21 精确历史包：`HISTORICAL_AUTHORIZATION_RECORDED / VALID_HISTORICAL`，依据本轮用户要求保留既有授权事实，不按新字段倒改。仍没有新运行许可或当前可信 registry 证明。
- 其它真实来源：`SOURCE_UNVERIFIABLE_AUDIT_ONLY / UNVERIFIABLE`。可读/可导入不是有效授权或可发布证据，正式报告持续阻塞。
- 纯 report 没有 store 时不能证明未知父链，不默认为有效来源。

已见登记包括隔离材料；新 calibration/formal 草案遇到已知 family 或同 input/setup 内容摘要，报告 `seen_material_reused`。改 ID 不恢复“未见”；该机制不宣称能识别任意语义改写。历史已执行批次原 seen 列表不修改。

`calibration.validate_source_facts` 只验证历史结构与时序；store 的审计 import/read 使用它，不授予正式资格。`validate_source` 与正式 `validate_execution_materials` 额外拒绝来源不可验证/当前无资格的校准源。时序合法、可审计读取与正式资格是不同结果。

纯 `report(batch, store=...)` 不写文件。`publish_report` 及 CLI `report/verify` 会追加派生报告和更新 latest-report；原封存库仅使用 read/纯 report，verify 必须先复制到新临时目录。

离线机制通过与真实通道验证、在线授权、质量/发布通过分别记录。c21 v1_release=FAIL、r3 四 pending、Ubuntu NOT RUN、C2C NOT REVIEWED 不因本机制变绿而改变。

## 底层预算公共入口

`AuthorizedBatch` 无会话构造仅用于历史字段/时序读取；client/start/respond 必须有显式模拟会话。`client` 接收具体 `ClientSnapshot` 和精确 `MockTransport`，返回不含 raw provider client、HTTP、凭据或可复用 send ticket 的受限 ModelClient；模拟 Authority 持有实际 `trust_env=False` 客户端与合成凭据，不执行调用者 builder。生产来源仍首先拒绝。使用后调用 budget.close() 关闭受限句柄及模拟 Dispatch 所拥有的客户端。

预算构造和使用重新核对 grant 中完整 proposal；原子预留在 authority 同锁内再次检查当前绑定和实际请求 token 数，不能以改写 batch 扩大回执限制。并发完成只提交本次 result 的 attempt 增量，再从 authority 刷新共享账，避免其它在途预算的旧 unknown 快照覆盖已结算行。

底层客户端同时检查已签模型的 wire_style 与 schema3 已签 stream/stream_fallback，偏离时在工厂构造前返回 approval_profile_mismatch。资源回收复用 ConstructionOwner、ResumableRollback、RollbackSlot：构造及返回期间保持调用者可达 owner，关闭失败保留重试进度；HTTP 的 is_closed 不能替代底层 MockTransport 已完成关闭的事实。这里仍只是离线 MockTransport 生命周期，不是生产凭据/网络隔离。

实际客户端从一个明确已签profile取执行限制：profile id能匹配时选择该profile，否则只接受唯一模型匹配，歧义拒绝；同一profile主处理与辅助共享一个模型不构成歧义。product的max_tokens不得超过profile与授权总上限；judge保留其自身授权总上限。实际thinking/response_format来自对应已签模型。单次timeout取已签限制、调用者更严格限制和授权总时限的最小值；overall限制形成客户端实际deadline，并继续与调用者deadline、authority原单调deadline取较早者。未声明字段不凭空补通用期限。

simulation会话从authority的started_monotonic直接继承共享截止时间，product→judge不再用墙钟重新换算；墙钟偏移不能扩大剩余I/O时间。authority保存状态时，目录FD复用artifacts.opened及OwnedDescriptor回滚，保存异常关闭许可；进程控制与未完成回滚的原错误链继续传播。

输出范围在 proposal 创建时固定为规范绝对路径；EvidenceStore 和离线 authority
也保留创建时的绝对位置，后续 cwd 改变不会让已有句柄指向另一目录。此处理不消除
符号链接，原有危险路径拒绝仍有效。复制到另一目录的新 store 不继承原输出许可。

一个操作 finish 后，authority 在原子 reserve 中拒绝该角色的任何新请求。已预留且
在途的请求仍可完成记账，不退还用量；其它已签操作继续共享原账和原起点。

执行与 judge 准入带 store 解析完整历史父链，隔离沿多级改名继续传播。缺失或无法
核验的祖先 fail closed；本轮不提供为带 parent 的派生历史包重新签发执行许可的
接缝。此限制不影响 import/read/纯 report 的审计能力，不倒改 c21 的历史授权事实。
