# V04 最终本地复核

固定候选 `70b6faea2e13d7254a62836c3e1becd3129cb7b9`，完整差异与三个文件 SHA-256 见 `candidate-source.json`。本次是实现者的本地自审，不是独立评审。

## Standards

依据用户文件归属／并行约束、AGENTS.md、RECIPES-v1、ADR-0045～0049、code-review 的 smell baseline。变更只在两个本页产品文件及唯一几何测试 hunk。公共配方复用；页内所有 CSS 限定 `.overview`。公共 token／sy- 定义、app.css、RECIPES.md、页头／导航／title、读取协议和 gitattributes 均不变。未发现阻塞的文档标准违背或新增 smell。

## Spec

初版对照追加发现节点标题 13px 与原稿 14.5px 的 1 项字级差异。已用现有 token 计算还原，原候选和评审记录在 `initial-candidate/` 保留。最终四视口实际 Dashboard 审计验证标题 14.5px、等宽说明 10.5px，无动画、具名局部键盘滚动与 3px 焦点；大数字、费用成功色、来源独立状态、无省略的结局与记录事实通过。用途预览受既有 API 事实范围限制。

所有业务、错误／恢复／来源身份逻辑保留。几何测试仍精确检查 307／308px 一／两列，其他行为断言不变。完整浏览器初轮 3 项账本测试失败，原流单列保留；相同断言／超时的定向复测 3 passed，未修改共享代码或这些测试。固定候选完整浏览器 06：543 passed；Python 09：6445 passed、1 deselected、14 warnings，开始／结束全部 1630 文件一致。02 的候选变化失败与检查安排修正单独记录，不覆盖原始失败。其余门禁均已通过，见 V04.md 与 gates/。

Standards: 0 项；Spec: 1 项已修复、0 项剩余；各轴无阻塞发现。
