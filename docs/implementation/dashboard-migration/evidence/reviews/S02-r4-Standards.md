# S02 r4 — 独立 Standards review

**结论：PASS；0 项硬违规，0 项新增 optional smell。** S01 既有可选 P3 不变。本结论限固定 S02 候选及本轮已验证依赖组合。

- Base: `a4168881bd9057a900e8d353002307adff714c42`
- Head: `0f0911c15b95c3b4501ceb7228f8b61784f6da14`
- Tree: `2b4471749763c39cdce1bfa0c22c915b4d4c53e0`

审阅 r3→r4 全部实际增量：S02 `app.js` 与四份测试，以及合并的 S01 范围。18 个 S01 文件与已评审 r5／集成 base 字节相同，复用 [S01 r5 Standards](S01-r5-Standards.md)。当前 base→head 的 45 文件范围与交接清单一致；未打开另一 reviewer 报告。

## 修复与硬标准

原 r3 P2 已修复。`app.js:435–437,705,765` 同时绑定原 Run controller 与 shell generation；真实离页立即退休恢复资格，迟到首状态不能重建继任 Models。符合 `INTERFACES.md` I00 页面身份／退休及 I06 dirty／dispose 规则。精确回归保留原 DOM、焦点、输入，并验证下一次真离页仍有守卫；原页导出恢复亦通过。

新增保存核对保留状态维度：首个 idle 只将旧 pending 标为待核对；`app.js:360–363` 仅以身份匹配的持久 `run_pair` 更新该 Session／Run 的保存事实。无关 Run 不会解除目标状态，不以 idle 或正文取得证明保存，符合 ADR-0024／0029 与 I05。

已按完整 12 项 Fowler baseline 复核适用增量；未发现新增可操作启发式问题，未重复列工具强制项。

## 证据与适用范围

合并后 **45 browser PASS** 包含真实 S01 持久／pending 投影定位、身份合并、跨 Session 草稿及生命周期回归；另 **44 read-pages PASS** 覆盖 Runs／Inbox／分页／trace export，**50 backend PASS** 覆盖共享读取／回复／资源。合并前 26 PASS 仅作直接修复后继，不替代组合证据，不累加测试数。两项 first FAIL、原 browser-r2 **351 PASS / 18 FAIL** 均保留；不宣称最终全量全绿。typecheck／Ruff／固定 diff --check 通过，未重复无影响广测。

[独立候选核验](S02-r4-candidate-check.json) 确认 r4 身份、18 资源字节/hash、38 条引用闭包／登记及 164 source／25 AC 原身份。S03 UI 入口、完整来源返回／长历史和 S11 安装矩阵、native 200% zoom、真实中文 IME 仍待验；真实 BFCache **BLOCKED** 不变，synthetic persisted 仅证明恢复边界。
