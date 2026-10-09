# #119 Dashboard 十页迁移：最终组合与验收候选

本候选实现最终 `/` → `/overview` 的 `replaceState` 规范化与品牌入口，并修复 Models／Memory 在提交 B 后改回旧保存值 A 时未保护后继输入的问题。已完成实际四组安装 HTTP 和旧包→新包→支持目标的同状态演练。**整体仍为 BLOCKED：真实中文 IME 缺证；本地完整门禁运行中，最终独立两轴和远端 CI 尚待协调器审定。** 本文是可审阅执行快照，不是产品完成、合并或发布声明。

完整提案见 [309 源项／26 AC／12 MCE／8 G](dashboard-migration/acceptance-proposal.json)。此时提案为源项 300 PASS／7 BLOCKED／2 NOT RUN，AC 24 PASS／1 BLOCKED／1 NOT RUN，8 个组合链路 PASS。每行保留原责任片段身份与旧结果，另列当前裁定；原设计、首次失败和 canonical ledger 未覆盖。相连 AC 的整体状态不能由某条源项 PASS 推导。

## 固定身份与改动

| 身份 | 值 |
| --- | --- |
| 产品基线 | `22c8720e1ec874fd012cc79b086cd8aace4c79b6` |
| S11 全部已接受依赖 base | `8f579a4712075721728b2043d9f626689ff377a8` |
| 固定产品／测试／harness 候选 | `675cb695fed3d14059dd17071aef160971b3f303` |
| 候选 tree | `e36bf2c212a040dfd3a97187e41517401773f0e5` |
| `src` tree | `5e5affdf0c481f4b5ad38f02cf47d7fc2d5c483a` |
| `src/agent_alfred` tree | `035329e45cb97a7c170538139e510e14c960e995` |
| 支持回退目标 | `989662c1229d347d0a66a8b4e06e2ba894b9ca6e` |
| 固定源归档 | `9716e0c395c603ccc262284de577cfb8df3f8d48` |

本执行文档为该候选的文档后继；产品自 `593564f` 后未变。PR head、test-merge SHA、main merge SHA 在本快照中均未冒称已取得，由协调器在实际远端执行后分别写回。

- `c839736`：Models 单独冻结 submitted，保留原 baseline／CAS；pending／unknown 时比较提交值，确认失败／采用／成功才沿原协议退休。Memory 优先比较固定提交请求。原 A→提交 B→恢复 A 的三项真实 HTTP 失败已保留；修复覆盖 pending、unknown、一次 POST、持久值及焦点。
- `593564f`：根地址初始、显式导航和历史恢复规范化到 Overview，保留 query；品牌经过原离页守卫。原根入口失败及后继通过均保存。
- `0758e46`／`ef96f40`：保留有意义的后继输入与现有业务断言，修正最终入口、链接角色、折叠详情和可见状态文案带来的旧测试假设。新增十页流式连续性、价格清空→probe→Ops、单个真实遗忘事件对四类隐藏消费者的组合覆盖。
- `c6178cb`／`675cb69`：[安装闭包探针](../../scripts/dashboard_installation_probe.py)与[整包生命周期检查](../../scripts/check_dashboard_lifecycle.mjs)。无新增业务 API、存储迁移、真实 provider 调用或用户状态操作。

窄语义修复已经独立 [Standards](dashboard-migration/evidence/reviews/S11-semantic-Standards.md)／[Spec](dashboard-migration/evidence/reviews/S11-semantic-Spec.md) PASS；该批准不扩大为最终 S11 双轴批准。上游接受的 handoff、review、hash 索引在提案及其 `evidence/` 中。

## 实际组合与安装证据

| 链路 | 当前实际证据 |
| --- | --- |
| G01 | 同一个流式 Run 与新草稿经过全部十页，Session／Stream 不变；Inbox B 预览／Run 返回、显式跨 Session 精确定位、最新位置、原生 Back／Forward／reload 由当前公共路径组合 |
| G02 | 真实 Overview 完整来源→固定期间 Ops／Run→返回；跨午夜、价格改变、快照过期仍保留来源身份 |
| G03 | Memory 保存→下一 Run 实际命中→当前引用；Skill 与人格显式工具使用产生实际结果和后续 Run |
| G04 | Behaviour 保存→下一 Run；聚合固定原 Session／输入、发送前核验、原 Session 草稿及保护变化 |
| G05 | 实际 MCP 来源替换／退休／维护→Tools／Connections→新 Ops→Run→原 snapshot 返回；授权和清理分别核对 |
| G06 | 显式零与清空不同；固定旧 probe 费用 `0.00022` 不随清空改变，新 probe `0.00088`，Run／Attempt／Ops准确；丢202仅显式核对、不猜ID、不重投 |
| G07 | 同一个真实删除事件撤销隐藏 Run 引用、Overview 库存、聚合资料、Database 结果／复制与在途响应；迟到响应不复活 |
| G08 | 三个独立 installed env 依次正常关闭，保留同一新建隔离 state 和浏览器标签；旧、新事实经支持目标实际读取 |

