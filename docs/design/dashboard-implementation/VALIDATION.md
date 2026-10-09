# 实现与验收接缝

本文件是后续产品验证规格，**以下产品检查全部 NOT RUN**。本轮只执行 [静态核对](STATIC-CHECKS.json)。源项／AC／MCE 的原始结果保留在 [固定验收映射](../issue-92/acceptance-map.json)，执行证据另存绑定候选的目录，不覆盖设计或首次失败。

## 接缝选择与新增覆盖

使用隔离本地状态、真实 Host、SQLite、HTTP、查询 worker、浏览器及现有离线模型。允许控制外部 IO、时钟、服务结果和 HTTP／SSE 到达顺序；生产分类、定位、计价、去重、保存判据、保护及资源释放不能替换为预设成功。等待权威事件／可见状态，不靠固定 sleep 猜竞争。

| 验证族 | 复用的真实接缝 | 此次需要证明的独立失效方式 | 责任 |
| --- | --- | --- | --- |
| 期间完整性与计价 | Host accounting snapshot、真实 Run／Attempt 账目、`test_ops_snapshots.py` | 超过 50 条、全用途／准入、旧无 Run 消息、跨自然日／DST、无效时区、坏时间 U、价格冻结、作废 Attempt、unknown／零／微小正数；工具缺口不污染模型证明 | S01；S04 展示；S11 G02／G06 |
| 临时资源 | 实际账目构造、保留 snapshot 容器／读取锁 | 预置别人的有效 Ops snapshot，重复进入、失败、取消、替换期间；Overview 留存槽数为零，旧 snapshot 字节／身份／有效期不变，无悬挂事务；预算失败不产生部分成功 | S01、S04 |
| 同修订记忆计数 | MemoryQueryService、SQLiteStore、MemorySync，`test_memory_queries.py`／`test_memory_read_revision.py` | 超过 100 条，count 不取正文；读取竞争只接受一致 revision；替代 Store 缺能力整体不可用；隐藏时 revision 推进撤值，旧响应／重连不复活 | S01、S04；S11 G07 |
| 安全摘要与用途 | 实际 Run 查询和 HTTP，sessions／recording／aggregation 回归 | chat／aggregation／system／unknown 在列表、当前槽、locate 一致；未获准输入使用可辨识敏感标记确认不披露；无 trace／正文读取；坏收尾证据、重启 interrupted、recorded 对旧 pending 的单调合并 | S01、S03 |
| 来源定位 | 实际 Session／Run 列表、分页、消息分段及新定位 HTTP | 相同目标分别位于短、长历史，定位请求数不随前置页数增长；有界输出／keyset 查询，不读取全部正文；特殊／空 ID、损坏锚点、filter=all、活动槽、runs_pending、目标消失与正常 cursor 后续 | S01、S03 |
| 精确正式记录 | `test_reply_recovery.py`、真实记录屏障及 `/api/mainbar/locate` | 投影命中不等待数据库保存锁；早期完整正文不截断；no_reply／withheld／unavailable 分开；用户只有预览如实标注；同文旧消息不同身份；定位与普通历史／恢复并发只显示一份正式结果 | S01、S02、S03 |
| 壳层及历史 | mainbar／inbox／pagination／tabs／streaming／restart browser | 实际请求／连接计数，十页往返，单层面板，Back／Forward 取消守卫 URL 恢复，同地址不重建；草稿隔离、标签复制边界、存储不可用；迟到响应不切 Session／夺焦 | S02；S11 G01 |
| Run 证据／Graph／导出 | run-path／trace_export browser、真实 run-evidence、ZIP | 已准备非实际调用，作废仍计账，缺过程不抹账；图文同源锚点，权威缺失与标旧不同；三态完整性、生成／取消／过期／cleanup、原生下载所有权 | S03；S11 联合 |
| Memory／聚合保护 | 真实命令、遗忘／镜像／提炼及聚合，#90 原用例 | 掉回执同 operation ID 核验，迟到回执不清新输入；外改确认身份；未选资料不读，发送前复核，固定原 Session；隐藏引用／预览／草稿／结果副本失效 | S05、S06；S11 G03／G04／G07 |
| Behaviour 图与统计 | topology／routing-statistics browser 及服务测试 | 读取不保存，冲突／损坏恢复，图可标旧而统计撤值，未知版本／零分母／覆盖，响应逆序不拼时点 | S06 |
| Tools 来源授权 | 真实授权存储、MCP 隔离 fixture 与 Tools／Connections | 同名不同 source、退休／替换、draft／saved／effective、掉回执不重送、授权不跟随显示名／旧来源复活；人格调用真实结果／Run 证据 | S07；S11 G03／G05 |
| 设置清空与并发 | `test_settings_commands.py`、`test_model_settings_store.py`、settings browser | 先持久已有值→清空→重建 store＋GET；最后一维 null 与显式 0；未指定仍保留；expected revision 冲突、在途新增编辑；价格回退链与旧快照独立 | S08；S11 G06 |
| 探针与连接维护 | `test_inference_probe_target.py`、真实准入／Run／MCP cleanup | 未保存草稿不进入探针目标，受理不冒成功，丢响应不自动重投；连接／支持／授权分开，清理受阻与释放分别可见 | S08；S11 G05／G06 |
| Ops | accounting browser、真实 snapshot/page/detail | 期间／时区／价格固定，完整聚合不误逐 Run；财务／历史正文／当前核验分离；过期返回不换 snapshot，来源恢复不抹筛选 | S09；S11 G02／G05／G06 |
| Database | database／cancel／status／lifecycle browser、真实 worker，native lifecycle 脚本 | 草稿≠提交 SQL≠结果，示例替换守卫、取消≠完成、预算／列／行／字节限制、cleanup 未释放准入受限；真实遗忘与隐藏／文档生命周期清除副本 | S10；S11 G07／G08 |

