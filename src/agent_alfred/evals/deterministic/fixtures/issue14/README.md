baseline-*.json：由未改形基线 4aa34f22d16ec5c1d58c191c5179ace0cc22fdd1 的 trace._prepare_payload 产出，禁止改形后重新生成。不是模型实测。
openai-tool.json / anthropic-tool.json：按 docs/research/provider-protocol-diff.md §3 的厂商文档示例形状改写城市、工具名与不透明 ID，统一语义以比较投影。非真实请求、非录制响应。

xAI ticks 换算依据：[官方 Cost Tracking](https://docs.x.ai/developers/cost-tracking)，2026-09-06 只读核验；1 USD = 10^10 ticks。测试使用文档数值，不代表本轮实际费用。
