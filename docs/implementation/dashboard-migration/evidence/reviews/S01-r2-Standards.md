# S01 r2 — 独立 Standards review

**结论：FAIL。** 固定范围：18文件，`git diff base...head`；只读评审，无产品修改。

- base：`7d87243ff715eaecc42159f35aa9f223c4ef8ce7`
- head：`93c87bc5bf2a38cf92e2df27c743f50f59df7620`
- tree：`c4b5fedf0f390013fe2fd1106b49ce9c7dd77b0b`

## Documented hard violation

**[P2] 消息锚点未沿用公共SQLite整数域校验。** [/Users/nineofour/.codex/worktrees/dashboard-s01/Agent-Alfred/src/agent_alfred/runtime/source_locations.py:63](/Users/nineofour/.codex/worktrees/dashboard-s01/Agent-Alfred/src/agent_alfred/runtime/source_locations.py:63) 仅检查 `type(value.get("id")) is not int` 与 `value["id"] < 1`，随后直接绑定SQLite。可规范编码的 `id=9223372036854775808` 因此通过校验。真实隔离Dashboard HTTP复现返回 **500 internal_error**，而 [I04:99](/Users/nineofour/.codex/worktrees/dashboard-s01/Agent-Alfred/docs/design/dashboard-implementation/INTERFACES.md:99) 明定损坏锚点 **400 invalid_anchor**；[公共cursor:1–9、33–41](/Users/nineofour/.codex/worktrees/dashboard-s01/Agent-Alfred/src/agent_alfred/runtime/cursor.py:33) 明定统一整数域。复用 `parse_cursor_position_int` 并将其失败映射为 `InvalidAnchor`，补该边界回归。[复现证据](S01-r2-anchor-repro.json)：0模型请求，服务正常关闭；首次复现夹具错误单独保留。

## Optional heuristic smell

**possible Duplicated Code（P3，非阻塞）**：[runs.py:1176](/Users/nineofour/.codex/worktrees/dashboard-s01/Agent-Alfred/src/agent_alfred/runtime/runs.py:1176) 的 `for key in ("purpose", "filter", "purpose_known", …)` 与 [api.py:1047](/Users/nineofour/.codex/worktrees/dashboard-s01/Agent-Alfred/src/agent_alfred/gateway/web/api.py:1047) 的 `key: _run_json(run)[key] for key in ("purpose", "purpose_known", "filter", …)` 重复维护同一元数据白名单，后者每字段重建整份投影。可收敛为同一命名投影；本项不构成明文违规。

## 检查依据与限制

18个文件manifest与固定head逐一吻合。已读I00–I05、S01完整来源项及适用领域/ADR；复用backend 678、定向57、回复27、边界10、旧browser39及Ruff/typecheck记录，不合计重叠测试数。未重复全测；完整Python中止，仍为NOT RUN，最终项目门禁归S11。工具已强制事项不列人工finding。

汇总：Standards **1项硬违规（最严重P2）＋1项可选启发式（P3）**；Spec未评判，未读取另一reviewer结论。
