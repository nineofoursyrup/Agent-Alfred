# S01 r4 — Spec review

**结论：FAIL，1 项 P2 片内缺口；不授予片内或整体 AC PASS。**

固定 base `7d87243ff715eaecc42159f35aa9f223c4ef8ce7` → head `5672fbb9e9aa13224bc1d9f07dcd736c033ae0df`，tree `fa4e825ca0895048626bc300fa584b0e9440abf7`。核对 18 个文件的固定差异／manifest、完整来源与 72 项 source hash、19 项 AC 归属、r3→r4 增量及既有检查；未读取其他评审轴报告。

1. **[P2] 会话 Run 定位丢失相同 revision 的 ID 边界。** `src/agent_alfred/runtime/source_locations.py:173` 把选定上界 `(activity_revision, run_id)` 改成 `(revision+1, "")`，于是普通降序分页重新纳入该 revision 的所有更大 Run ID。三条 Run `tie-a/b/c` 同为 revision 100 时，定位 `tie-a`、`limit=1/2` 返回 200 与 `placement=page`，却分别只包含 `[tie-c]`／`[tie-c,tie-b]`，目标不在页中。违反冻结 `INTERFACES.md:95`：“返回定位页包含目标及最多 limit−1 条最近的较新邻项”，也违反 `issue-89/DESIGN.md:54,239` 的原条目恢复与禁止替代。既有 keyset 合同支持相同 revision 的 ID 决胜；须保留完整排序上界并补目标包含／续页行为回归。

已使用固定 head 导出的代码、隔离真实 Host／SQLite 与 ScriptedModel 复现上述两种 limit，未调用真实模型或修改产品。详见 [details](S01-r4-Spec-details.md)、[复现](S01-r4-spec-repro.py)、[原始日志](S01-r4-spec-repro.log)。

检查证据：已核对后端 678 PASS、旧浏览器 39 PASS、r3 受影响 425 PASS、r4 受影响 306 PASS，以及 Ruff／类型检查。未将这些数量相加；r4 首次 HTTP 500→400 失败保留。全 Python 824 passed／1 deselected 后中断，不能称全量 PASS。72 项 coverage 的实施者片内 PASS 不能覆盖本次 I04 实际 FAIL。

修复需绑定新候选，再定向验证受影响定位。S02／S03／S04 消费者组合、S11 的十页／309 源项、最终全门禁、安装后 HTTP、原生 200%／IME 与历史 BFCache BLOCKED 保持独立责任。
