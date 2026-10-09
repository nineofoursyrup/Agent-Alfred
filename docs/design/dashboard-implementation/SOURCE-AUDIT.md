# 固定来源与源码核对

本记录区分已确认设计、当前实现事实和本轮拟定接口。工作树：`/Users/nineofour/.codex/worktrees/issue-92-migration-contract/Agent-Alfred`；起始 HEAD 为 `9716e0c395c603ccc262284de577cfb8df3f8d48`，工作树起始干净。本轮只在本目录新增规格包。

## 固定权威输入

先读取 [HANDOFF-TO-SPEC](../issue-92/HANDOFF-TO-SPEC.md)，随后核对 [CLAUDE.md](../../../CLAUDE.md)、[issue tracker 规则](../../agents/issue-tracker.md)、[领域规则](../../agents/domain.md)、[CONTEXT](../../../CONTEXT.md) 与相关 ADR。源规格全文已按交接顺序展开；没有用聚合验收表代替全文。

| 设计 | 固定提交 | 本轮采用的主要合同 |
| --- | --- | --- |
| #86 视觉 | `541befcbe4f771d6f0d7ce3dfc820357c91c7cbc` | [DECISIONS](../issue-86/DECISIONS.md)、[tokens](../issue-86/tokens.json)、[baseline/r1](../issue-86/baseline/r1/README.md) 与原始资产 |
| #87 壳层 | `751d5b84e1b7a633019920a805ab647c1b15a419` | [SPEC](../issue-87/SPEC.md)，R01–R10、P01–P15、CE-01–CE-24 |
| #88 Overview | `0e9915b5f020a756776f3e67fe13f352da3bb983` | [DESIGN](../issue-88/DESIGN.md)，来源／期间／覆盖／库存／当前槽／失效／跳转 |
| #89 收件箱／运行 | `0120f0aff56b7c8b29854c5d9be5fe475b8b06b7` | [DESIGN](../issue-89/DESIGN.md)，D01–D16、P01–P16、CE-01–CE-26、M01–M22、I01–I06 |
| #90 Memory／Behaviour／Tools | `7d175d6a65a93aec725cf86e6be3df167d3ca74c` | [DESIGN](../issue-90/DESIGN.md)、[ACCEPTANCE](../issue-90/ACCEPTANCE.md) |
| #91 Models／Connections／Ops／Database | `1a343ae1894554ad0167273a34b1f6bce171fb5b` | [DESIGN](../issue-91/DESIGN.md)、[ACCEPTANCE](../issue-91/ACCEPTANCE.md) |
| #92 整体迁移 | `9716e0c395c603ccc262284de577cfb8df3f8d48` | [SPEC](../issue-92/SPEC.md)、[SLICES](../issue-92/SLICES.md)、[ACCEPTANCE](../issue-92/ACCEPTANCE.md)、[VALIDATION-PLAN](../issue-92/VALIDATION-PLAN.md)、[归档核对](../issue-92/ARCHIVE-VALIDATION.md) |

检查方法：#92 [source-index.json](../issue-92/source-index.json) 中 31 个源文件的 bytes／SHA-256，分别对上游固定 commit、总归档 commit 和工作树；再对 #92 [manifest](../issue-92/manifest.json) 所列 37 文件核对归档及工作树。309 项原文沿原格式重算 hash：规范段取行区间并保留末尾换行，单行表格项不追加换行；生成的反向切片索引不能增删或重分配原 owners。固定输入继续只读，后续实现结果另建候选记录。

