# #92 Dashboard 迁移：事实与输入状态

核对日期：2026-10-09（Asia/Shanghai）。本文件是设计调查，不是产品验收记录。

## 工作身份

- 议题：[#92 决定：Dashboard 迁移的切片顺序与整体验收合同](https://github.com/nineofoursyrup/Agent-Alfred/issues/92)；父地图 [#85](https://github.com/nineofoursyrup/Agent-Alfred/issues/85)。
- 本会话已按仓库约定将 #92 指派给 `nineofoursyrup`；设计访谈后用户授权归档收尾，实际提交／发布／关闭结果以 [PUBLICATION.md](PUBLICATION.md) 引用的远端 resolution 为准。
- 文档工作分支：`codex/issue-92-migration-contract`。
- 文档起点：`1a343ae1894554ad0167273a34b1f6bce171fb5b`，含 #86、#87、#88、#90、#91 的归档。
- 产品基线：`22c8720e1ec874fd012cc79b086cd8aace4c79b6`。已比较 `src/`、`tests/`、`.github/` 与依赖文件；文档起点相对于产品基线没有这些范围的差异。
- 主工作区已有的 `CONTEXT.md` 修改及未跟踪 `tmp/` 均保留；#89 原设计已逐字节归档，当前文档分支只合入已确认的领域补充。

## 上游输入

| 票据 | 当前核对状态 | 固定归档 / 来源 |
| --- | --- | --- |
| #86 视觉基线 | CLOSED / completed | [541befcbe4f771d6f0d7ce3dfc820357c91c7cbc](https://github.com/nineofoursyrup/Agent-Alfred/commit/541befcbe4f771d6f0d7ce3dfc820357c91c7cbc)，[决定](../issue-86/DECISIONS.md) |
| #87 壳层 | CLOSED / completed | [751d5b84e1b7a633019920a805ab647c1b15a419](https://github.com/nineofoursyrup/Agent-Alfred/commit/751d5b84e1b7a633019920a805ab647c1b15a419)，[规格](../issue-87/SPEC.md) |
| #88 Overview | CLOSED / completed | [0e9915b5f020a756776f3e67fe13f352da3bb983](https://github.com/nineofoursyrup/Agent-Alfred/commit/0e9915b5f020a756776f3e67fe13f352da3bb983)，[设计](../issue-88/DESIGN.md) |
| #89 收件箱 / 运行 | CLOSED / completed；原设计字节未变 | [0120f0aff56b7c8b29854c5d9be5fe475b8b06b7](https://github.com/nineofoursyrup/Agent-Alfred/commit/0120f0aff56b7c8b29854c5d9be5fe475b8b06b7)，[设计](../issue-89/DESIGN.md)、[resolution](https://github.com/nineofoursyrup/Agent-Alfred/issues/89#issuecomment-6065550390) |
| #90 Memory / Behaviour / Tools | CLOSED / completed | [7d175d6a65a93aec725cf86e6be3df167d3ca74c](https://github.com/nineofoursyrup/Agent-Alfred/commit/7d175d6a65a93aec725cf86e6be3df167d3ca74c)，[设计](../issue-90/DESIGN.md)、[验收](../issue-90/ACCEPTANCE.md) |
| #91 Models / Connections / Ops / Database | CLOSED / completed | [1a343ae1894554ad0167273a34b1f6bce171fb5b](https://github.com/nineofoursyrup/Agent-Alfred/commit/1a343ae1894554ad0167273a34b1f6bce171fb5b)，[设计](../issue-91/DESIGN.md)、[验收](../issue-91/ACCEPTANCE.md) |

归档阶段 GitHub native dependency 读回：#92 的六项依赖 #86–#91 均 CLOSED，`blocked_by=0`。原 #89 OPEN／未归档缺口保留在历史副本；本轮由用户明确授权完成其归档与收尾，不把此前本地确认冒充当时已完成远端归档。

#89 的 `APPROVAL.md` 记录 Q1–Q11 及整合版确认。`DESIGN.md` SHA-256 为 `35ba39e52415b8e349d3658970932bcbd23019ef561f55a41642369afbe0af84`；`APPROVAL.md` 为 `40061bc6e21d5c7209512565928fa81493376f15069f4953759f4f081f94a524`。两者与原本地输入字节一致，已在固定提交中逐文件回读；来源索引现使用该提交的永久链接。

## 沿用、不重新征询的合同

1. 十页、中文深色、A 视觉基线；Overview 为最终默认首页，旧九页地址及深链接保留。
2. 唯一 MainBar / Stream、标签页隔离的 Session 与草稿；预览或导航不自动选择、新建 Session 或重投请求。
3. 壳层 1100 CSS px 断点、232 / 320 栏宽及面板历史已定；面板显隐不等于实际离页，隐藏内容仍接受保护失效。
4. `phase`、`outcome`、`recording_state`、正文加载、过程证据、费用与连接状态保持分轴；未知、缺失、失败不能变为成功。
5. Overview 的三项指标及完整数据来源、自然日口径、五条终态加至多一个当前槽已经确认。
6. #89 已确认并归档会话运行含 aggregation 的一致筛选、准入摘要披露、有界精确回复定位及列表稳定阅读；原确认与归档身份分别保留。
7. #90 / #91 已确认各页信息层级、折叠、草稿和操作边界；Models 已有值清空的两个同根问题属于已确认实施责任。
8. 文档完成、产品实现、独立评审、Git 交付与 v1 发布是不同结论。本迁移仍独立于既有 v1 发布门槛。

## 会影响切片的代码事实

| 接缝 | 现有事实 | #92 必须安排的责任 |
| --- | --- | --- |
| 公共样式 | [app.css](../../../src/agent_alfred/ops/static/app.css) 全局覆盖 tokens、按钮、表格、卡片、MainBar，图还有硬编码颜色 | 确立公共样式所有者及十页兼容证据；页面特例不能无界影响其他页 |
| 路由与页面生命周期 | [app.js](../../../src/agent_alfred/ops/static/app.js) 的 `route()` 集中创建、销毁页面；[runs.js](../../../src/agent_alfred/ops/static/runs.js) 与 [memory.js](../../../src/agent_alfred/ops/static/memory.js) 另有直接 history 写入 | 导航、面板历史、未保存输入守卫、资源释放由统一接缝协调 |
| 多页共用文件 | [pages.js](../../../src/agent_alfred/ops/static/pages.js) 同时包含收件箱、Models、Connections、Behaviour | 页面切片应有明确修改归属；是否提取模块是实施选择，不借机扩展产品架构 |
| 入口白名单 | [assets.py](../../../src/agent_alfred/gateway/web/assets.py) 维护可服务页面 | `/overview`、`/` 默认入口与前端导航必须一致落地 |
| Run 只读补充 | 当前 Run JSON 缺独立记录状态，聚合筛选分类存在不一致；`/api/reply` 只补正文，不提供完整历史位置 | #88 / #89 的元数据、准入披露及有界定位应共享可信来源，不能各页猜测 |
| Overview 来源 | 当前没有满足完整期间统计及同修订记忆计数的总览读取 | 安排真实读侧、有限资源所有权及过期回收；不得逐页读正文数数或侵占 Ops 快照 |
| 设置清空 | `settings_commands.py` 的清空语义与 `pin()` 的保留语义冲突，见 [#91 FACTS](../issue-91/FACTS.md) | 归入 Models 的完整行为切片，覆盖持久值清空、重载及价格回退 |

## 验证来源与边界

- #87 的用户路径与 CE、#88 的 CE、#89 的路径与 CE、#90 / #91 的路径与反例应按源票和原 ID 引用，不通过重新编号或测试总数丢失责任。
- 关键跨片链路包括：草稿 → 预览其他 Session → Run → 返回 → 显式定位；Tools → Connections；Tools → Ops → Run → 返回原快照；Memory 遗忘 → 隐藏的聚合 / Database / 当前记忆引用失效。
- 项目现行门禁来源：[Dashboard 验证说明](../../dashboard.md)、[CI](../../../.github/workflows/ci.yml)、[package.json](../../../package.json)、[pyproject.toml](../../../pyproject.toml)。包括静态检查、技能与环境一致性、确定性 Python、Dashboard typecheck / browser、构建及隔离安装验证，具体分配在 #92 决策后完成。
- 当前 CI 只对 `main` push、目标为 `main` 的 PR 和手动 workflow_dispatch 触发；若采用集成分支，必须显式安排其等价 CI，不能假设已有触发覆盖。
- 既定四视口为 1440×900、1280×800、390×844、320×800；壳层 1099 / 1100 及 #89 内容 679 / 680 边界另检。真实缩放、中文输入法、移动软键盘、原生返回与 BFCache 的证据类型不可互相冒充。
- 历史 BFCache 真正恢复分支仍为 BLOCKED，见 [#66 实施记录](../../implementation/issue-66-database-console.md) 和 [#91 验收说明](../issue-91/ACCEPTANCE.md)。新证据只能另记，不覆盖首次结果，也不为测试放宽 `no-store`。
- 本轮产品测试、浏览器行为测试、外部模型调用均 **NOT RUN**。只做只读事实核对及访谈文档检查。

## 第一轮确认后的补充事实

### 真机访问与回环边界

**本段保留第二轮提问前的调查，不是当前执行方案。** 用户随后明确「只做模拟，不真机了」，Q6 已改为移动模拟；无需取得 Android、设置 USB 转发或提供 iPhone 访问服务的方案。

[ADR-0014](../../adr/0014-local-dashboard-threat-model.md) 限定本机回环服务；[guard.py](../../../src/agent_alfred/gateway/web/guard.py) 只接受 `127.0.0.1` / `localhost` 的 Host，Origin 还须匹配实例实际端口，写入仍校验 CSRF。因此，直接让手机访问 Mac 的局域网地址不是现有可用合同，不能为验收随手改为 `0.0.0.0` 或放宽跨源规则。

2026-10-09 核对的 [Chrome 官方 USB 端口转发说明](https://developer.chrome.com/docs/devtools/remote-debugging/local-server/#case-2-set-up-port-forwarding-through-usb-for-your-android-device) 明确支持 Android 真机通过 USB 将设备本地端口映射到开发机服务，不依赖局域网配置。由此提出 Q6：同端口映射到隔离验收实例的回环监听，在真机 Chrome 中使用 `localhost:<实际端口>`，保留浏览器提交的 Host / Origin 与正常 CSRF。该兼容性是结合本地 guard 的设计推论；尚未连接设备或实测。设备是否可用仍是环境事实，不由选择方案自动证明。

### 停机、切换及资源所有权

[RuntimeHost.close](../../../src/agent_alfred/runtime/host.py) 关闭准入后等待已接收工作、普通修改及记录收尾，再释放依赖资源；未完成返回 False。[DashboardRuntime.close](../../../src/agent_alfred/gateway/web/server.py) 继续持有尚未释放资源及状态目录所有权，可重试推进关闭；[CLI 关闭处理](../../../src/agent_alfred/gateway/cli.py) 对关闭未完成保留明确失败状态。换版本不能把一次 close 请求当成已安全停机，也不能删除锁文件启动第二个宿主。

[assets.py](../../../src/agent_alfred/gateway/web/assets.py) 按请求从当前包内读取前端资源，而旧标签页可能仍执行已经加载的 JavaScript。现有合同不提供跨版本的热替换事务，因此 Q7 建议使用已确认关闭后的整包切换和显式页面刷新；对同一保留状态做升级／回退验收，而非新增不停机版本协商机制。

本次补查未改变服务绑定、设备配置、运行进程或个人状态；产品及真机检查仍为 NOT RUN。
