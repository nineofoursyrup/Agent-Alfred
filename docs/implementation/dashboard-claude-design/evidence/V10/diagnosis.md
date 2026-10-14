# V10 首次失败与修复

首次定向批次（01-target）为 90 passed / 12 failed，225.262 秒，exit 1；源码身份见原始 JSON inputs。只归档失败用例自身的 error-context、test-failed PNG 和 trace.zip。

## 确认的页面 CSS 回归

`database_lifecycle.spec.js:77` 和 `database.spec.js:375` 的 `getByRole('region', {name:'已提交查询'}).toBeEmpty()` 找不到元素。Trace 和 ARIA snapshot 显示查询结果与提交关联已经清空，草稿保留，状态已恢复可执行。原因是新增 `.db-submitted:empty {display:none}` 把原本保留的空 named region 从可访问树移除。这与业务清空失败不同。

预测：取消空 region 的 `display:none`，仅将边框、底色和 padding 限于 `:not(:empty)`，原断言应通过。最终 CSS 正是这一修复；没有改 JS 状态逻辑、定位器、断言、timeout 或 retries。原样复测两例均通过。

## 清理首错、teardown 超时与级联端口冲突

`database_status.spec.js:95` 的 STD-06/SPEC-12 场景先实际得到 `cleanup=failed`、新句柄 HTTP 503；这是预期的注入故障。原始 trace 中，随后 `database_status.spec.js:123` 的 `recovered.cleanup` 仍为 `failed`，与期待的 `released` 不符（见 cleanup-timeout-timeline.json）。接着 finally 的 server.close 未在整体 15000ms 内完成，最终命令输出报告 test timeout。

同一 fixture PID 51710 之后为 PPID 1，并在 `127.0.0.1:18104` 上 LISTEN；lsof 的 cwd 为本 worktree，命令为 `tests/browser/database_server.py --port 18104 --state .../alfred-memory-browser-UppvPE`。其后九个用例（含 SSE 恢复、延迟删除、两种回执丢失、两种空身份、隐藏 Memory/Database）启动 fixture 时失败，原始 exception 为 `OSError: [Errno 48] Address already in use`。SSE 恢复这次没有进入恢复业务断言，因此此失败的直接原因是 fixture 端口被遗留进程占用。

再次检查进程准备清理时 PID 已自行退出，未实际发出 kill。没有修改共享服务、fixture 或生命周期测试。十二个真实失败项以原测试、原断言单独复测为 12 passed，28.080 秒。清理恢复为何第一次未获 released 的上游原因仍为 UNKNOWN；独立通过不是根因证明，也不替代完整浏览器门禁。

## 临时取证辅助器失败

原稿首次截图已成功加载 unpkg React 18.3.1 / ReactDOM 18.3.1（HTTP 200）并切到 Database。几何辅助器错误地假定 SQL CONSOLE 文本位于无子元素的 div，找到 undefined 后读取 parentElement 失败；改为已有可访问文本 locator 的 evaluate 后四视口完成。实际导航可访问名称为 `Database state.db`，继续使用前缀定位。

产品前两次取证将 Playwright 留下的两个空 `style=""` 属性算成产品行内样式，临时断言得到 2。记录中两者为 `textarea#database-sql` 和 `textarea#message`，均没有 CSS 声明。生产源码不写 style 属性或 `.style`；后续统计有效行内 CSS 声明，结果均为 0，空属性另行留在 JSON。

第三次产品取证完成前三个视口，320px 首次查询没有得到预期的六个 TD；原始 partial JSON 和命令输出保留，现场未记录足够信息确认上游原因，标 UNKNOWN。辅助器随后等待真实入口 HTTP 响应完成，并等待长值查询清理已释放后才关闭前一个 context；这不修改产品、测试或超时。第四次完整四视口取证通过，没有自动重试或重新发起失败查询。

## 首轮完整浏览器与 SSE 回归调查

03-browser：542 passed / 3 failed，1559.741 秒（26.0m），exit 1。失败为 Database SSE 恢复、Memory G2 D09 queue reload (committed=true)、Routing Statistics CE-03/08/14；三者各自的 error-context 与 trace.zip 已保存。运行器没有生成这些用例的失败 PNG，未补造截图。04-browser-failures 原样独立执行实际三个失败项为 3 passed（19.110 秒）；没有改测试、重试、超时或非归属页面。