测试文件的精确导航见 [源码与测试入口](SOURCE-AUDIT.md)。扩展现有测试优先；一条真实行为可以承载多个相互关联的断言，不为 source ID、字段、状态数机械新增独立用例。临时检查足以证明的静态事项不强加永久产品测试。

## 来源、修订与失败的合并规则

- 记录 `process_instance_id`、Session／Run、operation、snapshot、revision、page／source generation 和所选目标。过时请求的内容不能由“最后到达”覆盖当前对象；实例变化不能继续把旧快照称当前。
- SSE、普通 HTTP、完整正文、生命周期、记录、trace、计价、连接、授权和清理各自有可见状态。source unknown、读取 failed、内容 stale 与实例 offline 可并存，不压成单个绿色／红色徽标。
- 故障用例必须检查原动作是否真的只提交一次；受理未知／缺回执／取消请求均不能推定服务未执行。恢复读取只恢复读取，不能新建动作。
- 用真实数据库提交／服务事件屏障控制关键交错；证明页面隐藏不触发 dispose，同时保护订阅仍能清掉隐藏内容。实际离页移交资源所有权，而非以页面卸载假定后台取消成功。
- 同候选已有确定性 PASS 只有在代码、输入、依赖、配置及环境继续适用时复用。修复后的 successor 保留首次 FAIL，只复核受影响检查与必要项目门禁。

## 十页视觉、键盘与范围

固定参考为 #86 `baseline/r1` 与 tokens，原始 HTML／预览／截图不覆盖。记录参考及候选 hash、浏览器／操作系统、字体、DPR 和场景后比较；字体抗锯齿差异可解释，遮挡、信息丢失、不可操作或不达对比度仍 FAIL。

| 范围 | 要求／记录 |
| --- | --- |
| 四参考视口 | 1440×900、1280×800、390×844、320×800 CSS px；十页复杂内容及适用正常／加载／空／错／断连或过期／未知／未保存／长内容状态，可组合场景，不要求笛卡尔积截图。 |
| 壳层断点 | 1099／1100，实测左 232／右 320 与中央余宽；收起不留空栏，窄屏导航与 MainBar 互斥。 |
| 中央列表断点 | 679／680，量实际中央内容宽度；不能用整个 viewport 代替。同一记录字段、完整 ID、操作和异常均可达。 |
| 键盘与焦点 | 当前可操作区域、Esc、遮罩、返回焦点、守卫默认留页、局部滚动、长输入、移焦后迟到响应不抢焦；减少动效无循环／过渡。 |
| 真实桌面 200% | Chromium 原生 browser zoom 200%，记录操作与可见几何／输入／发送／关闭可达性；CSS zoom、字体覆盖和截图缩放不能替代。 |
| 真实中文 IME | 记录系统输入法、组合输入→确认 Enter→换行／发送的真实操作；确认不得误发，草稿与焦点保留。合成 composition 测试为补充。 |
| 移动模拟 | 仅 Chromium 390×844／320×800、触控、DPR／可用高度变化；覆盖聊天、设置、Run 查看、面板／返回。结论必须明确“模拟”。 |
| 移动真机 | iPhone、iOS Safari、真实移动软键盘：`scope=excluded_by_user`、`result=NOT RUN`；不因其缺证阻塞本次限定范围，也不称通过。 |
| BFCache | 真实 Back／Forward、reload、隐藏／恢复与文档释放分别验证；保留历史真实 BFCache `BLOCKED`。不得改 no-store 或用人工 persisted 事件提升历史结论，也不新增无条件真 BFCache 门槛。 |

无法取得真实桌面缩放／IME 证据时，相应必需 AC 保留 NOT RUN／BLOCKED 并说明实际缺失条件；移动模拟通过不能替代。

## 项目必需门禁与触发

固定基线 CI 自动触发仅为 main push、目标 main 的 PR，另支持 workflow_dispatch。实施前为迁移集成分支配置明确触发或固定候选等价检查；不要把 main 的历史 CI 视为集成 PR 已检查。

以下为后续执行集合，**本轮未运行**。CPython 3.14 须提供 Database 所需共享 SQLite C API，Node 22；记录实际 patch 版本、依赖锁、extras、操作系统及端口／隔离状态。最终候选按当时实际 CI 核对命令。

