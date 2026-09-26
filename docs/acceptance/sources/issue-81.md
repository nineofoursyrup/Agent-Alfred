# #81 实现：Graph 单次运行路径与证据的完整用户路径

来源：https://github.com/nineofoursyrup/Agent-Alfred/issues/81

<!-- graph-run-evidence-spec:r1 -->
# GRAPH-RUN-EVIDENCE-SPEC-r1

Part of [地图 #1](https://github.com/nineofoursyrup/Agent-Alfred/issues/1)。设计来源：[决定 #73](https://github.com/nineofoursyrup/Agent-Alfred/issues/73)。

规范 readiness：**READY FOR IMPLEMENTATION**。10 项要求、22 项验收、12 个关键反例及公开测试边界已确认，无未决验收 blocker。标签不代表认领、实现、验证或产品交付。

## Problem Statement

用户要对一次消息分流或手动聚合 Run 查明真实执行过哪些节点、走过哪些分支，在哪里跳过、失败、恢复或进入普通循环回退。当前结构图只说明流程可能如何执行，最终结果摘要无法证明中间路径；升级后的当前图、残缺 trace 或旧进程画面还可能被误当作历史事实。

用户需要在运行详情中获得有身份、有证据边界的单次路径，能分清计算、波提交与持久保存，并在证据不足时看见准确的未知或不可用原因。

## Solution

在运行详情加入默认收起的“本次执行路径”。新 Run 保存实际捕获的历史结构与最小执行事实，在完整拓扑上叠加已证实路径，同时提供等价文字明细；当前和历史 Run 均使用手动刷新快照。

详细路径与历史拓扑沿用 trace 生命周期。运行中观测明确不承诺已保存，重启只读取持久证据；裁剪、损坏、旧数据、不支持及超限分级降级。观察不改变既有业务、预算、记录和计量，也不根据最终结果重建不存在的过程。

## User Stories

1. **R-01** — As a CLI/Web 用户, I want 从运行详情和两条 workflow 既有入口查看明确 Run 的路径, so that 不混淆不同运行或另找历史入口。（Q1）
2. **R-02** — As a 路径查看者, I want 读取完整历史结构、真实节点/边/wave 状态及等价文字明细, so that 识别实际发生的步骤而不被颜色或局部事件误导。（Q4）
3. **R-03** — As a 故障调查者, I want 分清节点故障、图内恢复、图外回退、图前绕行、Graph/Run 结局及 NoAction, so that 理解最终结果来自哪一段实际过程。（Q4）
4. **R-04** — As a 路径查看者, I want 结构、说明、身份、证据与读取边界一致, so that 不把不同 Run、图代际或读取时刻拼成一条路径。（Q2/Q5/Q7）
5. **R-05** — As a 历史 Run 查看者, I want 新 Run 保存当时图并在升级后仍可核验，细节随 trace 保留, so that 历史记录不会套用当前结构。（Q3）
6. **R-06** — As a 路径查看者, I want 区分进程内观测和持久事实并看见崩溃后的真实缺口, so that 不把曾经显示过当成已经保存。（Q5）
7. **R-07** — As a 历史 Run 查看者, I want 明确识别旧数据、部分证据、损坏、裁剪、不支持和超限, so that 不用缺失证据判断成功、失败或未执行。（Q3/Q6）
8. **R-08** — As a 路径查看者, I want 手动刷新、读取失败恢复、迟到响应和重启身份具有一致行为, so that 可以可靠地切换和复查运行。（Q2/Q7）
9. **R-09** — As a CLI/Web 用户, I want 通过键盘或窄屏读取路径且观察不影响业务、计量、隐私与既有路径, so that 安心调查而不触发额外执行。（Q1/Q4/Q7/Q8）
10. **R-10** — As an 实现验收者, I want 使用真实公开链路和确定性反例核验全部合同, so that 交付结论能由原始证据复核。（Q8）

## Implementation Decisions

### 来源、批准及验收权威

- 上游 [#71 范围规范](https://github.com/nineofoursyrup/Agent-Alfred/issues/71#issuecomment-5742773780) 的 S04；[#72 Resolution r1](https://github.com/nineofoursyrup/Agent-Alfred/issues/72#issuecomment-5742990883) 与 [#75 BEHAVIOUR-TOPOLOGY-SPEC-r1](https://github.com/nineofoursyrup/Agent-Alfred/issues/75) 固定静态结构入口、身份与观察合同。
- 源设计 `GRAPH-RUN-EVIDENCE-DESIGN-r3`，SHA-256 `ad3628e63cb428f450f066f673b86d0991653a70e11723bf0f8614f4135a2c7b`；两次“全按建议”确认 Q1–Q3/Q4–Q8，随后“确认”批准完整 r3 及 R-01–10、AC-01–22、CE-01–12。`APPROVAL-r3.md` 覆盖冻结设计中的确认前时态。
- 本文为唯一实施验收定义，完整内联所有要求、验收及反例，无必读的本地 acceptance companion。设计、批准记录及本文哈希只作溯源，不构成另一套可独立修改的验收规则。没有删除、替换或降级已批准项。
- 本地来源目录 `/Users/nineofour/Agent-Alfred/.scratch/graph-run-evidence-73/`；发布后本文原样作为实施票正文，URL/哈希回读写入同入口 LOOP.md。后续变更需显式修订。

### Q1 入口与归属
在运行详情增加默认收起的“本次执行路径”，绑定明确 run_id；两条 workflow 已有查看运行入口落到同一详情。Behaviour 保留当前声明结构，不将历史路径混入其中；不新增独立 Graph 页或 Run 选择器。
替代：在 Behaviour 另设历史 Run 选择/叠加视图，带来第二套导航与身份状态。

### Q2 当前/历史与刷新
当前及历史 Run 均可查看，但 v1 路径面板是带读取时间的手动刷新快照，打开/重开/主动刷新时读取，不自动追踪节点动画。当前 Run 明确标进行中与证据可能未保存；其它既有运行详情实时行为保持。
替代：本票就做实时节点/波观察，需同时冻结重连缺口、暂态撤销、事件/快照合并与自动更新期间的阅读体验。

### Q3 历史证据保留
从新能力启用后的 Run 保存当时绑定的结构描述和身份；具体节点/边/波路径证据随 trace 保留策略。程序升级后仍优先用该 Run 当时的结构；trace 裁剪后保留已有 Run/结果/账目，并明确详细路径不可用。旧 Run 缺证据不回填当前结构或推测节点轨迹。
替代：另行永久保存逐节点路径摘要，获得裁剪后路径，但增加独立持久合同与数据生命周期。

### Q4 展示深度与状态语义

在该 Run 当时的完整拓扑上叠加已证实的路径，保留等价的节点/边文字明细，按 wave 分组。默认概览展示图结果、Run 结果、恢复/图外回退；展开节点显示稳定身份、可证明的状态及原因和相关 Step/Attempt 链接，不复制输入正文或正式回复，也不保存任意 node state/writes。

节点执行状态、波是否提交及证据是否充分分开：已开始但无结论不等于成功；显式 succeeded/failed/skipped/aborted 各自呈现；未开始须有完整快照或明确未执行事实支持，缺事件一律不自行判未开始。skipped 保留 disabled_by_config 或 all_inbound_not_taken 原因。边区分 taken/not_taken/未决/未知；taken 只说明分支决议生效，不等于目标节点成功。节点计算完成但波尚未提交时不能标成已提交。整波撤销不抹掉前波已提交路径，也不意味着工具副作用或费用被撤回。

图内 failed 节点经 error 边恢复，仍保留其失败与恢复路线；Graph 失败后普通循环成功时同时呈现 Graph 失败、图外回退已进入、Run 的真实最终结果。图内 fallback 分支、图外回退、图前绕行分别说明；图外阶段不伪装成 describe 中的节点/边。NoAction 保留具体原因而不制造回复/草稿。

### Q5 新证据及持久性边界

从实际捕获的 Run/Graph 实例生成不可混代的结构与最小路径事实，补足波提交/撤销与边决议的证据；字段/内部事件类型由实现选择。语义要求是不能因读取停在同波多条事件中间而把半个波标成整体提交；不能根据终态猜测未记录路径。结构、说明、身份与证据使用同一 Run 绑定，说明不参与结构 hash。

当前 Run 手动读取内存中的一致快照，明确“本次进程观测，尚未保证保存”；记录落定后从既有受管 trace 读取，沿用异步记录与现有 Run 收尾持久性屏障，不逐节点新增 fsync。崩溃后只信重启可读的持久证据，内存中曾完整的图不得冒充已保存。可预期的新观测故障/超限只能让路径降级，不新增业务中止、重试或回退条件；既有 fatal 发布/业务错误语义保持，不承诺吞掉所有 BaseException。

不新增永久逐节点账、不修改 Run/Attempt/费用计数。两种 workflow 与 CLI/Web 共用真实 Run 绑定；根本未入图且有明确绕行证据时显示原因，无证据时显示未知。

### Q6 缺失、不兼容与资源上限

分级：旧 Run 无历史结构时不画图，可保留可解读且属于该 Run 的事件明细及已有摘要；未知结构/证据 schema 或身份矛盾不强行拼图，不套当前图。合法未知节点/文案保留原标识并标暂无说明，不隐藏合法结构。

trace 尾部截断但可验证连续前缀时仅呈前缀所能证明的事实，标“不完整”，余下保持未知；内部坏行/身份矛盾不能跳洞后继续声称完整路径，路径不可用而独立可信摘要仍可见。区分裁剪、缺失、损坏、不支持、读取超限；不把 unavailable 当成“未执行图”。沿用现有历史 trace 读取 32 MiB 上限与资源边界（调整须显式修订）；超限不无限加载，不呈半张图冒充完整。新有界当前快照溢出时显式标缺口/不可用，不占用业务准入或阻塞执行；当前观测缓存的精确容量由实现按现有约定选择并在接口及测试中固定。本文不声称已有自动按天裁剪调度器，也不为 #73 新增保留策略管理功能。

路径状态、trace 完整性、Run outcome、recording_state 分轴。刷新有效确认已裁剪/不可用时撤下先前可用路径，保留独立可信摘要；网络读取失败时可保留原快照但标旧和失败。

### Q7 刷新、导航和服务重启

每次完整读取按明确 run_id 和本次请求资格替换快照，不与浏览器 SSE 增量拼接。面板展开/重开/手动刷新时读取；关闭、切换 Run、离页使旧请求失效，最新有效请求胜出，不靠响应到达时间。加载/失败时同一 Run 的旧快照明确标记；不能在 B Run 页展示 A 的旧图。

一份历史 Run 的原始 process_instance_id 可与当前提供 HTTP 的宿主不同，这是正常历史身份，不应因重启就拒绝所有旧 Run。已失效旧宿主发来的迟到响应不能覆盖当前读取；读取来源身份与 Run 原始身份分别校验。曾显示的运行中内存快照在宿主更换后标旧且不可当持久证据，重新读取后只呈可核验的历史事实。打开路径不切换目标 Session、不保存参数、不重投请求。

继承 #72 的图/文字信息等价、键盘/窄屏操作与页内视口惯例；本次路径默认收起、页内选择保留，同一结构刷新保持观察位置，跨 Run 或结构变化清除旧选择；不新增跨重载观察偏好。

### 公开证据合同的统一判读

本节澄清 Q4–Q7 之间的交互，不新增执行策略或另一套结果记录。

### 身份与读取边界

每次可展示的读取必须绑定 run_id、workflow/graph_id、Run 原始宿主身份、捕获图的发布代际、结构 schema 与 topology_hash、路径证据格式版本。缺失身份不能借当前宿主或当前图补齐。来源身份/读取时间及有序证据边界用于说明快照来自哪个读取者、覆盖到哪里；它们与历史 Run 原始身份分开。字段名称与传输路径由实现确定，语义必须可从公开响应核验。

新 Run 的描述必须来自执行实际捕获的图实例及其同代说明，不能在收尾或 GET 时抓取“当前图”顶替。说明不进入 topology_hash；Run 结构与证据不能仅因 hash 相同就被认作同一进程/代际/行为版本。版本受支持且身份一致时，图形与明细均从同一不可变快照生成。不得合并别的 Run、另一进程中的 seq 或旧请求的部分字段。

当前快照是读取边界之前的已观测事实，不保证读取结束时仍然最新。快照中“已开始，未观察到终态”不承诺节点此刻仍执行。图的结束、Run 的结局、会话结果是否 recorded、trace/路径的完整度各自显示；一个轴不得替另一轴作结论。现有 Run 详情可能通过 SSE 更新得更快，本面板仍显示自己的读取时间与事实来源，不将新 Run 徽标反向涂到旧路径上。

### 节点、边与波

| 观察事实 | 可显示的结论 | 不允许推断 |
|---|---|---|
| 身份有效且覆盖完整的当前快照明确尚未开始，或明确未执行事实 | 未开始/未执行，并说明证据范围 | 仅缺事件就判未执行 |
| node started，未有终态证据 | 已开始，尚未观察到终态 | 成功或持续实时执行 |
| 可证明 succeeded 及所属波提交 | 成功、结果已提交 | 模型调用成功就等于波提交 |
| 明确 failed | 失败及原因；如恢复则单独展示恢复 | 隐藏故障、将其改成 skipped |
| 明确 skipped | 跳过及配置禁用/全部入边未选中原因 | 没有证据就跳过 |
| 明确 aborted / 波撤销 | 本波未生效、保留前波结果 | 外部副作用/费用已经撤销 |
| 缺证据、截断、矛盾或无法解读 | 未知或明确不可用 | 把未知折叠成未执行/未选中 |

边身份至少区分 source/target/kind/label，不能合并同端点的 full/fallback 等不同条件边。taken/not_taken 需要实际生效决议证据；无完整决议时以尚未决议或未知呈现，区别依赖覆盖证据。taken 不保证目标开始或成功，not_taken 不把其他来源的可达节点一并判 skipped。

一个波的原子性是业务状态语义；读取证据可能只覆盖其部分发布过程。有效波提交/撤销凭其可证明的完整边界确认，不根据单个 node.finished 推导整波。一条已核验事实可以保留，但不得假造同波其他节点/边状态。记录每波的最小完整事实或等价可核验边界均可；不指定内部事件类，但新能力在正常完整记录下必须能回答波提交与各边决议，不能把所有情况永久降为未知。

### 生命周期与安全

当前/收尾未落定的观察来自有界内存，不新增跨进程恢复承诺。若同进程保留未记录终态投影，只能标为进程内证据，并受既有有界生命周期约束；不得建立永久内存兜底。持久 trace 可提供的证据与 recording_state 独立判断：会话保存失败不自动抹除完整 trace，trace 失败也不改已确认业务结局。

查看、刷新及协议错误不触发 fsync、业务执行、重新编译、自动重试、参数保存、Session 切换或修复动作。新增观测普通故障遵循原 sink 隔离/丢失标记，中央 fatal 发布和控制异常仍保持原语义。历史细节/拓扑与 bundle 生命周期一致，不使用永久旁路存储挽回已裁剪数据。本票不新增裁剪按钮、自动调度器或保留设置。

仅展示结构、稳定 ID、状态/原因及已有 Step/Attempt 证据入口；不存在或不可读取的关联证据明确标不可用，不伪造跳转内容。继承中央脱敏与现有 HTTP Host/Origin/鉴权等保护，文案按纯文本渲染，不增加任意 node state/writes、凭据或原始输入/回复副本。流程查看不是新的 trace 导出或正文查看器。

### 公开模块合同与已核验事实

固定源码基线为 `c5b3a12f078af0bf3722d941c8dc3b6d9c07438f`，本轮远端 main 仍为该提交；当前主 checkout `31daa31010dc96639664da1be0767f51a7de83a0` 较旧。实施需核对当时最新基线和并行改动，不根据旧 checkout 的 stub 判断已交付能力。

- Graph 编译/运行合同：从真正捕获的编译图取得历史 describe 及对应说明；补足每波和边决议的最小可核验证据，保持原执行/提交语义。现有 engine 先提交整波再逐节点 emit，普通事件前缀不能自动证明整波。`graph/engine.py`、`graph/context.py`、`events.py` 是相关公共行为的承载模块，不要求固定文件布局。
- 宿主/记录合同：Run 原始身份与当前读取服务身份分开；有界当前投影、受管 trace、RunRecorder/SQLite 与既有记录屏障保持一致。当前生产记录没有历史 describe，不能把此功能写成仅前端叠图。
- 查询合同：新增路径只读响应要包含完整快照身份、证据范围、状态/原因与受支持版本判读。具体 HTTP 路径由实现选择；原 `/api/run-evidence` 对 active/pending 返回 `live, events=[]` 并交 SSE 提供过程的合同保持，不能借新能力改写。
- 历史投影当前会漏 `route_label`，浏览器 live 投影也丢部分 Graph 身份/原因；本功能不依赖该不完整投影猜测路径，而须产生符合本规范的完整读取合同。`runtime/evidence.py`、`runtime/host.py`、`runtime/recording.py` 及 `ops/static/runs.js` 为相关读写边界。
- 普通 trace prepare/commit 异常由现有 sink 隔离并记不完整，但 GraphRunContext.emit/中央 fatal 发布与 BaseException 并非无条件 best-effort。新观测故障不得新增业务中止或吞掉原本须传播的错误。
- 新格式版本/字段及可能需要的存储结构由实现选择；保持 ADR-0009/0010/0017/0019/0020/0023/0024/0026/0037/0038/0039 约束。如确需 schema 变更，遵循冻结迁移不可改的现有项目约定，不为持久化详细路径另建永久逐节点账。
- #70 导出、#74/#76 统计不是本票新增前置，也不接管其实现或回写；共享事件/trace 变动须保持既有消费合同。#72 的设计前置已满足，#75 的代码已在调查基线中，不能重新制造未解决依赖。

### 领域术语与架构取舍的交付承接

实现应承接下列已确认的本地领域记录，并保留其他任务的 CONTEXT/ADR 修改；正文已包含全部语义，不依赖本机文件才可理解。

- **本次执行路径**：一条具体 Run 有证据支持的节点执行、分支决议和恢复经过；绑定其当时拓扑，最终结果不能代替过程证据。避免将其称作流程拓扑或执行预演。
- **波提交／波撤销**：本波缓冲写入与分支决议成为有效结果或整批不生效；单节点计算完成不等于波提交，撤销不否定前波结果、不撤回外部副作用/费用。
- **跳过**：配置明确禁用或全部入边 not_taken 而未执行，有明确原因；与失败、没有事件或未知不同。
- **ADR-0043（若实施基线编号冲突则按项目惯例保留决定并调整编号）**：单次路径与当时拓扑共享 trace 保留边界，接受裁剪后细节不可恢复及旧数据不回填的代价；沿用异步 trace 与既有屏障，不新增逐节点 fsync 或永久旁路记录。常规观测故障降级，不更改原业务/fatal 发布语义。

## Testing Decisions

### Q8 公开验收与替换边界

正常路径采用真实浏览器→HTTP→RuntimeHost/Coordinator→两张真实已编译 Graph→事件/trace/RunRecorder→实际读模型；核对节点、边、wave、Run 身份、费用及结果。CLI/Web 入口均覆盖。

外部模型传输可用 ScriptedModel，外部工具/MCP 网络可使用受控替身；这些替身只替换外部边界，不替换 GraphResult、路径摘要、证据读模型或正常成功拓扑。实际失败由真实执行节点、存储 I/O 或发布边界触发。运行次序、响应逆序、记录未落定用确定性屏障/受控时钟和 ID，不用 sleep 凑概率。未知协议注入单列，不冒充正常链路证据。

既有确定性 Run/路由/聚合/计量/trace 读取及 browser 门禁按影响运行；新增路径读取不改老 /api/run-evidence 的 active=live,events=[] 合同，不为本票修改 #70 导出规范；共用组件变动须做现有兼容回归。每个已确认 CE 对应候选与原始证据；设计确认不表示验证通过。

所有新增接口路径、文件布局、缓存实现、事件类型及 schema 版本号由实现按上述行为合同选择，不向用户转嫁可查证的实现事实。

### 现有测试先例与确定性控制

所有以下路径均相对于实施仓库。沿用先例的公开行为和屏障，不把旧 PASS 当本票证据：

- `src/agent_alfred/evals/deterministic/test_dashboard_evidence.py`：运行中 HTTP 交 SSE 的原合同、真实 Run 历史缺失/损坏；保留既有行为，新增路径使用自己的完整快照 seam。
- `test_graph.py`、`test_graph_compile.py`、`test_graph_recording.py`、`test_graph_review_regressions.py`（同目录）：真实 builder/引擎/记录边界，补足半波前缀与整波撤销，不 mock GraphResult。
- `test_runtime.py`、`test_trace_sink.py`（同目录）：`RuntimeHost(before_recording_commit=...)` 与 `EnteredEvent`、受控模型事件、真实 trace 短写/EIO、write/fsync 及 drain 屏障。FanOut 测试 sink 可截留节点发布边界，checkpoint 不是波提交证明。
- `test_message_routing.py`、`test_routing_behaviour.py`、`test_aggregation.py`、`test_aggregation_safety.py`、`test_behaviour_topology.py`（同目录）：既有业务、预算、摘要/记录和拓扑合同。
- `tests/browser/routing.spec.js`、`aggregation.spec.js`、`topology.spec.js`：真实 Host/HTTP 下的 UI、提交不明、观察状态、键盘/窄屏先例。新增路径测试必须让两张真实 workflow 产生自己的证据，异常协议 fixtures 单列。

### 必需验证与证据报告

先完成逐 AC/CE 的确定性测试、真实公开路径与兼容回归，再运行实施时适用的项目门禁。当前基线 `.github/workflows/ci.yml` 的门禁为：`uv run ruff check`、`uv run python scripts/check_skills.py`、`uv run python scripts/check_env_example.py`、`uv run --extra mcp pytest`、`uv build`、`uv run python scripts/check_mcp_installations.py --output <evidence-path>`、`npm run typecheck`、`npm run test:browser`；按该工作流准备 Python 3.14、dev/mcp extras 和浏览器依赖。

门禁与原始日志须绑定同一候选；新增故障注入及 mock 边界分别列出；不得通过放宽断言、sleep、无根据重试或替换成功读模型获得表面通过。`requires_key` 维持非默认检查，本规范不新增付费模型评估要求。产品测试、独立评审及交付 CI 未发生前分别记 NOT RUN/NOT REVIEWED。

### Acceptance criteria

以下均为已确认的可观察目标，不是已运行结果。来源为已批准设计 r3 的同编号 AC；决定与反例映射见下表。

- **AC-01 / R-01**：两条 workflow 的既有查看运行入口及直接 Run 详情 URL 指向同一 run_id；默认收起“本次执行路径”。Behaviour 静态结构不混入历史状态，无新增顶级 Graph 导航/Run 选择器。
- **AC-02 / R-04/R-08**：首次展开、重开、手动刷新读取完整快照；保持展开不轮询、不用 SSE 自动涂改路径。显示读取时间和进程内/持久来源，既有运行详情实时行为保持。
- **AC-03 / R-02**：新功能启用后两张真实 workflow 的保存 describe 与图/文字节点、边、条件标签、终点类别完整一致；同端点不同条件边不被去重。wave 分组准确，文案与当时结构同代，合法缺文案标识保留。
- **AC-04 / R-02**：已开始、可证明成功、失败、跳过、撤销、未开始/未执行、未知按统一判读区别；跳过理由可见，缺事件不被认作未执行，节点详情不复制输入或正式回复。
- **AC-05 / R-02/R-04**：同波读取中段和波失败分别呈现可证明事实；无完整提交证据不标整波提交，撤销不抹前波。正常完整记录必须可读到实际波结论，不可用恒 unknown 规避。
- **AC-06 / R-02**：条件/normal/error 边决议与真实执行一致；taken 不等于目标成功，未决和未知分开，不从最终分类或 Run 结果补齐。正常完整记录能核对每条边的适用决议。
- **AC-07 / R-03**：真实覆盖 error 恢复、图内 fallback 分支、图失败后的普通回退及阻止、明确图前绕行、NoAction；分别显示原故障、阶段及 Graph/Run 结局。图外阶段不加进 describe，NoAction 不制造空助手消息/草稿。
- **AC-08 / R-04/R-05**：执行用 G1 而随后发布 G2，读取仍为 Run 绑定的 G1；即使 hash 相同也不混代。原始实例、图发布代际与当前服务来源身份可核对，不称 hash 为行为版本。
- **AC-09 / R-04/R-06**：一次有效快照内部身份、结构和有序证据边界一致；读取与 Run 收尾竞争时得到一种一致来源，或明确重新读取/不可用，不返回内存一半+持久一半。Run 结局、记录状态与路径证据覆盖分轴。
- **AC-10 / R-05**：正常新 Run 收尾后重载及真实重启可从保留 trace 读取原结构/路径；不依赖当前图实例或浏览器缓存。旧 Run 缺结构不回填，不迁移编造历史。
- **AC-11 / R-06**：屏障前可见、trace 写入失败、记录未落定/失败及重启各场景如实展示来源与缺口；不以旧内存观测证明已保存，不自动重跑，trace 完整与会话 recorded 不互相替代。
- **AC-12 / R-05/R-07**：受管 bundle 删除并记录裁剪事实后刷新撤下路径，明确“已裁剪”，Run/摘要/账目仍保持；无裁剪事实的缺失标缺失/不可用，不能冒称正常到期。
- **AC-13 / R-07**：唯一尾部截断只允许可信连续前缀支持局部事实；内部坏行、身份不匹配、非法事件顺序或未知必需 schema 不拼装路径；已有独立可信摘要保留。不支持与无图/未执行不同。
- **AC-14 / R-07/R-09**：历史 32 MiB 上限及当前有界观测缓存超限都有稳定可展示原因；不无限加载/等待，不阻塞业务准入。当前缓存容量由实现显式记录并做容量边界测试，不能静默丢前缀后宣称完整。
- **AC-15 / R-08**：首次读取失败显示原因及重试；同 Run 网络失败保留旧快照时必须标旧/失败；有效新响应确认不可用/裁剪时撤下旧路径，可信部分证据按其覆盖整体替换。下一次成功读取恢复，不要求重新执行 Run。
- **AC-16 / R-08**：同 Run 两次读取逆序返回、关闭重开、离页或切换 Run 时，仅当前视图最新有效响应更新，A 的响应不会显示在 B 的标题下，选择详情亦无串位。
- **AC-17 / R-05/R-08**：P2 服务可显示持久 Run 原始身份 P1；P1 旧请求迟到不能覆盖 P2 当前读取。同 hash 不抵消请求失效。旧运行中快照宿主变化后明确失去当前资格，不冒充历史持久证据。
- **AC-18 / R-08/R-09**：同页重开/同 Run 同结构刷新保持合法选择与视口，跨 Run 或结构变化清除旧节点并适应全图；重载默认收起，无新增观察偏好持久化。不改业务表单、目标 Session 和聚合请求。
- **AC-19 / R-02/R-09**：桌面/窄屏、纯键盘均可打开关闭/刷新/缩放/适应及读取节点、边、wave 状态和关联证据；状态不只依赖颜色，图与文字无信息缺口。
- **AC-20 / R-09**：观察前后 Run/Attempt/Step/费用、业务工具调用、分流设置、聚合固定请求及会话结果无观察造成的变化；响应丢失/记录 pending 下不解除禁止重投、不自动修复/重试。
- **AC-21 / R-09**：沿用现有 HTTP 保护和中央脱敏；结构说明、原因及标识按纯文本展示，无新任意 state/writes/凭据/正文副本。稳定 Step/Attempt 引用可定位已有证据，目标缺失明确不可用，不造内容。
- **AC-22 / R-10**：交付以同一固定候选逐项提供 AC/CE 证据，正常成功用两条真实 workflow 与公开读取；独立 Graph 边界测试补足波反例，协议破坏注入单列。保留旧 active run-evidence 接口、既有拓扑/路由/聚合/trace/计量合同并运行相关项目门禁；未运行检查明确 NOT RUN。

### 决定、验收与反例覆盖

| 决定 | 要求 | 验收 | 核心反例 |
|---|---|---|---|
| Q1 入口 | R-01/R-09 | AC-01/18/20 | CE-03/08/11 |
| Q2 手动快照 | R-04/R-08 | AC-02/09/16/17 | CE-04/08/12 |
| Q3 历史保留 | R-05/R-07 | AC-08/10/12/13 | CE-01/06/07 |
| Q4 状态与呈现 | R-02/R-03/R-09 | AC-03/04/05/06/07/19/21 | CE-02/04/05/10/11 |
| Q5 持久性 | R-04/R-06/R-09 | AC-05/08/09/11/20 | CE-01/04/06/09/12 |
| Q6 分级降级 | R-07 | AC-10/12/13/14/15 | CE-07/09 |
| Q7 刷新与身份 | R-04/R-08/R-09 | AC-02/09/15/16/17/18/20 | CE-03/08/11/12 |
| Q8 验收边界 | R-10 | AC-22；全部 AC | CE-01–CE-12 |

## Critical Counterexamples

**状态说明**：以下逐项原义承接已获整体确认的设计 r3。Q1–Q8、完整 R/AC/CE、精确预期与测试边界全部 CONFIRMED；每个 case 的运行验证均 NOT RUN。它们不是已观察到的缺陷清单，没有 deferred 必需反例。

### CE-01 历史 Run 不得套用新图

- **Basis:** R-04/R-05；Q3/Q5；AC-08/10；#72 身份合同。
- **Sequence:** 真实消息分流 Run A 捕获 G1；在其开始后发布 G2（分别测试不同 hash、同 hash 不同代际）；A 完成并保存后查看；再以新宿主 P2 读取 A。另有旧 Run 不含历史 describe。
- **Expected behavior:** A 的结构、说明和路径始终属于其实际 G1，保留原始实例身份，P2 只是读取服务方；无历史结构时不画图，可呈可解读事件/独立摘要，不引用当前 G2 补图。
- **Verification:** 真实 Host 的图/Registry 发布、编译图、Run 与 trace、HTTP/browser。外部模型用受控替身，发布/执行屏障固定捕获先后；正常成功不 mock describe/GraphResult。
- **Decision status:** **CONFIRMED**；来源为 Q1–Q8 两轮批准及设计 r3 整体“确认”，完整验收定义无删改；验证 **NOT RUN**。

### CE-02 整波撤销不是整图从未执行

- **Basis:** R-02/R-09；Q4/Q5；AC-04/05/20；ADR-0010。
- **Sequence:** 前一波 W1 成功提交；W2 的 A 已开始并返回合法结果，随后同波 B 失败无恢复边，或 wave 写集/router 校验失败；W3 未开始。另测前波已发生真实计量的模型调用。
- **Expected behavior:** 保留 W1 已提交事实；W2 已开始节点标撤销并显示可证明原因，W3 只在明确未执行证据支持时标未执行，否则未知；不把 W2 计算结束当提交，也不把 W3 当 skipped。既有费用/副作用不被显示成已撤回。
- **Verification:** 真实 builder 编译多波测试图、引擎、预算、事件及 trace，测试节点实际触发失败；经路径公开 HTTP 和浏览器检查。真实两条业务 workflow 成功覆盖另见 CE-10，测试图不替代业务验收。
- **Decision status:** **CONFIRMED**；来源为 Q1–Q8 两轮批准及设计 r3 整体“确认”，完整验收定义无删改；验证 **NOT RUN**。

### CE-03 观察不使请求重投或计量改变

- **Basis:** R-01/R-09；Q1/Q7/Q8；AC-01/18/20；#72 CE-07。
- **Sequence:** 浏览器提交真实聚合，服务端已接受但 202 响应被截留/丢弃，原页面保持提交不明；通过已有运行入口定位该 Run 查看/刷新/关闭路径。另测模型阻塞中和 before_recording_commit 屏障下。
- **Expected behavior:** 固定 Run、目标会话、请求和禁止重投状态不变；不出现第二 Run、新 Attempt 或新业务工具调用；原结果恢复路径仍可用；观察不保存分流参数或改变聚合草稿。
- **Verification:** 真实 browser/HTTP/Coordinator/SQLite/RunRecorder；控制真实接受响应和模型/记录提交屏障。比较前后 Run/Attempt/费用及固定请求，不能用假准入/假记录结果证明。
- **Decision status:** **CONFIRMED**；来源为 Q1–Q8 两轮批准及设计 r3 整体“确认”，完整验收定义无删改；验证 **NOT RUN**。

### CE-04 半波事件前缀不能证明完整提交

- **Basis:** R-02/R-04；Q4/Q5；AC-05/06/09；engine.py 的提交与逐节点发布事实。
- **Sequence:** 同波 A/B 实际执行；停在首个节点完成事实公开后、该波完整可证明边界尚未公开时，请求路径快照。随后放行、重新读取；另将测试 trace 留在该边界之前的有效连续前缀。
- **Expected behavior:** 首次只呈已有证据，不因 A 完成而宣称整波或 B/出边已提交；局部状态可见但缺口明确。完整发布后正常快照显示该波及边的真实结论，不永久用 unknown 规避。截断历史不能获得后续内存事实。
- **Verification:** 真实多节点编译图、FanOut 与测试 sink/发布屏障；另一线程真实 HTTP 读取、browser 渲染。若实现采用原子波事实，在其发布前/后控制同一语义边界；禁止 mock 路径读模型或 sleep。
- **Decision status:** **CONFIRMED**；来源为 Q1–Q8 两轮批准及设计 r3 整体“确认”，完整验收定义无删改；验证 **NOT RUN**。

### CE-05 图失败但 Run 经普通循环完成

- **Basis:** R-03/R-09；Q4；AC-07/20；原有消息分流回退合同。
- **Sequence:** 真实消息分流图失败且满足既有回退条件；普通循环返回成功回复。对照组让已有副作用/unknown 等条件阻止回退。
- **Expected behavior:** 两组均保留 Graph Failed 与原原因；第一组标图外回退已进入、Run completed；第二组显示真实阻止原因及 Run 结局。不将图内 fallback label 当故障回退，不给 describe 增加普通循环假节点，既有预算/计量不重置。
- **Verification:** 真实 routing/Graph/普通循环/共享预算与记录；模型和外部工具可控替换，实际触发故障及副作用事实；通过 HTTP/browser 和账目核验。
- **Decision status:** **CONFIRMED**；来源为 Q1–Q8 两轮批准及设计 r3 整体“确认”，完整验收定义无删改；验证 **NOT RUN**。

### CE-06 屏障前可见不等于重启后已保存

- **Basis:** R-06/R-08；Q5/Q7；AC-09/11/17；ADR-0019/0023/0024。
- **Sequence:** P1 中读取当前路径；阻塞 trace 写入/持久屏障或保持记录 pending，终止 P1 后以同目录启动 P2，再打开同 Run。分开测试完整可读 trace、仅有效前缀、缺记录和会话记录失败。
- **Expected behavior:** P1 内存不能补 P2 的缺失证据；依据实际可读持久记录呈现完整/局部/不可用。Run 结局遵守原索引恢复语义，recording_state 与 trace/路径完整性不互相替代。不能断言屏障前全部数据必失，也不能保证必存；没有自动重跑。
- **Verification:** 真实宿主进程/状态目录/RunRecorder/trace、HTTP/browser；受控 I/O/记录屏障与进程终止固定持久前缀。使用已完成屏障的对照 Run 验证正常历史恢复。
- **Decision status:** **CONFIRMED**；来源为 Q1–Q8 两轮批准及设计 r3 整体“确认”，完整验收定义无删改；验证 **NOT RUN**。

### CE-07 裁剪、损坏和未知 schema 不制造路径

- **Basis:** R-05/R-07；Q3/Q6；AC-10/12/13/14/15；ADR-0019/0020。
- **Sequence:** 完成真实 Run 并展示路径。各独立用例：通过受管测试边界删除 bundle 并记裁剪事实；无裁剪行而文件缺失；唯一尾截断；内部坏行；错 Run 身份/非法 seq；未知必需 schema；缺历史 describe；文件超过 32 MiB；各场景刷新。
- **Expected behavior:** 分别显示已裁剪、缺失、部分、不可信/不支持、结构不足、超限；只可信连续前缀可支持局部事实。内部损坏不得跳洞；裁剪/不可用撤下旧路径，独立可信结果/账目保留；任何情况不套当前图或显示成未执行。资源有界。
- **Verification:** 真实 trace/SQLite/读取接口/browser。由存储裁剪记账 seam 构造真实生命周期事实，不冒称已有自动调度器；损坏/协议注入是异常测试，单独标记。恢复用真实有效保留 bundle 或新的有效读取，不执行原 Run。
- **Decision status:** **CONFIRMED**；来源为 Q1–Q8 两轮批准及设计 r3 整体“确认”，完整验收定义无删改；验证 **NOT RUN**。

### CE-08 迟到响应与重启身份

- **Basis:** R-04/R-08；Q2/Q7；AC-16/17。
- **Sequence:** 截留 A Run 的请求 a1，切到 B 并先完成 b1，再放行 a1；同 Run 另测旧刷新后到、关闭重开、离页。P1→P2 重启后取得新读取，再放行 P1 的旧响应；同时验证由 P2 读取原始身份 P1 的历史 Run。
- **Expected behavior:** 仅最新有效请求整体更新；无跨 Run 节点/选择串位，旧宿主响应不能覆盖当前读取。合法历史 P1 身份保留，不因当前宿主 P2 而拒绝；相同 hash 不恢复失效请求资格。
- **Verification:** 真实 HTTP/browser 响应屏障及真实 host 身份；检查 run_id、服务来源、结构/路径集合与读取提示，用屏障而非延时碰运气。
- **Decision status:** **CONFIRMED**；来源为 Q1–Q8 两轮批准及设计 r3 整体“确认”，完整验收定义无删改；验证 **NOT RUN**。

### CE-09 观察及持久化降级不改变业务

- **Basis:** R-06/R-07/R-09；Q5/Q6/Q8；AC-11/14/20/21。
- **Sequence:** 真实 Run 执行并刷新路径，同时使新观测缓存达到/超过配置上限；另测 sink 序列化/commit 普通 Exception、trace os.write/fsync 错误。对照输入相同且无该观测故障的执行。
- **Expected behavior:** 可预期观测失败明确标缺口/不可用，保留原业务 outcome/reply/Attempt/费用和原有记录故障语义；不引入额外网络往返、自动回退或重试。中央 fatal/控制异常仍按原合同传播，不能为测试通过吞异常。
- **Verification:** 真实 Host/Graph/FanOut/trace/记录及公开查询；只替换实际外部模型、存储 I/O 或受控容量边界，记录原始结果/计量对照。路径 GET 本身不占用执行准入。
- **Decision status:** **CONFIRMED**；来源为 Q1–Q8 两轮批准及设计 r3 整体“确认”，完整验收定义无删改；验证 **NOT RUN**。

### CE-10 恢复、跳过与无动作不混同

- **Basis:** R-02/R-03；Q4/Q8；AC-03/04/06/07。
- **Sequence:** 在两条真实 workflow 中依次覆盖：正常分支；消息分流的 full 与 fallback 条件；真实上下文故障经过 error 恢复；手动聚合未选择来源而跳过；明确 NoAction；明确分流关闭/图不可用而图前绕行。
- **Expected behavior:** 图/文字保留全部历史结构及不同标签；实际决议、配置跳过、失败与恢复路线和原因准确。NoAction 没有空回复/草稿；图前绕行有明确证据时说明未入图及原因，无证据保持未知。taken 的目标若失败仍是失败。
- **Verification:** 真实 Host/两张编译图/业务入口/记录与公开 HTTP/browser；模型替身和真实输入投影/来源读取故障控制触发路径，不替换 GraphResult。断言所选与未选边/节点、会话写入和已有摘要一致。
- **Decision status:** **CONFIRMED**；来源为 Q1–Q8 两轮批准及设计 r3 整体“确认”，完整验收定义无删改；验证 **NOT RUN**。

### CE-11 可访问明细与合法未知标识

- **Basis:** R-02/R-09；Q4/Q7/Q8；AC-03/18/19/21。
- **Sequence:** 桌面和窄屏下纯键盘打开路径、选择节点/关联 Step，刷新同结构后切换 Run；在受支持 schema 的真实测试图中包含无中文说明的合法标识及含标记字符的展示文案。
- **Expected behavior:** 图与文字覆盖同一节点/边/wave/状态；不依赖颜色或画布定位。同行为刷新保留合法视口选择，跨 Run 清除；缺中文只标暂无说明不删结构。文案为纯文本，关联证据不存在则明确不可用，不制造正文或泄露新敏感字段。
- **Verification:** 真实浏览器两类 viewport 与键盘、实际运行/读取；未知合法结构通过 builder 生成并绑定测试 Run，协议恶意值单列异常注入；检查 DOM 文本、安全约束和集合等价。
- **Decision status:** **CONFIRMED**；来源为 Q1–Q8 两轮批准及设计 r3 整体“确认”，完整验收定义无删改；验证 **NOT RUN**。

### CE-12 手动读取遇到 Run 收尾

- **Basis:** R-04/R-06/R-08；Q2/Q5/Q7；AC-02/09/11/15。
- **Sequence:** 在 before_recording_commit 阶段请求路径；截留响应，同时让 Run 记录成功或失败并由既有 SSE 更新外部 Run 徽标，然后释放响应；用户再次手动刷新。另使首次/后续读取网络失败，再恢复。
- **Expected behavior:** 路径保留其读取时来源和边界，不因更晚的外部 Run 徽标自动宣称路径完整/已保存；每次读取全为一种一致快照，若源切换无法一致则明确失败及可重试，不能拼接。网络失败旧图明确标旧；下一次成功整体替换。
- **Verification:** 真实 Host 记录屏障/HTTP/browser 与持久 trace，控制响应次序，分别测试 recorded/failed；检查读取次数、快照身份、独立徽标与内容，无 SSE 自动路径更新。
- **Decision status:** **CONFIRMED**；来源为 Q1–Q8 两轮批准及设计 r3 整体“确认”，完整验收定义无删改；验证 **NOT RUN**。

## Readiness and Open Decisions

- **整体设计 CONFIRMED；规范 READY FOR IMPLEMENTATION**。Q1–Q8、R-01–R-10、AC-01–AC-22、CE-01–CE-12、精确预期及公开 seam 已获两轮批准和 r3 整体“确认”。
- 未决产品选择 **none**；未决验收 blocker **none**；required deferred cases **none**。没有用户未批准的删除、替代或排除。
- readiness 与 publication 分开：发布目标为地图 #1 下独立的 `wayfinder:task` + `ready-for-agent` 实施票；不认领、不启动执行 agent。本轮用户显式调用 to-spec，承接其“生成规范并发布项目 tracker”的动作范围。
- 原 #73 是设计票，本次完整 resolution 回写并按 completed 关闭仅表示裁决完成；新增产品由实施票承接，不能把决策关闭当产品交付。
- 产品候选 NOT STARTED；全部新增产品检查/浏览器验收/付费模型评估 NOT RUN；独立 Standards/Spec NOT REVIEWED。

## Out of Scope

- 不提供通用选图执行、重放执行、节点/边/规则编辑、参数预演或新增 workflow。
- 不新增 Graph 顶级页、Behaviour 历史 Run 选择器、路径动画/SSE 自动跟随、跨重载观察偏好；路径需要用户主动刷新。
- 不新增永久逐节点账、原始 node state/writes、正文副本、保留设置/裁剪调度器、搜索/导出工作台；保留期后细节不可恢复。
- 不回填无法证明的历史结构或路径；老数据和失败场景允许明确不完整/未知，不允许虚假完整。
- 不更改消息分流/聚合执行、预算、工具副作用、会话写入、记录或费用策略。#70 导出与 #74 统计合同不由本票重开；#72/#75 静态结构合同继续适用。
- 内部实现自由度包括具体 HTTP 路径、模块/事件类型、缓存结构及受控上限；必须在实现证据中明确并覆盖边界，不以自由度更改上述用户行为。

## Further Notes

唯一状态入口：`/Users/nineofour/Agent-Alfred/.scratch/graph-run-evidence-73/LOOP.md`。进入实施先读取其当前 revision、权威 Issue URL/规范哈希、批准与发布回读；使用同一入口，不另建竞争状态文件。源设计 r3 和 APPROVAL-r3.md 用于确认溯源，不是额外必读验收 companion；全部运行时验收已在本文。

下一 owner 为未来被用户启动的实现会话，需核对最新源码并承接本文全部库存及公开测试边界，逐项绑定实现候选和原始证据。本次发布不授予产品实现或 Git 提交/推送/合并授权。规范 URL/哈希、实施票 labels/assignees/父关系和 #73 resolution/关闭、地图指针须发布后分别回读。后续任何产品交付仍须实施及候选绑定验收，不能凭 ready-for-agent 标签推断完成。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/81#issuecomment-5746548404

<!-- issue-81-acceptance -->
# #81 实现验收

Run 详情已加入默认收起、手动读取的“本次执行路径”。路径使用该 Run 捕获的结构与有序事实，分别显示节点计算、波提交、Graph/Run 结局和记录来源；历史路径与 trace 共享保留生命周期。观察失败、损坏、裁剪及超限有明确状态，保留独立可信的 Run 摘要。

## 固定身份与交付链

- 规范：本 Issue `GRAPH-RUN-EVIDENCE-SPEC-r1`；22 AC / 12 CE，未修改确认的预期或缩减范围。正文 SHA256 `13ddc86a0debc26435658266a08331088196892115a5e4604b49e47753f2bd2c`。
- 最终候选：`issue81-c6-cba6eda6a264`；完整 25 文件，包含 baseline `98c8afa19a3484f7fdd4108569d9a6847707f7a1` 以来全部提交和本地改动。
- Manifest SHA256 `f8f4b5dcb93f07dabb0dcef92b4e2f6adcbb8cb439b4dd2d8fa42e328d99b189`；完整 diff SHA256 `88298869b30c2883ee5d393873c0965a17f96d6a979768758126d1da0185de8a`。
- [PR #82](https://github.com/nineofoursyrup/Agent-Alfred/pull/82) 已合并；head `d0855cb3caaa759855d9873544da2cf5139a0737`；merge `da5a9e3fee6cf6b9e9254ba9c53d73b1ab63d7f0`。
- 精确暂存区、提交树、merge tree 逐文件核验一致；树 `f9d20581a8f638640742aa98e1aff449de0a4262`；merge parents 为上述 baseline + head，远端 main 包含 merge。
- [PR-head CI 35477668650](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/35477668650)：**SUCCESS**，绑定 `d0855cb`，后端 **4967 passed / 1 deselected**，browser **351 passed**。
- [merge-SHA CI 35478877496](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/35478877496)：**SUCCESS**，绑定 `da5a9e3`，后端 **4967 passed / 1 deselected**，browser **351 passed**。

## 独立评审与检查

同一 C6 的 fresh entry `01a0bc0a-b6ec-7723-871e-5f32b50aa5b8` 汇合：

- Standards `01a0bc0c-6f6c-7603-bee3-3f5d7d1bd35e`：**PASS**。
- Spec `01a0bc0c-6f69-7061-a906-30c27d319c16`：**PASS**。
- 两轴分别审查完整候选及全部验收范围，实际进程 exit 0、原报告哈希与首尾 no-drift 均核验。未决产品 blocker、证据 blocker、optional 均为 **0**。
- STD-01、SPEC-01、COORD-01、STD-02、SPEC-02、EVID-01、STD-03、SPEC-03、COORD-02、SPEC-04、EVID-02、CI-01 均保留来源及回归证据，当前候选分别确认 resolved。
- 本地 C6 新跑全 browser **351**、专项 browser **70**、Ruff / skills / env-example / build / MCP installations / typecheck 全 PASS。Python 完整 **4967 / 1 deselected** 和专项 **86** 按 C5 相同输入字节明确复用，两轴另行核验复用边界；不冒称 C6 本地新跑。
- 原 PR CI `35475196032` 的 checkbox readiness 失败保留原始证据；真实 GET 响应屏障单次复现，增加真实 enabled 等待后保持原 NoAction/逆序/导航断言。未增加 sleep、retry 或放宽预期；修复后 PR-head CI 已全新运行完整工作流成功。

## 逐项 AC / CE 验收

以下 P 为 [test_run_path.py](https://github.com/nineofoursyrup/Agent-Alfred/blob/da5a9e3fee6cf6b9e9254ba9c53d73b1ab63d7f0/src/agent_alfred/evals/deterministic/test_run_path.py)，B 为 [run-path.spec.js](https://github.com/nineofoursyrup/Agent-Alfred/blob/da5a9e3fee6cf6b9e9254ba9c53d73b1ab63d7f0/tests/browser/run-path.spec.js)；以交付 commit 的源码为准。

| AC | 公共路径验收证据 |
|---|---|
| AC-01 | B：真实 routing / aggregation 既有 Run 入口，默认折叠与独立 Run URL。 |
| AC-02 | B：折叠无请求、展开/重开/手动刷新；实际 recording 屏障验证 SSE 不重写已读快照。 |
| AC-03 | P/B：两条真实图的 9/15 节点、10/20 边及条件标签与捕获 describe 一致。 |
| AC-04 | P/B：实际节点完成、跳过、失败、恢复、整波撤销和半波未知分别呈现。 |
| AC-05 | P：原子 wave 事实发布屏障及 node/writes/router 三类实际失败；B：局部前缀与撤销明细。 |
| AC-06 | P/B：full/unknown/no_reply、normal/error 条件边与实际执行结果，未走分支不推断目标成功。 |
| AC-07 | P/B：真实恢复、普通回退、阻止回退、disabled/unavailable 绕行、NoAction；SPEC-04 实际政策事实交叉校验。 |
| AC-08 | P：G1/G2 同 hash 与不同 hash 发布；B：P1/P2 重启，始终使用原 Run 结构代际。 |
| AC-09 | P：固定 snapshot prefix 与锁外复制；B：持久化收尾竞争、同 Run 逆序响应及整体来源替换。 |
| AC-10 | P/B：完整 trace 持久读取和真实重启；旧记录缺结构不从当前图补齐。 |
| AC-11 | P/B：真实 write/fsync 故障、SIGKILL 四种持久边界、recording pending/failed；Run 与 trace 覆盖分轴。 |
| AC-12 | P/B：删除受管 bundle 并记录 prune fact，撤下路径并保留 Run/独立摘要；普通缺失另行标示。 |
| AC-13 | P/B：合法连续前缀及损坏/身份/schema/顺序/终态/阶段矛盾拒绝；恢复原 bundle 后重读。 |
| AC-14 | P：32MiB 历史上限、当前 4096 events / 2MiB 精确边界；B：实际缓存超限后的持久恢复。 |
| AC-15 | B：首读/重读网络失败、旧快照标示、有效不可用响应撤图、partial 替换和恢复。 |
| AC-16 | B：同 Run 逆序、关闭重开、离页、A/B Run 切换，迟到响应无串位。 |
| AC-17 | B：真实服务 P1/P2 重启，合法原始身份保留，P1 迟到响应无覆盖资格。 |
| AC-18 | B：合法选择与视口保留、跨 Run 清理、重载默认折叠；聚合请求/目标 Session 不变。 |
| AC-19 | B：1280×850 与 390×844、键盘开关/刷新/缩放/适应、等价文字明细及 Step 引用定位。 |
| AC-20 | P/B：实际公开 Ops 对照 Attempt/usage/cost/tool；失去 202 与 pending recording 无重投，snapshot 复制不阻塞业务。 |
| AC-21 | P/B：Host/Origin 保护、序列化前最小事实白名单、纯文本未知标识、稳定既有引用与中央脱敏。 |
| AC-22 | 下列 12 CE、当前候选独立双轴和八项门禁；旧 run-evidence、静态图、路由、聚合、trace-export、计量回归。 |

| CE | 验证范围 |
|---|---|
| CE-01 | G1/G2 同/不同 hash 发布与真实重启，历史结构/文案不混代。 |
| CE-02 | 真正先提交一波后触发三类下一波失败，保留前波和已发生费用。 |
| CE-03 | 聚合真实丢失 202、真实 model/recording 屏障，原请求恢复且 POST/Run/计量不重复。 |
| CE-04 | 实际 path.wave 发布前屏障；半波前缀不得显示完整提交。 |
| CE-05 | 真实图失败→普通循环完成，真实副作用/期限/预算阻止，以及矛盾阶段拒绝/恢复。 |
| CE-06 | 四种真实 SIGKILL 后重启：完整、前缀、缺失、recording_failed；不从旧进程恢复内存。 |
| CE-07 | 裁剪、内部坏行、身份/seq/schema/生命周期/终态/阶段矛盾；拒绝和恢复各有对照。 |
| CE-08 | 实际响应屏障控制 A/B 与 P1/P2 迟到顺序，不使用 sleep 制造竞态。 |
| CE-09 | observer prepare/commit/capacity、trace write/fsync、锁外 snapshot 复制；实际业务及费用不受观察影响。 |
| CE-10 | 两条真实 workflow 正常成功、error recovery、source skips、NoAction、图不可用绕行；专用图仅补边界。 |
| CE-11 | 两种视口与键盘、图文等价、原 Step 定位、合法未知标识及文本注入防护。 |
| CE-12 | 实际 recording commit 前后和同 Run 请求逆序，手动快照边界一致、恢复无需重新执行。 |

所有协议损坏验证均区分异常注入与正常业务成功，并保留拒绝及恢复原 trace 后的成功读取；恢复不重投业务。公开验收使用真实 Host/Graph/trace/RunRecorder/HTTP/browser，模型边界按规范使用确定性替身；专用 Graph 只补充边界反例，不替代两条真实 workflow 的正常验收。

限制：付费外部模型测试及部署均未运行；完整 Python 门禁按仓库配置排除唯一 requires_key 用例。交付仅覆盖 #81 的路径证据与 Run 详情，不代表整个 Behaviour v1 已完成。最终 Issue state/state_reason 由验收回写后独立关闭并回读。
