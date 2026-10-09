# S01 r6 — 独立 Standards review

**结论：PASS；0 项硬违规，0 项新增 optional smell。** 既有可选 P3 不变；最高严重度仍为非阻断 P3。

- Base: `e40f006b54817380807fe62372374c0013ce2398`
- Head: `7f4bce39e452762dcbbb1e97cce566a85ab029b9`
- Tree: `5597c29efc6d1abf934c7bb4757b9ce7a061882f`

完整审阅 base→head 三个测试文件、44+/7−；manifest 的固定字节／hash 全匹配，无生产、合同、依赖或资源改动。复用 [S01 r5](S01-r5-Standards.md) 的未变生产范围，原 r5 证据 hash 保持。未读取另一轴报告。

## Documented hard violations

无。`test_routing_baseline.py:83–105` 仅排除获准新增的 `message_anchor`，排除前以真实 SQLite 持久行核验 version、kind、Session、segment、row ID，并用 `zip(strict=True)` 检查数量、Run ID 与 role。原最终 `business_contract(results[0]) == business_contract(results[1])` 未改，旧请求、结果、事件、消息、引用仍精确比较；probe 的空消息路径不跳过旧比较。符合 `INTERFACES.md` I04 稳定行锚点及 I09 兼容要求，不是通用忽略新字段。

`test_dashboard_evidence.py:216–259` 为实际 failed gate／ordinary fallback 两次调用分别产生 `failed-attempt-1/2`；事件、ModelError 和 AttemptRecord 使用同一当次身份，明确断言调用数、两个 aborted 及各自 7 output tokens。原 recorded、完整 trace、终态错误断言保留，符合 CONTEXT 的 Run／Step／Attempt 身份及 ADR-0024 记录事实边界。`runtime/telemetry.py:109–110` 的重复身份拒绝规则未改；旧失败数据库确实有两个同名 Attempt，属于夹具输入错误。

`test_web_api.py:889` 仅在旧内存 facade 的严格对象期望中增加 `message_anchor: None`；旧字段未删。

## Optional heuristic smells／验证

按完整 12 项 Fowler baseline 判断本次增量，无新增可操作 smell；既有 S01 元数据投影重复 P3 不在本次测试修复中处理，未重复列工具强制项。

读取并复用 **92 相关 PASS**，含三个完整文件及坏 telemetry 保护；Ruff／固定 diff --check 通过。首次 CI **6400 PASS / 8 FAIL**、本地 **8 FAIL / 1 PASS** 和后继记录均保留，不将局部 PASS 当完整 CI。未重跑无影响检查；集成后 CI successor 与最终门禁仍由协调器负责。
