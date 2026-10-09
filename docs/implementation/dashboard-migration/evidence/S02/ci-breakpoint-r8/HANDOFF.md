# S02 r8 CI 断点焦点调查交接

结论：受控真实路径确认，CI 所见的启动/断点症状由首次 Inbox 挂载夺走 MainBar 编辑焦点引起；已审 r7 的首次焦点保护能够消除该链。本轮只增加有价值的回归，**没有产品代码修改**。

固定 head `552ab48fc1d757ea881107827e583d592a5403b1`，tree `e4fa378f6e161834d528769865ab97ee1bc4517f`，base `bba439d7faeedde71970a74cea146a5e896e5345`（tree `745d8609cc61d3b171da96aa250c26303fa2227e`，与已审 r7 相同）。分支 `codex/dashboard-s02`，工作树 `/Users/nineofour/.codex/worktrees/dashboard-s02/Agent-Alfred`，clean。`candidate.diff` 仅 `tests/browser/shell-startup.spec.js` +30行。未 push、未 merger、未写 ledger/tracker；独立增量双审由 coordinator 调度。

## 原 CI 与保全

PR run `37855089183`：head `ddefb659`，实际 checkout `f86c57961f2ec294b44ef9b3506b6b0bc30e6bbd`，Python **6420 PASS / 22 skipped / 1 deselected**；browser **397 PASS / 1 FAIL**，原 `shell.spec.js:156` 的 crossing-breakpoint 场景。只读获取该实际 Git object 后确认 tree `0de2736d42da93209172cb3d05b3796088ca1072` 与 `ddefb659` 完全相同；checkout/head没有混写。

原 trace 的 API 顺序是 fill → setSelectionRange → 1100→1099 → `toBeVisible`短暂通过 → `toBeFocused`找不到消息框。根节点最终是 narrow/空panel/MainBar hidden，初始同步与 Inbox 首次挂载处在输入附近。原 CI 没有记录 activeElement 日志，因此实际内部因果由下面单独标识的受控真实复现来验证，不把推论当作云端插桩观察。

原17文件artifact及其manifest、原CI日志/identity、root的12次原测试通过样本，连同r7产物共30个文件已逐字节保全到 `ci-original-artifacts/`、`ci-original-metadata/`、`previous-r7/`。`preservation-manifest.json`记录原路径、复制路径、hash；原始CI FAIL不覆盖。12 PASS仅是有限未受控样本，不能撤销FAIL。

## 紧凑反馈与因果对照

应用 `diagnosing-bugs`。先在CI实际源码上得到红色回归，再验证两个假设：第一优先是首挂标题夺焦，预测移除/保留首挂焦点保护将分别失败/通过；第二是media事件时序本身，预测即使首挂仍在输入也可能丢编辑识别。后者在这条明确受控链中没有出现，不能据此排除所有其他media时序问题。

新测试仍用真实隔离Host/SQLite、离线ScriptedModel、实际HTTP、native EventSource与matchMedia。仅暂停真正SSE请求（包括显式新Session后的替换连接），让用户先完成原操作：1100宽、点击新建、fill、setSelectionRange(2,5)。随后释放真实首个snapshot，等待实际Inbox构造，再触发原生viewport切换。没有手工dispatch composition/popstate、没有强制focus修补结果，也没有放宽原断点/选区/宽屏偏好不变量。

| 对照 | 结果 | 可见因果信号 |
| --- | --- | --- |
| 实际 CI checkout `f86c579` 的固定源码 | **1 FAIL**，`ci-source-first-fail.log` | 首态前 active=message / pages0；首挂后 active=H1 / pages1；1099后 panel空 / MainBar hidden=true，消息框无法操作。 |
| 当前 bba/r7 的隔离源码，仅撤去首挂MainBar焦点保护 | **1 FAIL**，`focus-control.log` | 完全相同的 message→H1→hidden 链。`focus-control.diff` 是唯一生产差异；其他导出字节逐一核验。 |
| 当前 bba/r7 原样 | **1 PASS**，`current-successor.log` | 首挂前后 active=message；1099后 panel=mainbar / hidden=false，选区[2,5]；回1100焦点/选区/宽屏偏好保持。 |
| 最终 test-only 候选 | **18 PASS**，`final.log` | 全部 startup5 + shell13，包含原CI测试，以及已审首次entry/state、panel native Back、同revision重启和隐藏保护。 |

三份 `*-observations.json` 均从各实际运行trace里的测试附件提取；失败trace/screenshot和成功trace原字节都保留。`source-export-manifest.json`校验1210个导出文件：CI源码精确匹配其Git提交；单变量control只改首挂焦点保护。诊断副本在本证据目录，未回写工作树。临时config生成先发生一次f-string语法错误，尚未启动产品测试；保留 `ci-source-tooling-setup.log` 与note，和真实首FAIL分开。

红色反馈命令（同一测试，唯一改变服务源码）：

```sh
PATH=/opt/homebrew/opt/node@22/bin:$PATH ALFRED_BROWSER_TEST_PORT=17830 npm run test:browser -- --config=/Users/nineofour/.codex/dashboard-migration-20261009/S02/ci-breakpoint-r8/ci-source.playwright.config.mjs tests/browser/shell-startup.spec.js --grep 'first native snapshot' --output=/Users/nineofour/.codex/dashboard-migration-20261009/S02/ci-breakpoint-r8/ci-source-first-fail-artifacts
```

该命令已真实产生1FAIL；不要重新运行到原artifact路径覆盖它。当前绿色执行去掉诊断config，使用 `current-successor.log`所列独立输出路径。不是云端CI重跑，也不是统计稳定性声明。

## 适用范围与交付

- `source-scope-delta.json`只增加 #87 **R02 / R10** 的片段证据；完整164 source / 25 AC继承保全的r7映射，whole source/AC/G仍 **NOT RUN**。旧失败及S03–S11责任保留，不复制整套映射或改冻结原文。
- `resource-identity.json`核验原20资源/42边的每个实际bytes/hash均与r7相同；本轮无资源注册/产品接口变化。既有typecheck、ruff、20资产HTTP及未变64回归按原范围继承，未机械重跑。新测试经实际Playwright解析/执行，`git diff --check` PASS。
- `causal-chain.json`串联原CI identity→精确旧源码FAIL→单变量control FAIL→已审r7绿色→当前test-only候选。原PR run状态仍保留FAIL，由coordinator后续CI与整体验收处理。
- `evidence-validation.json`核验candidate/base/diff、clean、原artifact保全、导出对照、观察值、R02/R10冻结元数据、运行资源身份和端口释放。

环境为此前相同macOS/Node22/Python3.14.7/Chromium，本地离线隔离服务使用17830–17839许可范围；没有用户服务、真实provider或付费执行。原Linux CI与本地受控验证明确分开。原生200%/真实中文IME、真实BFCache与最终安装/全仓组合不由本轮新增认证。完成交接后释放slot，等待独立增量复审。

收尾时共享 coordinator metadata `terminal-identities.json` 已有外部更新；保全副本仍匹配初始hash，当前版另存 `ci-current-metadata/`，两版并列于 `external-metadata-drift.json`。原17个CI artifact字节未变，未改写或隐藏原FAIL。第一次全路径原文件hash复核因此报差异，属于证据整理检测，不是产品测试失败。
