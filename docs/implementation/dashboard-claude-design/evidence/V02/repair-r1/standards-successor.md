固定候选：`.scratch/v02-review-repair-02/manifest.json`；基线 `6b5769583617714ca36e9676d0a2c3c990155735`。独立 Standards 复核：`v02_standards_review`。

六项副本 SHA-256 全部匹配；相对前一固定候选仅 `app.css:77` 新增规则。CSS SHA-256：`9fe02014005d3e731f81cfe24bf83b176d213043677be5032f63161ec2d12d51`。

最新 Std-1 的真实角色值、间距 token 和共用页面 padding／页头负 margin 保持。`:root`、公共 `sy-` 配方及业务 CSS 未变。新增规则限定 `#workspace #page-header #shell-status:has(>[data-created-receipt])`，使用既有间距 token 限高、局部滚动与 `overscroll-behavior:contain`；无回执及窄屏迁入面板的摘要不命中。其余源码、DOM 和测试断言未变。

本复核未运行测试；协调方报告原用例三视口 3 PASS，完整浏览器重跑仍待完成，不视为已通过。原 `standards.md` 保留。

Standards findings：0；可选 smell 建议：0；最严重级别：无。
