# Dashboard 迁移：新会话 to-spec 入口

用途：把 [#85 Dashboard 设计地图](https://github.com/nineofoursyrup/Agent-Alfred/issues/85) 的已确认决定整理为可实现规格。当前工作是设计归档，产品实施、产品验收与发布均未完成。

## 从哪里接手

- 仓库：`nineofoursyrup/Agent-Alfred`。
- 完整文档归档分支：`codex/issue-92-migration-contract`，同时包含 #86–#92；固定归档提交以 [#92 resolution](https://github.com/nineofoursyrup/Agent-Alfred/issues/92) 的实际 SHA 为准。核对该 SHA 后再引用，不只依赖可移动的分支名。
- 当前本地工作树：`/Users/nineofour/.codex/worktrees/issue-92-migration-contract/Agent-Alfred`。若该目录不再可用，从相同固定提交建立隔离工作树；不要在有用户修改的主工作区 reset／stash／覆盖。
- 产品基线：`22c8720e1ec874fd012cc79b086cd8aace4c79b6`。main 仍是原产品版本；归档分支只有文档与领域词汇变化。
- 当前文档入口：[SPEC.md](SPEC.md)、[SLICES.md](SLICES.md)、[ACCEPTANCE.md](ACCEPTANCE.md)、[VALIDATION-PLAN.md](VALIDATION-PLAN.md)、[source-index.json](source-index.json)、[acceptance-map.json](acceptance-map.json)、[PUBLICATION.md](PUBLICATION.md)。

## 权威来源与读取顺序

1. 仓库 `CLAUDE.md`、`docs/agents/issue-tracker.md`、`docs/agents/domain.md`；领域词汇使用 `CONTEXT.md` 和相关 ADR，不另建平行词汇表。
2. #85 Destination／Notes、已发布决策摘要，以及 #92 的规范与来源索引。源索引固定 #86–#91 的提交和文件摘要；#89 现为已归档来源，不再依赖另一个本地未提交目录。
3. 按工作主题展开各源规格全文：#86 视觉及 baseline/r1，#87 壳层，#88 Overview，#89 收件箱／运行，#90 Memory／Behaviour／Tools，#91 Models／Connections／Ops／Database。聚合验收表不能替代源合同。
4. #92 的映射保留 72 条用户路径、128 条源反例、81 个规范段、22 项迁移能力、6 项读取缺口；另有 26 项 AC 和 12 条迁移反例。它们是后续验证责任，全部产品结果仍 NOT RUN。
5. 读取 [ARCHIVE-VALIDATION.md](ARCHIVE-VALIDATION.md) 与 manifest 核对交接材料。历史副本只用于追溯，不将旧 BLOCKED 或后来文档 PASS 误读为当前产品验收。

## 已决定的边界

- 十页、中文深色、#86 A 基线；唯一 MainBar／Stream，旧九路由及 Run 深链接兼容；最终默认首页为 Overview。
- 10 个实现切片＋1 个最终集成片。S01 共享读取、S02 壳层和公共呈现；S03／S04 依赖两者，S05–S10 依赖 S02；跨片完整路径由 S11 收口。S03 先于 S04 是排程建议，不是硬依赖。
- 各片独立实现与评审，汇入迁移集成分支；十页整体验收后才具备进入 main 的资格。开发混合布局不成为正式双布局开关，最终入口变更须进入验收候选。
- 移动端只做 Chromium 视口／触控模拟。iPhone／iOS Safari／真实移动软键盘明确范围外且未执行，不是本次必过门槛。桌面真实 200% 缩放和中文 IME 仍必需；历史 BFCache BLOCKED 保留。
- 正常关闭、确认收尾、整包切换、重启、旧标签页显式刷新；支持目标回退保留 Session、消息、记忆、账目、设置及遗忘事实，不回灌旧数据库。未释放时不删锁强行并启。
- S08 须修复已有显示名／最后一维覆盖价清空，且与 UI 变更分别可追踪；来源、计价、授权、记录、正文、trace、清理和保护寿命不因统一视觉而合并。
- 全部外部验证默认离线、隔离本地状态。文档归档和决策票关闭不授权产品实现、真实模型调用、main 合并或发布，也不改变 v1 发布门槛。

## 新会话的工作目标

使用 `/to-spec` 复用现有 r1 整合整张地图，不重做已解决的访谈。结合实际源码明确 S01／S02 的公共接口参数、返回形状、身份／错误／清理协议及消费者责任；检查其余切片与既有实现和测试接缝的对应关系。私有命名和常规模块划分由执行者决定；只有新发现会改变产品行为、数据边界或验收范围的实质冲突才重新 grilling。

输出可供后续 `/to-tickets` 使用的实现规格及真实阻塞关系。是否在该新会话创建票据、发布规格或继续实施，以用户在新会话的明确要求为准；此入口本身不启动后续动作。
