# S11 CI repair r3 — 独立增量核验

候选 head `06588168d5049040abf0a4560091d659d244ddcf`，tree `ae04fad8eff5f0eed6afb06050e4afeb6707506c`，src tree `0eaae1fbd39ec02656f4a61982ec73a06c46fe2f`，package tree `91f1c1abcc26ea92be40fb5acee5fb93fbd62a0c`。与更新后的 REPAIR-HANDOFF 一致。已完整审查 37cd→4da 的六文件，再逐行审查 4da→065 的 app.js、shell.js、mainbar-empty-identity.spec.js；其余三份最终测试文件与前次相同。没有产品、规范、canonical、Git 状态或 GitHub 写操作，没有新增服务或重复测试。

## 退休时序及 r2 P2 关闭

`shell.js:181–183` 对非 initial mount 先推进 navigationGeneration 并调用 onNavigate，再判断是否 started。`app.js:808–809` 把通知接到现有唯一 Mainbar 的 retireLocation，移除旧页面 dispose 对全局定位的重复退休。这覆盖 controller 尚不存在的真实导航，也保持正常页面切换／恢复的原行为。

已经逐条检查触发路径：普通 navigate 的同地址在 200 行返回、取消在 203 行返回，均不进入 mount；Back／Forward 取消只恢复历史，亦不 mount；同文档面板历史只 present；首次 start 调用 mount(true) 不通知。确认业务导航、接受的跨页 popstate 和真实 restore 才进入非 initial mount。通知发生在路由确认之后，旧页面释放之前，取消不会误退休旧有效请求。

`retireLocation` 先推进 locateGeneration、abort 并清 controller，再清旧 loading。旧请求的 catch 首先检查自己的 generation／aborted，因此新请求启动后，旧请求的取消回调不清新状态。正文、记录身份、Session、navigationGeneration 和 readingIntent 的原检查保持；自动首次 mount 不再被误作导航。

符合 I06:137/138/142 的导航／显隐／退休边界和 I07:163、#89 D08:163 的有效动作与迟到响应约束。r2 的新 finding 可关闭；r1 触控修复的代码未被本次改动，既有 click/tap 与独立焦点保护继续存在。

## 真实 RED/GREEN 与证据限度

完整阅读 `startup-retirement-red.log`：same-page PASS，navigate 在释放首 SSE 与旧定位响应之前、49 行的“不得仍正在定位”断言 FAIL。它直接验证立即退休，不能由仅在 late-response 分支清理的修复通过。

最终永久用例包含两条互补路径：

- same-page：真实空 Session／Run 的定位 200 暂缓；先首次自动挂载，再释放成功响应；handler 确认 delivered，仍断言 present-empty query、单正式记录、完整正文、recorded、独立普通历史核对、原草稿及 0 POST。
- navigate：首 SSE 与旧定位 200 都未释放时确认真实 Database 导航；立即检查 loading 消失及一次 requestfailed。随后初次挂载完成，再发一个新显式定位；它仍 pending 时编辑 SQL，并结束已取消的旧 handler，新 loading／无旧正文／SQL 焦点仍有效；最后释放新真实 200，要求 applied、正确请求与已保存事实、原草稿、SQL 焦点、两次显式定位读取和 0 POST。

外部原 probe 的旧 `route.fulfill` 有 catch，不能证明旧 200 已投递。永久用例明确证明的是实际浏览器取消、旧 handler 结束不影响新 pending、以及新请求成功应用；它不依赖对已取消旧响应的投递作虚假断言。

`startup-retirement-green-01.log`：16 PASS（16.5 秒），包括上述两路径、已挂载导航退休、迟到来源焦点、Inbox、reconnect、初次挂载的 unchanged／navigate／focus／close／reading／opener-focus 及四个 click/tap 创建组合。typecheck-04 PASS；python-assets-protocol-03 29 PASS。新证据已覆盖此次改变的独立失效方式，未重复未改变的全量验证。

## 身份、历史与后续责任

25 个 handoff 证据 hash、6 个最终文件 hash、head／tree／src／package tree 全部核验；37cd→065 与 4da→065 的 diff --check 均通过。规范、依赖、Python、Playwright config、CSS 和既有 HTTP／SSE wire 未变化。旧 r1/r2 报告、真实首败、r2 未签发初稿 manifest 继续保留；4da 完整 browser 的 SIGINT／exit130 仍是未完成，不能记 PASS。

本次 r3 是代码／测试 Spec PASS，不是整个 S11 的验收裁定。完整最终 browser、安装后的所有资源字节、同一状态 G08、最终 CI 与后续可携带文档需分别固定并核验。此次仅改定位退休时点，不改变 root 已验证的原生 200% 控件／发送／CSS 路径；其适用性可继承，同时最终资源身份仍须核对，本报告不声称重新执行原生测试。真实 IME 仍 BLOCKED，不因本次修复变成 PASS。
