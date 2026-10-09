# S06 r2 — 待独立复评的固定候选

已修复协调器接受的 `S06-r1-Standards-F1`（原报告 alias `S06-ST-01`），并与真正 accepted S03/S08 集成组合验证。**待原 Standards 增量复评及首次完整 Spec 评审**；实施者不登记独立 PASS。原 r1 正式 FAIL、固定候选和 212 份证据完整保留，重新逐项核验 hash 无差异。

- Worktree：`/Users/nineofour/.codex/worktrees/dashboard-s06/Agent-Alfred`
- Branch：`codex/dashboard-s06`
- Head：`48cf21162bd59d066669fe9a82cc795e1b3d3eb4`
- Tree：`f495edbe74a00484c177953417c5def8bd297ecf`
- Accepted integration base：`6c152d62c2aec40a14d8164bad9e902141e31724`
- Base tree：`bcb8daef29e256e0c4743f9f2400f3e069ab0e2c`
- 原 r1：`8b55bcd11b20f6f948853ae458a67da18dc3a190` / `d302114316a86a10c2811d141137f0f38c006c37`
- 当前工作树 clean；13 个 S06 变更文件。`candidate.json`、`candidate.diff`、`changed-files.txt` 固定完整候选；`repair.diff` 为同步前相对 r1 的有界修复，`after-sync.diff` 为共享清理组合测试。

## 修复行为

聚合表单保存独立提交快照；分流选择保存独立 `submittedChoice`。HTTP 在途或回执未知时，离页守卫及未提交提示只比较当前值与提交快照。没有后继编辑、或修改后恢复提交值时可以直接离开；目标、关键词、来源或目标 Session 的真实新编辑仍受守卫保护。仅切换 MainBar Session 不改变提交归属。

明确拒绝后恢复按原确认基线判断未提交输入；成功仍只按原请求回执更新确认基线，保留提交后的新输入。未知回执继续明确标为未知，聚合不会自动重送。分流只读刷新不覆盖后继编辑、不推进原 CAS revision；显式 rebase 仍单独采用当前读取版本。旧请求的 `expected_revision`、原 Session／Run、已接受服务动作及回执归属未改变。

## 接受基线同步与冲突

本地修复提交 `e0c9a239`、独立字段覆盖 `23a0ad75`；同步接受基线于 `a4044886`；组合清理测试于最终 `48cf2116`。

两处首次 merge 冲突保存在 `merge-accepted.log` 与 `merge-first-conflicts/`：原 conflicted 文件、stage 1／2／3、hash 全部保留。`assets.py` 取两侧显式资源并集；`pages.js` 仅导出 Behaviour、Models、Connections，S03 Inbox 继续由 accepted `app.js` 直接导入。`merge-resolution.diff` 可复核。

S03 的 `aggregationFacts` 完整函数与接受基线逐字节一致，S06 现在直接消费其 typed cleanup。新组合测试在真实 Behaviour → Run → Behaviour 导航时持有旧资料节点，确认两处 owner 退休立即清空已打开正文。`shared-seams.json` 还验证 accepted `app.js`、Run/Inbox/source 与 S08 Models/Connections/settings 实现均未被本片覆盖。

## 本轮检查

| 检查 | 实际结果 | 证据 |
| --- | --- | --- |
| 固定 r1 产品上的提交快照反例 | **4 FAIL（真 RED）**；产品 diff 空 | `red.log`、`red-results/`、`red-product-diff.txt`、`red-tests.diff` |
| 首轮有界修复 | **8 PASS** | `green-r1.log` |
| 未知分流回执后刷新覆盖后继编辑 | 独立 **RED → GREEN**；保留原失败 | `unknown-refresh-red.log`、`unknown-refresh-red.diff`、`unknown-refresh-green.log` |
| 最终 accepted S03/S08 + S06 受影响组合 | **27 PASS**，54.9 s | `combination-r1.log` |
| Typecheck、修改的 Python registry Ruff、diff check | **PASS** | `typecheck-candidate.log`、`ruff-candidate.log`、`diff-check-candidate.log` |
| 最终 wheel + sdist | **PASS**；各一份，31 个闭包资源与源码逐字节一致 | `build-candidate.log`、`resources-candidate.json`、`dist-candidate/` |
| 实际源码服务 HTTP + 浏览器模块 | **PASS**；31 资源／11 入口／30 实际加载资源，无 JS 错误 | `source-http.json`、`source-http.log` |
| 原材料、来源和资源所有权回读 | **PASS**；原 212 文件不变；36 source／21 AC 完整保留 | `prior-preservation.json`、`source-verification.json`、`shared-seams.json` |
| 正常关闭与端口释放 | **PASS**；17960–17969 全部重新绑定成功 | `port-release.json` |

27 项包括延迟／丢失／明确拒绝回执、每个聚合后继字段、恢复提交值、真实 CAS 冲突与刷新、接受后正常离页、资料清理和遗忘的 opened／late 分支、真实历史轮数、Behaviour 重启、Run-path 1280／390 键盘路径、旧响应淘汰及丢失 202 不重送。全部使用隔离真实 Host／SQLite／HTTP／原生 SSE，模型为项目已有离线 fixture。延迟探针只暂缓真实服务器响应抵达浏览器，没有伪造成功响应。

源码 HTTP 核对 MIME、CSP、no-store、nosniff 和内容 hash；其 head/tree 绑定本候选。它仍是源码 fixture 检查，未冒称四种已安装产物 HTTP 矩阵。环境锁与 r1 相同，见 `environment.json`。唯一生成在工作树的组合截图已移入本目录 `combination-r1-artifacts/`，未遗留 dirty 文件。

## 复用与剩余责任

`checks.json` 明确区分本轮和继承检查。未重跑 r1 已通过的 176 后端、60 项整轮、最终聚合 22 项、S02 组合 19 项和原视觉／触控模拟；其范围、原 head 与证据保留。原 Run-path 68／70 与两个后继 PASS 仍如实保留，本轮仅重验五个受影响 Run-path 用例。没有把本轮 27 项当成新一轮 60／70／全量结果。

`source-coverage.json` 枚举全部 36 source／21 AC。原 bounded fragments 标注原候选，新增组合证据标注 r2；每个 whole source、AC 和 G 仍为 **NOT RUN**。完整 S11 跨片链、全项目 pytest/browser/CI、四安装 HTTP、upgrade/rollback、native 200%／真实中文 IME 尚未完成；真实移动设备仍是用户排除，历史 BFCache **BLOCKED** 不变。

未修改全局 ledger、未向 GitHub 写入、未 push／合入共享 integration 或 main、未触发真实 provider。后续由协调器在此 head/tree 上完成两轴独立评审与串行集成；如需修复，应建立新轮证据并保留本候选和所有首次失败。
