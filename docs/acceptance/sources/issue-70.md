# #70 实现：trace 导出的完整用户路径

来源：https://github.com/nineofoursyrup/Agent-Alfred/issues/70

# TRACE-EXPORT-IMPL-SPEC-r1 — trace 导出的完整用户路径

<!-- trace-export-implementation-spec:r1 -->

本票是已确认设计的施工规范，不是新的设计访谈或实施启动指令。以 [TRACE-EXPORT-SPEC-r1 决策 resolution][R] 为上位产品合同，**AC-01–AC-30 与 CE-01–CE-12 的唯一权威定义继续位于该 resolution 的 §11 / §12**；本规范增加结构化用户故事、实施归属、来源映射与反例执行步骤，不替换其预期结果。

上位 resolution 正文 SHA-256：`abad4a244015cd0d16bb5b6c4abbe3fc228ad919005e4f8086b8083a4035ae01`。实施与评审必须绑定该内容身份，不能只凭同名链接猜测未变。下文 `R§n` 指该 resolution 的第 n 节；D01–D12 是施工映射，不是新产品裁决。

## Problem Statement

用户能在 Dashboard 找到 Run 并查看部分过程、工具历史，却不能把这一 Run 的完整追踪范围下载成可离线核对的文件。直接复制目录会混淆业务终态、记录成功、追踪完整性和净化效果，也可能暴露正文、环境路径或标识；失败、中断、遗忘及工件缺失时尤其容易产生误导。

## Solution

在运行详情提供唯一导出入口，面向真实持久 Run 的全部用途。用户选择默认“分享净化”或显式“保留诊断正文”，系统验证停止与落盘边界，校验整包身份、事件和工件，生成带源完整性及净化清单的 ZIP，再交给浏览器原生下载。

故障 Run 可以取得如实声明缺失的诊断包；未知来源、内部坏行、不安全路径和不稳定源不能伪装成成功。导出属于人工阅读，不回流模型、检索或提炼；系统负责临时副本的失效及真实清理，不能承诺撤回用户已取得的字节。精确行为以 R§1–9 为准。

## User Stories

1. 作为用户，我希望从运行列表、会话或 Ops 定位同一个 Run 后在运行详情导出，以便不会因入口不同取得不同范围。（R§3；AC-01）
2. 作为用户，我希望聊天、探针、聚合等真实持久 Run 都能接受核验，以便系统用途不被遗漏；旧消息不被伪造成 Run。（R§3–4；AC-01、AC-03）
3. 作为用户，我希望运行和记录尚未稳定时知道需要等待，以便不会把滚动快照当作完整历史。（R§4；AC-03）
4. 作为排错用户，我希望失败、中断及可验证的记录失败 Run 仍可生成诊断包，以便不因业务失败失去排错依据。（R§4；AC-03、AC-04）
5. 作为用户，我希望分清完整、已知不完整、证据未知、净化移除和源文件缺失，以便不把下载成功误读为追踪完整。（R§2、§4；AC-04–AC-06）
6. 作为准备分享的用户，我希望默认只保留允许的结构与占位，以便减少自由正文及环境信息意外泄露。（R§6；AC-09、AC-11）
7. 作为诊断用户，我希望显式保留再次脱敏的正文，同时知道它仍可能含私人信息，以便自主决定是否分享。（R§6；AC-10、AC-12）
8. 作为离线读者，我希望 ZIP 有中文说明、清单、版本和一致工件引用，以便不用联网或启动服务就能核对内容。（R§5；AC-02、AC-07、AC-12、AC-28）
9. 作为用户，我希望重启后仍准确找到原 Run，并按事件发布顺序阅读，以便不会混淆不同进程的事件或被墙钟回拨误导。（R§5；AC-08）
10. 作为用户，我希望只能导出目标 Run 的受管落盘追踪，以便不会夹带当前记忆、配置、账单或任意业务文件。（R§1、§5；AC-13、AC-14）
11. 作为执行遗忘的用户，我希望旧临时导出失效并实际清理，而旧 trace 仍可按确认的人工历史边界重新核验导出，以便不破坏既有遗忘语义。（R§7；AC-15、AC-16、AC-25）
12. 作为用户，我希望同时发起、超限、超时或磁盘故障都有明确结果，以便不会悄悄排队、截断或自动重试。（R§8–9；AC-18–AC-21）
13. 作为用户，我希望能取消生成，清理未完成时如实显示原因，以便不把停止等待当作实际停止。（R§8；AC-21、AC-25）
14. 作为下载用户，我希望只有完整生成的包才能开始下载，中断后由我重新发起，以便不取得未经验证的混合文件或意外重复下载。（R§3、§8–9；AC-22、AC-23）
15. 作为用户，我希望切换 Run、离页、重连及重启不会恢复旧任务或下载凭据，以便不会误取另一任务的文件。（R§3、§7–8；AC-24、AC-26）
16. 作为用户，我希望恶意网页、归档内容和错误消息不能借导出泄漏数据或执行内容，以便维持本地 Dashboard 的既有安全边界。（R§5–9；AC-27、AC-28）

## Implementation Decisions

### D01 — 宿主所有权与来源边界（R§1、§3、§10）

由 RuntimeHost 所有的导出服务处理目标身份、资格、任务与资源；HTTP 只作参数/来源校验、调用和响应映射。正文来源限中央 FanOutSink/Redactor 路径形成的落盘 trace 与工件；源 meta 经身份校验并二次净化。DB/宿主只提供生命周期、记录/屏障、裁剪和失效控制事实，不把整个 Run 行、原始模型请求或内存转录塞进包。

语义接口为发起、查询当前任务、取消、一次性下载；具体 API 路径、机器码和内部模块布局由执行者沿仓库惯例确定，这些名称不是未决产品选项。实现必须保留 R§9 的全部失败类别、恢复行为和 HTTP 已开始响应后的语义。

### D02 — Run 资格与三轴完整性（R§2、§4）

