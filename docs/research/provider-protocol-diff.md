# 调研：Anthropic 原生 messages 与 OpenAI-compatible chat completions 的工具调用与流式差异

- 关联票：[#7](https://github.com/nineofoursyrup/Agent-Alfred/issues/7)（本票），前置于 [#2 决定：内部消息、工具调用与模型响应协议的形状](https://github.com/nineofoursyrup/Agent-Alfred/issues/2)
- 调研日期：2026-08-26
- 主源：Claude 平台官方文档（platform.claude.com）、OpenAI 官方 API 参考（developers.openai.com）、各网关官方文档

---

## 0. 一句话结论

**两侧不是「字段改名」的关系，而是「内容块流」与「消息+侧车数组」两种结构的关系。**内部协议应当**以 Anthropic 的内容块模型为基线**（它是两者中信息量更大的一方：一条 assistant 消息里 text / thinking / tool_use 有序共存，且带 block 索引），把 OpenAI-compatible 当作**有损投影**来适配；反方向（以 OpenAI 为基线）会在多工具顺序、思考块、缓存计费、流式增量语义上不可逆地丢信息。

三条最硬的约束：

1. **工具结果的容器不同**：Anthropic 是「一条 `user` 消息装下全部 `tool_result` block」，OpenAI 是「每个 tool call 一条独立的 `role: "tool"` 消息」。内部协议必须把「一批工具结果」建模为**一个原子单位**，否则往 Anthropic 侧渲染时会踩到「分开发会训练模型不再并行调工具」的坑。
2. **流式增量语义不同**：Anthropic 的工具参数是**带 block index 的 partial JSON 增量**，OpenAI 是**带 `tool_calls[].index` 的 arguments 字符串拼接**；两侧都无法在中途拿到合法 JSON，内部协议的流式层必须暴露「块开始 / 增量 / 块结束」三段式，而不是「文本流」。
3. **缓存 token 字段两侧都不通用**：Anthropic 用 `cache_creation_input_tokens` / `cache_read_input_tokens`，OpenAI 用 `prompt_tokens_details.cached_tokens`，DeepSeek 直接另起炉灶用 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`，且 Anthropic 自家的 OpenAI 兼容层把这两个 details 对象**恒置空**。内部 usage 模型必须显式区分「未提供」与「为 0」。

**对建图前提 #1 的修订建议（重要）**：地图 Notes 里写「grok / deepseek / OpenCode Zen / OpenCode Go 都是配置表里的一行 `{..., style}`」。实测官方文档后，**`style` 不能挂在 provider 上，必须挂在 model 上**——见 §8.1，OpenCode Go 同一个 base 下，`deepseek-v4-flash` 走 `/chat/completions`，`qwen3.7-max` 走 `/messages`（Anthropic 风格），`grok-4.6` 走 `/responses`（OpenAI Responses，第三种形状）。

---

## 1. 端点与基线

| | Anthropic 原生 | OpenAI-compatible |
|---|---|---|
| 端点 | `POST /v1/messages` | `POST /v1/chat/completions` |
| 鉴权头 | `x-api-key: <key>` + `anthropic-version: 2023-06-01` | `Authorization: Bearer <key>` |
| 一次请求返回几条候选 | 恒 1 条（无 `n`） | `choices[]` 数组，由 `n` 控制 |
| 系统提示位置 | 顶层 `system` 参数（独立于 `messages`） | `messages` 数组里的 `role: "system"` / `"developer"` |
| 助手回复载体 | `content: []`，有序内容块数组 | `choices[0].message`，`content` 是字符串或 null |
| `max_tokens` | **必填** | 可选（`max_completion_tokens`） |

来源：[Claude API cURL 示例与必需头](https://platform.claude.com/docs/en/api/messages)、[OpenAI Create chat completion](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)

> 补充事实：Anthropic 自己提供 OpenAI 兼容层（`base_url=https://api.anthropic.com/v1/`），官方明确定位为「主要用于测试与对比模型能力，**不是**大多数场景的长期或生产方案」。见 [OpenAI SDK compatibility](https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk)。

---

## 2. 工具定义 schema 对照

| 语义 | Anthropic | OpenAI-compatible |
|---|---|---|
| 数组位置 | 顶层 `tools: []` | 顶层 `tools: []` |
| 嵌套层级 | **扁平**：`{name, description, input_schema}` | **两层**：`{type: "function", function: {name, description, parameters, strict}}` |
| JSON Schema 字段名 | `input_schema` | `function.parameters` |
| 名称正则 | `^[a-zA-Z0-9_-]{1,64}$` | `a-z A-Z 0-9 _ -`，最长 64 |
| 严格模式 | `strict: true`（顶层同级字段，非 `tool_choice` 内） | `function.strict: true` |
| 描述可选性 | `description` 可选但强烈建议 | `description` 可选 |
| Anthropic 独有 | `input_examples`（示例入参数组，需通过 schema 校验，否则 400）、`cache_control`、`defer_loading`、`allowed_callers` | — |
| OpenAI 独有 | `type: "custom"` 自定义工具（自由文本 / lark / regex 语法约束） | — |

来源：[Define tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools)、[OpenAI Create chat completion → tools](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)

### `tool_choice` 对照

| 语义 | Anthropic | OpenAI |
|---|---|---|
| 模型自己决定（默认） | `{"type": "auto"}` | `"auto"` |
| 必须调至少一个 | `{"type": "any"}` | `"required"` |
| 强制某个工具 | `{"type": "tool", "name": "X"}` | `{"type": "function", "function": {"name": "X"}}` |
| 禁用工具 | `{"type": "none"}` | `"none"` |
| 关闭并行 | `tool_choice.disable_parallel_tool_use: true`（**嵌在 tool_choice 里，不是顶层参数**） | 顶层 `parallel_tool_calls: false` |
| 限定子集 | 无原生等价 | `{"type": "allowed_tools", "allowed_tools": {mode, tools}}` |

来源：[Parallel tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use)、[Define tools → Forcing tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools)

> 注意：Anthropic 侧 `tool_choice` 为 `any` / `tool` 时，API 会预填 assistant 消息强制工具使用，**模型不会在 `tool_use` 之前输出自然语言**，即使你要求它这么做。这会影响 Dashboard「逐轮页」上「模型说了什么」的展示预期。

---

## 3. 工具调用在响应中的位置与 id 关联

### Anthropic：内容块

```json
{
  "id": "msg_01Aq9w938a90dw8q",
  "model": "claude-opus-5",
  "stop_reason": "tool_use",
  "role": "assistant",
  "content": [
    { "type": "text", "text": "I'll check the current weather in San Francisco for you." },
    { "type": "tool_use",
      "id": "toolu_01A09q90qw90lq917835lq9",
      "name": "get_weather",
      "input": { "location": "San Francisco, CA", "unit": "celsius" } }
  ]
}
```

### OpenAI：message 上的侧车数组

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [{
        "id": "call_abc123",
        "type": "function",
        "function": { "name": "get_current_weather", "arguments": "{\"location\": \"Boston, MA\"}" }
      }]
    },
    "finish_reason": "tool_calls"
  }]
}
```

| 维度 | Anthropic | OpenAI |
|---|---|---|
| 位置 | `content[]` 里的 `tool_use` 块，与 `text` / `thinking` **同一数组、有序** | `message.tool_calls[]`，与 `message.content` **并列的两个字段** |
| id 字段 | `id`（惯例前缀 `toolu_`），正则 `^[a-zA-Z0-9_-]+$` | `id`（惯例前缀 `call_`） |
| 参数类型 | `input` 是**已解析的 JSON 对象** | `function.arguments` 是**JSON 字符串**，官方明示「模型不一定生成合法 JSON，可能幻觉出 schema 外的参数，调用前必须自行校验」 |
| 文本与调用共存 | 天然共存且保序 | `content` 通常为 `null`；同时有文本时两个字段各自独立，**顺序信息丢失** |
| 工具名长度上限 | `tool_use.name` 最长 200 | 定义端 64 |

**关键结论**：`text` 与 `tool_use` 的**相对顺序**只在 Anthropic 侧存在。内部协议若要保留（Dashboard「逐轮页」需要），必须自己建模块序列；往 OpenAI 侧渲染时把所有 text 块合并进 `content`，并接受顺序被压平。

来源：[Handle tool calls](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)、[openai-python `ChatCompletionMessageFunctionToolCall`](https://github.com/openai/openai-python/blob/main/src/openai/types/chat/chat_completion_message_function_tool_call.py)

---

## 4. 工具结果回传

| 维度 | Anthropic | OpenAI |
|---|---|---|
| role | `user` | `tool` |
| 结构 | `content[]` 里的 `tool_result` block | 独立的一条消息 |
| 关联字段 | `tool_use_id` | `tool_call_id` |
| 结果内容 | 字符串，或 `text` / `image` / `document` / `search_result` 块数组 | 字符串，或仅 `type: "text"` 的内容块数组 |
| 错误标记 | `is_error: true`（结构化字段） | **无对应字段**，只能把错误写进 `content` 文本 |
| 空结果 | 允许省略 `content` | `content` 必填 |
| N 个调用 → 几条消息 | **1 条** `user` 消息装 N 个 `tool_result` | **N 条** `role: "tool"` 消息 |

Anthropic 侧硬性格式要求（原文）：

> - Tool result blocks must immediately follow their corresponding tool use blocks in the message history. You cannot include any messages between the assistant's tool use message and the user's tool result message.
> - In the user message containing tool results, the tool_result blocks must come **FIRST** in the content array. Any text must come **AFTER** all tool results.

违反会拿到 400，错误文案形如 `tool_use ids were found without tool_result blocks immediately after`。

```json
// ❌ 400
{"role": "user", "content": [
  {"type": "text", "text": "Here are the results:"},
  {"type": "tool_result", "tool_use_id": "toolu_01"}
]}

