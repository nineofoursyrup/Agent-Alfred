# 复现证据与边界

执行：`node /Users/nineofour/.codex/dashboard-migration-20261009/review-pr120-20261009/spec/mainbar-repro.mjs`

- `mainbar-repro.json`：成功执行的两条行为复现，candidate/tree、真实 Session/Run 身份、预期和实际。
- `mainbar-repro.mjs`：独立脚本，启动仓库现有 `tests/browser/server.py`，临时 SQLite/Host/HTTP 与 offline BrowserModel；创建 27 条实际 Run，普通 MainBar 25 条窗口不包括前两条。
- SP-120-02 的两次精确定位、正文和普通历史全部读取真实端点，不合成响应。
- SP-120-01 使用 `route.fetch()` 取得真实响应后只延迟交付；触发 `window offline` 事件执行真实 app 断连处理；没有声称物理断网或实际 SSE 网络故障。先前额外在原有 controlled EventSource fixture 的 error 路径确认同样结果；该辅助结果未冒称原生产品验收。
- `sequential-location.png`、`disconnect-location.png`：场景截图；判定以真实响应与 DOM 身份断言为主。
- `mainbar-repro-first-harness-failure.mjs`、`mainbar-repro-second-harness-failure.mjs`、`first-harness-failure.txt`：保留两次 setup 脚本失败。修复后完成复现；不覆盖失败历史，不将其算作产品故障。
- `fixture.log`：成功复现服务日志。

未调用真实 provider，未使用用户业务状态，未运行全量测试。临时 Chromium 已关闭；服务在 finally 中 SIGTERM 并等到 exit。17971 辅助服务也正常 SIGTERM 退出。最终 `lsof -nP -iTCP:17971 -iTCP:17972 -sTCP:LISTEN` 无 listener；Git 状态为空，HEAD/tree 均仍与候选一致。
