# S10 r2 Standards — 首次完整核验

结论 **PASS**；documented hard violations 0，可选 Fowler smells 0。固定 head `3e87a076b7dc645c576121282f7c9a4af6873d89` / tree `f9774fff7d404d02453eadb046ea9e1082ed8d9a` / base `a87ed6783df7af736ce0769b7037dd7a5d04df63`。当前工作树干净，merge-base与完整10文件diff一致：910 insertions / 144 deletions。未修改产品、ledger、GitHub或Git提交。未读取其他轴的评审报告。

## 固定范围与规范

先以git archive固定r1 `48a9b409482a76ddc17e7487e0c5b87c2577993b` / `d0a8de66a741337616a967d545ce34014532e150`、base `87aab611e4a040ad0edc9956a43f6dff5d4944f3`，再导出最终r2。审查涵盖原10文件整个范围与新增修复；未用owner正在变化的文件充当固定源码。

已读 code-review skill 的全12项Fowler基线和项目CLAUDE、CONTEXT相关领域事实、docs/agents/domain、issue-tracker、triage-labels；Database ADR-0040、生命周期ADR-0046、issue-66实现/服务入口、#91 FACTS/R01/R09–R12与全部映射原文。Dashboard实施SPEC/INTERFACES/SLICES/VALIDATION/SOURCE-AUDIT/traceability、#87完整规格、#86 tokens和dashboard验证规则与已读S06固定文档逐字节相同，复用文本理解并按S10具体范围重新审查；没有继承上一候选的产品结论。

重点规则：INTERFACES I00 的只读/身份/所有权、I06唯一壳层/页面端口、I08 Database释放职责、I09资源闭包；#91 R01原生输入/焦点/完整信息；R09 SQL提交与草稿、分页复制；R10执行/正文/cleanup分轴与实际清理后准入；VALIDATION真实公共接缝、首次失败/证据范围。Database手写SQL离页仍会丢失输入，不把已执行SQL视为已保存持久草稿。

## 全文件与hunk覆盖

| 文件 | 审查内容／结论 |
| --- | --- |
| `src/agent_alfred/gateway/web/assets.py` | 仅新增 database.css 具名服务登记；与 accepted registry 并集精确一致，MIME/CSP/no-store/nosniff 沿原入口。 未发现硬违规。完整hunk坐标见verification JSON。 |
| `src/agent_alfred/ops/static/database.css` | 完整32行逐条审查；选择器限定 .db-console，公共tokens和中央679/680响应式；长值/表格局部滚动，状态仍有文本。 未发现硬违规。完整hunk坐标见verification JSON。 |
| `src/agent_alfred/ops/static/database.js` | 完整文件和全部差异审查：目录字段/limits；默认保留SQL替换对话；提交快照与后继草稿；typed cells、重复列、本地100行分页和显式复制；执行/正文/cleanup分轴；known-handle状态和fresh能力双门；generation/probe/cancelTask归属；memory/protection/offline/新实例/pagehide/close；r2无ID未execute分支。 未发现硬违规。完整hunk坐标见verification JSON。 |
| `src/agent_alfred/ops/static/index.html` | 相对accepted仅增加database.css；保留behaviour.css与其他accepted字节。首次相邻stylesheet冲突三阶段已校核，未将冲突原文覆盖。 未发现硬违规。完整hunk坐标见verification JSON。 |
| `tests/browser/database.spec.js` | 全部差异：默认收起目录、精确可见文本；隐藏MainBar下真实遗忘并拒绝旧HTTP；替换默认/选区/Enter；延迟真实A响应与B草稿；真实1000行截断与页2复制范围/元信息。未以DOM假清理替代真实服务。 未发现硬违规。完整hunk坐标见verification JSON。 |
| `tests/browser/database_cancel.spec.js` | A取消回执/状态延迟与B竞争，显式实际清理回读后才准入B；原旧回执隔离断言保留。 未发现硬违规。完整hunk坐标见verification JSON。 |
| `tests/browser/database_lifecycle.spec.js` | 新增实际Host restart：旧query失效但不伪称旧实例cleanup已验证；保留SQL、零自动重跑、新显式查询成立。原真实reload/close/offline用例继续适用。 未发现硬违规。完整hunk坐标见verification JSON。 |
| `tests/browser/database_migration.spec.js` | 完整新文件182行；21批准对象所有字段对真实目录、展开状态/完整limits；四视口真实SQL类型/重复列/长值/局部键盘滚动；64/65列、字节截断、剪贴板权限失败及实际679/680、1099/1100几何。mock仅OS剪贴板权限边界。 未发现硬违规。完整hunk坐标见verification JSON。 |
| `tests/browser/database_native_lifecycle.mjs` | 仅控制对照页端口以保证隔离；可信原生事件与真实worker路径保留。原生headless与真实桌面zoom/IME严格区分。 未发现硬违规。完整hunk坐标见verification JSON。 |
| `tests/browser/database_status.spec.js` | 完整差异及调用：补充展示矩阵明确标注mock，真实丢执行回执/cleanupfailed/heal保留；新增真实lost-issuance与execute/status-loss对照，实际HTTP状态、query身份和请求数证明准入边界。 未发现硬违规。完整hunk坐标见verification JSON。 |