// ✅
{"role": "user", "content": [
  {"type": "tool_result", "tool_use_id": "toolu_01"},
  {"type": "text", "text": "What should I do next?"}
]}
```

来源：[Handle tool calls → Important formatting requirements](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)

> Anthropic 官方对这一差异的自述：「Unlike APIs that separate tool use or use special roles like `tool` or `function`, the Claude API integrates tools directly into the `user` and `assistant` message structure.」

---

## 5. 多个并行工具调用

| 维度 | Anthropic | OpenAI |
|---|---|---|
| 默认是否并行 | 是 | 是（`parallel_tool_calls` 默认开） |
| 表达方式 | 一条 assistant 消息里多个 `tool_use` block | `message.tool_calls[]` 多个元素 |
| 结果回传 | **全部塞进同一条 `user` 消息** | 每个 call 一条 `role: "tool"` 消息，顺序无强制约束 |
| 拆开发的后果 | 官方明说会「训练」模型少做并行调用 | 无此约束 |
| 未执行的调用 | 仍必须补一条 `tool_result` + `is_error: true` | 仍必须补一条 `role: "tool"` 消息 |
| 流式里的区分键 | `content_block_start.index`（内容块下标） | `delta.tool_calls[].index`（tool_calls 数组下标） |

Anthropic 官方 troubleshooting 原文：

```json
// Wrong: separate user messages reduce parallel tool use
[{"role":"assistant","content":[tool_use_1, tool_use_2]},
 {"role":"user","content":[tool_result_1]},
 {"role":"user","content":[tool_result_2]}]

