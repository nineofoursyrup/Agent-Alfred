# V04 本轮修复自审

这是实现者自审，不替代独立评审。产品源文件 SHA-256 见 [candidate-source.json](candidate-source.json)。本轮没有改永久测试、公共层、配置或依赖。

| 评审项 | 处理与证据 |
| --- | --- |
| 费用完整性事实 | Attempt／费用未知、模型覆盖与 U > 0 的三类事实都在费用卡，金额之后、视觉标题之前；mixed／interrupted／bad-time 的实际 API 与快照见 screenshots-audit.json。 |
| 架构图完整显示 | 780×480，1:1；1440 滚动区 783/783，其余三视口 ArrowRight 生效；6 节点、6 边、3 分区；文字 bbox 不相交、不压节点边框，连线标签距所属线不超过 12px，连线不沿边框走。 |
| 用途完整换行 | opaque-runs 在 320px 的 future-purpose 完整占两行，91×34px，没有裁剪；结局和记录状态完整。 |
| 节点标题 token | var(--font-size-body)，实测 13px；原稿 14.5px 的刻意偏离与公共 token 建议写入 V04.md。 |
| 几何值 | 明确页内尺寸与描边用字面值；间距 token、padding 和期间 flex-basis 未动。 |
| 重复模板 | 记忆分项模板一处；费用用 metricValue 的 caption/success 参数，caption 移到金额前，保持「精确 USD 8.125」文本顺序。 |
| 面板转换命名 | asPanel 负责面板类、头部构造和 h2 移动，保留其余 section 子节点。 |

临时探针与截图由 capture.mjs 采集；全部永久浏览器测试与 typecheck、静态资产测试结果见 review-r2/gates/。两个本轮采集定位错误原样保留，各自单独归档，没有修改既存首次失败。