调用接口还包括：dom.node 的纯文本输出；app.mountPage每次独立root、csrf/instance/connected权威上下文；shell统一守卫/显隐/dispose；原生EventSource与memory/protection通知；既有Database HTTP签发/execute/cancel/status、catalog与实际worker清理服务。SQL仍仅POST，无正文入URL/持久通用缓存；无新增API/第二MainBar/第二Stream或自动SQL执行。

## 有界疑点与实际排除

静态检查曾怀疑：无query_id时disconnect退休generation，迟到签发和后续probe可能无法恢复清理门。独立probe只延迟真实成功签发响应，使用固定archive服务、真实Host/SQLite/原生SSE及Chromium context.setOffline；生产判断和cleanup结果未替换。

实际原生重连还同步memory/protection状态，因此此组合没有形成永久阻塞。query_id `xTOEVH4ka-0DX-yT1dWX31xL` 实际收到取消200，真实状态为cancelled/released；显式状态GET200，执行按钮可用、SQL草稿原样、零execute。第一次probe结果就是NOT_REPRODUCED；不把该怀疑列为finding。服务正常close，18064重新可绑定。原JSON、mjs、log、完整截图及trace均保留，未重复跑全套。

## 可复用验证及限制

- r1：178证据文件hash匹配，12条first-failure链全部保留。10-core successor自身仍有1 FAIL；14-regression successor仍有2 FAIL；16 fixture失败与17产品失败分列；34包扫描工具错误独立保存。没有把中间successor名称当作全绿。
- r2：61文件hash匹配。真实receipt boundary第一次1 FAIL＋1 PASS，后继2 PASS；27受影响Database检查、8接受候选组合、最终4消费者检查通过。新catch、实际worker cleanupfailed/heal、旧A/新B与迟到签发、真实遗忘、reload/close、offline/newinstance均有对应真实路径。
- 180后端结果按27份固定输入/测试/fixture/lock/config不变复用；13份原有Database/shared消费输入在最终S06同步前后完全相同。peer Models/settings后端仍归其已接受证据，不将180扩大为其他owner整体通过。
- typecheck与assets记录、日志hash/mtime及执行命令已读；没有为进入评审重跑未变化机械门禁。r1完整44浏览器/视觉/原生证据仅沿仍适用路径继承，本轮不是完整44/46运行。
- 独立从HTML → imports → CSS引用发现31资源/75边，再反查31登记；最终源码HTTP的200、MIME/CSP/no-store/nosniff、全部字节以及正常服务关闭吻合固定Git。两份最终包实际打开，各32成员（HTML＋31资源）与固定源码逐字节匹配，原包也保留。
- 独立观察批准公共组件参考、1440 Database顶部、390长结果、320顶部截图；原四视口/真实679/680/1099/1100几何及14/13/12px、触控模拟、reduced-motion记录已核对。Database CSS字节未变，新peer CSS有独立命名空间；参考为公共tokens/密度，不虚构原Database逐像素原型。
- r1真实隐藏标签持续预算约5195.57ms，真实worker timed_out/released；真正BFCache仍BLOCKED。可缓存对照页的persisted不代替产品BFCache。

核验脚本初次/第二次各有schema KeyError（slice_fragment_evidence、result）：原映射冻结状态与handoff扁平字段形状不同，未触发产品失败。两次脚本和错误记录已独立保留；后继按实际metadata、原文hash/归档、whole_source_result分别严格校核，结果errors=[]。这两次工具失败没有被删除。

## 31来源原文闭包

下表均指已审查的S10片段；所有whole source保留NOT RUN，原文本/hash/来源归档/owners/AC/G及r2→r1指针一致。其余owners与S11责任不被Standards结论替代。

