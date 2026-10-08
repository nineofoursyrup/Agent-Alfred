# #92 验证分配与执行交接

本文件是后续实施计划，**本轮没有运行其中的产品、浏览器、构建、原生交互或升级回退检查**。本次文档检查见 [VALIDATION.md](VALIDATION.md)。规范为 [SPEC.md](SPEC.md)，验收出口为 [ACCEPTANCE.md](ACCEPTANCE.md)。

## 开工前与每片交接

1. 回读 #85／#92 的 native dependencies 和 [source-index.json](source-index.json)；#89 已补齐正式归档身份且原设计字节不变，本轮六项上游依赖均已关闭。新会话仍核对实际来源与状态，不凭历史记录跳过新出现的阻塞。
2. 记录实际产品基线、HEAD/tree、未提交修改及环境；保留共享目录／用户改动。建立隔离工作树和专用集成分支，公共文件有明确整合者。分支／票据／PR 的创建按后续授权执行。
3. 固定 S01／S02 的公共接缝，设置集成分支的 CI 触发或等价固定候选执行。当前 CI 只自动处理 main push 和以 main 为目标的 PR，亦有 workflow_dispatch；不要假设对任意集成 PR 已执行。
4. 每片把源项→实现范围→片内／组合责任→适用检查→证据绑定到候选；部分完成须说明具体缺项。页面可用或 PR 合入集成分支不能直接将全路径置为 PASS。
5. 独立 Standards／Spec 评审查同一候选和本片范围；修复后登记 successor，只补受影响的行为／评审与必需机械检查。代码、输入、依赖、配置和环境没变的确定性结果可复用。

## 项目机械门禁

以下命令来自当前 [CI](../../../.github/workflows/ci.yml) 和 [Dashboard 验证说明](../../dashboard.md)，不是本轮执行记录。后续若项目脚本变化，以实际候选配置核对，不盲目照搬旧命令。Python 3.14 要有 Database 所需 SQLite C API，Node 为 22；记录实际 patch 版本及锁文件身份。

```sh
uv sync --extra dev --extra mcp --locked
uv run ruff check
uv run python scripts/check_skills.py
uv run python scripts/check_env_example.py
uv run --extra mcp pytest
npm ci --ignore-scripts
npx playwright install chromium
npm run typecheck
npm run test:browser
uv build
uv run python scripts/check_mcp_installations.py --output /tmp/mcp-artifacts.json
git diff --check
```

Linux CI 的浏览器安装沿现有 `--with-deps` 配置。本地可沿 docs/dashboard.md 的 `.venv/bin/...` 等价命令；差异必须仍覆盖相同环境／extras，不把缺依赖的失败藏作无关结果。

| 阶段 | 检查分配 |
| --- | --- |
| S01 读侧 | 新完整计数、元数据、时区／修订、来源及精确定位公共读取；失败／隐私／界限与旧消费者回归；适用 Python／HTTP 和静态门禁 |
| S02 壳层 | 全部现有九页仍可导航／操作的现有 browser 门禁，MainBar／历史／输入守卫的增量行为；全局样式、typecheck 与首次新增资源的包／HTTP 检查接缝 |
| S03–S10 页面 | 各自全部源路径的片内责任及关键反例、真实公共接口和视觉／焦点；共享配置／持久语义／清理等变更包含相应服务失败路径。未完成的另一端明确交给 S11 |
| 每次集成 | 固定实际集成候选检查冲突和涉及的联合行为；原项目必需门禁仍执行，不能仅由人工看 diff 替代 |
| S11 最终候选 | 上述完整 CI 集合、最终入口、所有 AC 和源项、跨页组合、视觉、桌面原生交互、移动模拟、升级回退；可复用仍适用证据，不为评审阶段无条件重跑 |

现有 browser 套件串行启动真实隔离 Dashboard／本地存储和离线模型；端口冲突应明确失败，不复用用户正在运行的服务。迟到／逆序／断连在 HTTP、SSE 或外部 IO 边界控制，等待权威事件或可见状态，不依赖固定 sleep 猜时序。外部凭据及付费执行不是本计划前提。

## 真实公共接缝与增量缺口