逐项实现 R§4 矩阵，不用一个 success/complete 字段合并 Run 结局、recording_state、trace_incomplete、源完整性与净化结果。accepted/running/实际记录 pending 不生成；记录失败需证明 Run 与 trace 写侧已停止。重启后 telemetry 缺失是证据未知，不自动解释为仍在保存，也不能只因缺记录就放行。

已知缺失或屏障失败优先 `known_incomplete`；无已知缺失但正证据不足为 `unknown`；可信终态、记录/屏障、结构、顺序、收尾和全部引用工件通过才 `verified_complete`。空 trace、尾部截断和缺引用工件可如实诊断；内部坏行、整包/trace 文件缺失、已裁剪、未知格式或不安全结构按矩阵分别拒绝，不能补造事件或文件。

### D03 — 文件格式、历史兼容和稳定身份（R§5）

ZIP：`alfred-trace-<随机导出标识>.zip`；固定成员 `manifest.json`、`README.txt`、`bundle/meta.json`、`bundle/trace.jsonl`、`bundle/artifacts/`。UTF-8、中文说明、JSON/JSONL/纯文本，导出 schema=1，与源布局/事件版本及净化规则版本分开。支持明确识别的 `legacy-unversioned` 已知形状及当前 `artifacts/tool-<seq>.txt`；未知形状不猜测、不迁写原 bundle。

真实持久 Run 身份用于服务端定位，原始标识不进归档路径；包内同类身份一致别名化，不附原值表、原存储标识或原文摘要。核对事件 Run/process，按原 seq，不要求连续、不按墙钟排序。源尺寸/摘要不可直接充当净化后事实；引用、长度、摘要按实际导出字节重算，manifest 不声称自校验。分享档 ZIP 元数据不泄露绝对时间。无关联工件不直接复制；未知文件或不安全结构拒绝。详细字段边界全部继承 R§5–6，不把本节摘要当替代 schema。

### D04 — 固定两档净化（R§6）

完全沿用 R§6 字段矩阵：默认分享仅允许结构、枚举、耗时、允许的计数及相对时间，自由正文/思考/提示词/参数/结果/错误/工件正文占位；诊断保留再次脱敏文本和历史时刻。两档的关联身份、结构路径、端点和环境信息仍须别名/移除，动态名字不是安全枚举。

当前凭据、敏感键、已知身份原值及状态根/主目录规则覆盖正文与工件。未知非关键字段移除并记录固定原因，未知关键结构/事件/格式整次拒绝；被删键名或异常原文不能作为原因泄出。诊断模式不保证任意 PII、路径写法、编码标识或编码秘密被识别。不存在关闭中央脱敏、单字段危险开关或“安全公开”保证。

### D05 — 源一致性与裁剪协调（R§7）

读取租约与 deleting 预约同锁线性化，源租约持续到任务终结清理，包括待下载及发送期；这允许该 bundle 的裁剪延后，但不允许长期持 DB 写锁。生成完成和下载开始前重验身份；读取中或生成期间源变化整次失败，不能拼新旧内容。

复用受管文件描述符与身份检查，不能以 Path 先检查后重新 open 代替，也不能把安全 fd 租约误认为保留引用租约。不承诺防同账号恶意进程，但已列路径/替换反例必须拒绝。导出上限不等于已有写侧阀门；本票不补全自动保留系统。

### D06 — 失效与遗忘清理（R§7）

绑定实例、Run、源身份、导出/净化版本、Redactor protection_version 和 memory revision。任何记忆/保护变化都失效，包含无关记忆编辑；通知不是权威证据，发起/发布/下载关键边界重验。失效与发送准入有确定顺序，不能“检查后无锁稍后发送”；旧页、漏通知、迟到响应均不能复活任务。

接入既有遗忘受管清理目标与真实释放核验；不能替换当前单一回调而使 Database 或其他消费者丢通知。已删除记忆的旧 trace 可在清理后重新人工发起，不进入模型/检索/提炼。已发字节不能撤回，释放未完成不能把清理项标 complete。

### D07 — 单任务、限制与预算（R§8）

全宿主生成/ready/发送/清理合计一项，无队列、抢占、自动重试。源合计、净化未压缩合计及 ZIP 分别上限 512 MiB，按实际编码字节；恰好边界允许继续，超1字节失败，不能靠压缩率或静默丢弃满足限制。

生成60秒从接受起，下载120秒从接受下载起，ready5分钟从生成完成起；按单调时钟。允许有界读写及处理，不整包入内存。取消/超时先停止新发布/发送，资源还活跃时保留清理状态与所有权，不提前开放下一项，不承诺同步IO可瞬时硬中断。

### D08 — 浏览器与下载安全（R§3、§8–9）

完整验证和生成后才能下载；用原生流程，无整包页面累积或持久正文缓存。下载凭据不可预测、一次性、绑定任务与实例，普通任务ID不是下载授权；消费与下载接受原子决定。沿用 Host/Origin/CSRF/no-store，不为原生下载开来源绕过口，不记录凭据和正文。

无 Range 续传、跨实例恢复或自动再下载。发 ZIP 头前可安全结构化拒绝；发送开始后失效/超时/断连只能终止连接并更新任务，不能拼第二份JSON错误。服务端传输结束不等于用户文件保存成功；下载文件或残片不由系统删除。

### D09 — 终结、权限及恢复（R§8–9）

生成失败、取消、失效、过期、发送完成及断连都进入真实清理；目录0700、文件0600，资源包含临时文件、句柄、缓冲、发送与源租约。清理失败可见、保持所有权、暂停新导出，核实释放后才开放。

离页/切Run清理未开始下载的任务；已开始原生下载可继续。关闭服务先停止准入并排空，不先关闭仍被使用的依赖。重启只回收已验证的本功能残留，不恢复旧任务/凭据，不通配删未知目录、源 bundle 或用户副本。

### D10 — 精确失败合同（R§9）

必须区分：未知Run/无持久身份；未停止/记录pending；系统裁剪/无裁剪事实的缺失；可读但不完整；不支持/损坏/身份或路径安全失败；源或版本变化；busy/各项超限/磁盘不足/IO失败/生成超时；用户取消/断连/下载超时/凭据失效重用；清理受阻。对应 UI、任务状态、manifest、README 不互相矛盾；错误文本也受净化约束，不泄露坏行、原路径或堆栈。不得借重试重复创建任务或修改源。