```sh
uv sync --extra dev --extra mcp --locked
uv run ruff check
uv run python scripts/check_skills.py
uv run python scripts/check_env_example.py
uv run --extra mcp pytest
npm ci --ignore-scripts
npm run typecheck
npx playwright install chromium
npm run test:browser
uv build
uv run python scripts/check_mcp_installations.py --output /tmp/mcp-artifacts.json
git diff --check
```

Linux browser 安装沿 CI 的 `--with-deps`。本地等价命令仍须覆盖同一依赖与环境；端口冲突不得复用用户服务来“跑绿”。必要检查通过后不因评审／收尾阶段重复跑未变化的确定性集合。

## wheel／sdist 与静态资源

1. 绑定候选生成的一个 wheel、一个 sdist 和 SHA-256；保留既有 dist，先核对脚本实际输入集合，避免把旧包算入当前四组。当前隔离安装脚本会遍历匹配产物，实施须确保与声明集合一致。
2. 保留现有包内源码文件 hash 核对；wheel／sdist × base／mcp 四组都启动实际安装的本地服务。现有 smoke 只显式读 `/database` 与 `/assets/database.js`，本次必须补新／改资源及页面入口。
3. 从 HTML、JavaScript import、CSS 引用闭包与实际变更生成需要的资源集合，反查服务登记；只遍历 ASSETS 会漏掉未登记文件，不能作为完整清单。
4. 四组安装后 HTTP 检查 `/overview`、旧九路由、Run 深链接，以及新／变更静态资源的 200、MIME、适用 CSP／no-store／nosniff 和包内字节；缺资源和 MIME 错误均 FAIL。
5. 源码 browser 验证实际模块加载与失败请求。分别报告“源码浏览器行为”“安装包文件身份”“安装后 HTTP”，不声称运行了未执行的四套安装后完整 browser suite。

S02 起各片持续提供自己新增／改变的静态引用清单，S11 取最终闭包。默认入口最后切换后，候选或相关文件有变化必须补入口／资源／路由证据，不能借切换前结果。

## 升级与支持目标回退

仅在后续获授权的实现／验证任务中使用隔离持久状态，不触碰用户运行服务。预先记录旧产物、新候选、支持回退目标、依赖闭包与 S08 等必须保留修复；此处不选定任意旧 main 为安全目标。

1. 在旧候选经公开路径产生可核对的 Session／消息、Memory、账目和设置，并记录草稿及旧深链接。未保存页面输入由用户显式保存或放弃，原证据不能用作覆盖后来事实的备份回灌。
2. 使用既有正常关闭流程停止准入，等待受理 Run、变更、记录、查询／导出／MCP 清理及宿主所有权释放。close 为 False 或所有权仍在时保持阻塞，不能删锁、覆盖包或并启第二个写宿主。
3. 确认释放后切整个新产物，启动并显式刷新旧标签。核对新实例与资源、旧路由、Session 和草稿及无自动重投；经新版本公开路径产生新消息、设置变化与遗忘事实。
4. 正常关闭，再切支持目标，读取同一持久状态。检查新增事实保留、遗忘保护与清空语义修复不退化，依赖消费者无悬空入口。不得恢复旧数据库或清状态。
5. 如实记录撤回后失去的新界面能力；回退成功与十页迁移完成是不同结论。数据模型若需本合同之外变化，或必须支持不停机混版本，属于新实质决定，不能暗中扩范围。

## 候选、证据和评审交接

每份候选结果至少记录：规格身份、固定源归档、产品 base／head／tree（未提交时用明确内容 manifest）、依赖候选、产物及 hash、执行命令／步骤、输入、环境、预期／实际、source／AC／MCE／G IDs、结果、证据附件、执行时间、首次失败和修复关联。PR head、test-merge SHA、main 实际 merge SHA 分列，不能互换名称。

结果使用 PASS／FAIL／BLOCKED／NOT RUN；范围排除另记 scope。必需范围中已有 FAIL 不能被其他 BLOCKED／环境缺口掩盖；源项须所有责任片段和必要组合都有有效证据后才整体 PASS。S11 对全部 309 源项负责整合，不能只接手其 25 项直接片段；G08 从 #92 整体合同继承，不因上游 source_items 中没有 G08 字段而遗漏。

每片及最终候选独立 Standards／Spec 评审绑定相应真实范围；产品实施者自评不能替代独立评审。当前规格整理不启动多代理产品评审，也不登记不存在的评审通过。最终检查／评审就绪仍与 commit／push、合并、运行切换、发布权限分开。

## 本规格包的可复核检查

在此工作树运行：

```sh
python3 docs/design/dashboard-implementation/verify_spec.py
git diff --check
```

脚本只读本地文件和 Git：核对固定来源文件、归档 manifest、309 个源片段 hash、切片责任反向索引、DAG、AC／MCE／G 数量、产品 NOT RUN、相对文档链接和保护文件未改；stdout 输出 JSON，不访问产品服务或写 Git。生成的 [STATIC-CHECKS.json](STATIC-CHECKS.json) 另含规格文件内容摘要，作为本轮静态观察。后续产品实现之后，应单独使用当时的产品验证流程；此脚本的“只改文档”检查不是产品候选门禁。