| 主题 | 可复用入口及必须补足之处 |
| --- | --- |
| MainBar／Session／记录 | `tests/browser/mainbar.spec.js`、`inbox.spec.js`、`tabs.spec.js`、`recording.spec.js`、`streaming.spec.js`、`pagination.spec.js`；服务侧 sessions／reply_recovery 相关确定性用例。新增精确目标读取必须验证真实实现，不能拿正文恢复测试充数 |
| Overview | `test_ops_snapshots.py` 的真实 Host/SQLite、`accounting.spec.js` 的快照屏障、Memory 修订读取；补完整来源与三分区行为，不把拼好的指标直接塞 UI |
| Run／Graph／导出 | `run-path.spec.js`、`trace_export.spec.js` 和 run-evidence；图文同源、权威缺失、当前记忆保护、原生下载与所有权 |
| Memory／Behaviour／Tools | [#90 ACCEPTANCE](../issue-90/ACCEPTANCE.md) 的原用例入口；尤其未确认动作、遗忘跨页失效、Skill／人格、聚合发送前核验、同名来源及授权回执 |
| Models／Connections／Ops／Database | [#91 ACCEPTANCE](../issue-91/ACCEPTANCE.md) 的原用例入口；清空已有值需真实持久重读，SQL 使用真实 worker，财务和正文寿命分别验证 |

这些路径只用于接手导航；测试存在或名称相同不证明覆盖当前候选。实施者根据实际变更选择扩展位置，保留有价值的失效方式，不为映射中的每一行另写一份重复测试。

## 视觉、桌面原生交互与移动模拟

- 使用 #86 `baseline/r1` 固定资产；登记候选与参考 hash、浏览器／操作系统、字体、DPR、视口和场景。四视口检查十页复杂内容；关键状态可在同一路径组合，不要求笛卡尔积截图。
- 壳层宽度 1099／1100 与内容区 679／680 分别设置并测实际几何。表格／卡片字段等价、完整 ID 入口、关键状态、焦点、滚动与减少动效均由可见行为核对；颜色对比按原阈值验证。
- 桌面 Chromium 使用真实浏览器 200% 缩放，中文输入法完成组合→确认→换行／发送；记录原生动作和实际观察。人工 composition 事件、CSS 字号放大继续是补充证据，不代替原生检查。
- 移动只做 Chromium 390×844、320×800 视口／触控模拟并覆盖可用高度变化；记录具体参数，走聊天、设置、运行查看及面板返回。iPhone／iOS Safari／真实移动软键盘为 `scope=excluded_by_user`、`result=NOT RUN`，没有真机证据不阻塞本次范围。
- 真实 Back／Forward、隐藏、reload／close 与 BFCache 分别登记。`no-store` 等保护不变；历史 BFCache 恢复 BLOCKED 保留，即使当前实际返回触发 reload 或人工事件通过也不升级该结论。

## wheel／sdist 与静态资源

当前 [隔离安装脚本](../../../scripts/check_mcp_installations.py) 已比对 `src/agent_alfred` 全部文件 hash，并在 wheel／sdist × base／mcp 四种安装中启动真实 Dashboard 和 Database 查询。它目前只显式读取 `/database` 与 `/assets/database.js`；源码 browser 服务又会插入源码目录，因此下面的补足属于后续实施责任。

1. 使用隔离候选构建目录，保留任何原有 dist，不删他人产物。绑定本候选一个 wheel 和一个 sdist、各自 SHA-256、构建环境及 source tree；当前脚本会遍历 dist 中全部匹配包，先核对输入集合，不能把残留包计入当前四组。
2. 在现有安装 smoke 扩展资源覆盖：以 HTML／JavaScript import／CSS 引用和实际变更生成所需资源清单，核对服务登记；只遍历 ASSETS 不足以发现漏登记文件。
3. 对每组安装经实际 HTTP 验证新／变更资源的 200、MIME、适用 CSP、no-store、nosniff 和包内字节；验证 `/overview`、九旧路由及 Run 深链接的入口。缺资源或错误 MIME 即 FAIL。
4. 源码 browser 检查模块实际加载及失败请求。报告明确分为源码浏览器行为、安装包文件身份、安装包 HTTP 资源；无需复制四套完整 browser suite，也不能将三者拼称完成了实际未执行的安装后全浏览器验收。

## 升级与支持目标回退演练

只在隔离状态目录与明确授权的实现任务中进行；本轮没有操作任何运行服务。先登记旧候选、新候选、支持的回退候选及 S08 等必须保留修复的提交身份。

1. 旧候选建立可核对的 Session／消息、记忆、账目和设置；保留标签页草稿，未保存页面输入先显式保存或放弃。基线记录不用于覆盖后续真实事实。
2. 调用既有正常关闭流程；检查准入已止、已获准 Run／修改／记录和查询／导出／MCP 清理完成、所有权已释放。未完成保持阻塞，可按原协议重试，不删锁强行继续。
3. 切换完整新产物并启动，显式刷新旧标签页；验证新实例、旧地址／Session／草稿及零自动重投，再通过公开路径产生新版消息、设置修改和遗忘事实。
4. 正常关闭，按具体依赖闭包切回支持目标，继续读取同一状态。检查新版已发生的事实、遗忘保护及需保留修复；不得回灌旧数据库或清目录。
5. 回退撤掉的界面能力如实登记；若遗留消费者不兼容、清空修复倒退或资源未释放，回退验收 FAIL／BLOCKED，不宣布“任意旧版安全”。不新增不停机双版本协商或数据库逆迁移。

## 交接结果与裁定

执行结果另存候选目录，引用 [acceptance-map.json](acceptance-map.json) 原 ID。每项包括结果、预期／实际、步骤／命令、证据、候选／环境、执行时间、首次失败和修复关联；未执行写原因，依赖外部条件写条件。HEAD、PR test merge 和 main 实际 merge SHA 各自记录，禁止重命名成同一证据。

设计静态检查、产品实现就绪、切片评审通过、整体验收完成、Git 交付与发布是不同状态。设计阶段只形成第一类结果，后续归档授权与远端结果见 PUBLICATION；#89 原未归档状态保留在历史副本，当前固定来源已补齐。所有产品检查仍需后续实现和实际执行。