### D11 — 实现分工与基线缺口（R§10）

| 所有者 | 责任与复用限制 |
| --- | --- |
| RuntimeHost / 导出服务 | 资格与停止证明、单任务、权威版本、读租约、生成/取消/失效/清理所有权 |
| trace读取/净化/归档 | 全包验证、已知历史形状、工件关联、两档净化、完整性与清单、有限资源打包 |
| HTTP/浏览器 | 同源安全、任务/状态/取消/一次性原生下载、有界发送、页面/连接/实例隔离 |
| Memory/保护生命周期 | 扩展清理目标和失效消费者，保持既有Memory/Database语义 |
| 验收/文档 | 按全部AC/CE验收、相关回归、领域术语和ADR，保留规范单源 |

事实基线 `2533d6818c4b6a896f460b20d5e0fe40142d335e`：没有完整 ZIP 导出；evidence 的32MiB投影与 ToolHistory 的64MiB trace/256KiB分段不满足整包合同；ToolHistory 读取期间持 DB 锁，不能直接包进压缩路径；尚无通用 TraceStore 引用计数/deleting；源meta缺版本；未发现完整自动保留和写侧512MiB/磁盘阀门。现有 Database 的受控发送/清理模式可参考，但它发送内存JSON，不是ZIP下载器。这些是实施缺口，不是需要重开产品裁决的依据。

### D12 — 变更边界（R§10、§13）

如需 schema 变更用前向迁移，不改已发布DDL；无需要不强加迁移。现有未提交文档只语义移植相关局部，不整份覆盖最新CONTEXT。不实现独立财务导出、任意文件下载或自动保留系统。本票交付必须分别报告实现/验收/远端状态，不凭 ready-for-agent 宣称完成。

## Testing Decisions

### 已确认公共边界与替身

设计和所需测试边界均已确认（Q13、R§11），本轮不再访谈：真实 RuntimeHost、临时 SQLite/FTS 和托管文件、真实 ToolRegistry、真实 HTTP 与浏览器。模型外部依赖通过已有 ModelClientFactory/ScriptedModel 注入；只有需要真实 Attempt wire 形状的场景才使用已有 Adapter + 本地假传输。不得用静态宿主、假API成功响应或手拼UI状态证明合同。

观察入口分四类：S1=宿主公共能力/实际持久记录；S2=真实HTTP状态、响应字节及任务状态；S3=真实浏览器导航、原生下载、解压文件和可见状态；S4=受管资源/磁盘/权限/清理结果与源不变性。文件内容/尺寸/摘要等读回是黑盒证据，私有函数被调用不能代替它。

允许注入 FakeClock、模型IO、本地文件IO失败、确定性屏障和信号；先等待明确事件，再改变单一条件，再观察结果，禁止用 sleep 猜窗口。浏览器可以延迟/重排真实服务器响应（先 route.fetch 再延迟释放），不能伪造业务成功。新导出能力从其公共HTTP/宿主入口验证，具体函数名不是验收前提。

### 现有测试先例（固定到事实基线，不声称已经覆盖新功能）

以下文件均在 [基线源码][BASE] 下，实施时可参考并扩展相邻公共边界：
- `src/agent_alfred/evals/deterministic/test_ops_history.py`：真实Host和工具产生长UTF-8工件；替换文件拒绝；参数脱敏与裁剪后账目保留；失败close保留所有权。
- `src/agent_alfred/evals/deterministic/test_trace_bundle_integrity.py`、`test_trace_sink.py`：真实bundle写入、部分写及持久性边界；不能把旧green当新导出证据。
- `src/agent_alfred/evals/deterministic/test_forgetting_trace.py`、`test_forgetting.py`：非终结前缀屏障、删除/清理及失败不伪装完成。
- `src/agent_alfred/evals/deterministic/test_database_lifecycle.py`、`test_database_http.py`：取消、失效、发送与真实释放模式；不复制SQL业务语义到人工trace历史。
- `src/agent_alfred/evals/deterministic/test_web_http.py`：真实socket、来源/CSRF和关闭排空先例；其中使用facade的协议单测不能代替本票真实Host验收。
- `tests/browser/runs.spec.js`、`accounting.spec.js`：运行定位与Ops跳转；`database_cancel.spec.js`：延迟真实响应、旧动作不覆盖新动作；`database_lifecycle.spec.js`、`restart.spec.js`：实例/页面生命周期。

### 全部验收来源映射

本表只指定来源、施工归属与验证入口。**每项完整预期逐字以 [R§11][R] 相同编号为准**；30项无删除、替换、合并取消或延期。执行者逐项填写PASS/FAIL/NOT RUN、候选身份和可访问证据。

| 原编号 | 决定来源 | 施工归属 | 验证入口 |
| --- | --- | --- | --- |
| AC-01 | R§3 | D01 | S2、S3 |
| AC-02 | R§3、§5 | D03、D08 | S3、S4 |
| AC-03 | R§4 | D02 | S1、S2、S3 |
| AC-04 | R§2、§4 | D02 | S1、S2、S3 |
| AC-05 | R§4 | D02 | S2、S4 |
| AC-06 | R§4、§9 | D02、D10 | S1、S2、S4 |
| AC-07 | R§5 | D03 | S2、S4 |
| AC-08 | R§2、§5 | D03 | S1、S2、S3 |
| AC-09 | R§5–6 | D03、D04 | S2、S3、S4 |
| AC-10 | R§6 | D04 | S2、S3、S4 |
| AC-11 | R§5–6 | D03、D04 | S3、S4 |
| AC-12 | R§5–6 | D03、D04 | S3、S4 |
| AC-13 | R§4–5、§7 | D03、D05 | S2、S4 |
| AC-14 | R§1、§5 | D01、D03 | S1、S4 |
| AC-15 | R§7 | D06 | S1、S2、S3、S4 |
| AC-16 | R§7 | D06 | S1、S2、S4 |
| AC-17 | R§7、§10 | D05 | S1、S2、S4 |
| AC-18 | R§8 | D07 | S2、S3、S4 |
| AC-19 | R§8 | D07 | S2、S4 |
| AC-20 | R§8 | D07 | S1、S2、S3、S4 |
| AC-21 | R§8–9 | D07、D09、D10 | S1、S2、S4 |
| AC-22 | R§3、§8 | D08 | S2、S3 |
| AC-23 | R§8–9 | D08、D10 | S2、S3 |
| AC-24 | R§3、§7–8 | D06、D08、D09 | S2、S3 |
| AC-25 | R§7–8 | D06、D09 | S1、S2、S4 |
| AC-26 | R§8 | D09 | S1、S2、S4 |
| AC-27 | R§8–9 | D08、D10 | S2、S3、S4 |
| AC-28 | R§5–6 | D03、D04 | S3、S4 |
| AC-29 | R§7–8、§10 | D05、D06、D09、D11 | S1–S4 |
| AC-30 | R§10–13 | D11、D12 | 候选、逐项证据与范围读回 |

