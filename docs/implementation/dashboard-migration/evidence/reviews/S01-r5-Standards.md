# S01 r5 — 独立 Standards review

**结论：PASS；0 项硬违规，1 项未变的可选 P3。** 结论仅覆盖 S01 固定候选，不代表跨片组合或最终验收。

- Base: `7d87243ff715eaecc42159f35aa9f223c4ef8ce7`
- Head: `3c0cd57edfb9f67e78130ca8ed94c6dbb24fda5a`
- Tree: `0472b276c7d7520c006ebb2d3f4249a4df66a7a3`

复用 [r4 Standards](S01-r4-Standards.md) 未变范围，独立核查 r4→r5 两文件、68+/4−：`runtime/source_locations.py` 与 `evals/deterministic/test_dashboard_shared_reads.py`。完整 18 文件 manifest 的 SHA-256、字节数与固定 head 逐项相符，文件集合与 base→head 差异一致。

## Documented hard violations

无新增项。`source_locations.py:165–188` 取至多 limit 个真实较新邻项，将额外一项的完整 `(activity_revision, run_id)` 交给既有严格排他 cursor；不再以 revision+1 丢弃次级排序身份。邻项不足时 `page_limit=min(limit,len(newer)+1)` 终止于目标；普通 reader 仍生成目标之后的下一段游标。活动目标沿原分支 limit=1、next_cursor=None。符合 `docs/design/dashboard-implementation/INTERFACES.md` I04 的目标包含、有界较新邻项、既有顺序及游标兼容规则；未引入写操作、重定义 ID 或另建分页解析器。

## Optional heuristic smell

未新增。r4 的 **possible Duplicated Code（P3，非阻塞）** 原样保留：`runtime/runs.py:1176` 与 `gateway/web/api.py:1047` 重复 Session Run 元数据字段白名单；可后续提取共同命名投影。本次无需为此扩围。

## 检查适用性

已读取 `composite-location-r5-first.log` 的 2 FAIL 及 `composite-location-r5-successor.log` 的 **264 PASS**。新增真实读侧测试覆盖同／不同 revision、三目标×limit1/2、普通 cursor 完整后续及 Host snapshot／模型请求数不变，断言针对独立行为失效。复用未变检查，不叠加测试数量；Ruff 日志通过，固定 diff --check 通过。未重跑广测、未修改产品、未打开另一 reviewer 报告。完整 Python、安装产物及最终 UI 门禁继续由 S11 负责，原 NOT RUN / BLOCKED 不变。