每链的测试文件、具体断言范围和日志 hash 见提案 `integration_chains`，不是按测试数量或单边 mock 计通过。

[安装态记录](dashboard-migration/installed-http.json)绑定 fresh `dist-02` 中恰好一个 wheel 和一个 sdist；wheel／sdist × base／mcp 全部通过。每组实际安装后启动本地 HTTP，16 个入口／query／opaque Run 深链与 34 个资源分别 GET／HEAD，共 100 项检查；闭包从 HTML、JS import/export、CSS 引用发现后再反查登记。检查 MIME、CSP、no-store、nosniff、Content-Length、字节 hash，并保留现有全部包文件身份、CLI、Graph、真实查询 worker 和 MCP smoke。源码 browser 行为与安装态 HTTP 分别报告，未声称执行四套完整安装态 browser suite。

[整包生命周期记录](dashboard-migration/lifecycle.json)保留每代实例、包路径、HTML hash、状态与显式请求。旧包建立 Session／消息／两条 Memory／显示名／零输出覆盖价／账目；新包保留原 Session 并写新消息、清显示名与最后一维覆盖、保留新 Memory、删除旧 Memory；支持目标读取两代消息与 Run、旧新保留记忆、被删记录404和 forgetting complete、两覆盖 null／revision4、相等 Ops summary。两次旧标签显式 reload 各零自动 POST。三个宿主均 `close=true`、exit0，无锁删除、数据库恢复、状态清空或并行写宿主。

支持目标保留所有已接受片段和 `c839736` 语义修复；**实际失去的界面能力是根地址与品牌入口恢复 Inbox**。十页和修复仍在，不宣称任意旧 main 安全。原清单里的 `source_tree` 指 `src/agent_alfred`；便携副本另写 `source_path`／`package_source_tree`／`src_tree`，原结果及其 hash 保留。

## 门禁、首次失败与环境边界

[检查记录](dashboard-migration/checks.json)包含命令、exit 和 hash。[原浏览器全量](dashboard-migration/evidence/S11/browser-full-01.log)为 514 PASS／11 FAIL，未改写；[索引](dashboard-migration/browser-first-failure-index.json)逐项保留。针对旧入口／角色／标签的 11 个后继均已通过，最终 full-02 仍单列为必需门禁。组合首轮的价格常数误写 `0.00014`，按固定价格 `.22 / million × 1000` 修为 `0.00022` 后通过；没有改变生产计价。生命周期首轮错误要求旧包存在新 UI region，后继改用版本稳定的公开持久消息读取后通过，首轮失败保留。

Python full-01 在 `04:11:51Z` 以 base 产品启动；当时仅 browser 测试已有未提交改动。入口于 `04:12:04Z` 修改，后继 JS 语义修复于 `04:16:10Z` 固定。全程 Python 生产／测试、依赖锁与配置未变；[适用性](dashboard-migration/check-applicability.json)分开记录资源读取时点和最终文件身份，不把整次运行冒称从 clean `675cb69` 启动。最终 full-browser-02 从固定测试 `ef96f40` 启动，后续仅生命周期 harness／本文变化，无 browser 输入或产品变化。

原生 [Chrome 200%](dashboard-migration/native-desktop.json)由协调器在产品 `593564f` 上实际完成，当前 `src` 完全相同：CSS 864×453、DPR4、html/body zoom1，输入、发送、关闭、草稿、焦点与局部滚动可达。真实 SQLite 核对1 Session／2 Runs／4消息，正常退出后恢复原110%。系统可用输入源只形成普通字母，没有真实中文 composition／确认Enter证据，因此 AC03 与7个直接含IME义务的源项保持 **BLOCKED**。人工 composition 用例只作补充。

移动证据仅 Chromium 390×844／320×800 视口和触控模拟；iPhone／iOS Safari／真实移动软键盘 `scope=excluded_by_user, result=NOT RUN`。历史真实 BFCache **BLOCKED** 保留，no-store 未改。任何本地／远端新失败继续保留，不由IME缺口掩盖。完整 Python／browser、最终两轴及远端CI完成后，协调器可在 canonical记录／PR 更新结果，并明确本快照的观察时点。

复现使用项目 [CI](../../.github/workflows/ci.yml) 的完整命令及 CPython3.14.7共享 SQLite C API／Node22。安装检查接受 `--dist <fresh-dist> --output <report>`；生命周期脚本接收三个已独立安装的 Python／artifact 身份、未存在的隔离目录与专用端口清单，顺序执行三个整包。所有可执行测试和 harness 均在仓库；大体积 trace、隔离状态／环境和浏览器 profiles 保留在本地证据目录，未加入仓库。
