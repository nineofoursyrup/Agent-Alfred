# #46 实现：切片④b — 看见记忆（Memory 编辑页）

来源：https://github.com/nineofoursyrup/Agent-Alfred/issues/46

## Parent

Part of [地图：Agent-Alfred v1](https://github.com/nineofoursyrup/Agent-Alfred/issues/1)；实施来源：[看见记忆决策](https://github.com/nineofoursyrup/Agent-Alfred/issues/31)。

## Memory 裁决规范（权威补充）

[完整独立规范](https://github.com/nineofoursyrup/Agent-Alfred/issues/31#issuecomment-5588135839)。本段明确修订前文不一致的旧表述；其余原范围保留。证据基线 `55418dad266eed4df8a3e40670a069e3dcddc65d`；实施须核查实际起点，不能用旧主工作目录的HEAD替代。

### What to build

R01/R06–R14的HTTP/页面接入及完整浏览器路径：三标签、只读SkillCatalog、CRUD/回执/检索证据/统计、队列/镜像、失效与恢复。复用已完成MainBar/Session；不做Skill匹配注入。

### Acceptance criteria

- [x] A35：Given 两标签、旧读取被barrier延迟；When 一标签删除、再释放旧读取；Then 在线详情/检索/草稿清除，迟到响应不恢复正文。
- [x] A36：Given 已显示正文的页面断线；When 重连时记录已删；Then 断线隐藏，核验不存在，不重放缓存正文。
- [x] A37：Given 当前记录编辑/删除/读取故障；When 展开旧gate引用；Then 新版本标明，删除无旧文，故障不当不存在，无相关度分数。
- [x] A38：Given 相同创建时刻、多页、查询改变/数据变更；When 翻页和刷新；Then 内部稳定顺序，cursor绑定查询，无按MemoryId排序；陈旧游标明确重读。
- [x] A40：Given 空库、无结果、存储错、版本冲突；When 页面操作；Then 独立状态，失败不冒充空，冲突保草稿、已删清草稿。
- [x] A41：Given builtin/user覆盖、重复名、非法名称；When 构建Catalog/list/load；Then 整体覆盖透明、重复启动失败、名称只查索引、正文只读。
- [x] A42：Given Skill尚无匹配功能、文件已修改；When 页面查看/重启；Then 无伪造匹配状态，重启才重载，不出现编辑/注入控制。
- [x] A47：Given 队列failed/awaiting/invalidated、宿主忙；When 页面重试/批准；Then 安全状态和操作明确，409不排队，不绕阈值。
- [x] A52：Given 全部真实组件、ScriptedModel、临时SQLite及浏览器；When 明确保存→下一Run命中→来源→编辑冲突→删除→再查；Then 页面与业务证据一致；删除后受管路径无正文且旧ID不命中；历史边界可核对。
- [x] A55：Given 无活动Run的手动变更、通知队列溢出、旧进程帧；When SSE失效及重连；Then memory_patch不伪造Run/seq/checkpoint；不可投递则重连核验，旧revision不能恢复正文。

本票须阅读完整规范中上述R段与既有ADR。主责以外的接口实现由相应票验收，不能用替身PASS冒充下游已集成。原票已有的其它验收仍有效。

### Blocked by

- [施工 17](https://github.com/nineofoursyrup/Agent-Alfred/issues/17)
- [实现：记忆遗忘与来源隔离](https://github.com/nineofoursyrup/Agent-Alfred/issues/45)
- [施工 19](https://github.com/nineofoursyrup/Agent-Alfred/issues/19)
- [施工 16](https://github.com/nineofoursyrup/Agent-Alfred/issues/16)
- [施工 18](https://github.com/nineofoursyrup/Agent-Alfred/issues/18)
- [施工 36](https://github.com/nineofoursyrup/Agent-Alfred/issues/36)

GitHub原生依赖是权威；本清单供独立阅读。阻塞清零与全票规范就绪是两件事。

### 实施与授权

独立agent、TDD、适用机械检查及固定候选Standards/Spec独立两轴评审。只做本票；先核查认领与worktree。没有自动提交、推送、合并、发布或关闭票授权。缺失且影响产品验收的选择交调度agent与用户，不猜默认。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/46#issuecomment-5645328177

<!-- issue-46-final-closeout -->
## 已完成验收

Memory 编辑页与 HTTP 接入已合入 main：功能 PR #52、浏览器恢复用例时限修正 #53、在途 HTTP 关闭排空修复 #54。最终合并提交 `c002a963676d5d26526c24be882c1dfc2a0af4ce`。

| 验收项 | 已验证行为 |
| --- | --- |
| A35 | 双标签删除后清除详情、检索与草稿；延迟旧读取不能恢复正文。 |
| A36 | 断线隐藏正文，重连回读不存在事实，清理已删除记录草稿；读取故障不当作删除。 |
| A37 | 历史引用回查当前版本；修改、删除、读取故障分别呈现，无伪造相关度分数。 |
| A38 | 相同创建时间稳定分页，cursor 绑定查询与数据修订；陈旧游标明确重读。 |
| A40 | 空库、无结果、读取失败、忙态和版本冲突分别处理；冲突保草稿、确认已删清草稿。 |
| A41 | SkillCatalog 启动索引、整体覆盖、重复/非法名称拒绝、缺目录不冒充空目录，正文只读。 |
| A42 | 无匹配/编辑/注入控制；文件变化在重启后才加载。 |
| A47 | 提炼队列批准、重试、拒绝与失效状态明确；忙时 409 不排队、不绕阈值，历史队列可翻页和精确筛选。 |
| A52 | 真实 Dashboard、Host、临时 SQLite/FTS、遗忘/提炼/镜像与 ScriptedModel 完整浏览器链路：明确保存、下一 Run 检索命中、来源、编辑冲突、删除、再查不命中；受管正文及历史边界可核对。 |
| A55 | 无 Run 的手动变更通过真实 SSE 失效；溢出关闭连接并重连核验，旧进程或倒退修订不能恢复正文，不伪造 Run/seq/checkpoint。 |

实现与测试索引：[验收映射](https://github.com/nineofoursyrup/Agent-Alfred/blob/c002a963676d5d26526c24be882c1dfc2a0af4ce/docs/implementation/issue-46-memory-page.md)。额外回归覆盖 HTTP 200 损坏回执、未确认命令及队列/镜像动作刷新恢复、离页后的受管正文清理、范围选择及迟到回读，以及 HTTP/SSE 关闭前依赖排空。

## 最终验证

- 同一冻结候选独立 Standards / Spec 均 PASS，0 项待修问题；提交树与被审/被测文件哈希一致。
- 本地完整 Python：3772 passed、1 deselected；浏览器：142 passed；Ruff、skills、环境示例、typecheck、diff 与哈希检查通过。
- PR #54 CI：[34687665653](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34687665653)，绑定 `01fea71187bea639d692aebb1f7c4dda536b8497`，SUCCESS。Python 3772 passed / 1 deselected，浏览器 142 passed。
- 合并后 main CI：[34688103884](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34688103884)，绑定实际合并提交 `c002a963676d5d26526c24be882c1dfc2a0af4ce`，SUCCESS。Python 3772 passed / 1 deselected，浏览器 142 passed。
- #17、#45、#19、#16、#18、#36 原生依赖均已 CLOSED / COMPLETED。

此前两次合并后 CI 的失败已保留：#52 的长恢复用例整条时限问题由 #53 修正；#53 后观察到子进程 SIGSEGV，#54 修复真实复现的 HTTP 读取未排空即释放资源缺口并启用 faulthandler。未取得证明 SIGSEGV 与该缺口同源的原生栈，最终以新提交两阶段 CI 的实际结果验收。

边界：使用离线 ScriptedModel，未调用付费模型；项目默认 excludes requires_key（1 deselected）。原始会话、既有 trace 与外部副本不追溯擦除；不将本票验收当作 Skill 匹配/注入或其他未交付下游功能的完成。

本地冻结包、两轴评审、红绿复现、门禁及发布账保存在 `/Users/nineofour/Documents/ChatGPT/reviews/issue-46-2026-09-12/`。
