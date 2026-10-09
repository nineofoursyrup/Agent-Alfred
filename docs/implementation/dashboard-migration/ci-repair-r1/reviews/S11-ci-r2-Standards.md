# S11 CI r2 Standards — FAIL

候选 `4da39e52194e62f0f23637a83c8556069eb6a07d`；tree `4ce90f7a91c21a9a5c14834e352500b5088cb405`。

1 P2：**S11-CI-STD-02**，`app.js:316`。首次 SSE/mount 前发起定位并真实导航时，没有旧 controller 执行 retireLocation；迟到分支返回 retired，却持续显示“正在定位”。owner 真实浏览器 1 RED 与固定源码相符。应在实际导航提交时立即退休旧读取；旧完成不能清理新定位。

触控缺陷 S11-CI-STD-01 已修复；24 例、typecheck、29 Python 日志核验通过，54 项只读绑定核验通过。无新增可选 smell。首败与临时报告保留；probe 屏障诊断详见 details。未自行运行产品测试。后继安装、G08、native、全量及 CI 须另行绑定，真实 IME 仍 BLOCKED。