### 必需检查

新增全部AC/CE公共路径证据及相关旧路径回归，尤其真实浏览器下载后解压内容核验；不能以打包单测或CI绿灯替代。按实施时适用项目说明运行检查；当前基线 CI 已有 `uv run ruff check`、skills/env校验、`uv run --extra mcp pytest`、`uv build`、artifact安装核验、`npm run typecheck`、`npm run test:browser`，参见 [.github/workflows/ci.yml][CI]。本票不改造整套发布门禁，不新设付费模型必测项。所有新功能测试本次均NOT RUN；后续未执行的必需项是验收缺口，不能自动改成非目标。

## Critical Counterexamples

以下逐一保留 R§12 的 CE-01–CE-12；每条均为**已确认要求，NOT RUN，并非已观察缺陷**。具体前置/顺序是对既有预期的执行展开，不改变原裁决。除另写说明，失败不得修改源bundle、业务结局或记录事实；重试只由用户显式重新发起，原错误不得因重试被抹掉。

### CE-01 — ZIP合法不证明源完整

来源：R§4–5、§12 CE-01；AC-02、AC-04、AC-06。前置：真实Run已停止，源事件引用一件工件。顺序：Run落定 → 在导出前移除该引用工件 → 经HTTP生成 → 浏览器下载并解压。预期：如果其余来源安全可读，可输出诊断包，但源完整性为known_incomplete，页内/README/manifest明确缺失，不伪造工件正文或摘要。恢复：只有补回可验证来源并新发起任务才重新判断。入口S2/S3/S4；用真实生成工件和确定性的删除时点，不伪造ZIP成功。

### CE-02 — 收尾事件不等于记录或屏障成功

来源：R§4、§12 CE-02；AC-03、AC-04。前置：真实路径生成run.finished，同时通过记录提交/屏障故障或已支持的历史缺证形成记录失败/屏障未知。顺序：等待写侧确已停止 → 核验持久事实与宿主观测 → 发起诊断导出。预期：不补成recorded，不标verified_complete；有明确追踪失败为known_incomplete，仅缺正证据且无已知缺失为unknown；不能把实际pending放行。恢复：重启恢复保留原Run身份与未知事实，不猜终态。入口S1/S2/S3；使用提交/flush边界屏障及真实持久记录。

### CE-03 — 新保护规则不能留下旧可下载包

来源：R§7、§12 CE-03；AC-15、AC-23、AC-25。前置：真实源含当时尚未登记的凭据文字。顺序分别覆盖：生成已读内容、即将发布ready、ready尚未下载 → 在控制屏障中登记该凭据使protection_version变化 → 释放后续阶段/用旧凭据请求下载。预期：任务失效，不能发布旧包或批准旧下载，后续发送被停止，清理完成前不开放下一项；旧状态迟到不能复活。恢复：显式新任务采用当前保护规则。入口S1/S2/S4；注入阶段屏障而非sleep，不跳过真实Redactor版本变化。

### CE-04 — 正文删了但旁路仍泄露

来源：R§5–6、§12 CE-04；AC-09、AC-11、AC-27。前置：真实输入带可辨认正文/身份/路径/动态错误键，源工件有原文摘要，源时间已知。顺序：生成分享包 → 解压所有成员及读取ZIP元数据 → 读取任务/错误响应。预期：无原文、原文摘要、原路径、动态键原值或源绝对时间旁路；导出字节摘要仍能验证。恢复：净化失败不提供不合规下载，调整实现后重验两档。入口S2/S3/S4；固定测试标记、文件字节与ZIP元数据读回，不能只搜一个JSON字段。

### CE-05 — 不同正文通路不能漏掉净化

来源：R§6、§12 CE-05；AC-09–AC-12。前置：真实工具分别产生内联与超阈值文本工件，正文/参数/错误含已知凭据和敏感键；包含多字节文本。顺序：分享导出完成清理 → 诊断导出 → 检查所有事件、工件、引用、长度和摘要。预期：分享占位完整；诊断各通路均按当前规则处理且UTF-8完整；默认档成功不能代替诊断档证明。恢复：任何失败清理并保留源不变。入口S1/S3/S4；复用真实Tool/ToolSuccess及ScriptedModel生成路径。

### CE-06 — 检查后替换不能逃逸受管读取

来源：R§4–5、§7、§12 CE-06；AC-13、AC-17。前置：真实目标Run和另一Run/目录外文件。顺序：UI定位/服务端初检完成 → 受控暂停实际读取或发布边界 → 替换为链接、另一Run文件或同名新文件 → 恢复。预期：身份不符或源变化整次拒绝，不泄出替代目标字节、不产生混合包。恢复：恢复可信源后必须新任务。入口S2/S4；临时目录、事件屏障与实际文件替换，不靠sleep或仅断言校验函数调用。

### CE-07 — 序号与墙钟各有边界

来源：R§2、§5、§12 CE-07；AC-08。前置：真实发布流程含transient事件造成合法seq间隙，并控制墙钟回拨；另准备不同进程身份的事件。顺序：正常Run导出并验证原seq顺序 → 再对源施加跨process同seq混入 → 新任务读取。预期：合法间隙/回拨不被改排序或单独判缺失；跨进程身份混入拒绝。恢复：同持久Run在重启后仍能定位，但旧任务凭据不能复用。入口S1/S2/S4；FakeClock与真实FanOut发布，受控损坏仅用于负例。

