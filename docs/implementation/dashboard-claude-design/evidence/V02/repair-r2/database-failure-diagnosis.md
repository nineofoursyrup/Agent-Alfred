# 首次完整浏览器失败的只读诊断

Reviewer: `/root/v02_ci_readback`；source SHA: `067f483c7629c36c5ac18ce82be4de4796498158`；未修改文件或运行测试。
Case: `database_migration-390x844-08fca-remain-reachable-at-390x844`。

直接原因：DOM 进入连接中断状态后，既有保护契约清空受管查询结果。上游原因 UNKNOWN，无法确认物理 SSE 断开。

- MainBar 打开后 `2026-10-11T10:45:18.013Z`：仍有 6 个 TD，末格保留 LAST-VALUE，状态 complete。
- `10:45:18.088Z`：TD 变为 0，状态 unavailable／不可用 · 连接中断。
- 查询 cancel POST 于 `18.086Z` 发起，最终返回 200。`/api/events` 最终记录含 `net::ERR_ABORTED`，但 receive=-1 且无中止时刻，不能据此确认物理 SSE 断开或原因。
- 清空在断言失败前约 4 秒发生，属于连接失效处理，并非测试 teardown。

app.js 的 Stream disconnected 回调调用 database.js 的 disconnect()，继而 retire、cancel、clearResult()。没有额外页面导航或查询重执行；结果在打开面板后短暂仍存在，证据不支持面板切换立即重挂载清空。

新 CSS 在 390×844／无创建回执时不命中。现有 trace 没有保存 SSE body／服务器关闭原因，服务日志也没有此处 exception；不能据此称为 flake 或声称已定位上游原因。单独不变原用例 1 passed 只作为诊断，不当作稳定性结论或完整门禁结果。

首次失败全部保留在 full-first-failure/。保持原断言、timeout、retry，以相同候选重新运行完整门禁；不越界改变 Database 的连接失效规则。
