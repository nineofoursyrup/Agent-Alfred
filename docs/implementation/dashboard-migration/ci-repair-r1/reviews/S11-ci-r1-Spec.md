# S11 CI repair r1 — Spec

**FAIL：1 个 P2。** 固定 base `37cd0180fd5222d0753a0f3203ae56b1df6e0f0a` → head `b21451fb4674ce693cec2d53d16bc94647fa4965`，tree `0b0bef55e1de4f33891abc08ae33f3f03128985f`。已独立读完全部 6 文件增量及相邻所有权代码，未读取本轮另一轴结论。

- **[P2] 触控打开同一 Mainbar 仍撤销在途新建意图。** `app.js:204–215` 在 pointerup 清除 opener 标记，但 Chromium `hasTouch` 的真实 `tap` 随后才产生兼容 mousedown/focusin；该 focusin 被误当作独立意图。真实服务已创建 Session、201 回执暂缓时，仅 tap 打开 Mainbar，释放回执后仍未选择新 Session（期望真实 ID，实际 null）。这仍违反 I06 的纯呈现边界与 #89 D04 的新建成功采用路径；属于本次修复的触控遗漏。

已核对原创建 2 RED、首次挂载定位 1 RED、过宽初版 focus guard 1 RED、22 PASS、40 次有界重复 PASS、typecheck 与 29 个 Python assets/protocol PASS。原 Inbox／reconnect 的测试屏障保留实际采用结果、精确身份、一次读取及权威保存断言；定位改用 navigationGeneration 保留真实离页退休保护。这些通过不能冲销新增 touch probe 的 1 FAIL。

完整 browser 在修改后继前正常 SIGINT，保留中止记录，未作为 PASS。全量门禁、安装资源、原生 200%、G08、最终 CI 由 root／owner 继续绑定后继；真实 IME 仍 BLOCKED。

[详情与证据](S11-ci-r1-Spec-details.md) · [固定身份及证据核验](S11-ci-r1-Spec-verification.json)。本报告与先前只读 diagnosis 分开，均保留不覆盖。
