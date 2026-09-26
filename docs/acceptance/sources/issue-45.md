# #45 实现：记忆遗忘与来源隔离

来源：https://github.com/nineofoursyrup/Agent-Alfred/issues/45

## Parent

Part of [地图：Agent-Alfred v1](https://github.com/nineofoursyrup/Agent-Alfred/issues/1)；实施来源：[看见记忆决策](https://github.com/nineofoursyrup/Agent-Alfred/issues/31)。

## Memory 裁决规范（权威补充）

[完整独立规范](https://github.com/nineofoursyrup/Agent-Alfred/issues/31#issuecomment-5588135839)。本段明确修订前文不一致的旧表述；其余原范围保留。证据基线 `55418dad266eed4df8a3e40670a069e3dcddc65d`；实施须核查实际起点，不能用旧主工作目录的HEAD替代。

### What to build

R09–R10、R13–R14的生命周期协议：从共享delete到来源传递隔离、trace顺序屏障、无正文失效与受管清理恢复的可验证公共服务路径。以注入端口独立验收；T16/T18/T19/TM负责其真实消费者接入。

### Acceptance criteria

- [x] A28：Given 来源R1→读取R2→读取R3及无关R4；When 遗忘；Then 已知闭包隔离R1/R2/R3，R4保留；无正文关联重启稳定。
- [x] A29：Given 历史关联缺失；When 遗忘并重启；Then needs_scope，未假称完成；用户指定范围后才能完成相应隔离。
- [x] A30：Given 受管内容投影、历史会话/trace、独立同义记忆；When 遗忘；Then 投影清理；人工历史和独立记忆保留且不误称全域擦除。
- [x] A34：Given 删除已提交，文件端口失败或进程在边界中断；When 同operation重试/重启；Then cleaning/failed诚实，旧副本禁读，最终complete无复活。
- [x] A54：Given 删除前trace停在真实异步队列，注入写失败/成功；When 尝试删除并释放barrier；Then 失败不执行删除；成功先确定历史写入再删，不提前封存Run，不让旧正文在删除后晚到落盘。

本票须阅读完整规范中上述R段与既有ADR。主责以外的接口实现由相应票验收，不能用替身PASS冒充下游已集成。原票已有的其它验收仍有效。

### Blocked by

- [施工 17](https://github.com/nineofoursyrup/Agent-Alfred/issues/17)

GitHub原生依赖是权威；本清单供独立阅读。阻塞清零与全票规范就绪是两件事。

### 实施与授权

独立agent、TDD、适用机械检查及固定候选Standards/Spec独立两轴评审。只做本票；先核查认领与worktree。没有自动提交、推送、合并、发布或关闭票授权。缺失且影响产品验收的选择交调度agent与用户，不猜默认。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/45#issuecomment-5602762043

## Issue #45 v27 验收总结

本票核心端口已验收。依据 #31 权威裁决、已批准的 A29 补充、固定 v27 验收映射与原评审任务外部 Standards PASS0 / Spec PASS0，所有已确认发现均关闭。

### 逐项验收

| 项目 | 核对结果与确定性证据 |
| --- | --- |
| A28 | PASS：R1→R2→R3 已知闭包原子隔离，无关 R4 保留，无正文关联跨重启稳定。覆盖 `known_closure_is_atomic_and_survives_restart`、晚到来源/读取和多操作后继测试。 |
| A29 | PASS：A29-01–24 全部映射已执行；固定历史范围、确认时成员冻结、部分确认保持 needs_scope、跨 Session、旧版本首次确认 stale、成功动作原回执重放、后继传播、四用途未知范围禁读、无关组保留及重启恢复均覆盖。补边不推断全局来源完整；范围全解决且清理/重建/验证完成后才 complete。 |
| A30 | PASS：受管投影清理，人工历史及独立同义记忆保留。覆盖 `independent_same_content_and_raw_history_survive_projection_erasure`、同事务投影失效与意图、v5 删除记录升级恢复；不宣称全域擦除。 |
| A34 | PASS：文件失败、中断、同 operation 重试与重启恢复；共享目标、多操作、失败记账不可写及重新发现投影回滚后持续禁读；原删除回执保持。needs_scope 优先，失败明细诚实；资源 retry 不替代重建与验证完成。 |
| A54 | PASS：真实异步 trace 队列的非终结屏障；写入/fsync/超时/部分写/溢出失败不执行删除，成功先确定历史持久前缀再删，Run 后续仍可写，成功屏障后事务回滚受测。 |

完整逐项映射：[实现与验收说明](https://github.com/nineofoursyrup/Agent-Alfred/blob/772b02d1c5181a6e90aa9f7675f498e109a49313/docs/implementation/issue-45-forgetting.md#验收对应)。确定性测试源码：[遗忘公共路径](https://github.com/nineofoursyrup/Agent-Alfred/blob/772b02d1c5181a6e90aa9f7675f498e109a49313/src/agent_alfred/evals/deterministic/test_forgetting.py)、[真实 trace 屏障](https://github.com/nineofoursyrup/Agent-Alfred/blob/772b02d1c5181a6e90aa9f7675f498e109a49313/src/agent_alfred/evals/deterministic/test_forgetting_trace.py)。

### 固定候选、评审与测试证据

- [PR #48](https://github.com/nineofoursyrup/Agent-Alfred/pull/48) 已按 merge commit 合并；候选提交 `3b67deb5409cf4cfb4ea22f25b8e85b47c003fc6`。
- [合并提交 772b02d1c5181a6e90aa9f7675f498e109a49313](https://github.com/nineofoursyrup/Agent-Alfred/commit/772b02d1c5181a6e90aa9f7675f498e109a49313)；合并 tree `921ad242b997005c54a7448a57ca5fe2b1e71d84` 与已审 24 文件候选一致。
- v27 manifest SHA256：`8964983fd649d85617c65d51059bc64e60bd5287d87377ecff3a1c70b3f713d6`；完整 diff SHA256：`a2d023c1feaa7f8173fbaa1796ae9e4a449ee3247d6ad7fd09ba023ef49098cf`。
- 外部原评审任务：Standards PASS0 / Spec PASS0；父评审独立复跑确认。Standards 19 个真实 SIGINT 边界全部通过，另 12 恢复场景通过；Spec 原 P2 关闭，12 项多操作/状态/重启/恢复矩阵通过。
- [PR CI](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34355020041) 与 [合并后 CI](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34355948892) 均 SUCCESS。合并后日志：Python `3258 passed, 1 deselected in 235.79s`；浏览器 `57 passed (39.4s)`；Ruff、技能校验、环境一致性、Dashboard typecheck 全部通过。需密钥测试按项目默认排除，没有付费模型调用。
- v27 本地定向 298 passed；wheel/sdist 非 editable 隔离安装各 290 passed，真实 CLI --help 通过，归档/安装源码与候选逐字节一致，scratch 排除；外部父评审独立复验现有产物各 290 passed。
- 本地证据归档保留于 `/Users/nineofour/Agent-Alfred-issue-45/.scratch/issue45/`：`EXTERNAL-REVIEW-v27.md`、`HANDOFF-v27.md`、`artifact-verification-v27.json`、`candidate-v27/manifest.json` 与 `candidate-v27/candidate.diff`（本地材料未作为仓库文件发布）。

### 下游交付边界

本次勾选和 completed 仅确认 #45 核心服务及端口合同：

- #16：真实工作窗口先过滤再选组、gate/回答安全输入及实际读取登记。
- #18：真实提炼批次、业务证据、Markdown 镜像与恢复接线。
- #19：真实 ToolRegistry 权限传递、删除后无下一次模型请求及固定收尾。
- #46：确认 UI、HTTP/幂等恢复、通知 wire/重连/迟到响应与人工历史入口。

上述真实消费者接入仍由各票交付和验收，不能以本票注入端口 PASS 或既有浏览器回归宣称产品全链路完成。本票验收证据齐全，按用户授权以 completed 关闭；不发布版本、不修改其他 Issue、不删除分支或 worktree。