// Correct: one user message with all results maintains parallel tool use
[{"role":"assistant","content":[tool_use_1, tool_use_2]},
 {"role":"user","content":[tool_result_1, tool_result_2]}]
```

来源：[Parallel tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use)

**内部协议要求**：一轮工具执行的输出必须是 `ToolResultBatch`（有序列表 + 与之配对的 assistant 轮次引用），不能是「零散的一条条结果消息」。这是往两侧都能安全渲染的唯一形状。

---

## 6. 流式事件对照

### Anthropic：命名 SSE 事件 + 内容块状态机

事件流固定为：`message_start` → （每个块：`content_block_start` → N×`content_block_delta` → `content_block_stop`）→ 1+×`message_delta` → `message_stop`；其间可能穿插任意数量 `ping`。

| 事件 | 载荷要点 |
|---|---|
| `message_start` | 完整 `Message` 对象、`content: []`、`usage.input_tokens` 已给出 |
| `content_block_start` | `index` + `content_block`（含 `type`；`tool_use` 块在此给出 `id` 与 `name`） |
| `content_block_delta` | `index` + `delta`，delta 类型见下表 |
| `content_block_stop` | `index` |
| `message_delta` | `delta.stop_reason` / `delta.stop_sequence` + `usage`（**累计值**，非增量） |
| `message_stop` | 空 |
| `ping` | 空，心跳 |
| `error` | `{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}` |

`content_block_delta` 的 delta 类型：

| delta type | 字段 | 用途 |
|---|---|---|
| `text_delta` | `text` | 文本增量 |
| `input_json_delta` | `partial_json` | **工具入参增量，是 partial JSON 字符串**；最终 `tool_use.input` 才是对象 |
| `thinking_delta` | `thinking` | 思考增量 |
| `signature_delta` | `signature` | 思考块完整性签名，在 `content_block_stop` 之前发一次 |
| `citations_delta` | `citation` | 引用 |

官方注解：当前模型一次只能吐出 `input` 里一个完整的 key/value，因此**工具调用时事件之间可能有明显停顿**；累计完成后再被切成多个 partial JSON 增量发出。可用 `eager_input_streaming: true`（挂在工具定义上，非 beta）开启细粒度参数流。

来源：[Streaming messages](https://platform.claude.com/docs/en/build-with-claude/streaming)、[Fine-grained tool streaming](https://platform.claude.com/docs/en/agents-and-tools/tool-use/fine-grained-tool-streaming)

### OpenAI：单一 chunk 类型 + `[DONE]` 哨兵

```json
{"id":"chatcmpl-123","object":"chat.completion.chunk","created":1694268190,
 "model":"gpt-4o-mini","system_fingerprint":"fp_44709d6fcb",
 "choices":[{"index":0,"delta":{"content":"Hello"},"logprobs":null,"finish_reason":null}]}
