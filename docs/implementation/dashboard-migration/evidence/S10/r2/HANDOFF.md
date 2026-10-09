# S10 / #118 r2 修复与已接受候选同步

固定 head `3e87a076b7dc645c576121282f7c9a4af6873d89` / tree `f9774fff7d404d02453eadb046ea9e1082ed8d9a`，base `a87ed6783df7af736ce0769b7037dd7a5d04df63` / tree `f495edbe74a00484c177953417c5def8bd297ecf`；worktree clean。r1 `48a9b409482a76ddc17e7487e0c5b87c2577993b` 和其原证据完整保留，`r1-preservation.json` 验证178文件；r1独立Spec的 FAIL 不被本轮实现者结果覆盖。首次完整Standards与Spec增量复审均 **NOT RUN**，由协调器分派。

## 修复与授权边界

修复 `S10-r1-Spec-F1`：签发请求结束却未取得ID时，本页不可能发送其后独立的execute请求。撤销这份未执行的页面关联，保留SQL草稿，明确未启动执行，并要求fresh能力核验；核验成功后才允许新的显式点击。没有自动重投，也没有把未知签发称作取消成功。已经发送execute、有已知ID时仍走真实状态/cleanup核验；目录available不能解除未知状态或真正cleanupfailed。

修复提交 `0d68b3f607a79a98a193bff574a892d1715a7bcd` 仅2文件；`repair.diff`与`candidate.diff`各有hash，后者是相对当前acceptedbase的完整10文件S10候选。同步只使用root明确的acceptedtip。首次同步6c152d62是自动合并，无冲突；base/ours/theirs HTML/assets原始字节和mergedhash保存在`accepted-sync.json`及对应文件。后续S06同步及新增消费者检查见11之后日志和accepted-s06-sync.json；Database源码未受其改变。 未修改ledger/GitHub、未push/main/integrationmerge、未部署或调用真实provider。

## 实际验证

- `01-receipt-boundary-RED.log`：真实签发200已到Host但浏览器回执被丢弃，实际句柄unused/released、零execute；手动核验后仍disabled，**FAIL**。已执行且状态读丢失的对照 **PASS**。首次trace/error context原样保存。
- `02-receipt-boundary-GREEN.log`：2项 **PASS**。失签发明确核验可恢复而不执行，下一次显式点击只产生一次新查询；已发送执行的对照仍必须取得真实query状态才解锁。
- `04-affected-database.log`：27项 **PASS**，含原cancel/late-issuance、旧A回执与B隔离、真实cleanupfailed/heal、lost-execute、reload/close/offline/newinstance、真实遗忘/晚到HTTP、提交SQL和草稿归属；**未重跑整套44/46**。
- `07-accepted-combinations.log`：8项 **PASS**，实测新Inbox/Run来源与MainBar、Models保存/清空/真实离线probe与Attempt、Database守卫/历史/显隐、跨9旧页唯一原生EventSource。全十页最终G链仍未验收。
- `12-final-consumers.log`：最终S06同步后4项 **PASS**（Behaviour触控/MainBar、两条Database恢复边界、9页原生EventSource）。此前27+8的适用性以`accepted-s06-sync.json`的13文件字节相同为据。
- typecheck PASS；assets20 PASS；最终真实源码HTTP引用闭包 **31 resources / 75 references** PASS，包括MIME/CSP/no-store/nosniff与原始字节；正常fixtureclose。
- 构建的唯一wheel与sdist各包含HTML＋闭包资源且逐文件等于当前源码，详见`package-members-final.json`。初始9cc候选构建保留在`dist`/`package-members-before-s06.json`；若有后续accepted同步，最终产物另存，原包不覆盖。**四组安装后HTTP/安装后browser未执行**。

详细命令见`commands.md`，日志文件hash/mtime见`check-log-manifest.json`，fixture公开接缝和结果见`checks.json`。同实例重连原本可通过shell保护失效恢复，属于已确认对照；不称其失败。原review的successor `observedFailure=false`针对最后重连状态，不抵销手动核验失败。

