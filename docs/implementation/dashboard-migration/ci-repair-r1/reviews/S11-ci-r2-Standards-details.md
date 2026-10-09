# S11 CI r2 Standards 细节

固定 base 37cd0180fd5222d0753a0f3203ae56b1df6e0f0a → 4da39e52194e62f0f23637a83c8556069eb6a07d 的六文件范围承接 r1 独立完整 review。本次完整阅读 b214→4da 两文件增量、全部产品 diff、Session race 全文和新日志；其余四文件与 r1 SHA-256 一致，继承原审查。仓库规则及完整十二项 smell 基线沿用，未将启发式意见升级为硬违规。

## 已修复触控问题

compatibility mousedown 重新建立相同 MainBar opener 激活边界，mouseup 清理；这覆盖 pointerup 后再到达的触摸兼容 focus。独立 focus 不获普遍豁免。永久 held201 测试参数化 click/tap × 无旧 Session/有旧草稿，真实服务产生 Session ID、真实 201，再核对选择、输入、旧草稿、单 POST 和唯一 Stream。24 PASS 日志仍包括独立 opener-focus、导航、关闭、输入、外部活动 Run、实例更换与小视口回执；typecheck 及 29 assets/protocol PASS。没有加 sleep、重试、超时或删除业务断言。S11-CI-STD-01 关闭，但原 r1 FAIL 不覆盖。

## 当前 P2：首次 mount 前的真实导航未退休定位

app.js:308 以 shell.navigationGeneration 绑定请求。shell.js:182–183 在非 initial mount 时先增加 generation，然后 !started 直接返回；这时还没有 controller，无法通过 app.js:805 的页面 dispose 触发 retireLocation。后续 app.js:316 因代次变化直接返回 retired，locationTarget.loading 却保留；因而 MainBar 仍声称正在定位已废弃的读取。catch 同样只检查 locateGeneration/abort；将生命周期退休放在导航提交时，比在某一种迟到响应分支清理更完整。

owner 的 startup-navigation 原始 probe 由真实服务新建 Session、发送并保存 Run，记录该真实 unread 身份，阻断首 SSE，显式核对未读并取得真实 locate 200，然后用真实导航进入 Database、输入 SELECT 73。原日志 1 FAIL，四秒内十二次读到“正在定位指定记录…”。独立读取 spec/config/log/trace 及固定 Git，确认该可见故障；未读取同轮 Spec 轴结论。

trace 还记录 release 时 route.fulfill 的 `Route is already handled!` 被 catch，网络中仍有真实 200。因此本审查不把首次 probe 描述为“受控迟到 200 已成功投递”，也不从中宣称 newer locate 隔离已验证；原诊断和失败文件全数保留。行为缺陷由可见 loading 状态和固定代码因果共同成立。修复验证须在 releaseLocation 之前断言旧 loading 已清，随后证明旧请求完成不能清理新定位；保留首次自动 mount/合法空 ID 可以正常完成，以及原有独立阅读和焦点保护。该要求对应既有 I06/I07 请求退休及 #89 D08 请求身份合同，不增加业务范围。

## 审计及边界

同名 JSON 保存六文件哈希、17项交接证据哈希、此前16项证据及三个首败报告的完整性；54项只读元数据/差异核验全部通过。这些审计不是产品 PASS。临时 PROVISIONAL 文件另存、未签发为正式结论。现场 tracked worktree 未变；观察到 untracked tmp/，未修改或清理。

b214 的完整 browser 因触控缺陷主动中止，exit 130 保留为历史 incomplete；此前 bounded40 和四安装结果也不转移为 4da 后继通过。当前核验不裁决最终 native200、安装产物、G08、全量 browser 或远端 CI；它们由 coordinator 按固定后继重新绑定。原生真实 IME 继续 BLOCKED，移动仍为 Chromium 模拟边界。
