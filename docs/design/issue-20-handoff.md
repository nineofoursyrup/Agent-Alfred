# #20 实施交接 — r3 已发布

## 接手入口

- 权威 spec：[GitHub #20](https://github.com/nineofoursyrup/Agent-Alfred/issues/20)，正文修订 r3。
- 本地逐字镜像：[issue-20-integrations-spec.md](issue-20-integrations-spec.md)。发布后已回读核对正文、标签、原评论与依赖。
- 发布回执：[issue-20-publication.json](issue-20-publication.json)。内容清单：[issue-20-design-manifest.json](issue-20-design-manifest.json)。
- 词汇变更：[CONTEXT.md](../../CONTEXT.md)，新增“可选集成”“可选集成注册表”。
- spec SHA-256：`916bc71306e1b43b1f597bc55f2bd0a0553088fe1d4e71dcd8de5be66361b53b`。

## 已确认范围与状态

Q1–Q6、Q7–Q12 两轮各获“全按建议”，用户随后对 r2 整体回复“[$to-spec] 确认”。r3 仅按指定模板等义发布；36 条用户故事、Q1–Q12 与 CE-01～CE-12 均已确认，不重问已定行为或测试边界。

交付路径是 Connections 配置/探活 → Tools 既有授权 → MainBar 搜索 → Ops credits。只增加生产 Tavily 集成，采用 urllib，默认未配置。没有未解决的设计问题或 deferred case。ready-for-agent 已添加；#20 保持 OPEN，原生依赖 #10/#19 均 closed。

当前仅完成设计与 spec 发布：没有实现 Tavily，没有运行实施测试、真实探活、真实搜索或模型演示；没有代码提交、推送、合并或关闭 Issue。设计确认与标签不等于验收通过。

## Git 与工作树归属

- 设计工作树：`/Users/nineofour/Agent-Alfred-issue-20-design`。
- 设计分支：`codex/20-integrations-design`。
- 核查与冻结基线：`796b88c7d1e134906e25cf42f259d20381017fc2`。发布前远端 main 仍为此值。
- 设计改动尚未提交；本交接、spec、发布回执、manifest 为本地新文件，CONTEXT.md 为本地修改。不要假设切到该分支就能从 Git 提交中取到这些文件。
- 原目录 `/Users/nineofour/Agent-Alfred` 是旧 HEAD `31daa31`，其原有 `.gitignore` 修改与本任务无关，须保留。
- 建议实施在从复核基线建立的独立工作树进行；从本设计工作树按 manifest 携带文档，核验哈希。若同名文档或 CONTEXT.md 在新基线已经变化，先核对合并归属，不盲目覆盖。
- 旧 r0/r2 留存于原目录 `.scratch/issue20-spec-20260913/`，仅为确认历史，不能作为当前实施依据。r2 获确认时 spec hash 为 `bcfc3f52d2378c5fcae4677c2ef2c0693fe364ea87d5fd24a043fcb6852395fc`。

## 先读与已有实现

按项目说明读取 CLAUDE.md、CONTEXT.md、适用 instructions、Issue #20 及评论，以及 ADR-0005/0006/0016/0032。公开票正文已完整包含合同，不依赖本设计聊天。

已有 ToolRegistry、Tools 授权、Host、Loop、凭据重读、Connections、Ops 与独立持久工具计量。优先复用现有机制，不能另建授权存储、模型循环或工具账。具体模块形状可选择，行为合同不能自行削减。

三处实施时必须显式兑现的现有接口差距：

1. 进入 tool callable 才是工具启动事实，但仍可能尚未发 HTTP。受信未发送证据和不计费表达须落实；不能把启动改成未启动，也不能依赖 external 的 cost=None 表达不计费。
2. 当前 timeout code、UnknownCost、stop_reason 各影响不同投影，不能自动保证结果未知一致。Q11 要求业务未知时两账均 unknown、停止本 Run、固定解释与 model_delivery=not_sent；仅成本未知不停止。
3. 凭据重读、能力视图与观测须同代发布；真实搜索错误不能被旧探活缓存/浏览器回包覆盖。当前数据结构不是已验收该合同的证明。

## CE 到测试证据的映射

| ID | 必须证明的边界 | 主要公共 seam |
|---|---|---|
| CE-01 | 六格授权/配置矩阵，零 HTTP 引导与拒绝，下一 Step 读引导 | Host.submit → 真实 Registry/Loop/授权存储 |
| CE-02 | 多标签探活、共享缓存、10/600s、429 与冷却 | 真实 Connections API/MutationGate + 双浏览器 |
| CE-03 | 换 key、同 key 新观测与旧回包乱序、重启 | 真实 Host/凭据服务/API/浏览器 |
| CE-04 | 完整 HTTP/结构/四态/业务结局表，不能盲目全部回喂 | urllib 回环 HTTP + Host 整链 |
| CE-05 | credits 非法/缺失/零/分数，裁剪后计量仍可读 | Host → SQLite 两账 → Ops |
| CE-06 | POST 后结果丢失、控制中断、计量失败，停止与关闭 | Host/Registry/Loop/两账，精确失败点 |
| CE-07 | 新 call 同 query 可执行，同身份不重复执行/计量 | Host 多 Step + 真实 ledger/registry 重放 |
| CE-08 | 短/新/旧 key 各出口脱敏，重定向零跟随 | 真实 transport/Redactor/投影/页面 |
| CE-09 | 空结果/坏字段/链接/容量/慢流与明确边界 | 真实 HTTP 编解码/投影/浏览器 |
| CE-10 | 进入 fn 前、进入后发送前、发送后三种预算边界 | 假单调时钟+真实 Registry/两账/Host |
| CE-11 | .env 重读各失败点，保留旧一致配置或暂停 | 真实临时文件/Host/授权 store/浏览器 |
| CE-12 | 无 extra/缺依赖/重复身份/无效 key 不混用 | 真实注册装配/目录/执行检查 |

主要驱动入口：build_default_host + ScriptedModelFactory；host.submit(SubmitRequest)/wait。观测 host.tools_catalog/tool_requests/read_tool_history 与真实 Dashboard/Ops API。只替换模型、外部 HTTP、时钟和明确 IO 故障点；真实回环 HTTP 证明 urllib 行为，双真实浏览器标签证明 UI 回包排序。

每个 CE 交付可定位用例、真实命令、固定候选及文件哈希、结果和证据位置。不能仅用测试名/旧 PASS/静态快照宣称覆盖；期望值来自 spec，而不是当前实现。

## 门禁与 live 收口

当前基线 CI 的真实命令可作起点，实施者在新基线复核后执行：

- `uv sync --extra dev --locked`
- `uv run ruff check`
- `uv run python scripts/check_skills.py`
- `uv run python scripts/check_env_example.py`
- `uv run pytest`
- `npm ci --ignore-scripts`
- `npm run typecheck`
- 浏览器依赖就绪后 `npm run test:browser`（CI 安装命令为 `npx playwright install --with-deps chromium`）

另须按项目约定验证 wheel+sdist 在干净临时环境安装与相关安装后行为；CI 现有列表不含这项，不能因此省略。所有默认测试保持离线，不把 requires_key 加进普通门禁。

真实服务 gate：实施与离线证据准备完成后，另获用户授权执行 1 次 GET /usage、1 次 basic search。搜索由 ScriptedModel 驱动真实 Host 与 Tavily即可；付费模型演示不属于 mandatory gate。真实失败不自动多次重试到通过，须记录实际请求数与结果。缺授权/凭据时只能交付“离线通过，真实 Tavily 未验收”，不能称整票完成或关闭 #20。

/usage 免费未证实已被明确处理：不作免费承诺，不伪造零费用；并非设计 blocker。协作式 IO deadline 不承诺精确墙钟强制取消。以上都是确认的范围，不能在实现中用未经声明的强杀/后台遗留请求替换。

## 可直接交给实施会话的首条 prompt

> 请实施 Agent-Alfred #20，先读取 https://github.com/nineofoursyrup/Agent-Alfred/issues/20 的 r3 spec，以及 `/Users/nineofour/Agent-Alfred-issue-20-design/docs/design/issue-20-handoff.md` 和同目录 manifest。Q1–Q12、CE-01～CE-12 与公共测试边界均已整体确认，请勿重问。按交接先核验文件哈希、Git 状态、远端基线和工作树归属，在独立工作树实现，保留其他改动。使用真实 Host/Registry/Loop/SQLite/HTTP 编解码及浏览器完成所有离线验收，逐 CE 给证据并完成项目门禁与隔离安装。遇到合同无法兑现时报告具体反例，不自行缩减范围。真实 Tavily/模型调用、提交、推送、合并和 Issue 关闭均待另行明确授权；先完成可审阅实现与离线验证。
