# S11 CI repair r2 — Spec

**FAIL：1 个 P2。** 固定 base `37cd0180fd5222d0753a0f3203ae56b1df6e0f0a` → head `4da39e52194e62f0f23637a83c8556069eb6a07d`，tree `4ce90f7a91c21a9a5c14834e352500b5088cb405`。未签发的初稿及 JSON 已原样保存在 `S11-ci-r2-pre-probe-report/`，root 未采纳初稿 PASS。

- **[P2] 首次挂载前的真实导航没有退休 Mainbar 定位。** `shell.js:181–187` 在尚未 start 时推进 navigationGeneration 后直接返回，没有 controller.dispose；`app.js:316` 迟到响应因 owner 已变返回 retired，却没有清旧 loading。真实已保存 Run 的定位 200 暂缓期间导航到 Database，随后首次挂载、编辑 SQL 并释放响应，仍永久显示“正在定位指定记录…”。owner 的真实 HTTP／SSE probe 已复现 1 FAIL。

最低修复边界是导航确认提交时立即退休旧读取并清 loading，在释放旧响应前即可观察；同目标、取消离页、首次自动挂载不退休。旧响应晚到不能清 newer locate 或改新 Session／输入／焦点。只等旧 200 返回时清理不足以闭合 I06／I07／D08。

已读完整 6 文件增量，重点复核 b214 后继两文件。r1 的触控 P2 已关闭：兼容 mousedown 仅为同一 Mainbar opener 设置激活标记，mouseup／focusin 清除；独立焦点、导航、编辑、关闭、进程更换和外部 Run 守卫仍有效。真实 held-201 的 click／tap × 有无旧 Session 四场景全部通过。

已核对 **24 项相关 browser PASS、后继 50 次有界重复 PASS、typecheck PASS、29 项 Python assets/protocol PASS**，17 个 handoff 证据 hash、6 个候选文件 hash 与两段固定差异检查一致。原始 CI、各次 RED、b214 touch FAIL、完整 browser 中止记录及 r1 FAIL 报告均保留。后继稳定性使用 `stability-02.log`，没有借用 b214 的 40 次结果。

上述通过及已关闭的触控 P2 不冲销新发现的 1 FAIL。全 browser、最终安装资源、原生 200%、G08 与最终 CI 的候选适用性另行验收；真实 IME 仍 BLOCKED，不能据此宣布 S11／AC25 或全部源项 PASS。

[详细核验](S11-ci-r2-Spec-details.md) · [身份和证据](S11-ci-r2-Spec-verification.json)。D09 只作关联来源、不追加本批 FAIL 的澄清见 [独立补充](S11-ci-r1-Spec-diagnosis-mapping-addendum.json)，原诊断保持不变。
