# Dashboard 开发与验证

页面通过现有 Dashboard 服务提供，入口为 `/inbox`，运行页为 `/runs`。
MainBar 属于壳层；页面导航不会新建 EventSource。Session 和草稿存放在
当前标签页的 sessionStorage；新建和继续会话均需显式操作。

生产资源为包内 `ops/static` 下的原生 JavaScript、HTML、CSS，无 CDN 或
运行时前端依赖。页面 CSP 仅允许同源脚本、样式和连接；API 的既有 CSP 不变。

开发安装：

```sh
uv sync --extra dev --locked
npm ci --ignore-scripts
npx playwright install chromium
```

机械门禁：

```sh
.venv/bin/ruff check --no-cache .
.venv/bin/python -B scripts/check_skills.py
.venv/bin/python -B scripts/check_env_example.py
.venv/bin/python -B -m pytest -q
npm run typecheck
npm run test:browser
git diff --check
```

浏览器套件串行执行，启动临时状态目录里的真实 Dashboard、离线模型及 v2
旧消息数据库。重连等边界用受控 EventSource 输入，等待以可见状态或事件为准，
提示计时使用浏览器虚拟时钟。测试端口默认 17736，可用
`ALFRED_BROWSER_TEST_PORT` 指定；已占用时失败，不复用其他服务。

## 运行详情只读补充接口

Issue #36 实施期间经用户授权新增 `GET /api/run-evidence?run_id=...`。
现有 Run/Session schema、HTTP/SSE 载荷、正文恢复和分页接口保持原契约。
此接口读取 Run telemetry；仅在持久 Run 已 finished 后读取同一 Run bundle 中的历史 trace，返回文本快照、
非文本块类型和用量事实；不返回 thinking 正文、工具参数、工具结果和 usage.raw。
重新使用当前中央 Redactor，读取过程不改变任何 Run 或记录状态。

活跃及 pending Run 返回 `trace_status=live`，过程仍来自既定 SSE ReplayRing，
不从磁盘回放。已恢复为 interrupted 的历史 Run 即使缺少 telemetry 也不称为实时。
Token／费用只取持久化 Attempt 账目；缺失过程时以独立账目区呈现，
明确不推断 Step／Attempt 发布顺序。

历史 `trace_status` 区分 available、partial、pruned、unavailable 和 too_large；
读取上限为每 Run 32 MiB，超过时明确不加载，不返回截断冒充完整过程。
它与持久 `trace_incomplete`、`outcome`、`recording_state` 分开显示。
缺失 trace 不推断 Run 失败，也不凭 trace 推断已保存。

现有模型提供有效的端点金额时呈现 exact；当前未安装价格解析器，
没有金额及价格依据时返回 unknown。界面可呈现已解析 estimated 费用与逐维
price_source/stale，但不会自行编造单价或从缺失 Token 推断零。
