# S11 CI repair r2 — 独立 Spec 复核

候选 `4da39e52194e62f0f23637a83c8556069eb6a07d`，src tree `598ca385e9c873a2f67815e0f5c638b6b4b85ffe`，package tree `8496b4a454f9a84b9a1f0b6c8c507e0e0387e1b4`。与更新后的 REPAIR-HANDOFF 一致。完整 37cd→4da 六文件逐行阅读；b214→4da 只有 app.js 与 session-create-race.spec.js，另四文件与独立 r1 审查逐字节相同。

## 正式结论：FAIL，首次挂载前导航的退休遗漏

本报告在签发前追加验证 owner 提出的真实边界：首次 SSE 尚未接受、中央 controller 尚不存在时，用户已可点击 Mainbar 核对并进行业务导航。原暂存的 scoped PASS 未交付、root 未采纳，主报告／详情／JSON 均原样保存在 `S11-ci-r2-pre-probe-report/`，含 SHA256 manifest；这次正式 FAIL 不改写那个早期判断。

固定 `shell.js:181–187` 的 mount 会先推进 navigationGeneration，但 `!started` 时直接返回，无法调用尚不存在的 controller.dispose。`app.js:805` 的正常页面 dispose 是现有退休入口；首次 mount(true) 同样没有前 controller。旧 `locateReply` 在 `app.js:316` 发现 owner 不同只返回 retired，locationTarget 仍 loading。

owner 执行的外部 `startup-navigation-probe.spec.mjs` 使用真实 Session、实际 Run 及已保存正文；reload 时只令普通历史一次失败，分别暂缓首次真实 SSE 与成功定位 HTTP 200。核对开始后、首次 mount 前点击真实 Database 导航，再释放 SSE、等待 Database 标题、编辑 `SELECT 73`，最后释放旧定位。实际 4 秒后 `#reading-status` 仍为“正在定位指定记录…”，1 FAIL。完整 probe／config／log 已读，测试没有预填正确业务结果。此前已 mount 的 retired-location PASS 不覆盖此路径。

依据 I06（142 行）真实离页退休读取、I07（163 行）有效动作／已退休读取边界及 #89 D08（163 行）离页后迟到结果不可影响新意图。正确退休不能依赖旧响应最终是否返回：应在导航确认提交时立即清 loading、abort 原读取；在释放 held 200 之前就必须能断言清理。取消导航、同目标和首次自动就绪继续保留有效定位。旧响应随后到达必须不能清理 newer locate 或抢新输入焦点。只在 stale-response 分支清状态不满足服务不返回时的退休语义。

直接受影响是 S02 壳层／Mainbar navigation retirement 的 AC05／AC06／AC07 接缝；这不是 r1 触控 finding 复发。此新增观察与更早原始 FAIL 分别保留，后继须另固定再审。

## r1 P2 关闭

原 b214 的真实 Chromium touch 顺序为 pointerup→touchend→mousedown→focusin，因 pointerup 清 token，opener 的兼容 focusin 被误当独立阅读；真实 201 已成功却未采用 Session，FAIL 保留。

4da 在 `app.js:206` 接收真实 mousedown，并只取该事件目标所属 `[data-open-panel="mainbar"]`；focusin 必须仍是同一 opener 才豁免。其后 focusin、pointerup、pointercancel 或 mouseup 清除标记。独立 focus 无激活标记，照常推进 creationIntent；mouse／touch 的其他目标会清标记，不因相邻区域名字而豁免。shell 的 automaticMainbarFocus 仍局限于 openPanel 同步调用，并通过 finally 撤销。

`session-create-race.spec.js:202–231` 将同一真实 held-201 行为参数化为 click／tap，tap 明确 hasTouch。两者均覆盖无原 Session 和有原 Session；读取真实创建 ID，再断言实际采用、输入可编辑且空、无 deferred receipt、原 Session 草稿仍在、一份存活 Stream 和仅一笔 Session POST。没有将新 Session／正文／状态写成假结果，原业务断言没有减少。

`affected-green-04.log` 末尾四项分别为 click false／true、tap false／true，全 PASS。24 项集合同时保留独立 opener.focus、主对话阅读焦点、关闭、Tab、导航、新编辑、另一标签真实 Run、进程更换与窄窗回执的保护。因该独立失效方式已由真实 touch RED 和固定后继 GREEN 闭环，r1 P2 可关闭；只报告 Chromium 触控模拟，不扩大到真机或 iOS。

## 完整增量的其余约束

- 精确定位继续按完整实例／Session／Run、请求代次和用户动作接受；仅把 owner 从首次 mount generation 改为真实 navigationGeneration。真实离页／恢复仍退休，初次自动就绪不再静默丢掉同页动作。空身份用例保留普通 history 一次失败、真实 HTTP 200、先自动挂载后释放定位响应、身份／正文／recorded／缺口及原草稿验证。
- Inbox 测试增加接受当前 Stream 的屏障和显式 selectSession 的 true／实际选中 A 断言；产品未同步切换守卫未动，原 A 草稿断言保留。
- reconnect 测试先等 domain 解码与排队历史刷新完成，再计重连读取。原一次读取、其他 Run 不证明目标保存、后续明确历史核对才确认的断言完整保留；无固定 sleep、放宽请求次数或跳过身份。
- 创建继续绑定原实例、Session、sessionGeneration、navigationGeneration 和 creationIntent；新建成功回执不可自动重投，另一 Run 或进程更换不能被 opener 豁免覆盖。readingIntent 仍逐事件推进，定位晚到时的焦点保护未取消。

规范与 frozen source owners、HTTP／SSE wire、Python、依赖、Playwright config、CSS／页面路由不在本次差异中。两段 git diff --check PASS。完整测试源码及相邻生产行为已在 r1 审查，后继重新读全部最终差异，不读取本轮另一轴报告。

## 证据及适用性

核验 REPAIR-HANDOFF 的 17 个证据文件 hash，固定 6 个文件 hash、head／tree／src／package tree。当前结果：affected-green-04 **24 PASS（28.3 秒）**，typecheck-03 PASS，python-assets-protocol-02 **29 PASS**。另全文读回固定后继 `stability-02.log`：原 6 条 CI 场景加 4 个 click／tap 创建场景，各重复 5 次，**50 PASS（59.9 秒）**；日志单独记录 hash。既有确定性通过不重复执行。

历史只保留不替代：原 PR 522 PASS／5 FAIL、Push 525 PASS／2 FAIL，创建 2 RED、首次挂载 1 RED、独立 opener-focus 初修 1 RED、close case locator 首败、b214 touch 1 FAIL、b214 完整 browser SIGINT／exit130，以及 b214 40 次重复。r2 正式结果因 startup navigation probe FAIL 为 FAIL；24／50 PASS 不冲销它。仍未封存的全量 browser、最终安装／native200／G08／CI 也不能算作通过。真实 IME BLOCKED 不因新修复取消。

验收账本建议：只能为已验证的单独浏览器责任追加后继通过，保留首败和旧 PASS；startup navigation 子责任必须另记本次 FAIL。D04/D08 的 S03 若因最终组合记 FAIL，明确它是组合证据受影响而非已证实 S03 实现错误。D09 只有关联来源，没有独立确认的原 CI 批次失败，不因原 diagnosis 扩大 FAIL；另有 JSON 补充而不改原诊断。R07 聚合仍受未解决 IME 子责任限制，不能直接整体 PASS。