## 继承适用性和责任

`inherited-inputs.json`核对27项Database后端/测试/fixture/依赖/配置字节仍与r1一致，180后端PASS按此范围复用；peer Models/settings后端由各自已接受证据及当前组合检查负责。环境通过原`environment-final.json`的hash限定引用，锁文件/工具链未变，不复制或重装环境。旧44 browser/视觉/原生证据只按未受影响路径继承，新增catch与改变的消费组合以当前检查为准，不能称当前全suite已运行。

r1视觉tokens/Database CSS未改变；新增peerCSS有自己命名空间，公共CSS只涉及MainBar deferred receipt。桌面200%/中文IME **NOT RUN**；原生生命周期旧证据为headless Chromium可信事件，不冒称桌面手动证据；真实BFCache **BLOCKED**，no-store保留。移动仍仅Chromium模拟，iPhone/iOS Safari/真实软键盘范围外NOT RUN。

31原段hash均在当前worktree重验；完整原文/owners/AC/G由`source-coverage.json`内hash限定的r1覆盖文件与冻结acceptance-map引用，逐项metadata也保留。源项和AC的**整体结果全部NOT RUN**，独立审查、其余owners、S11 G01/G04/G07及继承G08、最终CI/四安装/升级/支持目标回退继续由root/S11负责。

| Source | 原文位置 | S10片段结果 | r2证据 |
| --- | --- | --- | --- |
| issue-87:P02 | docs/design/issue-87/SPEC.md:192 | NOT RUN | accepted-combinations, affected-database |
| issue-87:P08 | docs/design/issue-87/SPEC.md:198 | PASS | accepted-combinations, affected-database, inherited |
| issue-87:CE-04 | docs/design/issue-87/SPEC.md:216 | PASS | accepted-combinations, affected-database, inherited |
| issue-87:CE-24 | docs/design/issue-87/SPEC.md:236 | PASS | receipt-boundary, affected-database, inherited |
| issue-87:section-12 | docs/design/issue-87/SPEC.md:185 | PASS | accepted-combinations, affected-database |
| issue-87:section-13 | docs/design/issue-87/SPEC.md:207 | PASS | accepted-combinations, affected-database |
| issue-90:P06 | docs/design/issue-90/ACCEPTANCE.md:16 | PASS | receipt-boundary, affected-database, inherited |
| issue-91:section-01 | docs/design/issue-91/DESIGN.md:7 | PASS | resources, package, inherited |
| issue-91:section-02 | docs/design/issue-91/DESIGN.md:15 | PASS | inherited |
| issue-91:R01 | docs/design/issue-91/DESIGN.md:26 | PASS | accepted-combinations, affected-database, inherited |
| issue-91:R09 | docs/design/issue-91/DESIGN.md:142 | PASS | receipt-boundary, affected-database, inherited |
| issue-91:R10 | docs/design/issue-91/DESIGN.md:155 | PASS | receipt-boundary, affected-database, inherited |
| issue-91:R11 | docs/design/issue-91/DESIGN.md:176 | PASS | inherited |
| issue-91:R12 | docs/design/issue-91/DESIGN.md:185 | PASS | receipt-boundary, affected-database, inherited |
| issue-91:section-15 | docs/design/issue-91/DESIGN.md:200 | PASS | resources, package, inherited |
| issue-91:P17 | docs/design/issue-91/ACCEPTANCE.md:27 | PASS | accepted-combinations, affected-database, inherited |
| issue-91:P18 | docs/design/issue-91/ACCEPTANCE.md:28 | PASS | receipt-boundary, affected-database, inherited |
| issue-91:P19 | docs/design/issue-91/ACCEPTANCE.md:29 | PASS | inherited |
| issue-91:P20 | docs/design/issue-91/ACCEPTANCE.md:30 | PASS | receipt-boundary, affected-database, inherited |
| issue-91:P21 | docs/design/issue-91/ACCEPTANCE.md:31 | PASS | receipt-boundary, affected-database, inherited |
| issue-91:P22 | docs/design/issue-91/ACCEPTANCE.md:32 | PASS | receipt-boundary, affected-database, inherited |
| issue-91:P23 | docs/design/issue-91/ACCEPTANCE.md:33 | PASS | inherited |
| issue-91:N23 | docs/design/issue-91/ACCEPTANCE.md:61 | PASS | accepted-combinations, affected-database, inherited |
| issue-91:N24 | docs/design/issue-91/ACCEPTANCE.md:62 | PASS | receipt-boundary, affected-database, inherited |
| issue-91:N25 | docs/design/issue-91/ACCEPTANCE.md:63 | PASS | inherited |
| issue-91:N26 | docs/design/issue-91/ACCEPTANCE.md:64 | PASS | receipt-boundary, affected-database, inherited |
| issue-91:N27 | docs/design/issue-91/ACCEPTANCE.md:65 | PASS | receipt-boundary, affected-database, inherited |
| issue-91:N28 | docs/design/issue-91/ACCEPTANCE.md:66 | PASS | receipt-boundary, affected-database, inherited |
| issue-91:N29 | docs/design/issue-91/ACCEPTANCE.md:67 | PASS | inherited |
| issue-91:N30 | docs/design/issue-91/ACCEPTANCE.md:68 | PASS | receipt-boundary, affected-database, inherited |
| issue-91:N31 | docs/design/issue-91/ACCEPTANCE.md:69 | PASS | inherited |

