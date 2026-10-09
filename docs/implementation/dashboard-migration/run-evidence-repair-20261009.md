# Run 路径引用同步修复

原集成 PR #120 合并为 `63fe1a6ac026084118cbc9585cedb30e7f03a0a2` 后，实际 merge CI [37935569616](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/37935569616) 出现 browser 540 PASS / 1 FAIL。原因是路径图先返回、节点已选中，而 Step / Attempt 证据后到：运行过程已显示证据，节点详情仍停留在“关联证据不可用”。两次受控真实响应顺序的 red 复现相同症状，原始失败不改写。

本次在 Run 过程 DOM 更新后仅同步可用性发生变化的引用。证据到达时恢复链接，消失时撤销，恢复后重新可达；保留未变引用、焦点、原始定义展开、选中节点、手动路径快照及视口，不新增路径读取。

产品提交 `e4b61cff8e9e482b168f7508bcd1fa8c56b1f308`，tree `123caa591cf8053a9cfbafa7fdbe2e9865e77686`，与[冻结候选](evidence/run-evidence-repair-20261009/candidate.json)逐字一致。支持提交 `faf587f00d6de01ae94180dd00fc5bdcb9a32d68`，tree `19be93d8a59b0cb463f7fb2fbcbc2d0d371699b0`，只保留原默认 Inbox 差异。新增三文件 diff 和两树身份均可回查。

- [独立 Standards](evidence/run-evidence-repair-20261009/reviews/standards.md) / [独立 Spec](evidence/run-evidence-repair-20261009/reviews/spec.md)：同候选 PASS，0 未解决发现。
- [验证记录](evidence/run-evidence-repair-20261009/verification.json)：137项既有相关回归及1项完整引用生命周期PASS，typecheck / diff check PASS；原1280/390键盘用例未放宽。
- [四安装](evidence/run-evidence-repair-20261009/installation-results.json)：wheel/sdist × base/mcp，各620包文件、34静态资源、16入口/深链、100 HTTP检查；源码行为与安装HTTP分别报告。
- [新 G08](evidence/run-evidence-repair-20261009/lifecycle-summary.json)：旧包→当前包→固定支持包，在同一新隔离状态中3次正常close0；持久事实、草稿、深链与零自动POST断言通过。原结果保留，[公开结果](evidence/run-evidence-repair-20261009/lifecycle-portable.json)仅删除3处进程CSRF。
- [诊断](evidence/run-evidence-repair-20261009/diagnosis.md)、[首败清单](evidence/run-evidence-repair-20261009/first-failure/manifest.json)、[red源码身份](evidence/run-evidence-repair-20261009/red-sources/manifest.json)、[后继适用性](evidence/run-evidence-repair-20261009/APPLICABILITY.md)、[81条验收的增量核对](evidence/run-evidence-repair-20261009/issue-audit-delta.json)及[发布清单](evidence/run-evidence-repair-20261009/publication-manifest.json)。

原生中文IME / 200% / MainBar等历史证据按未变字节及不相交行为范围继承，详见适用性报告；不称本次重新实测。iPhone / iOS Safari / 真实移动键盘仍为用户排除的 NOT RUN，历史 BFCache BLOCKED 和首 FAIL 保留。

此说明冻结时修复 PR CI、预期head合并、实际修复merge SHA CI与逐票验收关闭仍待完成。309 sources / 674 owner片段 / 26 AC的最终采用以实际门禁和GitHub验收回写承接，不由本说明提前判定完成。
