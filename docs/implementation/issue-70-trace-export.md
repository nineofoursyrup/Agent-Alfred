# Trace 导出实现与验证入口

规范来源：[实现票 #70](https://github.com/nineofoursyrup/Agent-Alfred/issues/70)，`TRACE-EXPORT-IMPL-SPEC-r1`；唯一产品验收定义为 [#69 resolution](https://github.com/nineofoursyrup/Agent-Alfred/issues/69#issuecomment-5742037842) `TRACE-EXPORT-SPEC-r1` 的 §11、§12。本文提供实现与可重复检查入口，不修改验收要求。领域决定见 ADR-0041。

运行详情的「追踪导出」提供默认分享净化与显式诊断正文两档。任务先生成完整 ZIP，再提供一次性原生 POST 下载；创建、ready、服务端传输结束均不表示浏览器已保存文件。页面只持任务状态和短期凭据，不读取或缓存 ZIP 正文。

## 接口与所有权

- `POST /api/trace-exports`：`run_id`、`mode`、`instance_id`，接受返回 202。全 Host 一个槽位，无队列。
- `GET /api/trace-exports/<task_id>`：状态、固定原因、真实清理状态，ready 才给下载凭据。
- `POST /api/trace-exports/<task_id>/cancel`：幂等取消；活跃生成或发送退出前保持 cleaning。
- `POST /api/trace-exports/download`：`task_id`、`download_token`、`instance_id`；JSON 使用既有 CSRF header；原生表单另含 `csrf`，同时校验 Host、Origin、请求 framing 和同一 CSRF 决策。不提供 GET 分享链接、Range 或续传。

`RuntimeHost.attach_trace_exports` 组装服务和 Memory 清理参与者。Dashboard 借用既有状态根租约，独立 Host 可申请根租约；导出服务先于底层文件、FanOut 和数据库释放。单任务持有源 fd、读预约、输出 fd、后台线程及临时目录，直到真实清理确认。`BundleLeases.deleting(run_id)` 与导出读引用共享同一预约锁；这里只提供协调原语，不实现自动 retention。

任务绑定进程实例、记忆 revision、当前 Redactor 保护版本及净化/schema 版本。读取生成、ready 发布、发送前均重验；发送使用非阻塞 socket，等待可写发生在数据库与保护锁外。失效通知以外仍查询权威版本。已发字节和用户另存副本不能撤回。

临时资源在 `state/trace-exports/export-<随机标识>/`，目录 0700、文件 0600。重启只清理由固定名称、固定 owner marker、受管 fd 与成员集合确认的本功能目录；部分删除/句柄关闭失败仍保留可重试所有权。未知目录和源 bundle 不删。

## 文件与边界

ZIP 使用 STORED（无压缩）成员，固定包含 `manifest.json`、`README.txt`、`bundle/meta.json`、`bundle/trace.jsonl`、`bundle/artifacts/`。原工件改用包内编号。清单和引用的 bytes/SHA-256 只描述实际导出字节；manifest 不递归校验自身。归档时间固定为 1980-01-01，文件无执行权限、链接或可越界路径。

`Source` 用受管描述符读取完整 bundle，核对持久 Run、存储摘要、meta、process、seq、目录成员与文件身份；支持明确的无版本旧格式和 `tool-<seq>.txt`。生成前后与下载前复查源变化。尾部唯一残片不输出，缺失关联工件列缺失；其他坏记录或不安全来源使整项失败。原 bundle 不修改。

两档使用同一身份别名和当前保护规则。分享档对自由正文及工件用占位；诊断档保留允许文本并再次处理凭据、敏感字段和已知目录。schema 1 以事件字段白名单导出，未知字段不透传。文本不是普遍 PII 匿名化；包为惰性 UTF-8 数据，无离线 HTML 执行器，也不能恢复原始运行。

源总量、导出未压缩总量及最终 ZIP 各限 512 MiB；等于允许，超 1 字节拒绝，不静默截断。生成 60 秒、发送 120 秒、ready 5 分钟，均用单调时钟。输出/工件分块写入；大 JSON 字符串由 fd span 读取，避免按页面 256 KiB 分段丢弃内容。读失败、磁盘不足和清理受阻均不开放下一槽位。

这些是导出边界，不表示源写侧阀门、自动保留、账单导出或 v1 其他门禁已经实现。删除前 trace 经新任务重验仍可人工导出；导出没有模型、检索或提炼回流入口。

## 验证索引

以下缩写为仓库相对路径，命令在仓库根运行：

- **core**：`src/agent_alfred/evals/deterministic/test_trace_export.py`
- **faults**：`src/agent_alfred/evals/deterministic/test_trace_export_failures.py`
- **http**：`src/agent_alfred/evals/deterministic/test_trace_export_http.py`
- **repairs**：`src/agent_alfred/evals/deterministic/test_trace_export_repairs.py`（独立评审反例与新增时序屏障）
- **publication**：`src/agent_alfred/evals/deterministic/test_trace_export_publication.py`（租约/线程组装中断、事件版本、ZIP落盘后换源、已知结果与耗时）
- **limits**：`src/agent_alfred/evals/deterministic/test_trace_export_limits.py`
- **browser**：`tests/browser/trace_export.spec.js`，真实 Dashboard 的外部模型使用 ScriptedModelFactory；故障通过现有 Clock、IO 和明确 Event 屏障注入。

| AC | 检查入口与观测 |
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

| CE | 对应可重复检查 |
|---|---|
| CE-01 | core `test_ce01_missing_referenced_artifact_does_not_become_complete` |
| CE-02 | core 缺 telemetry；faults `test_ac03_ce02_real_pending_then_failed_recording` |
| CE-03 | core `test_ce03_generation_rechecks_authoritative_version_without_notification` 与 ready 失效 |
| CE-04 | core 分享包隐私；browser metadata/原身份检查；白名单净化 |
| CE-05 | core 诊断内联/外置保护及跨块敏感键；browser 两档工具内容 |
| CE-06 | core 源替换链接；faults 工件路径拒绝矩阵 |
| CE-07 | core source matrix、seq gaps/clock rollback |
| CE-08 | core `test_ce08_cancel_does_not_release_active_file_reader` |
| CE-09 | core 清理失败保槽；http `test_ce09_real_forgetting_does_not_complete_while_zip_remains` |
| CE-10 | http `test_ce10_real_http_partial_body_stops_after_protection_change` |
| CE-11 | limits 实际边界两项 |
| CE-12 | http 真实 SIGKILL/重启；faults partial cleanup 恢复 |

执行 `uv run pytest -q` 加上述六个 Python 文件、`npm run test:browser -- tests/browser/trace_export.spec.js` 可重做导出验收。完整门禁沿用 `.github/workflows/ci.yml`、`npm run typecheck` 和 `npm run test:browser`。具体 PASS/FAIL、候选哈希、尚未完成的检查与独立双轴结论记录在实施交付的候选证据中；本索引本身不表示验收通过。

## 独立评审反例的回归

`repairs` 中 STD-01 在构造返回与 Host 接管的精确指令边界注入中断，检查 Host 关闭后新 fd 全部释放；STD-02 控制旧 worker 的第二次清理晚于同 Run 新任务 ready，确认删除预约仍被新任务阻止。SPEC-01 对短内联、长内联及工件的全部导出表示执行同一文本规则；SPEC-02 对同一事件 envelope/payload 身份冲突整次拒绝。

SPEC-03 的公共路径补充集中在 browser 的 SPEC03 用例和 repairs：内容已读/发布前保护变更、真实记录 pending/failed 和重启后 unknown、缺件警示、活动 IO 取消与第二标签 busy、生成中源替换、原生下载跨离页继续、断线/新实例/BFCache 恢复。Memory 清理恢复读回持久 complete，实际 512 MiB 矩阵经 HTTP 发起和读取终态，重启场景保留模拟用户另存 ZIP 并核对字节不变。具体候选是否获独立评审接受仍以绑定该候选的评审记录为准。

STD-03：任务先登记所有者，读取占用按 task_id 归属，取得租约/构造线程/真实启动返回边缘被中断后可清理且不消耗后继占用。SPEC-04：Gate 与 Graph 的显式事件 schema 仅支持版本 1，未知版本整项拒绝。SPEC-05/07：实际 archive.zip fd 同步完成后、ready 发布前再次确认源身份；回归同时确认 ZIP 已可完整解压，再注入同名源替换或新增保护值。

SPEC-06：按已知事件分别保留闭合结果枚举，并保留合法有限非负整数/浮点耗时及 Gate 的固定 timing 子字段。publication 通过真实 Gate skip/hit/miss/error、真实工具与 Graph 调用和模型停止原因比对源与两档离线输出；数值域负例仍不透传无效值。

STD-04：下载准入、凭据消费和发送活动接管均位于异常清理保护范围内；每次调用有独立发送所有者，只有实际取得发送权的调用可退休活动。publication 验证发送时钟前、源复核前及实际写入后被中断均失败并真实清理，不误报 transferred；并发错误凭据/已消费凭据请求不能退休仍在真实发送的调用。

发送退休也可重试：实际 IO 在调用线程的同步生成器帧内执行，读取、写入与可写等待期间不 yield；唯一 yield 表示所有字节与最终版本检查已完成。清理先确认帧不再运行并关闭帧，再释放 fd、目录和 reader；即使调用者 finally 或帧 close 返回边缘被中断，公开取消/关闭仍可重新观察并完成清理。publication 的退休反例核验用户 ZIP 完整、真实清理及后继导出，close 两侧中断仍保留占用到重试成功。

相邻生成退出反例：生成线程最后清除活动位时中断后，使用已有原生线程退出确认机制回收已退出 worker；仍活跃的生成 IO 保留资源。publication 通过真实未知版本失败和线程退出屏障确认，无手动清位参与成功断言。