| AC | S10直接片段结果 | 剩余责任 |
| --- | --- | --- |
| AC01 | NOT RUN | Final ten-page G01-G08, cross-owner source closure and root/S11 integration acceptance |
| AC02 | PASS | Final ten-page G01-G08, cross-owner source closure and root/S11 integration acceptance |
| AC03 | PASS | Native desktop200%/ChineseIME |
| AC04 | NOT RUN | Final ten-page G01-G08, cross-owner source closure and root/S11 integration acceptance |
| AC05 | PASS | Final ten-page G01-G08, cross-owner source closure and root/S11 integration acceptance |
| AC06 | NOT RUN | Final ten-page G01-G08, cross-owner source closure and root/S11 integration acceptance |
| AC07 | NOT RUN | Final ten-page G01-G08, cross-owner source closure and root/S11 integration acceptance |
| AC13 | NOT RUN | Final ten-page G01-G08, cross-owner source closure and root/S11 integration acceptance |
| AC14 | PASS | Final ten-page G01-G08, cross-owner source closure and root/S11 integration acceptance |
| AC16 | NOT RUN | Final ten-page G01-G08, cross-owner source closure and root/S11 integration acceptance |
| AC17 | NOT RUN | Final ten-page G01-G08, cross-owner source closure and root/S11 integration acceptance |
| AC19 | NOT RUN | Final ten-page G01-G08, cross-owner source closure and root/S11 integration acceptance |
| AC20 | NOT RUN | Final ten-page G01-G08, cross-owner source closure and root/S11 integration acceptance |
| AC21 | NOT RUN | Final ten-page G01-G08, cross-owner source closure and root/S11 integration acceptance |
| AC22 | PASS | Final ten-page G01-G08, cross-owner source closure and root/S11 integration acceptance |
| AC23 | NOT RUN | Final ten-page G01-G08, cross-owner source closure and root/S11 integration acceptance |
| AC25 | PASS | Independent reviews and final four installed artifacts/CI |
| AC26 | PASS | Final ten-page G01-G08, cross-owner source closure and root/S11 integration acceptance |

支持回退须保留r1清理门禁/迟到隔离/实例恢复及本轮“未执行签发失败可核验恢复”，并保留S08等已有持久化修复。本轮未实施升级/回退。任务服务正常关闭；保留worktree与证据供独立评审，工作slot可释放。