| Source | 原文精确范围 | 本次核对 |
| --- | --- | --- |
| `issue-87:P02` | `docs/design/issue-87/SPEC.md:192–192` | 原文/归档/metadata一致；accepted-combinations, affected-database的片段适用性已核对；whole NOT RUN。 |
| `issue-87:P08` | `docs/design/issue-87/SPEC.md:198–198` | 原文/归档/metadata一致；accepted-combinations, affected-database, inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-87:CE-04` | `docs/design/issue-87/SPEC.md:216–216` | 原文/归档/metadata一致；accepted-combinations, affected-database, inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-87:CE-24` | `docs/design/issue-87/SPEC.md:236–236` | 原文/归档/metadata一致；receipt-boundary, affected-database, inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-87:section-12` | `docs/design/issue-87/SPEC.md:185–206` | 原文/归档/metadata一致；accepted-combinations, affected-database的片段适用性已核对；whole NOT RUN。 |
| `issue-87:section-13` | `docs/design/issue-87/SPEC.md:207–237` | 原文/归档/metadata一致；accepted-combinations, affected-database的片段适用性已核对；whole NOT RUN。 |
| `issue-90:P06` | `docs/design/issue-90/ACCEPTANCE.md:16–16` | 原文/归档/metadata一致；receipt-boundary, affected-database, inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-91:section-01` | `docs/design/issue-91/DESIGN.md:7–14` | 原文/归档/metadata一致；resources, package, inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-91:section-02` | `docs/design/issue-91/DESIGN.md:15–25` | 原文/归档/metadata一致；inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-91:R01` | `docs/design/issue-91/DESIGN.md:26–39` | 原文/归档/metadata一致；accepted-combinations, affected-database, inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-91:R09` | `docs/design/issue-91/DESIGN.md:142–154` | 原文/归档/metadata一致；receipt-boundary, affected-database, inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-91:R10` | `docs/design/issue-91/DESIGN.md:155–175` | 原文/归档/metadata一致；receipt-boundary, affected-database, inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-91:R11` | `docs/design/issue-91/DESIGN.md:176–184` | 原文/归档/metadata一致；inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-91:R12` | `docs/design/issue-91/DESIGN.md:185–199` | 原文/归档/metadata一致；receipt-boundary, affected-database, inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-91:section-15` | `docs/design/issue-91/DESIGN.md:200–204` | 原文/归档/metadata一致；resources, package, inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-91:P17` | `docs/design/issue-91/ACCEPTANCE.md:27–27` | 原文/归档/metadata一致；accepted-combinations, affected-database, inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-91:P18` | `docs/design/issue-91/ACCEPTANCE.md:28–28` | 原文/归档/metadata一致；receipt-boundary, affected-database, inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-91:P19` | `docs/design/issue-91/ACCEPTANCE.md:29–29` | 原文/归档/metadata一致；inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-91:P20` | `docs/design/issue-91/ACCEPTANCE.md:30–30` | 原文/归档/metadata一致；receipt-boundary, affected-database, inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-91:P21` | `docs/design/issue-91/ACCEPTANCE.md:31–31` | 原文/归档/metadata一致；receipt-boundary, affected-database, inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-91:P22` | `docs/design/issue-91/ACCEPTANCE.md:32–32` | 原文/归档/metadata一致；receipt-boundary, affected-database, inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-91:P23` | `docs/design/issue-91/ACCEPTANCE.md:33–33` | 原文/归档/metadata一致；inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-91:N23` | `docs/design/issue-91/ACCEPTANCE.md:61–61` | 原文/归档/metadata一致；accepted-combinations, affected-database, inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-91:N24` | `docs/design/issue-91/ACCEPTANCE.md:62–62` | 原文/归档/metadata一致；receipt-boundary, affected-database, inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-91:N25` | `docs/design/issue-91/ACCEPTANCE.md:63–63` | 原文/归档/metadata一致；inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-91:N26` | `docs/design/issue-91/ACCEPTANCE.md:64–64` | 原文/归档/metadata一致；receipt-boundary, affected-database, inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-91:N27` | `docs/design/issue-91/ACCEPTANCE.md:65–65` | 原文/归档/metadata一致；receipt-boundary, affected-database, inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-91:N28` | `docs/design/issue-91/ACCEPTANCE.md:66–66` | 原文/归档/metadata一致；receipt-boundary, affected-database, inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-91:N29` | `docs/design/issue-91/ACCEPTANCE.md:67–67` | 原文/归档/metadata一致；inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-91:N30` | `docs/design/issue-91/ACCEPTANCE.md:68–68` | 原文/归档/metadata一致；receipt-boundary, affected-database, inherited的片段适用性已核对；whole NOT RUN。 |
| `issue-91:N31` | `docs/design/issue-91/ACCEPTANCE.md:69–69` | 原文/归档/metadata一致；inherited的片段适用性已核对；whole NOT RUN。 |