```

- 只有一种事件类型：`chat.completion.chunk`。SSE 无 `event:` 名，只有 `data:`。
- 首个 chunk 的 `delta` 带 `role: "assistant"`；末个 chunk `delta` 为 `{}` 且 `finish_reason` 非 null。
- 流以 `data: [DONE]` 结束。
- 工具调用增量：`delta.tool_calls: [{index, id, type, function: {name, arguments}}]`；`id` / `name` 通常只在该 index 第一个 chunk 出现，之后只有 `function.arguments` 字符串片段，**靠 `index` 拼接**。
- usage 默认**不发**，必须 `stream_options: {"include_usage": true}`，届时会在 `data: [DONE]` 之前多发一个 chunk（`choices` 为空数组、带 `usage`）。

来源：[Chat Completions streaming events](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)、[Create chat completion → stream_options](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)

### 逐项对照

| 维度 | Anthropic | OpenAI |
|---|---|---|
| SSE 事件名 | 有（`event: message_stop` 等） | 无，只有 `data:` |
| 结束标志 | `message_stop` 事件 | `data: [DONE]` 文本哨兵 |
| 心跳 | `ping` 事件 | 无标准心跳（部分网关自行插注释行） |
| 块边界 | 显式 `content_block_start` / `_stop` | 无；靠 `delta` 里出现哪些字段推断 |
| 工具参数增量 | `input_json_delta.partial_json` | `delta.tool_calls[i].function.arguments` |
| 并行工具区分 | `index` = 内容块下标 | `index` = tool_calls 下标 |
| 输入 token 何时可知 | `message_start` 就给 | 只有开了 include_usage 才在最后给 |
| 输出 token | `message_delta.usage`（累计） | 末尾 usage chunk |
| 停止原因何时给 | `message_delta.delta.stop_reason` | 末个 chunk 的 `finish_reason` |
| 流内错误 | **有标准 `error` 事件** | **无标准错误事件** |

---

## 7. 中途出错各自留下什么（决定回退策略）

这一节直接决定 Agent-Alfred 的重试 / 降级实现，也决定「不得伪造执行结果」这条红线在流式路径上怎么落地。

### 7.1 流内错误

| 情况 | Anthropic | OpenAI-compatible |
|---|---|---|
| 服务端过载 | 流中插入 `event: error`，`{"type":"error","error":{"type":"overloaded_error",...}}`（非流式对应 HTTP 529），此后**不会**有 `message_delta` / `message_stop` | 规范未定义流内错误事件。实际表现三种都有：连接直接断、发一个非规范的 `{"error": {...}}` chunk、或先 200 再中断 |
| 客户端能拿到什么 | 已收到的 `content_block_*` 事件构成的**部分块**；缺 `message_stop` 即可判定不完整 | 已收到的 chunk 拼出的部分文本 / 部分 arguments；**缺 `[DONE]` 是唯一可靠的不完整信号** |
| 未知事件类型 | 官方要求客户端**优雅忽略**未知事件（版本策略允许新增事件类型） | 同理需忽略未知字段 |

**回退策略含义**：
- 内部流式层必须维护「本轮是否正常收尾」的显式标志：Anthropic 看有没有 `message_stop`，OpenAI 看有没有 `[DONE]`。两者都缺，一律标记为 `错误`，**不得**把已收到的部分文本当成完整回复入库。
- OpenAI 侧的部分 `arguments` 字符串**几乎必然是非法 JSON**（例如 `{"location": "San Fra`）。绝不能 best-effort 补全后当成工具调用执行——那是伪造执行结果。正确做法：整轮作废重试。
- Anthropic 的 `error` 事件是**结构化**的，可以直接映射到内部错误类型（`overloaded_error` → 可重试）；OpenAI 侧只能从 HTTP 状态码 + 断流启发式判断，内部协议应给适配器一个 `retryable: bool | unknown` 三态，而不是二元布尔。

### 7.2 `max_tokens` 截断到一半的工具调用

| | Anthropic | OpenAI |
|---|---|---|
| 停止原因 | `stop_reason: "max_tokens"` | `finish_reason: "length"` |
| 留下什么 | `content` 最后一块是**不完整、非法的 `tool_use` 块** | `tool_calls[i].function.arguments` 是**被截断的字符串** |
| 官方建议 | 「retry the request with a higher `max_tokens` value to get the full tool use」——检测末块是不是 `tool_use`，是则加大 `max_tokens` 重发 | 无专门指引，同样只能加大上限重发 |

来源：[Handling stop reasons](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons)

### 7.3 服务端工具循环暂停

Anthropic 独有：`stop_reason: "pause_turn"`（服务端采样循环达到默认 10 轮上限）。恢复方式是把 user 消息 + assistant `content` 原样回传再请求一次，**不要额外加一条 "Continue."**——API 检测到尾部 `server_tool_use` 块会自动续跑。OpenAI-compatible 无此概念。

---

## 8. Token 用量字段对照

| 语义 | Anthropic `usage` | OpenAI `usage` |
|---|---|---|
| 输入 token | `input_tokens` | `prompt_tokens` |
| 输出 token | `output_tokens` | `completion_tokens` |
| 合计 | **无**（需自算） | `total_tokens` |
| 缓存写入 | `cache_creation_input_tokens` | `prompt_tokens_details.cache_write_tokens` |
| 缓存读取 | `cache_read_input_tokens` | `prompt_tokens_details.cached_tokens` |
| 缓存与 input 的关系 | `cache_*` 与 `input_tokens` **分开计**，`input_tokens` 不含缓存部分 | `cached_tokens` **含在** `prompt_tokens` 内 |
| 思考 / 推理 token | 计入 `output_tokens`，无单列字段 | `completion_tokens_details.reasoning_tokens` |
| 模态细分 | 无 | `prompt_tokens_details.{text_tokens, image_tokens, audio_tokens}` |
| 服务端工具用量 | `usage.server_tool_use` | 无 |
| 流式里的语义 | `message_delta.usage` 是**累计值** | 末尾单独一个 usage chunk |

**这是费用链最容易出错的地方**，直接关系到地图前提 #4：

1. **`total_tokens` 只在 OpenAI 侧存在。** 内部 usage 模型不要存 total，存 input/output/cache 四个原子字段，total 在渲染层算。
2. **缓存 token 的计费口径相反。** Anthropic 侧 `input_tokens + cache_read + cache_creation` 才是完整输入量；OpenAI 侧 `prompt_tokens` 已经是完整输入量，`cached_tokens` 只是其中被命中的子集。**照抄公式会让 Anthropic 侧漏算、OpenAI 侧重复算。**
3. **「没有」不等于「0」。** Anthropic 自家 OpenAI 兼容层把 `usage.prompt_tokens_details` 与 `usage.completion_tokens_details` **恒置为空**；OpenAI 侧不开 `include_usage` 时流式响应完全没有 usage。这两种情况都必须映射到内部的「价格未知」态，单列调用次数与 token 量，而不是按 0 计费。

来源：[Messages API 响应字段](https://platform.claude.com/docs/en/api/messages)、[OpenAI Create chat completion → usage](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)、[OpenAI SDK compatibility → Response fields](https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk)

---

## 9. 停止原因对照表

| 语义 | Anthropic `stop_reason` | OpenAI `finish_reason` |
|---|---|---|
| 自然结束 | `end_turn` | `stop` |
| 命中自定义停止序列 | `stop_sequence`（另有 `stop_sequence` 字段给出命中值） | `stop`（**与自然结束合并，不可区分**） |
| 达到输出上限 | `max_tokens` | `length` |
| 需要执行工具 | `tool_use` | `tool_calls` |
| 内容被安全过滤 | 无（拒答走 `refusal`） | `content_filter` |
| 模型主动拒答 | `refusal`（HTTP 200，附 `stop_details.{type, category, explanation}`） | 无（表现为 `stop` + 拒答文本） |
| 服务端工具循环暂停 | `pause_turn` | 无 |
| 撑满上下文窗口 | `model_context_window_exceeded` | 无 |
| 旧式 function 调用 | 无 | `function_call`（已废弃） |
| 流未结束 | 流中为 `null`，`message_delta` 才给 | 每个 chunk 的 `finish_reason` 为 `null` |

补充：
- `stop_details` **仅在** `stop_reason == "refusal"` 时非空，其余一律 `null`——读取前必须判空。
- **`stop_sequence` 与 `end_turn` 在 OpenAI 侧被合并成一个 `stop`**。内部协议若要保留「是不是命中了停止序列」，需要额外字段，且 OpenAI 侧只能填 `unknown`。

来源：[Handling stop reasons](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons)、[OpenAI Create chat completion → finish_reason](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)

---

## 10. OpenAI-compatible 网关的实际偏差

### 10.1 OpenCode Zen / OpenCode Go —— **最大的一个发现：style 是 per-model，不是 per-provider**

官方文档的 Endpoints 表明确按**模型**分派到三种不同 API 形状：

| 网关 | 模型举例 | 端点 | 实际协议形状 |
|---|---|---|---|
| Zen (`https://opencode.ai/zen/v1`) | `claude-opus-5`、`claude-sonnet-5`、`qwen3.7-max` | `/zen/v1/messages` | **Anthropic messages** |
| Zen | `gpt-5.6-sol`、`grok-4.6`、`muse-spark-1.2` | `/zen/v1/responses` | **OpenAI Responses**（第三种形状，非 chat completions） |
| Zen | `deepseek-*`、`glm-*`、`kimi-*` | `/zen/v1/chat/completions` | OpenAI chat completions |
| Go (`https://opencode.ai/zen/go/v1`) | **`deepseek-v4-flash`**、`deepseek-v4-pro`、`glm-5.3`、`kimi-k3`、`longcat-2.0`、`mimo-v2.5`、`hy3` | `/zen/go/v1/chat/completions` | OpenAI chat completions |
| Go | `qwen3.8-max`、`qwen3.7-max/plus`、`qwen3.6-plus`、`minimax-m3`、`minimax-m2.7/m2.5` | `/zen/go/v1/messages` | **Anthropic messages** |
| Go | `grok-4.6`、`gpt-5.6-luna`、`muse-spark-1.2-contributor` | `/zen/go/v1/responses` | **OpenAI Responses** |

模型目录：`https://opencode.ai/zen/go/v1/models`（与 `https://opencode.ai/zen/v1/models`），与地图前提 #3 的在线拉取一致。OpenCode 配置里的模型 id 形如 `opencode-go/<model-id>`。

来源：[OpenCode Zen](https://opencode.ai/docs/zen/)、[OpenCode Go](https://opencode.ai/docs/go/)

**对 Agent-Alfred 的三条直接影响：**

1. **配置表一行必须携带 per-model 的 `style` 与 endpoint path**，形如 `{name, base_url, api_key_env, catalog_url?, models: {<id>: {style, path}}}`；或者从 `/models` 目录里读出 style。「一个 provider 一个 style」的假设在 OpenCode 上直接不成立。
2. 地图选定的首个 provider 模型 `deepseek-v4-flash` 确实落在 `/chat/completions`，**`OpenAICompatibleAdapter` 就能跑通**——首个切片不受影响。但同一网关换个模型（如 `qwen3.7-max`）就得切到 `AnthropicAdapter`。
3. **存在第三种形状：OpenAI Responses API**（`/responses`，用 `input` / `output` / `call_id` / `function_call_output`，与 chat completions 不同构）。地图当前的「只写两个适配器」在覆盖 grok / GPT 系列时会不够。**建议在 #2 里显式记一笔：v1 只支持 `messages` 与 `chat/completions` 两种 style，`responses` 记为已知缺口**，不要假装一行配置就能接上。

### 10.2 DeepSeek

DeepSeek 官方同时提供 OpenAI 兼容与 Anthropic 兼容两套端点。

**OpenAI 兼容侧（`https://api.deepseek.com`）的实际偏差：**

| 项 | 偏差 |
|---|---|
| 缓存 usage 字段 | **不用** `prompt_tokens_details.cached_tokens`，而是顶层 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`（两者之和 = `prompt_tokens`） |
| 推理内容 | 额外的 `message.reasoning_content` / `delta.reasoning_content` 字段（thinking 模式），OpenAI 规范里没有 |
| 思考控制 | 非规范的 `thinking: {type: "enabled"|"disabled", reasoning_effort: "low"|"high"|"max"}` |
| finish_reason | 多一个规范外取值 **`insufficient_system_resource`**（推理中断） |
| 预填 | Beta 的 `prefix` 字段（强制 assistant 以给定前缀开头） |
| tools / tool_calls / role:tool | 与 OpenAI 规范一致（`type: "function"` 两层嵌套、`tool_call_id`） |

来源：[DeepSeek Create Chat Completion](https://api-docs.deepseek.com/api/create-chat-completion)

**Anthropic 兼容侧（`https://api.deepseek.com/anthropic`）的实际偏差：**

| 项 | 状态 |
|---|---|
| `anthropic-version` 头 | 忽略 |
| `anthropic-beta` 头 | 忽略（Files API 除外） |
| `container` / `mcp_servers` / `service_tier` / `top_k` | 不支持 |
| `document` / `search_result` 内容块 | 不支持 |
| `metadata` | 仅 `user_id` |
| `thinking` | 支持，但 `budget_tokens` 被忽略 |
| `output_config` | 仅 `effort` 生效 |
| `tool_choice.disable_parallel_tool_use` | **被忽略**（即无法关闭并行调用） |
| 模型映射 | `claude-opus*` → `deepseek-v4-pro`，其余 → `deepseek-v4-flash` |

来源：[DeepSeek Anthropic API 兼容](https://api-docs.deepseek.com/guides/anthropic_api)

> `insufficient_system_resource` 与「`disable_parallel_tool_use` 被静默忽略」是两个必须写进内部协议的现实：**finish_reason 的枚举必须是开放集**（未知值一律映射到内部 `error/unknown`，不能 crash），**并行开关必须视为「建议」而非「保证」**。

### 10.3 xAI Grok

base URL `https://api.x.ai/v1`，`/chat/completions` OpenAI 兼容；官方同时声明兼容 OpenAI SDK 与 Anthropic SDK。

| 项 | 情况 |
|---|---|
| **流式工具调用** | 官方原文：**"With streaming, the function call is returned in whole in a single chunk, not streamed across chunks."** 即工具调用**不做增量拆分**，一个 chunk 给全 |
| tool_choice | `auto` / `required` / `none` / `{"type":"function","function":{"name":"..."}}`，与 OpenAI 一致 |
| 并行工具 | 默认开，`parallel_tool_calls` 控制 |
| usage | `prompt_tokens` / `completion_tokens` / `total_tokens`，`prompt_tokens_details.{text_tokens, cached_tokens, image_tokens, audio_tokens}`，`completion_tokens_details.{reasoning_tokens, ...}` |
| usage 额外字段 | `num_sources_used`（联网搜索命中数）、`cost_in_usd_ticks`（**计费精度值，OpenAI 规范外**） |
| finish_reason 取值 | 官方文档**未明确列举**工具调用时的取值，不能假定一定是 `tool_calls` |

来源：[xAI API reference](https://docs.x.ai/docs/api-reference)、[xAI Function calling](https://docs.x.ai/docs/guides/function-calling)

> `cost_in_usd_ticks` 值得注意：这是**网关直接给出的费用**，比本地价格链更权威。地图前提 #4 的价格链应当在最前面加一档：**「响应里带的费用字段」优先于在线目录缓存**。但它是 xAI 私有字段，内部 usage 模型需要一个 `provider_reported_cost: Optional[Decimal]` 的口子。

### 10.4 Anthropic 自家 OpenAI 兼容层（作为「兼容层会丢什么」的权威样本）

这是唯一一份由厂商自己写明「OpenAI 兼容层到底丢了什么」的表，可以直接当作**其他网关也会有类似损耗**的先验。

| 字段 | 状态 |
|---|---|
| `tools[n].function.strict` | **忽略**（不保证 schema 一致） |
| `response_format` | 忽略 |
| `reasoning_effort` / `seed` / `logprobs` / `logit_bias` / `service_tier` / `store` / `user` / `metadata` / `presence_penalty` / `frequency_penalty` / `prediction` / `modalities` / `audio` | 忽略 |
| `n` | 必须恰为 1 |
| `temperature` | 只接受 0–1，>1 被截到 1 |
| 多条 system/developer 消息 | **全部提升到开头并用 `\n` 拼成一条** |
| Prompt caching | **不支持**（原生 SDK 才支持） |
| `usage.prompt_tokens_details` / `usage.completion_tokens_details` | **恒空** |
| `choices[].message.refusal` / `.audio` / `logprobs` / `service_tier` / `system_fingerprint` | 恒空 |
| 错误格式 | 保持 OpenAI 形状，但**文案不等价，只能用于日志与调试** |

官方总结：「Most unsupported fields are **silently ignored** rather than producing errors.」

来源：[OpenAI SDK compatibility](https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk)

> **这条是内部协议最重要的设计约束之一：兼容层的失败模式是「静默忽略」，不是报错。** 因此内部协议**不能靠「请求没报错」推断特性生效**。凡是影响正确性的特性（strict、并行开关、缓存、停止序列），必须在响应里做**事后校验**，校验不过就把该能力标为 `已配置未测试` / `错误`，而不是显示成功。这正对应红线里的四态显示。

---

## 11. 对 #2「内部协议的形状」的直接结论

1. **以内容块序列为基线。** 一条 assistant 轮次 = `blocks: [TextBlock | ThinkingBlock | ToolCallBlock]`，保序、带索引。往 OpenAI 渲染时压平为 `content` + `tool_calls`；往 Anthropic 渲染时 1:1。
2. **工具结果是批（batch），不是消息。** `ToolResultBatch { results: [ToolResult{call_id, content, is_error}] }`，由适配器决定渲染成 1 条 `user` 消息还是 N 条 `role:"tool"` 消息。`is_error` 必须是结构化字段（OpenAI 侧降级为文本前缀）。
3. **id 由内部生成并双向映射。** 两侧 id 格式不同（`toolu_*` / `call_*`），且回传时必须原样匹配。内部保存 provider 原始 id，不要自造。
4. **停止原因用内部开放枚举。** 建议内部集合：`end_turn | tool_use | max_tokens | stop_sequence | refusal | content_filter | paused | context_exceeded | error | unknown`。**未知值必须落到 `unknown` 而不是抛异常**（DeepSeek 的 `insufficient_system_resource` 就是活例）。
5. **usage 存原子字段 + 三态。** `input_tokens / output_tokens / cache_read_tokens / cache_write_tokens / reasoning_tokens / provider_reported_cost`，每个都可为 `None`（= 未提供 ≠ 0）。total 与费用一律在渲染层算。
6. **流式接口暴露三段式事件**：`BlockStart{index, type, meta}` / `BlockDelta{index, payload}` / `BlockStop{index}` + `TurnStart` / `TurnEnd{stop_reason, usage}` / `StreamError{retryable}`。这是能同时承载 Anthropic 命名事件与 OpenAI chunk 的最小公倍数。
7. **必须有「本轮是否正常收尾」的显式布尔。** 没有 `message_stop` / `[DONE]` 一律判失败，部分内容不得入库为成功结果。
8. **provider 配置表的 `style` 挂在 model 上，不是 provider 上**；并在 v1 明确把 OpenAI `responses` 形状记为已知缺口。
9. **能力生效与否要事后校验，不能靠请求成功推断**（兼容层静默忽略是常态）。

---

## 12. 来源清单

Anthropic / Claude 官方：
- [Messages API 参考](https://platform.claude.com/docs/en/api/messages)
- [Streaming messages](https://platform.claude.com/docs/en/build-with-claude/streaming)
- [Define tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools)
- [Handle tool calls](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)
- [Parallel tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use)
- [Handling stop reasons](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons)
- [Fine-grained tool streaming](https://platform.claude.com/docs/en/agents-and-tools/tool-use/fine-grained-tool-streaming)
- [OpenAI SDK compatibility](https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk)

OpenAI 官方：
- [Create chat completion](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
- [Chat Completions streaming events](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)
- [openai-python `chat_completion_message_function_tool_call.py`](https://github.com/openai/openai-python/blob/main/src/openai/types/chat/chat_completion_message_function_tool_call.py)

网关官方：
- [OpenCode Zen](https://opencode.ai/docs/zen/)
- [OpenCode Go](https://opencode.ai/docs/go/)
- [DeepSeek Create Chat Completion](https://api-docs.deepseek.com/api/create-chat-completion)
- [DeepSeek Anthropic API 兼容](https://api-docs.deepseek.com/guides/anthropic_api)
- [xAI API reference](https://docs.x.ai/docs/api-reference)
- [xAI Function calling](https://docs.x.ai/docs/guides/function-calling)

---

## 13. 未查清 / 留待实测

以下无法从官方文档确认，需要接上真实 key 后实测（**不得在文档或 Dashboard 里按推测显示**）：

- OpenCode Zen / Go 的鉴权头形式与 env 变量名（文档只说「复制 API key」，未写头名）。地图假定 `OPENCODE_API_KEY`，待验。
- OpenCode 网关是否原样透传上游的 usage 字段（尤其 DeepSeek 的 `prompt_cache_hit_tokens` 是否被改写成 OpenAI 形状）。
- xAI 在工具调用时 `finish_reason` 的实际取值（官方未列举）。
- 各网关流式中途出错时的实际字节流形态（是否发非规范 error chunk、是否发 `[DONE]`）。
- OpenCode `/models` 目录返回体里是否含 `pricing.prompt` / `pricing.completion` 以及 style 信息。