GitHub 使用 `gh api` 只读回读，观察时间 **2026-10-08T18:05:13.589821+00:00**（上海 2026-10-09 02:05）。[#85](https://github.com/nineofoursyrup/Agent-Alfred/issues/85) 的 #86–#92 七个子票均 `closed / completed`；[#92](https://github.com/nineofoursyrup/Agent-Alfred/issues/92) 的六个上游依赖均已关闭，开放 blocker 为 0；七个 resolution 均包含上述实际固定 SHA。原始精简回读见 [remote-readback.json](remote-readback.json)，含 resolution URL、更新时间及正文 hash。这是当时的远端设计状态，不是产品实施／验收状态；本轮没有 GitHub 写操作。

产品源码、测试、脚本、依赖和 CI 与产品基线 `22c8720e1ec874fd012cc79b086cd8aace4c79b6` 一致。归档在产品基线上增加设计和领域资料，没有已迁移产品可供本轮宣称通过。

## 本轮确认的源码差距与接缝

以下路径是**导航线索**，实现合同在 [INTERFACES](INTERFACES.md)。不要求执行者沿用私有文件拆分。

| 当前实现事实 | 实施含义／责任 | 源码入口 |
| --- | --- | --- |
| AccountingSnapshots 完整读取成员、工具计量、telemetry／裁剪并冻结价格；create 保留 snapshot，默认 900 秒、8 个、64 MiB；返回分页，现有服务无逐个主动释放接口 | I01 提取请求内摘要，不能简单把 create 当无资源 GET。模型与工具覆盖需区分，不能把现有 incomplete_runs 改名 | [accounting](../../../src/agent_alfred/runtime/accounting.py)、[accounting API](../../../src/agent_alfred/gateway/web/accounting_api.py)、[Host](../../../src/agent_alfred/runtime/host.py) |
| 期间归属为 started_at 或 accepted_at，custom 日期 end 已为排他；summary 有 exact／estimated／unknown 及混合覆盖，未直接提供模型完整性证明 | I01 增量返回固定当地日期和模型覆盖分类，沿同一完整集合及价格解释 | [normalize_filters／summary](../../../src/agent_alfred/runtime/accounting.py) |
| MemoryQueryService 注入 reading_stores；Store 有分页／搜索与 revision，尚无完整 count | I02 可选 count 能力，同一读上下文＋revision；不能拉所有正文或只看第一页 | [queries](../../../src/agent_alfred/memory/queries.py)、[storage](../../../src/agent_alfred/memory/storage.py)、[semantic](../../../src/agent_alfred/memory/semantic/__init__.py)、[episodic](../../../src/agent_alfred/memory/episodic/__init__.py)、[Memory API](../../../src/agent_alfred/gateway/web/memory_api.py) |
| Run list SQL 的 chat 组含 aggregation，但 classify_purpose 只把 chat 判为会话；列表序列化已有 preview，却无 admitted-only 披露门／recording 字段 | I03 修公共分类与安全投影，保持存储用途和原自动窗口规则；旧消费者回归 | [runs](../../../src/agent_alfred/runtime/runs.py)、[API](../../../src/agent_alfred/gateway/web/api.py) |
| 通用 evidence 从 telemetry 推记录状态，同时读 trace；坏 telemetry 不能作为新元数据的可靠成功判据 | I03 抽取无正文可信判据并供 evidence 复用；不逐行读 trace 或完整答复 | [evidence](../../../src/agent_alfred/runtime/evidence.py)、[recording](../../../src/agent_alfred/runtime/recording.py)、[telemetry](../../../src/agent_alfred/runtime/telemetry.py)、[snapshot](../../../src/agent_alfred/runtime/snapshot.py) |
| Run locate 已有有界邻项＋目标＋cursor，但自动按目标分组；Session／预览定位及历史消息稳定锚点缺失 | I04 扩展来源 filter 和 Session／消息／会话 Run 定位，保留原等待分段；不按页数遍历 | [runs locate](../../../src/agent_alfred/runtime/runs.py)、[sessions](../../../src/agent_alfred/runtime/sessions.py)、[HTTP handler](../../../src/agent_alfred/gateway/web/handler.py) |
| recover_reply 严格身份、先投影后存储、完整脱敏正文及 no_reply／withheld；不提供历史位置和用户记录 | I05 复用恢复能力，补一条定位 DTO；不把 reply HTTP 当定位已经完成或记录回执 | [replies](../../../src/agent_alfred/runtime/replies.py)、[MainBar 普通 DTO](../../../src/agent_alfred/gateway/web/api.py) |
| 路由替换直接关闭当前页；运行与记忆页也有自己的 history 操作；不同页面构造／close 接口不统一 | I06 统一路由协调与生命周期适配，同时保留各页的真实资源责任 | [app](../../../src/agent_alfred/ops/static/app.js)、[runs](../../../src/agent_alfred/ops/static/runs.js)、[memory](../../../src/agent_alfred/ops/static/memory.js)、[pages](../../../src/agent_alfred/ops/static/pages.js) |
| Stream 已有单一 EventSource、generation 和 frame 队列，MainBar 有 Run pair 去重、historyCursor 与回复恢复 | I07 延续原所有者，仅增独立定位状态；不要重写 wire protocol | [stream](../../../src/agent_alfred/ops/static/stream.js)、[app](../../../src/agent_alfred/ops/static/app.js) |
| pin 用 None 表示保留旧显示名／价格；set_display_name(None) 与移除最后一维覆盖均委托该逻辑，导致“清空”保留旧值 | S08 独立修复未指定／明确清空，真实持久重读验收，修复不得随 UI 回撤丢失 | [settings_commands](../../../src/agent_alfred/settings_commands.py)、[model_settings](../../../src/agent_alfred/runtime/model_settings.py)、[Host apply_settings](../../../src/agent_alfred/runtime/host.py) |
| Ops detail 顶层 summary 是全局集合，逐 Run 只有自身 attempts／tools；财务快照与历史正文保护不同 | S09 不把全局金额绑定单 Run，也不借 I03 推造 Ops 字段 | [accounting service](../../../src/agent_alfred/runtime/accounting.py)、[accounting UI](../../../src/agent_alfred/ops/static/accounting.js) |
| Database SQL／结果、句柄和 worker 有原有限制／取消／cleanup；页面 close 与 suspend 执行保护清理 | S10 沿原生命周期；句柄终态保留时间不是结果统一 TTL，cleanup 请求不是完成 | [database](../../../src/agent_alfred/database.py)、[database API](../../../src/agent_alfred/gateway/web/database_api.py)、[database UI](../../../src/agent_alfred/ops/static/database.js) |
| 包内服务登记九路由与资源，当前未登记 Overview；安装脚本校对全部源码文件，但显式 HTTP 资源检查有限 | S04 注册真实 Overview，S11 默认入口；各片收集引用闭包，安装后新增资源可达性补证 | [assets](../../../src/agent_alfred/gateway/web/assets.py)、[安装检查](../../../scripts/check_mcp_installations.py)、[CI](../../../.github/workflows/ci.yml)、[package](../../../package.json) |

这些差距与已确认 #88／#89 读取缺口及 #91 清空修复责任一致，没有发现必须重新选择产品范围、数据边界或验收环境的实质冲突。新增 endpoint 命名／DTO 与页面逻辑端口是实施细化；不是将当前源码误写成已符合设计。

## 现有测试导航

| 主题 | 可复用入口 |
| --- | --- |
| 会话／定位／正文 | [test_sessions](../../../src/agent_alfred/evals/deterministic/test_sessions.py)、[test_session_inbox_pin](../../../src/agent_alfred/evals/deterministic/test_session_inbox_pin.py)、[test_reply_recovery](../../../src/agent_alfred/evals/deterministic/test_reply_recovery.py) |
| 账目／计数 | [test_ops_snapshots](../../../src/agent_alfred/evals/deterministic/test_ops_snapshots.py)、[test_memory_queries](../../../src/agent_alfred/evals/deterministic/test_memory_queries.py)、[test_memory_read_revision](../../../src/agent_alfred/evals/deterministic/test_memory_read_revision.py) |
| 设置／探针 | [test_settings_commands](../../../src/agent_alfred/evals/deterministic/test_settings_commands.py)、[test_model_settings_store](../../../src/agent_alfred/evals/deterministic/test_model_settings_store.py)、[test_inference_probe_target](../../../src/agent_alfred/evals/deterministic/test_inference_probe_target.py) |
| MainBar／壳层 | [mainbar](../../../tests/browser/mainbar.spec.js)、[inbox](../../../tests/browser/inbox.spec.js)、[pagination](../../../tests/browser/pagination.spec.js)、[tabs](../../../tests/browser/tabs.spec.js)、[recording](../../../tests/browser/recording.spec.js)、[streaming](../../../tests/browser/streaming.spec.js)、[restart](../../../tests/browser/restart.spec.js)、[accessibility](../../../tests/browser/accessibility.spec.js) |
| Run／Graph／导出 | [runs](../../../tests/browser/runs.spec.js)、[run-path](../../../tests/browser/run-path.spec.js)、[trace_export](../../../tests/browser/trace_export.spec.js) |
| Memory／Behaviour／Tools | [memory](../../../tests/browser/memory.spec.js)、[topology](../../../tests/browser/topology.spec.js)、[routing-statistics](../../../tests/browser/routing-statistics.spec.js)、[aggregation](../../../tests/browser/aggregation.spec.js)、[tools](../../../tests/browser/tools.spec.js)、[skills](../../../tests/browser/skills.spec.js) |
| Models／Connections／Ops | [settings](../../../tests/browser/settings.spec.js)、[integrations](../../../tests/browser/integrations.spec.js)、[mcp](../../../tests/browser/mcp.spec.js)、[accounting](../../../tests/browser/accounting.spec.js) |
| Database | [database](../../../tests/browser/database.spec.js)、[cancel](../../../tests/browser/database_cancel.spec.js)、[status](../../../tests/browser/database_status.spec.js)、[lifecycle](../../../tests/browser/database_lifecycle.spec.js)、[native lifecycle](../../../tests/browser/database_native_lifecycle.mjs) |

这些测试的存在不等于覆盖新接口或已经执行；需按 [VALIDATION](VALIDATION.md) 的真实行为扩展。新增功能代码尚不存在，本轮不跑产品、browser、原生交互、构建或升级回退来制造无关绿灯。

## 规格包与状态

- [traceability.json](traceability.json) 是原 owners／AC／MCE 的反向索引，外加 S11 对全部 309 源项与 G01–G08 的整合责任。它不修改原始 source ID、原文 hash、来源结果或依赖边。
- [verify_spec.py](verify_spec.py) 和 [STATIC-CHECKS.json](STATIC-CHECKS.json) 提供可复算的文档检查；[首次检查器失败](STATIC-CHECK-HISTORY.json) 保留原错误与修复原因，不靠遗漏项目减少 FAIL。该工具错误不属于产品行为结果。
- 未修改原设计、获批视觉资产、CONTEXT、产品文件或主工作区。规格文件是本轮新增、尚未提交的可审阅材料；没有产品候选、产品评审或发布结论。
