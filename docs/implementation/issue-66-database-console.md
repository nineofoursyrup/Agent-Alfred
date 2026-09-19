# #66 Database 只读 SQL 控制台：实现与验收入口

规范源：[已确认的 #65 Resolution r1](https://github.com/nineofoursyrup/Agent-Alfred/issues/65#issuecomment-5686473812)。发布正文 SHA256：`b08f28c7be5458730d6a283fc07b2acbd5bda334268078e5f3192ff9c25d86e3`。此页只索引实现和验收，不另立产品决定。

## 实现

- Dashboard `/database` 与 MainBar；21 个批准的 `diag_*` 对象；手动执行、类型保真、内存分页、结果与草稿的页面生命周期。
- `database_console/`：独立受控进程、只读源事务、逐行保护、完整对象准备、SQL 授权、输入与结果预算。
- worker、IPC 解码结果、HTTP 编码及发送之间保持连续清理责任；取消、遗忘、保护变化和关停均等待实际释放。失败时保留 owner 并暂停准入，实际清理及能力核验后恢复。
- 必要 schema 包括派生列依赖；内容装入 Python 前检查源值及完整源行；核实 SQLite TEMP_STORE 构建选项与生效设置。
- SQL 引用发现用 SQLite EXPLAIN 编译与 authorizer，不执行表达式。最终查询用同一 SQLite 库的 `sqlite3_step` 逐行读取已保护的共享内存数据集，避免 Python DB-API 预取超出行数／字节截断探查；保留原投影的重复列名。
- 参见 [ADR 0040](../adr/0040-protected-diagnostic-dataset.md) 和 [Dashboard 使用说明](../dashboard.md)。

## 验收索引

测试均使用离线模型；不调用付费模型。源文件位于 `src/agent_alfred/evals/deterministic/`，浏览器用例位于 `tests/browser/`。

| AC | 可复现证据入口 |
|---|---|
| 01–05 | `test_database_http.py`、`test_database_console_objects.py`、`database.spec.js`：目录、真实对话持久化、精确对象字段、空结果和动态类型 |
| 06–10 | `test_database_console_sql.py`、`test_database_http.py`、`test_database_boundaries.py`、`test_database_lifecycle.py`：完整性、EOF、UTF-8 正文预算及真实 worker 的第 1001 行取消／超时／错误 |
| 11–16 | `test_database_http.py`、`test_database_console_sql.py`、`test_database_console_objects.py`：源事务快照、只读、SQL 边界、预先保护、Attempts 覆盖与成本 |
| 17–22 | `test_database_http.py`、`test_database_lifecycle.py`：单任务、句柄期限／容量、阶段取消、故障恢复、完成顺序、写入优先、spawn／结果交接竞态 |
| 23–25 | `test_database_lifecycle.py` 的真实遗忘阶段参数；`test_database_http.py` 的发送清理屏障；`database.spec.js` 的真实 SSE 重连、遗忘及迟到 HTTP 结果、离页／offline／BFCache |
| 26–29 | `test_database_http.py` 的 HTTP 安全、IO 总期限、资源清理；`test_database_boundaries.py` 的热 journal 拒绝恢复及内容哈希；`test_database_lifecycle.py` 的实际进程停止失败与关停 |
| 30 | `scripts/check_mcp_installations.py`：wheel／sdist 各 base／mcp 隔离安装，从 site-packages 启动完整 Dashboard，真实 HTTP 查询及 worker 回收，无模型调用 |

强制临时文件构建已另行实测：从 [SQLite 官方 3.53.4 amalgamation](https://www.sqlite.org/2026/sqlite-amalgamation-3530400.zip) 编译 `-DSQLITE_TEMP_STORE=0`，源 ZIP SHA3-256 为 `628a44cfe82c66aed1ccbbe85a562d2e33ebe64b3288981ed76285612227934e`。真实库报告 `TEMP_STORE=0`，即使 `PRAGMA temp_store=MEMORY` 回读为 2，公共能力检查仍拒绝；把该真实只读连接接到 Dashboard 诊断入口后，目录 available=false，签发句柄 HTTP 503。正常 CI 用模拟该不可变编译选项的回归测试，不在 CI 下载或编译 SQLite。热 journal 场景则通过真实子进程未提交退出构造，验证只读诊断没有改写数据库／journal。

## Python 构建能力

Python 3.14 版本号本身不足以保证 Database 可用。该构建还必须让 `_sqlite3` 暴露同一 SQLite 引擎的公开 C API（包括 `sqlite3_hard_heap_limit64` 和逐行 cursor API），支持共享内存数据集，并通过已有 JSON、函数及内存临时存储核验。128 MiB 限额覆盖 worker 中该引擎的所有连接；不会另找一套 SQLite 库绕过能力检查，也不向 worker 继承父环境。

CI-01 在 Linux 的 uv CPython 3.14.7 上实测 `_sqlite3` 为内建模块且进程不导出所需 C API；该构建的目录正确返回 `available=false`，句柄签发返回 HTTP 503 `database_unavailable`。这不代表所有 uv 提供的构建都相同，也不承诺所有 CPython 3.14 构建均受支持。不支持的构建继续明确不可用。

CI 使用 `actions/setup-python` 返回的精确解释器路径创建环境，能力仍由真实 HTTP／worker 检查验证，不能仅凭安装工具名称判为可用。

本地可用 `uv sync --python /absolute/path/to/python3.14 --extra dev --extra mcp --locked` 明确选定满足上述能力的构建，再运行下方门禁。发行包的四组隔离环境使用执行检查脚本的同一个 Python，以免安装检查隐式换用不同构建。`test_hard_heap_limit_applies_only_in_worker_process` 通过空环境子进程验证 Python 写入的共享内存行可被 C cursor 读取、两端限额均为 128 MiB、临时存储为内存且父进程限额未变。

## 完整本地门禁

```sh
uv sync --extra dev --extra mcp --locked
uv run ruff check
uv run python scripts/check_skills.py
uv run python scripts/check_env_example.py
uv run --extra mcp pytest
uv build
uv run python scripts/check_mcp_installations.py --output /tmp/issue-66-artifacts.json
npm ci --ignore-scripts
npm run typecheck
npx playwright install --with-deps chromium
npm run test:browser
git diff --check
```

Node 使用 22。所有浏览器检查保持 retries=0。交付范围为本地 diff、测试和独立复审；提交、推送、PR、合并及关闭 Issue 另需明确授权。

本机继承的 `NO_PROXY` 含 httpx2 不能解析的 IPv6 项；离线门禁对子进程显式设置 `NO_PROXY=* no_proxy=*`，不修改用户全局环境。完整首次检查由此暴露的既有 Anthropic 夹具错误，经客户端构造单变量验证及 34 项夹具检查确认后重新执行全量。

完整浏览器门禁还暴露了既有 Memory R08 夹具竞态：在实际点击前放开自动回读，会先显示成功回执并移除查询按钮。仅把测试回读门闩移到真实 DOM 点击，不修改 Memory 产品逻辑、超时、重试或断言；两个分支各复验 5 次通过。

## 独立评审修复

- SPEC-02：目录迁移时间也先校验源类型、UTF-8 和大小，再经过当前保护规则；发布前核对保护版本，变更即丢弃元信息。记忆修订只接受非负整数，损坏源值不作字符串回显。
- SPEC-03：排除出展示的已知消息块仍完整校验字段。Thinking 的 signature 和 ToolResult 的 is_error 保留合法可选缺省；合法无 TextBlock 仍返回空文本。WHERE／LIMIT 不能隐藏引用对象中的损坏行。
- SPEC-04：CAST 的表达式与 AS 后的类型声明分别处理，允许 SQLite 长度／精度语法；表达式仍受闭合函数名单及实际 authorizer 限制。
- SPEC-11／STD-05：执行、句柄、取消意图、状态探查和异步回调绑定同一执行身份与页面世代。句柄晚到先取消，不执行 SQL；旧取消／状态响应不改新查询。页面 pagehide 发起有界取消并清空本页数据。
- SPEC-10：真实 Chromium reload／close 对实际受阻 worker 验收，核对 PID 消失、源库写锁可取得及所有响应 owner／缓冲释放；另以浏览器真实 offline／online 验证恢复不重跑。真实 TCP 小接收窗口耗尽发送期限，修复超时后再次发送错误响应而额外占用一秒的问题。

新增反例入口：`test_database_review_repair.py`、`tests/browser/database_cancel.spec.js`、`tests/browser/database_lifecycle.spec.js`；`tests/browser/database_server.py` 只控制现有 worker FIFO 测试接缝，不替换 SQLite、HTTP、SQL 或取消结果。

### 终态回读与原生浏览器验收

STD-06／SPEC-12：状态回读分别解释执行 outcome 和 cleanup。执行失败不推断为清理失败，超时、失效、完成但结果未收到均保留本义；cleanup=pending 明示清理中，只有 cleanup=failed 才显示清理失败。`database_status.spec.js` 用真实400／504／200丢响应和真实worker停止失败覆盖主要路径；其余组合为明确标注的补充展示矩阵，原执行身份和页面世代检查保持。

`tests/browser/database_native_lifecycle.mjs` 在隔离用户目录启动原生 Chromium，以 `connectOverCDP(..., {noDefaults:true})` 连接，避免 Playwright 的默认焦点仿真和禁用 BFCache 参数影响验收。实际切换标签产生可信 visibilitychange；隐藏时结果／草稿保留，实际worker仍按原五秒预算超时。该脚本不改页面属性、不人工派发事件、不修改服务头。

当前 Chromium 153 的 Database 真实后退触发重新加载，浏览器给出的 BFCache 拒绝原因为 MainResourceHasCacheControlNoStore、JsNetworkRequestReceivedCacheControlNoStoreResource 和 BrowsingInstanceNotSwapped。相同浏览器的独立可缓存对照页成功保留同一文档并触发 pageshow.persisted=true。Database 的真正缓存恢复分支据此仍为 **BLOCKED，不能记 PASS**；可供判断的替代依据是实际后退重载清空草稿／结果、重新核验目录且不重跑SQL，并保留原有 persisted 事件处理回归作为补充。是否接受该替代依据须另行判断，产品 no-store 合同不变。
