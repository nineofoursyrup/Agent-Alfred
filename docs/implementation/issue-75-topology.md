# Behaviour 只读流程拓扑

验收权威：[Issue #75 / BEHAVIOUR-TOPOLOGY-SPEC-r1](https://github.com/nineofoursyrup/Agent-Alfred/issues/75)，规范 SHA256 `57e328827dbad88d35a95750a0e2ca456b084321a085d08d86bb8b851561c83a`。

Behaviour 保留消息分流的持久设置和手动聚合的固定请求；两个区块分别提供默认收起的“查看流程”。观察组件不重建表单，也不发送业务写请求。

`GET /api/behaviour/topology?workflow=message_routing|manual_aggregation` 返回当前运行宿主已发布的编译图。封套包含 workflow、graph_id、process_instance_id、publication_generation、read_at、status 和 description。description 原样包含 schema_version、topology_hash、topology 和编译时冻结的 presentation。图和发布代际通过同一个不可变引用读取；该 GET 不进入 mutation gate，不编译、不执行，不做模型/工具健康探测。

只有这两张图可查询。无图返回 `status=unavailable`、稳定原因和 `description=null`；未知 workflow 返回 400；读取故障返回 503 / `topology_read_failed`，不输出异常详情。查询沿用全局 Host/Origin 防护。聚合保持启动后固定，发布代际为 1，不新增热编译。

前端在整体校验之后才接受快照。SVG、节点说明、终点、连接和条件明细均迭代同一份 topology；presentation 只补中文解释，不决定结构。普通、条件、恢复边分别使用实线、虚线、点线；同端点 full/fallback 保留两条边和两个标签。原始数据声明、工具名和完整身份可展开阅读。

每图有独立请求序列；收起、离页、已知进程变化和断连撤销旧请求资格。重新展开/手动刷新读取，连接恢复只更新提示。失败保留明确标旧的成功快照；明确无图撤下旧图；协议不兼容不绘制部分新图。相同 schema/hash 保留视口和选择，结构变化适应全图并关闭旧详情。查看偏好不写入存储。

## 可复现验证

正常成功通过真实浏览器 → HTTP → RuntimeHost → 编译图 describe。`topology_server.py` 的构建控制经现有授权重新应用路径发布真实新图；缺图由启动构建失败产生。模型仅在传输边界使用 ScriptedModel。协议损坏、纯文本恶意文案、HTTP 故障和响应截留是单列的故障注入，不能作为正常成功证据。

Playwright 屏障保持拦截器注册到截留响应释放；不用 `{times:1}` 移除最后一个拦截器，以免浏览器提前继续已暂停的请求。时序用显式 Promise/模型/recording 屏障，不用 sleep。

| 反例 | 公开路径验证 |
|---|---|
| CE-01 | `topology.spec.js` real graphs、disconnection；`topology-observation.js` 在真实表单和业务请求期间核对字段及零 POST；`routing.spec.js` 保存冲突保持；topology 的真实保存响应丢失提示保持 |
| CE-02 | `test_behaviour_topology.py` 分流关闭、真实图、零模型调用/Run/设置写入/重编译；browser real graphs |
| CE-03 | browser actual publication + reverse HTTP responses；transport failure；disconnection；同 hash 新代际不自动替换 |
| CE-04 | browser protocol injection schema/duplicate/dangling/terminal/missing；真实启动无图、重新发布后恢复 |
| CE-05 | browser 图与文字节点/边集合逐项对比；真实新 builder 的未知节点/分支；后端中文说明覆盖 |
| CE-06 | browser collapse / navigate / restart；真实 P1/P2 同 hash 身份；首次连接身份时序 |
| CE-07 | `aggregation.spec.js` 在模型/记录屏障及真实 202 丢失期间调用 observeTopology，固定请求和禁重投保持，最终核对同一 Run/会话 |
| CE-08 | browser 1280px/390px 纯键盘、全部文字、缩放/适应、同结构保留、变结构重置；STD-01/SPEC-01 两图真实鼠标点选/切换与拖动分离 |

| 验收 | 对应证据入口 |
|---|---|
| AC-01–02 | browser real graphs；Behaviour 双区块文案；原会话/运行入口回归 |
| AC-03–05 | browser real graphs / actual publication；HTTP 中文库存覆盖 |
| AC-06 | HTTP disabled + browser real graphs；routing 配置损坏/冲突；逐图故障隔离 |
| AC-07–08 | actual publication、reverse responses、collapse/navigate/restart、disconnection |
| AC-09–10 | 同 schema/hash 的选择/视口；变结构；重载默认收起；既有持久设置与聚合结果重启回归 |
| AC-11–12 | real graphs + observeTopology 对原表单、保存冲突、pending/recording/202 丢失的原位观察 |
| AC-13–14 | 首次失败/重试、旧图读取失败、真实无图、5 类协议损坏、未知合法图、纯文本注入 |
| AC-15 | 桌面/窄屏纯键盘及完整明细；导航换行避免窄屏横向溢出 |
| AC-16 | 真实 HTTP 零副作用、既有 Host/Origin 拒绝；浏览器零 POST 和零 Run |
| AC-17 | `routing.spec.js`、`aggregation.spec.js`；`test_routing_behaviour.py`、`test_message_routing.py`、`test_aggregation*.py` 等全量回归 |
| AC-18 | 以上源码中的验收 ID；交接冻结 manifest/diff 和绑定候选的原始日志，正常路径与故障注入分别列明 |

运行仓库 CI 门禁：`uv run ruff check`、`uv run python scripts/check_skills.py`、`uv run python scripts/check_env_example.py`、`uv run --extra mcp pytest`、`uv build`、`uv run python scripts/check_mcp_installations.py --output /tmp/mcp-artifacts.json`、`npm run typecheck`、`npm run test:browser`。

本机测试需将 `DEVELOPER_DIR` 指向已安装的 Command Line Tools；SDK 离线测试进程移除本机带 IPv6 条目的代理环境变量，避免 httpx2 的 `Invalid port: ':1'`。浏览器可用 `ALFRED_BROWSER_TEST_PORT` 隔离并行任务的端口。没有付费模型语义评估要求；本机结果不代表 Linux CI 已运行。检查结论以固定候选的原始报告为准。
