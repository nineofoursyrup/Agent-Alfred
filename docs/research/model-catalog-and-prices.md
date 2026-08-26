# 调研：models.dev 目录结构与内置静态价表的生成方案

- 对应票：[#8](https://github.com/nineofoursyrup/Agent-Alfred/issues/8)，前置于 [#15 实现：模型目录、价格链与费用账本语义](https://github.com/nineofoursyrup/Agent-Alfred/issues/15)
- 调研日期：2026-08-26
- 数据快照：`https://models.dev/api.json`，ETag `aaf9c3a1e59f50cad9b83411bf864ce0`（当日 13:10 UTC）

---

## 1. 结论摘要

1. **可以用 `models.dev/api.json` 构建时生成静态价表**，许可证是 MIT，允许随包分发，条件是保留版权与许可声明。
2. **必须做**。`opencode-go` 的 `deepseek-v4-flash` 真实目录价是 `$0.22 / $0.66`（输入/输出，每 1M token）。若静态表为空、掉到第 5 档「未知 Provider 默认 $3/$15」，输入高估 **13.6 倍**、输出高估 **22.7 倍**（票面写「约 15 倍」，实测两个方向不同，输出更糟）。
3. **裁剪后极小**。全量 4.3 MB / 202 provider / 7303 模型；只裁 `opencode` + `opencode-go` 两家后是 **17 KB / 123 个模型**，进 wheel 毫无压力。
4. **`opencode` 与 `opencode-go` 必须当成两家独立 provider**。二者 base_url 不同，且共有的 17 个模型里 **6 个价格不一致**——`deepseek-v4-pro` 差 2.6 倍。按 provider id 建索引，绝不能按模型名合并。
5. **`api.json` 里没有任何生成时间戳字段**，快照日期只能由我们的生成脚本记录。ETag 可用于变更检测。
6. **不建议换数据源**。LiteLLM 与 OpenRouter 都**完全不收录 opencode**（见 §7），对我们的首个 provider 是零覆盖。

---

## 2. 数据源与许可（合规结论）

| 项 | 值 |
|---|---|
| API | `https://models.dev/api.json` |
| 仓库 | [`anomalyco/models.dev`](https://github.com/anomalyco/models.dev) |
| 许可证 | **MIT**，`Copyright (c) 2025 models.dev` |
| 体积 | 4,308,225 字节（约 4.3 MB） |
| 覆盖 | 202 个 provider / 7303 个模型 |
| 缓存头 | `cache-control: public, max-age=0, must-revalidate`；有 `ETag`，**无** `Last-Modified` |
| CORS | `access-control-allow-origin: *` |

**能否随 Python 包分发一份快照：能。** MIT 许可对再分发的唯一要求是「在所有副本或实质性部分中包含上述版权声明和本许可声明」。数据以 TOML 形式存放在该仓库内，与代码同受仓库根 `LICENSE` 覆盖（README 明确写「The data is stored in the repo as TOML files」）。

落地要求（生成脚本已实现）：
- 生成的 `prices.toml` 头部注释保留 `Copyright (c) 2025 models.dev` 与 `MIT` 字样，并注明来源 URL 与仓库地址。
- 在项目的第三方声明处（如 `NOTICE` 或 README 致谢段）列出 models.dev。

**更新频率：极高。** CI 里 `.github/workflows/sync-models.yml` 的 cron 是 `17 * * * *`，**每小时**对每个 provider 单独跑一次同步并开 PR。最近 14 天的提交数达到 API 单页上限 100 条（实际更多），提交流水如 `chore(sync): update OpenRouter model catalog`。

实测佐证：本次调研前后相隔约 10 分钟的两次拉取，ETag 已经从 `aaf9c3a1…` 变成 `5afc71e2…`。**这意味着任何快照在发布当天就已过期**——所以快照只能做兜底，不能做事实来源（这与 #1 Notes 第 3 条「模型目录在线拉取」的既定决策一致）。

### 三个同族端点

| 端点 | 体积 | 含价格 | 用途 |
|---|---|---|---|
| `models.dev/api.json` | 4.3 MB | ✅ | provider × 模型，含 `cost`。**我们用这个** |
| `models.dev/models.json` | 288 KB | ❌ | 355 条 provider-无关的模型元数据，无 `cost` 字段 |
| `models.dev/catalog.json` | 4.6 MB | ✅ | 前两者合并 |

> 注：`models.json` 虽小，但**完全不含价格**（已验证 `any('cost' in v)` 为 `False`），不能替代 `api.json`。

---

## 3. `api.json` 的字段结构

顶层是一个 **provider id → provider 对象** 的字典，**没有** `version` / `generated_at` 之类的元数据键。

### Provider 层字段（202 家统计）

| 字段 | 出现次数 | 说明 |
|---|---|---|
| `id` | 202 | provider id，与顶层键相同 |
| `name` | 202 | 展示名，如 `OpenCode Go` |
| `env` | 202 | 环境变量名数组，如 `["OPENCODE_API_KEY"]` |
| `npm` | 202 | AI SDK 包名，如 `@ai-sdk/openai-compatible` |
| `doc` | 202 | 文档链接 |
| `models` | 202 | 模型 id → 模型对象 |
| `api` | 176 | **base_url**。26 家缺失（本地/自托管类） |

> `env` 与 `api` 正好对应 #1 Notes 第 1 条里 provider 配置表的 `api_key_env` 与 `base_url` 两列，可以直接复用。

### 模型层字段（7303 个模型统计）

| 字段 | 出现次数 | 类型 | 说明 |
|---|---|---|---|
| `id` / `name` / `description` | 7303 | str | 基本信息 |
| `attachment` | 7303 | bool | 是否支持附件 |
| `reasoning` | 7303 | bool | 是否推理模型 |
| `tool_call` | 7303 | bool | **是否支持工具调用** |
| `release_date` / `last_updated` | 7303 | str | `YYYY-MM-DD` |
| `modalities` | 7303 | obj | `{input: [...], output: [...]}` |
| `open_weights` | 7303 | bool | 是否开放权重 |
| `limit` | 7303 | obj | `{context, output, input?}` |
| **`cost`** | **6876** | obj | **427 个模型没有价格** |
| `temperature` | 6817 | bool | 是否支持 temperature |
| `family` | 6639 | str | 模型族，如 `deepseek-flash` |
| `reasoning_options` | 5089 | list | 如 `[{type:"toggle"},{type:"effort",values:[...]}]` |
| `structured_output` | 4939 | bool | 结构化输出 |
| `knowledge` | 3947 | str | 知识截止，如 `2025-05` |
| `interleaved` | 884 | obj | 如 `{field:"reasoning_content"}` |
| `status` | 261 | str | `deprecated`(190) / `beta`(71) |
| `experimental` | 38 | bool | |

### `cost` 子字段（单位：**USD / 1M token**）

| 字段 | 出现次数 | 说明 |
|---|---|---|
| `input` | 6876 | 输入 |
| `output` | 6876 | 输出 |
| `cache_read` | 4405 | 缓存命中读 |
| `cache_write` | 1398 | 缓存写入 |
| `tiers` | 407 | **按上下文长度分档的阶梯价** |
| `context_over_200k` | 367 | 超 200k 上下文的价格（`tiers` 的旧式表达） |
| `reasoning` | 143 | 推理 token 单独计价 |
| `input_audio` / `output_audio` | 115 / 18 | 音频计价 |

`limit` 子字段：`context`(7303)、`output`(7303)、`input`(1310)。

**阶梯价是个坑。** 407 个模型的真实单价随上下文长度跳变，扁平的 `input/output` 会低估账单。例：

```json
{
  "input": 0.3, "output": 1.2, "cache_read": 0.06,
  "tiers": [{
    "input": 0.6, "output": 2.4, "cache_read": 0.12,
    "tier": { "type": "context", "size": 512000 }
  }],
  "context_over_200k": { "input": 0.6, "output": 2.4, "cache_read": 0.12 }
}
```

v1 的处理建议：静态表只存基础档 `input/output/cache_read`，但**打一个 `tiered = true` 标记**，让费用账本可以显示「此模型有阶梯价，估算按基础档」而不是假装精确。这符合红线「不伪造」。

### 裁剪后的样例 JSON

`opencode-go` 的 `deepseek-v4-flash` 原始条目（未裁剪，即 `api.json` 的真实内容）：

```json
{
  "id": "deepseek-v4-flash",
  "name": "DeepSeek V4 Flash",
  "description": "Official DeepSeek V4 Flash release with enhanced agentic capabilities and integrated DSpark speculative decoding",
  "family": "deepseek-flash",
  "attachment": false,
  "reasoning": true,
  "reasoning_options": [
    { "type": "effort", "values": ["low", "high", "max"] }
  ],
  "tool_call": true,
  "interleaved": { "field": "reasoning_content" },
  "structured_output": true,
  "temperature": true,
  "knowledge": "2025-05",
  "release_date": "2026-07-31",
  "last_updated": "2026-07-31",
  "modalities": { "input": ["text"], "output": ["text"] },
  "open_weights": true,
  "limit": { "context": 1000000, "output": 384000 },
  "cost": { "input": 0.22, "output": 0.66, "cache_read": 0.007 }
}
```

我们**只需要**其中的 `cost.*` 与 `limit.*`——这是把 4.3 MB 压到 17 KB 的原因。

---

## 4. `opencode` 与 `opencode-go` 的确切差异

两者是 `api.json` 里**两条完全独立的顶层条目**，`env` 与 `npm` 相同，`api`（base_url）不同：

| | `opencode` | `opencode-go` |
|---|---|---|
| `name` | OpenCode Zen | OpenCode Go |
| `api` | `https://opencode.ai/zen/v1` | `https://opencode.ai/zen/go/v1` |
| `env` | `["OPENCODE_API_KEY"]` | `["OPENCODE_API_KEY"]` |
| `npm` | `@ai-sdk/openai-compatible` | `@ai-sdk/openai-compatible` |
| `doc` | `https://opencode.ai/docs/zen` | `https://opencode.ai/docs/zen` |
| 模型数 | 93 | 30 |

模型集合：共有 17 个，仅 Zen 有 76 个，仅 Go 有 13 个。

**共有模型里 6 个价格不一致**（这是本票最关键的发现）：

| 模型 | Zen (`opencode`) | Go (`opencode-go`) |
|---|---|---|
| `deepseek-v4-flash` | in 0.14 / out 0.28 / cr 0.028 | **in 0.22 / out 0.66 / cr 0.007** |
| `deepseek-v4-pro` | in 1.74 / out 3.84 / cr 0.145 | in 0.66 / out 1.98 / cr 0.022 |
| `kimi-k2.5` | cr 0.08 | cr 0.1 |
| `minimax-m2.5` | cr 0.06 | cr 0.03 |
| `minimax-m3` | 无阶梯 | 有 `tiers` + `context_over_200k` |
| `qwen3.6-plus` | 无阶梯 | 有 `tiers` + `context_over_200k` |

注意 `deepseek-v4-flash` 的方向是**反直觉的**：Go 的输入输出都比 Zen **贵**，但缓存读便宜 4 倍。任何「两条条目差不多、随便取一条」的简化都会算错账。

> **结论**：价格查表的键必须是 `(provider_id, model_id)` 二元组，`provider_id` 取 `opencode-go` 这种 models.dev id，而不是 `opencode` 这种模糊前缀。

### 与在线目录的关系（复核票面前提）

票面前提是 `https://opencode.ai/zen/go/v1/models` 只返回 `id/object/created/owned_by`、不含价格。本次调研未持 key 复验该端点（需要 `OPENCODE_API_KEY`），沿用上游 session 的结论。它与 models.dev 的分工是：

- **在线目录**（第 1 档）：回答「现在有哪些模型可用」——权威、实时，但对 OpenCode Go **不含价格**。
- **models.dev 快照**（第 3 档）：回答「这个模型多少钱」——有价格，但会过期。

两者互补，不是替代关系。

---

## 5. 生成脚本的形态建议

### 推荐方案：构建时抓取 + 裁剪成 `prices.toml`，随包分发

脚本草案见 **[`gen_prices_draft.py`](./gen_prices_draft.py)**，纯标准库，已实测可跑（离线 + 联网两条路径都验证过）。

```
python3 docs/research/gen_prices_draft.py --out src/agent_alfred/data/prices.toml
# → wrote ...: 2 provider(s), 123 model(s), snapshot 2026-08-26
```

设计要点：

1. **provider 白名单**。默认只裁 `("opencode-go", "opencode")`。白名单里的 provider 不存在时**直接报错退出**，不静默跳过——models.dev 改 id 时我们要立刻知道。
2. **没有价格的模型不写入**。427 个模型没有 `cost`；对它们写 `0` 会让费用账本谎报免费。缺失就让它落到价格链下一档。
3. **标记而非猜测**：`tiered = true`（有阶梯价）、`free = true`（`input` 与 `output` 均为 0）。
4. **记录快照元数据**：`snapshot_date`（取 HTTP `Date` 头，避免本地时钟偏差）、`source_etag`、`license`、`copyright`、`unit`。
5. **TOML 键必须加引号**。模型 id 含点号（`gpt-5.1`、`gemini-3.5-flash`），裸键会被 TOML 当成嵌套分隔符。脚本里的 `toml_key()` 处理了这件事。
6. **`--from-file` 离线模式**，供确定性测试用固定 fixture 跑，不打网络。

### 实测输出（片段）

```toml
[meta]
source_url = "https://models.dev/api.json"
snapshot_date = "2026-08-26"
source_etag = "\"5afc71e219c1514f985ad7fac20b0fc2\""
license = "MIT"
copyright = "Copyright (c) 2025 models.dev"
unit = "usd_per_million_tokens"

[providers.opencode-go]
name = "OpenCode Go"
api = "https://opencode.ai/zen/go/v1"
env = ["OPENCODE_API_KEY"]
model_count = 30

[providers.opencode-go.models.deepseek-v4-flash]
input = 0.22
output = 0.66
cache_read = 0.007
context = 1000000
max_output = 384000
```

已用 `tomllib` 验证可解析，且含点号的键（`gpt-5.1`）往返正确。

### 一个真实的坑：Cloudflare 挡 urllib

models.dev 在 Cloudflare 后面，**默认的 `Python-urllib/3.x` User-Agent 会收到 HTTP 403**（curl 正常）。脚本必须显式设置 `User-Agent`。这一条在第一次联网实测时踩到了，已修进草案。

### 为什么不选其他形态

| 方案 | 判断 |
|---|---|
| 运行时抓 `api.json` 兜底 | ❌ 4.3 MB / 拉取 9 秒，且第 3 档存在的意义就是「在线拉取已经失败或没给价」，再打一次网络是自相矛盾 |
| 把整份 4.3 MB 塞进包 | ❌ 99.6% 是我们不用的 200 家 provider |
| 手写维护 TOML | ❌ 上游每小时变，手写必然烂掉，且无来源可追溯 |
| CI 定期自动开 PR 刷新快照 | ✅ **推荐作为后续增强**，与本方案正交：同一个脚本，跑在 CI 里，diff 非空就开 PR |

---

## 6. 快照过期怎么在 Dashboard 上如实显示

红线是「不得用静态假数据冒充已连接服务」。价格快照的诚实呈现要点：

1. **快照日期来自数据，不来自渲染时刻**。`prices.toml` 的 `[meta].snapshot_date` 是唯一事实来源，Dashboard 渲染时算 `已过期 N 天 = today - snapshot_date`，**不缓存这个差值**。
2. **每一条费用都带价格来源标签**，对应价格链五档：

   | 档位 | 标签 | 含义 |
   |---|---|---|
   | 1 | `在线目录` | 本次会话从 provider 拉到的实时价 |
   | 2 | `免费模型` | 命中免费规则 |
   | 3 | `静态快照 · 2026-08-26 · 已过期 12 天` | 来自 `prices.toml` |
   | 4 | `Provider 估算` | provider 级估算价 |
   | 5 | `默认估算 $3/$15` | 未知 provider 兜底 |

   第 3、4、5 档的金额在 UI 上应有视觉区分（例如加 `~` 前缀或置灰），让用户一眼看出这不是实价。
3. **过期分级而非二元**。建议：≤30 天正常显示；31–90 天黄色提示「快照较旧」；>90 天红色提示「快照已显著过期，建议重新生成」。阈值写进配置，不硬编码在模板里。
4. **绝不隐藏过期**。不要因为「过期了不好看」就退回显示「价格未知」——快照价仍比默认 $3/$15 准得多，如实标注即可。
5. **`tiered = true` 的模型**额外标注「含阶梯价，按基础档估算」。
6. **`价格未知`是独立状态**，不是 0。#1 Notes 第 4 条已定：拿不到 token 时标「价格未知」并单列调用次数与 token 量。静态表查不到的模型同理。

> 与四态（`未配置 / 已配置未测试 / 已连接 / 错误`）的关系：价格快照**不是**外部连接，不占用这四态。它是本地数据的新鲜度，用独立的「快照日期 + 过期天数」表达，不要混进连接状态。

---

## 7. 备选数据源对比

| 数据源 | 许可 | 规模 | 收录 opencode？ | 结论 |
|---|---|---|---|---|
| **models.dev `api.json`** | **MIT** | 202 provider / 7303 模型 | ✅ **两条都有** | **采用** |
| [LiteLLM `model_prices_and_context_window.json`](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json) | MIT（`enterprise/` 目录除外） | 129 provider / 3213 条 | ❌ **零收录** | 排除 |
| [OpenRouter `/api/v1/models`](https://openrouter.ai/api/v1/models) | 无明确数据许可 | 417 模型 | ❌ 只含 OpenRouter 自营 | 排除 |

**LiteLLM 为什么不行**：它有 3213 条记录、129 家 provider，但 `litellm_provider` 字段里**没有任何一条包含 `opencode`**。它确实有一条裸 `deepseek-v4-flash`，但那是 DeepSeek 官方直连价（`input_cost_per_token` = 4.4e-07，即 $0.44/M），与 OpenCode Go 的 $0.22/M **差一倍**。用它会算错我们唯一的首发 provider。

**OpenRouter 为什么不行**：417 个模型全是 OpenRouter 自己转售的，`pricing` 是每 token 的字符串（如 `"0.0000001"`）。它不知道 OpenCode Go 的存在。另外其数据许可未声明，随包分发有合规风险。

**补充观察**：models.dev 的覆盖面之所以碾压另外两家，是因为它每小时自动同步 200+ 家 provider 的目录（含 OpenRouter、NanoGPT、Kilo 等聚合商），且接受社区 PR。对一个「加供应商不写代码」的架构（#1 Notes 第 1 条），它的 `api` + `env` 字段正好就是配置表需要的两列——这是额外的契合。

---

## 8. 给 #15 的接口建议

1. 价格查表键为 `(provider_id, model_id)`，`provider_id` 用 models.dev 的 id（`opencode-go`）。
2. 查表返回值建议是一个带来源的结构，而不是裸数字：`Price(input, output, cache_read, source="static_snapshot", snapshot_date=..., tiered=bool)`。费用账本靠 `source` 决定 UI 标签，靠 `tiered` 决定是否加注。
3. `prices.toml` 用 `tomllib`（Python 3.11+ 标准库）读，随包走 `importlib.resources`，无需新增运行时依赖。
4. 生成脚本的最终落位建议 `scripts/gen_prices.py`（构建/维护脚本，不进 wheel），产物 `src/agent_alfred/data/prices.toml`（进 wheel）。
5. 离线确定性测试用 `--from-file` 配一份小 fixture，断言：`opencode-go/deepseek-v4-flash` 解析出 `0.22/0.66`；且它**不等于** `opencode/deepseek-v4-flash` 的 `0.14/0.28`。这条断言正好锁住本票最容易回退的那个 bug。

---

## 9. 复核方法

```bash
# 1. 拉快照（注意必须带 User-Agent，否则 Cloudflare 403）
curl -sSL https://models.dev/api.json -o api.json

# 2. 确认 opencode-go 的 deepseek 价格
python3 -c "import json;d=json.load(open('api.json'));print(d['opencode-go']['models']['deepseek-v4-flash']['cost'])"
# → {'input': 0.22, 'output': 0.66, 'cache_read': 0.007}

# 3. 跑生成脚本
python3 docs/research/gen_prices_draft.py --out - --from-file api.json --providers opencode-go
```

### 来源链接

- models.dev API：<https://models.dev/api.json>
- models.dev 仓库：<https://github.com/anomalyco/models.dev>
- models.dev 许可证：<https://github.com/anomalyco/models.dev/blob/main/LICENSE>
- 同步 workflow（cron `17 * * * *`）：<https://github.com/anomalyco/models.dev/blob/main/.github/workflows/sync-models.yml>
- OpenCode Zen 文档：<https://opencode.ai/docs/zen>
- LiteLLM 价表：<https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json>
- OpenRouter 模型端点：<https://openrouter.ai/api/v1/models>