### CE-08 — 停止HTTP等待不等于实际取消

来源：R§7–8、§12 CE-08；AC-17、AC-18、AC-21、AC-25。前置：真实任务持有IO/归档资源，确定性阻塞工作退出或关闭。顺序：发起 → 等资源已取得 → 用户取消/断开HTTP → 保持工作未释放 → 第二标签发起 → 释放阻塞并核验清理 → 再发起。预期：取消后不发布/继续发送，未释放阶段只显示正在清理、第二项busy；真实释放前不报取消完成；不能长期占DB写锁。恢复：清理核验后新任务可接受。入口S1/S2/S3/S4；可注入IO/close屏障，不用僵尸线程的假done代替退出。

### CE-09 — 通知送达不证明遗忘副本清理

来源：R§7–8、§12 CE-09；AC-15、AC-16、AC-25。前置：有受管临时导出，令其清理暂时失败。顺序：经真实Memory命令删除 → 等DB删除提交/修订发布 → 保持导出ZIP或发送资源残留 → 读取遗忘进度/导出状态 → 恢复清理并复核。预期：条目删除与副本清理分开，不能凭通知把对应清理项complete；导出不再可用，新准入受阻；实际清除后才complete。恢复：旧trace仍可按当前规则重新人工导出，不自动再入模。入口S1/S2/S4；既有cleanup port故障注入及实际文件/发送资源读回。

### CE-10 — 下载后失效不能声称撤回字节

来源：R§7–9、§12 CE-10；AC-15、AC-23、AC-25。前置：真实下载已被接受，受控接收端确认首段已交付。顺序：暂停下一发送准入 → 改变memory/protection版本 → 恢复发送路径 → 观察连接、任务和清理。预期：不批准后续发送；已获授权进入socket/浏览器的字节不可撤回，不断言零泄出；响应中不追加JSON错误、不报用户保存成功。恢复：清理后只能新任务，用户残片不删除。入口S2/S3/S4；真实socket或浏览器下载，明确首段确认与下一准入屏障，不能用收到取消事件推断实际停写。

### CE-11 — 压缩率不能绕过未压缩上限

来源：R§8、§12 CE-11；AC-19。前置：受控生成高度可压缩的有效文本/包，使至少一个受限未压缩集合超过512MiB而最终压缩体积较小。顺序：发起并观察计额和终结 → 分别对源、净化未压缩和ZIP三个计额边界执行恰好上限/超1字节控制。预期：任一超限失败，无静默截断/丢成员；恰好边界不因大小本身拒绝，仍需满足其他检查；所有临时资源清理。恢复：清理后用户可选择符合上限的Run显式发起，不能通过截断旧Run自动重试。入口S2/S4；实际编码字节和流式计数，不用伪造文件大小或调低产品阈值代替512MiB公共路径证据。

### CE-12 — 重启不复活任务也不扩大清理范围

来源：R§8、§12 CE-12；AC-23、AC-26。前置：创建真实ready/生成中受管残留，旁边保留合法源bundle、未知目录与模拟用户下载副本。顺序：终止宿主 → 重启同状态根 → 使用旧任务/凭据 → 核对清理对象；再注入残留清理失败分支。预期：旧任务/凭据无效，不自动恢复下载；只清已验证本功能残留；未知目录、源与用户副本不删；清理失败暂停新导出。恢复：受管残留核实清除后才能新任务。入口S1/S2/S4；真实重启与托管权限/所有者核对，不用替换成空状态目录回避恢复。

## Readiness and Open Decisions

- 整体设计：Q1–Q6、Q7–Q13均已确认，完整TRACE-EXPORT-SPEC-r1及持续至任务终结的源租约已获最终「确认」。决策票已CLOSED/COMPLETED。
- 所需测试边界：已确认真实Host/存储/HTTP/浏览器、ScriptedModel及可控时钟/IO/屏障；本规范保留全部AC/CE，给出来源和可观察结果。
- 未决产品问题/验收阻塞：**none**。没有required deferred case，没有用户批准的删除/替换，本轮也未添加新排除项。
- 就绪：**READY / ready-for-agent**，与已发布状态分开判定；保持已有授权标签。代码、具体路由名、模块组织和安全实现方法属于执行者在冻结合同内的实现选择。
- 产品实现：**NOT STARTED**；AC-01–AC-30与CE-01–CE-12全部**NOT RUN**；产品候选未冻结，Standards/Spec两轴均**NOT REVIEWED**，不由规范覆盖检查推出PASS。
- 下一负责人：另行认领本票的独立执行agent，当前产品写入者为none。本规划任务不创建执行会话、不实现、不提交、不推送、不合并。
- 若新事实与冻结合同冲突，保留证据并仅报告受影响决定/验收；不能自行降低完整性、净化或清理承诺，也不能因实现困难删除required case。

## Out of Scope

完整继承 R§13（最终确认所批准）：云端上传/共享、跨用户/远程鉴权、批量导出、运行中快照、孤立目录浏览、恢复/导入、离线HTML查看器、可执行工件、财务导出、任意业务文件下载、通用Graph UI、更多workflow、全自动保留系统及整套发布门禁。两档中没有关闭中央脱敏或逐字段危险开关。以上不豁免所需的有限读取租约、安全路径、下载生命周期与既有回归。

## Further Notes

### 交接、依赖与状态维护

