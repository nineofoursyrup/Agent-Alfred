# S06 r2 Spec 判断依据与适用性

评审对象为 `S06-r2-Spec.md` 的完整 base→head 13 文件候选；没有读取当前 S06 Standards 结论。评审仅写入本 reviews 目录，不修改产品、合同、原 evidence、ledger 或 GitHub。完整文件列表、36 原文 hash／owners、21 AC、两轮 evidence 与包字节校验见 `S06-r2-Spec-machineverification.json`，可运行同目录 `S06-r2-Spec-audit.py` 复核。

## 需求判断

| 原始要求 | 当前候选的行为与依据 | 结论 |
| --- | --- | --- |
| `docs/design/issue-90/DESIGN.md:105–106`：保存值、选择、回执和恢复分别表达；冲突、磁盘外改、损坏等不混同。`DESIGN.md:48–54`：刷新不接受新版本，旧回执不清后继输入。 | `behaviour.js:31–35,60–73,76–136` 分别持有 current/baseline/submittedChoice；POST 使用原 baseline revision，恢复使用当前原文件指纹；未知回执保留身份，rebase 是单独显式动作。既有 CE-13 真实外改／备份／重启检查继承，当前27中的 CAS、延迟、丢失回执验证保存后真实 GET 与未提交输入。 | 未发现违背 |
| `docs/design/issue-90/DESIGN.md:49–54,108,112` 和 `issue-87/SPEC.md:236`：冻结原对象、参数；只有真正后继编辑触发守卫；离页不撤销已接受请求。 | `aggregation.js:105–108,130–160,255–282` 独立冻结请求与表单快照，刷新会话保留当前明确选择；202 才登记 Run，丢失响应保持 unknown，无重新 POST 路径。当前27覆盖逐字段修改／恢复、明确拒绝、原 Session、实际导航和后继草稿保留。 | 未发现违背 |
| `docs/design/issue-90/DESIGN.md:107–113`，`issue-27-manual-aggregation-spec.md` Q10–Q14、F01–F06、CE-02/04/05/10/11/12/14/16：未选资料不读、发送前复核、独立草稿、NoAction、候选及未保存分别表达。 | 后端与176检查的生产路径和测试文件相对 r1 未变；已读 `test_aggregation_safety.py` 公开发送屏障／真实许可及 prepared-not-sent 覆盖。浏览器当前27保留全来源组合、实际轮数、丢202、资料遗忘 opened/late 和固定目标路径；原22与相关 successor 支持非法候选、未保存和 interrupted。`aggregation.js:181–242` 匹配 Run/Session/instance，以持久 Run 的 interrupted 优先于旧 trace；正式正文仅进入唯一 MainBar。 | 未发现违背 |
| `docs/design/issue-90/DESIGN.md:111`、`ACCEPTANCE.md:55,64`：当前资料读取及遗忘后失效；历史草稿不能补回旧资料。 | 当前 `aggregationFacts` 与 accepted S03 base 的函数一致；`aggregation.js:111,168,289` 消费返回 cleanup，Run owner 原 cleanup 未被覆盖。当前27真实 Behaviour→Run→Behaviour 用例保留节点引用并验证 disconnected 节点 `textContent=''`；同轮 opened/late 遗忘分别覆盖 Behaviour、MainBar、Run。 | 未发现违背 |
| `docs/design/issue-90/DESIGN.md:66–67,119–126` 与 `ACCEPTANCE.md:49–52`：图文同源、两图独立、统计版本/分母以及不同失效寿命。 | `topology.js` 调整信息顺序和折叠摘要但保留读取身份／结构校验／视口；`routing-statistics.js:33–110,123–154` 整体校验样本与 n/d 后一次发布条图/数表，unknown 不显示百分比、零分母无比例条，失效撤值。r1整轮60仍适用；当前候选拓扑/statistics 产品字节相对 r1未变，受影响历史Run五项在当前27复核。 | 未发现违背 |
| `docs/design/issue-90/DESIGN.md:15–36,142–155`、`issue-89/DESIGN.md:241–271`、`issue-87/SPEC.md:185–237`：默认信息结构、长内容、摘要、唯一主对话及广段继承责任。 | 分流→聚合，两个流程与统计默认收起；失败摘要位于折叠内容外；正文/技术定义可用键盘展开并局部滚动。页面未创建新 Stream，只有显式 locateReply 可切原 Session。沿完整 broadsection 读到其他页责任后，仅裁定 S06 入口、保留与接缝，未把那些页的完整实现计入本片通过。 | 未发现违背 |

代码引用均以 `src/agent_alfred/ops/static/` 为根。本次产品拆分、统计信封校验、提交快照与显式 rebase 均用于落实原需求；没有新增业务接口、自动来源、工具权限或正式回复缓存，未发现未经要求的产品范围。

## 证据适用性和边界

- 当前 `combination-r1.log` 的27项逐条回读：设置CAS与响应、聚合各后继字段及固定目标、真实遗忘、实际 owner 清理，以及五项共享 Run-path。并非60或70项全量重跑。
- 原 `browser-r1.log` 总共130项，125 PASS／5 FAIL；其中 Run-path 子集为68 PASS／2 FAIL。两个 Run-path successor 位于原 `browser-r2.log`，不将整个该轮（14 PASS／1 FAIL）改写成全绿。原其余失败与后继链也保持原文件字节，当前 relevant27再次通过。
- 176后端、60原整轮、22原最终聚合、19原S02组合以各自原候选和范围复用。r2没有改后端、fixture或相关依赖锁；当前 S03/S08 组合改变的 dirty/cleanup/Run 接缝已用27项单独补证。原 nativeSSE/Host/SQLite 测试通过不意味着供应商或付费执行。
- 当前 machine audit 没有重新启动产品服务：独立校验 clean/head/tree、完整diff、source-map metadata／archive bytes、r1 212与r2 78份 evidence、accepted base共享实现、两份包资源及依赖锁。两个包的31资源 hash确实逐字节匹配候选，而安装后四组HTTP仍未运行。源码HTTP证据绑定当前head/tree；不能提升为安装矩阵。
- 人工查看原320px流程与390px统计截图，确认文字清单、选中节点明细、状态和对应n/d可读；原4视口/679–680/1099–1100/触控几何为继承观察。没有把截图、缩放图或模拟输入当作 native200%／真实IME。
- 21AC中其他页直接操作（如完整trace导出、Memory维护、Tools授权、Models设置、Ops及Database）仅因 broadsection 映射而被S06继承；其完整产品责任保留给对应片和S11。S06新增入口及共享实现保持已经核验，whole source/AC/G仍NOT RUN。
- 本次没有具体未解决的实现疑点需要额外窄探针，因此复用适用确定性检查。没有重跑广测，也未使用18060–18069或触碰真实Host/用户状态。历史BFCache BLOCKED及移动排除与主报告一致。

Spec findings：0。所列最终集成缺证是明确保留的S11责任，不作为本片实现PASS的替代证据。