Database 这次进入真实 SSE 恢复流程，最后的第二次显式执行等待 `0 行` 失败，因此按本票回归调查。原始 HTTP trace 显示第二次 execute 在约 584.604ms 内 HTTP 200，returned_rows=0；实例、记忆修订 46 和保护版本 5 均与当时目录匹配；随后 status 回读 completed/released，目录仍为同一版本。故不是源库返回非零或准备预算耗尽。失败窗口的 error-context 显示 working/等待正文，断言结束后的 frame snapshot 已出现“空结果”“返回完整”“已收到查询正文”。HTTP 原文抽取与快照时序分别在 sse-first-full-http.json、sse-first-full-snapshots.json；后者为 trace 的压缩快照文本片段，初始快照中的“0 行”也可能来自目录“1000 行”，不能将它当作已显示空结果。

调查优先级：浏览器观察／任务时序；其次是正文归属或版本退休。版本一致、已进入后续 status/catalog 回读、后续快照真实呈现空结果，削弱持续版本失配和永久 CSS 隐藏的解释，但不能证明首失败的具体根因。

临时 .scratch 的 passive DOM MutationObserver 与 HTTP response 观测复用原始 database.spec.js 的测试函数：最终版式 3/3 passed（05，28.225 秒），通过只读浏览器路由提供基线 Database JS/CSS 的对照为 3/3 passed（06，28.390 秒）。服务、临时数据、SQL、原断言、15000ms 整体超时、4000ms expect、workers=1 和 retries=0 不变。所有样本的第二次查询都观察到 empty 与结果 metadata；server.close 后的 invalidated/unavailable 是 teardown 记录，不能当成主体失败。两组均未重复首错，因果关系仍 UNKNOWN，未声称 SSE 已被业务修复或排除时序回归。原始观测在 sse-observations-final.json、sse-observations-baseline.json。

## 第二轮完整浏览器

07-browser：543 passed / 2 failed，1139.354 秒（19.0m）。第一轮 SSE / Memory / Routing Statistics 的三项在此轮通过。新失败为未改的 Behaviour S06 CE-28 320px 空范围说明等待，以及 Database empty SELECT 场景在填写下一条 `SELECT FROM` 时触发 15000ms 整体超时；之前 HTTP 200、`0 行` 与列名 `n` 断言均已完成，不能描述为“空 SELECT 没返回”。各自原始上下文／trace 与实际生成的 Behaviour 失败 PNG 位于 second-full-browser-failure。08-browser-failures 原样独立复测为 2 passed（5.830 秒）。未修改 CSS／JS／测试或超时，具体上游时序原因仍 UNKNOWN。

## 首次完整 Python

03-pytest 实际执行完全部 6445 个选中项，6440 passed / 5 failed / 1 deselected / 15 warnings，测试摘要耗时 5215.43 秒；进程在摘要后滞留 `_thread._shutdown()` joining threads，最终针对本 worktree 的 pytest PID 62587 发出 SIGINT 后退出码仍为 1。命令总耗时 5526.675 秒，包括摘要后等待／收尾；没有把全部执行过的失败检查称为通过。进程验证、shutdown sample 和最终 KeyboardInterrupt 都保留。

首个 `test_writer_priority_stops_source_extract` 的 HTTP 客户端线程 `sock.recv` TimeoutError 导致 result 缺少 query；随后 `dashboard.close()` 返回 False。另两项 Database 生命周期失败分别是 SQL 阶段遗忘后的线程未在 join(2) 后结束、注入 kill 失败恢复后进程已 -9 但 console.released() 仍为 False。后续 Host 生命周期看到全局仍有 run worker，Trace Sink 看到残留 trace-drain。后两项是否由早先释放失效导致跨用例污染属于有依据的假设，尚未证明，不当作根因结论。

这五项不加载 Database 前端 JS/CSS；本票没有改 Python 实现、测试、fixture 或配置。会按原测试与原断言独立复测，并以新完整 Python 门禁结果决定 PASS；独立通过不会替代完整 gate。

09-python-failures 原样独立复测五个实际 Python 失败项为 5 passed（10.844 秒），未改被测代码／输入／fixture／断言。单独通过不替代 full gate。后续完整重跑串行执行，本票不同时运行浏览器套件。

10-pytest 在同一实现源码、依赖、配置下串行完整重跑通过：6445 passed / 1 deselected / 14 warnings，测试摘要 2757.90 秒。首轮五项失败及摘要后滞留记录仍为 FAIL 历史证据；没有用独立复测替代全量。串行重跑通过支持检查时序／资源释放失效具有不稳定触发条件，但没有足够证据把并行负载直接当成已确认根因。

12-browser 串行完整重跑通过全部 545 项，11.7m。此前两轮各自 FAIL 及原样独立复测记录均保留，最终 gate 只取12的完整通过记录；无需修改业务、非归属页面、断言、retries 或 timeout。
