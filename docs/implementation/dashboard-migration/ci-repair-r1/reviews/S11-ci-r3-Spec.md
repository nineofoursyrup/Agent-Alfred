# S11 CI repair r3 — Spec

**PASS：本次代码／测试增量无未解决 Spec finding。** 固定 base `37cd0180fd5222d0753a0f3203ae56b1df6e0f0a` → head `06588168d5049040abf0a4560091d659d244ddcf`，tree `ae04fad8eff5f0eed6afb06050e4afeb6707506c`。

r2 的首次挂载前导航 P2 已关闭。真实导航确认后，由 shell 直接通知 Mainbar 退休定位，即使页面尚未挂载也立即清 loading／abort；取消、同地址和首次自动挂载不通知。删除原 page.dispose 中的重复耦合，没有放宽定位的身份或焦点守卫。

永久测试在释放首 SSE／旧 HTTP 前断言 loading 清除及实际 requestfailed；之后新定位等待期间完成旧 handler，确认新 loading、SQL 焦点和草稿仍保留，再由新真实 200 完成定位。未把已取消旧响应的 handler 完成冒称旧 200 成功投递。

已核对 **RED 1 FAIL／1 PASS → 16 项相关 PASS、typecheck PASS、29 项 assets/protocol PASS**，25 个 handoff 证据 hash、最终 6 文件身份和两段差异检查一致。沿用已审查的未变化代码及适用证据；所有首败、r1/r2 FAIL 和未签发初稿副本保留。

本 PASS 仅覆盖代码／测试修复。最终完整 browser、安装资源、G08、远端 CI、可携带文档及真实 IME 各自状态独立；IME 仍 BLOCKED。原生 200% 已验证路径的适用性可沿用，但本报告不声称新增原生执行或已完成最终资源读回。

[详细核验](S11-ci-r3-Spec-details.md) · [候选与证据](S11-ci-r3-Spec-verification.json)。
