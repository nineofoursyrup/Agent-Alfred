# 补充采集失败与诊断

原临时审计把截图后浏览器中 `[style]` 的数量当作产品行内样式。它得到 1，失败原流保留。后继诊断明确唯一元素为 `<input aria-label="IANA 时区" style="">`；Playwright 截图临时隐藏 caret 后恢复属性，留下空 style 属性。产品源码没有 style 写入；首次截图前按全部属性检查为 0，截图后非空 style 为 0。诊断原流在 `capture-inline-style-diagnostic/`，后继审计同时保留两项计数。
