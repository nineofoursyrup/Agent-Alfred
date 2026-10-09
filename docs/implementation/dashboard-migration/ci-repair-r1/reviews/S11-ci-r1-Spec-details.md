# S11 CI repair r1 — 独立 Spec 核验

固定 head `b21451fb4674ce693cec2d53d16bc94647fa4965`；src tree `a93659a482b70122d5b85c80061832ba71413c67`；package tree `65f5f9ff76762fdafefbc92c86b785330673b324`。与 owner handoff 一致。完整差异仅 app.js、shell.js 及四个 browser tests，87 additions／12 deletions；规范、依赖、Python、原 canonical 未改。6 个文件 hash、11 项 handoff 证据 hash 与额外终态日志已独立核验，固定差异检查通过。

## P2：tap 的兼容焦点晚于 pointerup

规格：`docs/design/dashboard-implementation/INTERFACES.md:138` 要求 openPanel“仅改呈现”；`docs/design/issue-89/DESIGN.md:66` 要求“点击新建成功后打开新 Session 的 MainBar”。#87 R03／R10 分别保持呈现与会话选择分开、显式打开的焦点可达。没有导航、新编辑、会话改变或独立阅读时，同一 Mainbar 的打开属于兼容原新建的呈现动作。

候选 `src/agent_alfred/ops/static/app.js:204–215` 在 pointerdown 设置 `mainbarPointerOpener`、pointerup 清除；仅该 token 匹配时豁免 opener focusin。`shell.js:169–174` 的 automaticMainbarFocus 只覆盖点击处理器后主动聚焦目标，不能覆盖浏览器先聚焦 opener 的兼容 focusin。

由 reviewer 基于真实事件顺序提出风险，owner 执行现有真实服务＋成功回执屏障 probe，未替代业务执行：Chromium、`hasTouch:true`、1440×900、`locator.tap()`。先正常创建 Session，真实 POST 201 暂缓；tap 打开同一 Mainbar，再释放成功回执。记录的实际事件顺序为：

`pointerdown(mainbar) → touchstart(mainbar) → pointerup(mainbar) → touchend(mainbar) → mousedown(mainbar) → focusin(mainbar) → focusin(mainbar-title) → click(mainbar)`。

结果：服务生成 `6d5d795a8f464273891109c1aaaa572a`，客户端所选 Session 仍 null，等待 4 秒后 FAIL。普通鼠标 click 的 focusin 较早，因此原 22／40 通过不覆盖此独立失效方式。证据为 `S11/ci-r1/touch-probe-01.log`、`.config.mjs`、`.spec.mjs` 及其 trace。范围仅 Chromium 触控模拟，不声称真机／iOS 验证。

修复应只识别同一 opener 激活引发的兼容焦点，继续拒绝独立 keyboard/programmatic focus；不得全局忽略 focusin 或将所有 pointerdown 当呈现。建议在原真实 held-201 用例加入 tap，保持已有／无原 Session 两种归属及草稿、单 POST／单 Stream 断言。owner 已开始后继，当前固定候选仍 FAIL。

## 其他增量的适用结论

- 创建回执：现有 mouse 新用例 2 RED → PASS；新增独立 opener-focus 捕获过宽初修 1 RED，最终受控 guard 用例通过。真实新编辑、外部 Run、进程替换、导航、主动 Tab、关闭 Mainbar、阅读标题及独立 opener.focus 均保留回执，不自动切换；既有输入／选区／键盘可达与一份 Stream 断言没有删除。
- 首次挂载精确定位：真实空 Session／Run HTTP 200 被 held response 保留，同时释放首次真实 SSE 使自动挂载先发生；原产品 1 RED，修复后完整身份／正式正文／recorded／独立普通历史及无 POST 断言通过。改用已有 `navigationGeneration`，仅排除首次自动挂载；真实 navigate/restore 仍推进 generation，页面 mount 仍主动退休定位。已有实际导航后无 loading、无迟到正文、SQL 焦点保留的用例通过。
- Inbox：在跨 Session 精确定位后等待原 runtime 接受当前 Stream；显式继续返回 true、sessionStorage 实际为 A，最后仍要求 A 原草稿。守卫产品未放宽，测试不再忽略 false。
- reconnect：先观察已解码的正式回复，再等历史控件恢复。`loadMessages` 的 finally 在同一同步任务中启动排队 refresh 并重新 disable 控件，故此屏障不把中间瞬间当作全部完成。计数 route 之后仍必须仅一次 reconnect read；其他 Run 不能证明本 Run 保存，后续显式精确身份历史回读才显示已保存。未改为两次、未删除原业务断言、未加固定 sleep。

所有已保存 RED 和中间失败已读。affected-green-02 的 close case 失败是隐藏区域用 accessible-only textbox locator 无法找到；最终改用同一 `#message` 并新增区域隐藏断言，保留 disabled／Session／焦点断言，适合所测隐藏行为。

## 终态与边界

读取 `affected-green-03.log`：22 PASS；`stability-01.log`：8 场景 × 5，40 PASS；typecheck PASS；Python assets/tool protocol 29 PASS；均仅适用各自输入。新增 touch 1 FAIL 使当前 repair Spec 不通过。

`browser-full-01-interruption.json` 明确 b214、SIGINT、在产品后继修改前中止；完整 browser 无最终 PASS。后继须固定身份再审；安装包／全部资源 HTTP、实际桌面 200%、同一状态 G08、全 browser 与远端 CI 的后继适用性独立处理。不得把本报告恢复为整体验收，IME BLOCKED 继续保留。