## 18 AC原文闭包

| AC | 原始验收行 | 本次范围／保留状态 |
| --- | --- | --- |
| AC01 | `docs/design/issue-92/ACCEPTANCE.md:13` | 原文与r1/r2指针一致；Database部分与实际证据逐项检查，whole NOT RUN。 |
| AC02 | `docs/design/issue-92/ACCEPTANCE.md:14` | 原文与r1/r2指针一致；Database部分与实际证据逐项检查，whole NOT RUN。 |
| AC03 | `docs/design/issue-92/ACCEPTANCE.md:15` | 原文与r1/r2指针一致；Database部分与实际证据逐项检查，whole NOT RUN。 |
| AC04 | `docs/design/issue-92/ACCEPTANCE.md:16` | 原文与r1/r2指针一致；Database部分与实际证据逐项检查，whole NOT RUN。 |
| AC05 | `docs/design/issue-92/ACCEPTANCE.md:17` | 原文与r1/r2指针一致；Database部分与实际证据逐项检查，whole NOT RUN。 |
| AC06 | `docs/design/issue-92/ACCEPTANCE.md:18` | 原文与r1/r2指针一致；Database部分与实际证据逐项检查，whole NOT RUN。 |
| AC07 | `docs/design/issue-92/ACCEPTANCE.md:19` | 原文与r1/r2指针一致；Database部分与实际证据逐项检查，whole NOT RUN。 |
| AC13 | `docs/design/issue-92/ACCEPTANCE.md:25` | 原文与r1/r2指针一致；Database部分与实际证据逐项检查，whole NOT RUN。 |
| AC14 | `docs/design/issue-92/ACCEPTANCE.md:26` | 原文与r1/r2指针一致；Database部分与实际证据逐项检查，whole NOT RUN。 |
| AC16 | `docs/design/issue-92/ACCEPTANCE.md:28` | 原文与r1/r2指针一致；Database部分与实际证据逐项检查，whole NOT RUN。 |
| AC17 | `docs/design/issue-92/ACCEPTANCE.md:29` | 原文与r1/r2指针一致；Database部分与实际证据逐项检查，whole NOT RUN。 |
| AC19 | `docs/design/issue-92/ACCEPTANCE.md:31` | 原文与r1/r2指针一致；Database部分与实际证据逐项检查，whole NOT RUN。 |
| AC20 | `docs/design/issue-92/ACCEPTANCE.md:32` | 原文与r1/r2指针一致；Database部分与实际证据逐项检查，whole NOT RUN。 |
| AC21 | `docs/design/issue-92/ACCEPTANCE.md:33` | 原文与r1/r2指针一致；Database部分与实际证据逐项检查，whole NOT RUN。 |
| AC22 | `docs/design/issue-92/ACCEPTANCE.md:34` | 原文与r1/r2指针一致；Database部分与实际证据逐项检查，whole NOT RUN。 |
| AC23 | `docs/design/issue-92/ACCEPTANCE.md:35` | 原文与r1/r2指针一致；Database部分与实际证据逐项检查，whole NOT RUN。 |
| AC25 | `docs/design/issue-92/ACCEPTANCE.md:37` | 原文与r1/r2指针一致；Database部分与实际证据逐项检查，whole NOT RUN。 |
| AC26 | `docs/design/issue-92/ACCEPTANCE.md:38` | 原文与r1/r2指针一致；Database部分与实际证据逐项检查，whole NOT RUN。 |

## Fowler启发式与最终边界

Mysterious Name、Duplicated Code、Feature Envy、Data Clumps、Primitive Obsession、Repeated Switches、Shotgun Surgery、Divergent Change、Speculative Generality、Message Chains、Middle Man、Refused Bequest均按整个diff检查。执行身份已有Execution聚合；字段/状态映射服务既有wire协议；页面局部渲染与资源所有权符合仓库惯例，未发现值得另列的可选smell。工具已覆盖的类型/格式问题不重复包装为评审发现。

S11仍负责全309源项/26AC/12MCE与最终G01–G08、入口最终切换、四安装HTTP、CI、升级与支持回退。原生桌面200%和真实中文IME未运行；移动真机范围外NOT RUN。此PASS只为固定S10首次完整Standards结论，保留原first-red、其他轴结论与全部未完成责任。

硬finding 0；optional smell 0；最高级别无。