沿用现有地图原生子票，不新建重复票。原生前置为[决定：trace 导出的完整用户路径与验收](https://github.com/nineofoursyrup/Agent-Alfred/issues/69)、[实现：切片④c — 调用工具并看账（Tools 页 + Ops 账本页）](https://github.com/nineofoursyrup/Agent-Alfred/issues/55)、[实现：记忆遗忘与来源隔离](https://github.com/nineofoursyrup/Agent-Alfred/issues/45)、[实现：Database 只读 SQL 控制台](https://github.com/nineofoursyrup/Agent-Alfred/issues/66)，均已完成。状态及依赖在认领时再读回，不从旧记录猜测。

唯一当前状态入口沿用 `/tmp/agent-alfred-trace-decision/session-state.md`，本次发布交接目标 revision=3。维护者=当前规划任务 `01a0b9a7-f5b0-7182-91fd-1603d369f4c5`；产品写入者=none。执行者先读当前段、回报入口路径/读到revision/候选与证据，未移交维护权前不并发改状态单。状态不替代本规范及R；跨主机读不到临时入口时，由维护者传当前段/修订和必要证据，接收副本不是另一可写权威入口。没有状态文件不授权自行重构已冻结产品决定。

本轮只授权规范综合与发布到既有实现票；实施、Git提交/推送/合并及产品关闭按另行明确授权。执行前核对最新main和适用指令，隔离既有改动。

### 版本化伴随材料与代码事实

必读上位R正文hash见开头；其§11/§12是唯一验收定义，本规范的AC映射和CE步骤是执行索引。既有Tools/Ops与遗忘上下文以固定commit链接为准：
- [Tools/Ops 已发布规范][OPS]，D06及对应历史验收；不要把其中不含下载的旧范围误读为禁止本票已确认导出。
- [ADR-0003 中央脱敏][ADR3]、[ADR-0007 遗忘及人工历史补充][ADR7]、[领域词汇表][CTX]。
- 其他R§1列出的ADR均取同一事实基线[源码目录][BASE]，不依赖可漂移的main内容。

本地 `/Users/nineofour/Agent-Alfred` 的main为 `31daa31010dc96639664da1be0767f51a7de83a0`，落后于本规范事实基线；保留用户原有 `.gitignore` 修改。规划已经留下 `CONTEXT.md` 的trace术语局部修改，以及 `docs/adr/0041-trace-export-is-a-sanitized-manual-artifact.md`，未提交；应在最新基线语义移植相关部分，不能整份覆盖新版CONTEXT。ADR编号若被并行工作占用按新基线调整。上述本地文档不是远端执行的隐藏必需规范，R已包含完整产品合同。

[R]: https://github.com/nineofoursyrup/Agent-Alfred/issues/69#issuecomment-5742037842
[BASE]: https://github.com/nineofoursyrup/Agent-Alfred/tree/2533d6818c4b6a896f460b20d5e0fe40142d335e
[CI]: https://github.com/nineofoursyrup/Agent-Alfred/blob/2533d6818c4b6a896f460b20d5e0fe40142d335e/.github/workflows/ci.yml
[OPS]: https://github.com/nineofoursyrup/Agent-Alfred/blob/2533d6818c4b6a896f460b20d5e0fe40142d335e/docs/design/issue-32-published-spec.md
[ADR3]: https://github.com/nineofoursyrup/Agent-Alfred/blob/2533d6818c4b6a896f460b20d5e0fe40142d335e/docs/adr/0003-central-fail-closed-redactor.md
[ADR7]: https://github.com/nineofoursyrup/Agent-Alfred/blob/2533d6818c4b6a896f460b20d5e0fe40142d335e/docs/adr/0007-forgetting-must-be-real.md
[CTX]: https://github.com/nineofoursyrup/Agent-Alfred/blob/2533d6818c4b6a896f460b20d5e0fe40142d335e/CONTEXT.md


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/70#issuecomment-5742148301

## to-spec 发布回执 — TRACE-EXPORT-IMPL-SPEC-r1

本票正文已按 to-spec 模板综合为实现规范，基于[已完整确认的 TRACE-EXPORT-SPEC-r1 resolution](https://github.com/nineofoursyrup/Agent-Alfred/issues/69#issuecomment-5742037842)，没有重新裁决、删减或延期原要求。

- 施工规范正文 SHA-256：`b349246354f0f38e8b591779bb0f1000a904102a0e74f656a09a1fef27a9ea5d`。
- 上位决策正文 SHA-256：`abad4a244015cd0d16bb5b6c4abbe3fc228ad919005e4f8086b8083a4035ae01`。
- AC-01–AC-30、CE-01–CE-12 全保留；AC逐条来源映射，CE补齐前置、顺序、预期、恢复、公共入口和确定性控制。唯一权威验收定义仍在上位resolution§11/§12。
- 远端正文逐字与hash读回通过，标签保留 `wayfinder:task` + `ready-for-agent`；当前OPEN、无人认领、无未完成原生前置。
- 就绪=READY；未决产品问题/验收阻塞=none。产品实现NOT STARTED，全部产品AC/CE为NOT RUN，产品双轴NOT REVIEWED。
- 唯一当前状态入口：`/tmp/agent-alfred-trace-decision/session-state.md`，本次交接revision=3；维护者为规划任务 `01a0b9a7-f5b0-7182-91fd-1603d369f4c5`。后续独立执行agent先读该入口与本票/R，不并发改维护者状态；跨主机无法读临时路径时须传当前段/修订，规范与验收本身均可从GitHub完整读取。
- 下一负责人=另行认领的独立执行agent；本次没有启动实现、提交、推送或合并。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/70#issuecomment-5744558623

<!-- issue70-acceptance:trace-export-c9-3c657757c021 -->
# #70 实现验收

全 Run 分享净化/诊断正文 ZIP 导出已完成：运行详情唯一入口，真实来源完整性检查、当前保护规则、版本失效、一次性原生下载与可重试清理。关联 PR #77。

规范：TRACE-EXPORT-IMPL-SPEC-r1；验收唯一定义为 [#69 resolution](https://github.com/nineofoursyrup/Agent-Alfred/issues/69#issuecomment-5742037842) 的 AC-01–30 / CE-01–12。没有新增范围排除或未决产品决定。

## 候选与独立评审

- 候选 `trace-export-c9-3c657757c021`，完整 diff SHA256 `3c657757c02171fddce2b5269548bd527d96c81e59f22072302fbd35ae8e06bb`。
- 基线 `3cd9e80d35b0030b0e4591b1d4ee53588aacdb9a`；提交 `fad0872fbc57db765692b4cda6abdd2f2e98222c`，31 个本票文件及45个整合范围文件逐字节及 mode 匹配冻结候选，完整提交树也一致；未混入临时证据和历史 PNG。
- Standards PASS：独立子代理 `/root/c7_standards`（fork_turns=none）；Spec PASS：独立子代理 `/root/c7_spec`（fork_turns=none）。0 未决 blocking / optional；STD-01–04、SPEC-01–07、EXEC-01、CI-01/CI-02/CI-03 全部独立 resolved。
- 整合后本地全套 Python 4842 passed / 1 deselected；browser 268 passed；六文件专项 108 passed；退休回归 9 passed，旧候选相同触发两项 red。构建、ruff、typecheck、skills、env、wheel/sdist × base/mcp 四组合通过。
- 公共路径使用真实 Host / SQLite / HTTP / browser / native download / disk。外部模型使用 ScriptedModel，允许 Clock / IO 故障 / Event 屏障；具体证据和重现入口见 [实施说明](https://github.com/nineofoursyrup/Agent-Alfred/blob/fad0872fbc57db765692b4cda6abdd2f2e98222c/docs/implementation/issue-70-trace-export.md)。

首轮 Linux CI run35455227326 的浏览器247通过/2失败保留为真实失败：聚合丢回执提示与真实保存投影竞速；导出取消时把首个pending响应误作已释放。通过现有模型/记录/读取屏障确定性复现旧断言2失败，再修正两份测试的阶段控制并加强最终状态/持久身份/后继下载断言；产品代码、fixture、配置不变。c7 完整browser249通过，Standards隔离重跑两例通过，Spec独立108通过，无sleep/超时放宽/伪造成功或验收降级。

第二轮 Linux run35457145976 中前两项修复通过，另有一项首次来源读取早于 MemorySync 核验的测试假设失败（browser248通过/1失败）。c8 使用真实 state 响应屏障，验证未核验时零正文请求、核验后显式读取同 ID/version/revision 的200正文，保留断网/重连/真实删除/迟到响应保护。原断言在两种核验时序下确定性red，同族6例green、Standards独立6例green、c8全browser249通过。CI-03已独立resolved，产品代码不变。

c8 build与wheel/sdist×base/mcp四组合重新通过；两轴独立校验wheel25范围源文件及sdist全部31范围文件与冻结候选相同。

c9 已整合上游 PR #78 / #75，完整代码树 `b1ac2117ed9c4f2581a0b6c94cef7418b8fe8de8`。新基线31文件、旧基线45文件和570条index逐项绑定；从两基线均能重建同一整仓tree。双轴重新检查合并后的全部合同与冲突接缝，全量Python/browser及构建安装矩阵重新运行。c8旧基线CI35459147835取消为superseded，不是新候选的成功证据。

## 两阶段远端 CI

- PR-head [CI35460866385](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/35460866385)：`fad0872fbc57db765692b4cda6abdd2f2e98222c`，pull_request，SUCCESS。Linux Python4842passed/1deselected/700.32s，browser268passed/7.7m；其余必需步骤全部通过。
- 实际合并：PR77于2026-09-19T18:41:50Z MERGED；merge SHA `47cffe452f92c20a19e6e46494c647de4d0113f1`，完整tree与已审c9一致。
- 实际 merge-SHA [CI35462021899](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/35462021899)：`47cffe452f92c20a19e6e46494c647de4d0113f1`，push，SUCCESS。Linux Python4842passed/1deselected/706.36s，browser268passed/7.8m；其余必需步骤全部通过。远端main已读回包含该合并提交。

## 验收矩阵



绑定 c9 候选；执行入口来自已提交实施说明，逐CE结论来自独立Spec报告。AC01–30 / CE01–12 全部通过，无排除/待定。

| AC | 已验证行为与入口 |
|---|---|
| AC-01 | `runs.js` 的持久 Run 详情装配；既有 runs/accounting/routing 浏览器用例与 browser 真实会话→Run。用途选择不改变导出接口 |
| AC-02 | browser 多 Step/Attempt、内联与外置工具两档原生 ZIP，离线解压逐文件核对 |
| AC-03 | faults `real_pending_then_failed_recording` 使用真实 finalizer 屏障和 SQLite 失败；core 完成运行，既有 Runtime 状态回归 |
| AC-04 | core `missing_referenced_artifact`、`missing_telemetry`、source matrix；http 跨重启完整性 |
| AC-05 | core source matrix：空 trace、唯一尾行、内部坏行 |
| AC-06 | faults `real_bundle_refusals`；core 缺关联工件；服务查询 trace_prunes |
| AC-07 | core source matrix 旧形状/未知版本，faults 伪造 meta/重复身份 |
| AC-08 | http 真实子进程 SIGKILL 重启；repairs 真实 FanOut transient 序号间隙、FakeClock 回拨；core process 负例 |
| AC-09 | core 分享字节隐私与 browser ZIP 元数据；未知字段由 schema 白名单移除 |
| AC-10 | core 诊断保护、多字节、敏感键超长空白/JSON 转义；browser 真实工具正文 |
| AC-11 | core 字节与摘要检查、browser 原 Run/状态目录/凭据不在包内 |
| AC-12 | core 大诊断字符串与工件摘要；browser 大于 256 KiB 中文工件完整 |
| AC-13 | core 下载前替换为链接；faults 跨路径工件引用、硬链接、伪造/重复 meta |
| AC-14 | faults 未知成员拒绝；Source 成员检查，archive 仅复制验证过的关联工件 |
| AC-15 | core 生成中漏通知版本核对、ready 失效；http 真实发送 64 字节后改变保护，余字节为零 |
| AC-16 | http `real_forgetting`：实际 Memory 删除提交、ZIP unlink 失败阻止 complete，恢复后读回同一 forget_cleanup 项 complete，并可重新导出历史 |
| AC-17 | core `read_lease_blocks_deleting` 与源替换；源读取在 DB 锁外，socket 可写等待在版本边界外 |
| AC-18 | browser 双标签 busy/离页清理；core 取消和清理失败保槽位 |
| AC-19 | limits 真实 HTTP 创建/终态读取加实际 512 MiB、+1 磁盘；源、ZIP、未压缩上限分别计算；另验证高度可压缩输入 |
| AC-20 | core 单调 Clock 验证 60/120/300 秒，生成 IO 未退出前仍 cleaning |
| AC-21 | faults 磁盘不足/写失败/短读/关闭失败；core cancel、unlink 失败后真实恢复 |
| AC-22 | browser 原生 download 事件、下载路径和离线 ZIP 内容；页面文案不声称已保存 |
| AC-23 | http Origin/CSRF/凭据重用、真实已开始响应失效无第二份 JSON；core 一次性及过期 |
| AC-24 | browser ready 离页取消、迟到 create、已接受原生下载离页继续、重连/重启及缓存页面恢复；generation/实例/revision fencing |
| AC-25 | core 各终结状态检查 released；faults IO/close/Host 退出；服务不持有用户下载路径 |
| AC-26 | http 子进程崩溃重启；faults 部分清理重试和权限核对 |
| AC-27 | http 原生表单 Origin/CSRF；handler 既有 guard；现有全部 HTTP guard 回归 |
| AC-28 | browser 脚本文本、ZIP路径/固定时间检查；ZIP只有 UTF-8 数据，无执行器 |
| AC-29 | faults Host 关闭超时保留数据库；现有 Runtime/Memory/Database/HTTP 完整回归 |
| AC-30 | 本文、固定规范来源及候选绑定的检查记录；不扩大功能完成范围 |

| CE | 独立验证的公共行为与证据边界 |
|---|---|
| **CE-01** | core真实工具产生外置文本后移除实际引用文件；browser `SPEC03 CE01` 真实HTTP生成/原生下载，页内known_incomplete、artifact-1_missing与README/manifest一致，无伪造文件，逐导出字节校验。S1–S4。 |
| **CE-02** | faults真实finalizer Event暂停，pending start拒绝；SQLite trigger导致实际提交失败后保留failed/known_incomplete。http/browser同持久根重启并缺telemetry，输出unknown，run.finished不补recorded。使用允许提交故障与历史缺证替身，不造成功API。S1–S4。 |
| **CE-03** | core ready新凭据/漏通知重验；repairs实际trace pread之后及最终ZIP fsync屏障；本轴barrier_probe确认旧ZIP确含待登记值，然后Redactor.remember导致invalidated/released/no token。重新显式生成可用，不用sleep猜窗口。S1/S4，配合http凭据拒绝与发送例。 |
| **CE-04** | faults动态键/原摘要/无关联工件；core及browser读取所有成员、manifest hash、固定1980 ZIP元数据与随机名称，原正文/RunID/状态根/原摘要不旁路。错误文本固定闭合码，未知字段不回显键名。S1–S4。 |
| **CE-05** | 真实ToolRegistry/ToolSuccess内联、>256KiB工件与大StringSpan，两档全成员净化；跨块unicode敏感键/大空白和中文UTF8保留，重算工件bytes/SHA与引用。browser多Step/多Attempt两档原生ZIP。S1/S3/S4。 |
| **CE-06** | faults绝对/越界/跨seq引用、硬链接、伪meta拒绝；core ready后换链接；browser实际pread后同名换inode；publication最终ZIP fsync后换源，经HTTP观察source_changed/unsafe_source、released、无下载token；本轴probe复核。S1–S4。不承诺抵御任意同账号恶意进程。 |
| **CE-07** | repairs真实工具progress→FanOut transient产生自然seq间隙，FakeClock墙钟回拨；导出seq/历史ts逐项与源相同，源字节不变。core保持seq但改process即拒绝；http同Run重启定位。S1/S2/S4。不是仅用手改seq冒充发布证据。 |
| **CE-08** | core活动pread/生成60秒预算下cancel保持cleaning；browser真实取消、第二标签busy，释放Event后取消完成和后继ready。c6退休九项区分仍活动和已退出资源，未提前放槽。S1–S4。同步IO不承诺瞬时硬中断。 |
| **CE-09** | http真实Memory删除提交，archive.zip unlink失败时相同operation_id/trace-export:managed持久cleanup行非complete；恢复后retry_cleanup读回同项complete，历史trace可重新导出，Database原通知仍有效。S1/S2/S4；未接模型/检索/提炼。 |
| **CE-10** | http实际socket先交付64字节PK前缀，再在锁外select屏障新增保护，后续body为空，IncompleteRead终止，没有第二份JSON，最终invalidated/released。browser已接受原生下载跨离页仍可继续；文案不声称撤回或保存成功。S2/S3/S4。已发字节不计作可撤回。 |
| **CE-11** | limits真实Dashboard/guarded HTTP create/status/cancel、真实磁盘512MiB与+1：source/output/ZIP独立计额，非改阈值/伪stat。高压缩输入output_limit；output恰好边界可进入ZIP检查但ZIP overhead另拒绝。S2/S4。大包不为证明已读回的尺寸而重复整包网络搬运。 |
| **CE-12** | http真实子进程SIGKILL、同根重启，旧instance凭据409，仅精确owner目录回收；源、unknown目录、模拟用户另存ZIP字节不变。faults部分删除保留所有权、暂停新任务并可重试。S1/S2/S4。证明ready崩溃及部分清理窗口，不宣称穷举所有指令点。 |

## 边界与限制

`requires_key` 按项目配置默认排除，NOT RUN；规范明确不新增付费模型必测项。已发字节/用户另存副本不可撤回；文本净化不承诺任意 PII 匿名化或任意同账号恶意进程防护；不声称穷举所有崩溃指令点。

本票不实现自动 retention、财务快照导出、云分享、导入、通用 Graph UI 或路由统计 #76。Behaviour #75 为已交付上游，c9 保留其合同；本票不表示部署或 PyPI 发布。主工作区既有改动保留。

验收结论：PASS。全部 AC01–30 / CE01–12、同候选独立双轴、PR-head 与实际 merge-SHA CI 均已完成，本票满足 completed 关闭条件。评审结论对应 c9，没有将相邻 #76 的交付计入本票。
