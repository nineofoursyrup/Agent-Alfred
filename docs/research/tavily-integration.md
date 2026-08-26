# 调研：Tavily API 与可选集成注册表的五要素映射

- 票：[#10](https://github.com/nineofoursyrup/Agent-Alfred/issues/10)（前置于 [#20 实现：可选集成注册表与 Tavily web_search](https://github.com/nineofoursyrup/Agent-Alfred/issues/20)）
- 日期：2026-08-26
- 结论优先级：本文所有「实测」标记的内容为本次真实发起 HTTP 请求所得；其余为官方文档摘录并附链接。**未标注来源的推断一律写成「判断」并给出依据**。

---

## 0. 一句话结论

Tavily 用 Bearer 认证、单一 POST 端点即可满足 `web_search`；**健康检查用免费的 `GET /usage`**；**不引入 `tavily-python`，用标准库 `urllib.request` 直连 REST**，因此这个集成的「依赖 extra」为空——注册表必须支持「无 extra 的可选集成」这一形态。

---

## 1. 认证与端点

| 项 | 值 | 来源 |
|---|---|---|
| 搜索端点 | `POST https://api.tavily.com/search` | [API Reference: search](https://docs.tavily.com/documentation/api-reference/endpoint/search) |
| 用量端点 | `GET https://api.tavily.com/usage` | [API Reference: usage](https://docs.tavily.com/documentation/api-reference/endpoint/usage) |
| 认证 | 请求头 `Authorization: Bearer tvly-YOUR_API_KEY` | 同上 |
| 密钥前缀 | `tvly-`（开发密钥实际形如 `tvly-dev-…`） | 官方示例 |
| 可选头 | `X-Project-ID`：把用量查询限定到某个 project | usage 文档 |

密钥只有一个，没有 client id / secret 之类的第二字段，也没有 OAuth 流程——这让它成为一个非常干净的注册表首样例。

## 2. 请求参数

来源：[API Reference: search](https://docs.tavily.com/documentation/api-reference/endpoint/search)

| 参数 | 类型 | 默认 | 取值 | 说明 |
|---|---|---|---|---|
| `query` | string | **必填** | — | 查询串 |
| `search_depth` | string | `basic` | `basic` / `advanced` / `fast` / `ultra-fast` | 延迟与相关性的取舍；`advanced` 计 2 credits，其余 1 credit |
| `topic` | string | `general` | `general` / `news` / `finance` | 搜索领域 |
| `max_results` | integer | `5` | 0–20 | 返回结果条数上限 |
| `chunks_per_source` | integer | `3` | 1–3 | 每个来源的内容片段数 |
| `include_answer` | bool/string | `false` | `true` / `false` / `basic` / `advanced` | 是否附带 LLM 生成的直接答案 |
| `include_raw_content` | bool/string | `false` | `true` / `false` / `markdown` / `text` | 是否附带清洗后的正文全文 |
| `include_images` | bool | `false` | — | 是否返回图片 |
| `include_image_descriptions` | bool | `false` | — | 图片描述 |
| `include_favicon` | bool | `false` | — | 每条结果附 favicon URL |
| `include_domains` | array | `[]` | 最多 300 个域名 | 白名单 |
| `exclude_domains` | array | `[]` | 最多 150 个域名 | 黑名单 |
| `time_range` | string | `null` | `day`/`week`/`month`/`year` 或 `d`/`w`/`m`/`y` | 按发布时间过滤 |
| `start_date` / `end_date` | string | `null` | `YYYY-MM-DD` | 按发布日期区间过滤 |
| `country` | string | `null` | 国家名，如 `united states` | 提升该国结果权重 |
| `language` | string | `null` | ISO 639-1 或语言名 | 提升该语言结果权重 |
| `filter_by_language` | bool | `false` | — | 严格按语言过滤（而非仅加权） |
| `auto_parameters` | bool | `false` | — | 让 Tavily 自动推断上述参数 |
| `exact_match` | bool | `false` | — | 只返回精确短语匹配 |
| `include_usage` | bool | `false` | — | **响应内联本次调用的 credit 消耗** |
| `safe_search` | bool | `false` | — | 过滤成人 / 不安全内容 |

### 对本项目的取用建议

- **`include_usage: true` 应当常开。** 它让每次搜索的响应里带回 `usage.credits`，可以直接喂进 Ops 账本，与地图 Notes 第 4 条「Token 永远是唯一事实来源，费用在渲染层算」同构——这里 credits 就是那个「事实来源」。
- `search_depth` 默认留 `basic`（1 credit）。`advanced` 双倍计费，应当是显式参数而非默认。
- `include_raw_content` 默认关。打开会把整页正文塞进模型上下文，token 成本远高于 credit 成本。
- `include_answer` 建议关：Alfred 自己就是那个做总结的 Agent，让 Tavily 再生成一遍答案是重复付费且削弱可追踪性。

## 3. 响应结构

来源：[API Reference: search](https://docs.tavily.com/documentation/api-reference/endpoint/search)

```json
{
  "query": "string",
  "answer": "string（include_answer 为真时）",
  "images": [{ "url": "string", "description": "string" }],
  "results": [
    {
      "id": "string",
      "title": "string",
      "url": "string",
      "content": "string",
      "score": 0.81025416,
      "raw_content": "string（include_raw_content 为真时）",
      "favicon": "string（include_favicon 为真时）"
    }
  ],
  "auto_parameters": {},
  "response_time": 1.67,
  "usage": { "credits": 1 },
  "request_id": "string"
}
```

> **说明**：以上是官方 API Reference 给出的 **schema**（字段名、类型、以及 `score` / `response_time` 的官方示例值）。官方文档**没有**提供一份填满真实数据的完整响应样例，本次调研也没有可用密钥去实发一次搜索，因此**上面不是一次真实调用的抓包**，不得当作实测数据引用。`results[].content` 是 Tavily 抽取的相关片段（受 `chunks_per_source` 控制），不是整页正文。

`request_id` 值得记进 trace：出问题时这是向 Tavily 报障的唯一凭据。

## 4. 定价与配额

| 档位 | 价格 | 额度 | 来源 |
|---|---|---|---|
| Researcher（免费） | $0 | **1,000 credits / 月，无需信用卡** | [定价页](https://www.tavily.com/pricing)、[API Credits](https://docs.tavily.com/documentation/api-credits) |
| Pay As You Go | **$0.008 / credit** | 无月度下限 | 定价页 |
| Project | 滑杆计价 | 4,000 credits / 月起，更高速率限制 | 定价页 |
| Enterprise | 定制 | 定制 | 定价页 |

credit 消耗（[API Credits](https://docs.tavily.com/documentation/api-credits)）：

- Search `basic` / `fast` / `ultra-fast`：**1 credit / 次**
- Search `advanced`：**2 credits / 次**
- Extract / Map / Crawl / Research 另计（本项目 v1 不用）

**免费额度换算**：1,000 credits/月 ≈ 1,000 次 basic 搜索。以私人助手的用量，免费档基本够用——这支持「默认未配置、用户自己去申请一个免费 key」的产品姿态，不需要预设付费。

### 速率限制

来源：[Rate Limits](https://docs.tavily.com/documentation/rate-limits)

| 端点 | Development key | Production key |
|---|---|---|
| 标准端点（含 `/search`） | 100 RPM | 1,000 RPM |
| `/crawl` | 100 RPM | 100 RPM |
| `/research`（建任务） | 20 RPM | 20 RPM |
| **`/usage`** | **10 次 / 10 分钟** | **10 次 / 10 分钟** |

超限返回 `429`，并带 `retry-after` 头（单位秒），官方明确建议按该头做退避重试。Production key 需要付费计划或开启 PAYGO。

> ⚠️ **`/usage` 的 10 次 / 10 分钟是本次调研最重要的实现约束**，见第 6 节。

## 5. 四种失败的 HTTP 状态码与响应体

这一节直接决定四态怎么判，所以尽量走实测。

### 5.1 实测结果

以下为本次调研**真实发起的请求**（用一个格式合法但不存在的密钥 `tvly-dev-0000000000000000000000000000`），原样粘贴：

**① 完全不带 `Authorization` 头** → `HTTP 401`

```json
{
    "detail": {
        "error": "Unauthorized: missing or invalid API key."
    }
}
```

**② 带无效密钥打 `/search`** → `HTTP 401`

```json
{"detail":{"error":"Unauthorized: missing or invalid API key."}}
```

**③ 带无效密钥打 `/usage`** → `HTTP 401`

```json
{"detail":{"error":"Unauthorized: missing or invalid API key."}}
```

**④ 密钥无效 + 请求体缺 `query`** → `HTTP 422`（**不是文档写的 400**）

```json
{"detail":[{"type":"missing","loc":["body","query"],"msg":"Field required","input":{"raw_input":{}}}]}
```

### 5.2 三条实测得出的关键结论

1. **「缺密钥」和「密钥错误」在服务端不可区分**——两者都是 401、同一句 `Unauthorized: missing or invalid API key.`。所以 **`未配置` 这个状态必须在客户端本地判定**（环境变量不存在 / 为空串就直接短路，压根不发请求），绝不能靠打一次 API 看返回码来推断。这也顺带省掉一次网络往返和一次误报。
2. **参数校验错误返回 422 而非文档所写的 400**，响应体是 FastAPI 风格的 `detail` **数组**（`[{type, loc, msg, input}]`），与 401/432/433 的 `detail` **对象**（`{error: "..."}`）结构不同。解析错误信息时必须同时处理这两种 `detail` 形状，否则会在 422 上抛 `TypeError`。
3. **鉴权失败先于参数校验**：③ 中密钥无效直接 401；④ 中密钥同样无效却返回了 422，说明校验顺序并非严格「先鉴权后校验」，两类错误都可能先冒出来。因此错误分支要按状态码穷举，不能假设优先级。

### 5.3 完整状态码对照

文档来源：[API Reference: search](https://docs.tavily.com/documentation/api-reference/endpoint/search)、[Rate Limits](https://docs.tavily.com/documentation/rate-limits)

| 场景 | HTTP | 响应体 | 是否实测 |
|---|---|---|---|
| **未配置** | 无（不发请求） | — | 客户端本地判定 |
| **鉴权失败**（缺失或无效密钥） | `401` | `{"detail":{"error":"Unauthorized: missing or invalid API key."}}` | ✅ 实测 |
| 参数非法 | `422` | `{"detail":[{"type":"missing","loc":[...],"msg":"...","input":{...}}]}` | ✅ 实测 |
| 参数非法（文档口径） | `400` | `{"detail":{"error":"<校验错误>"}}` | 文档 |
| 速率超限 | `429` + `retry-after` 头 | `{"detail":{"error":"Your request has been blocked due to excessive requests..."}}` | 文档 |
| **配额耗尽**（计划额度） | `432` | `{"detail":{"error":"This request exceeds your plan's set usage limit..."}}` | 文档 |
| **配额耗尽**（PAYGO 上限） | `433` | `{"detail":{"error":"This request exceeds the pay-as-you-go limit..."}}` | 文档 |
| 服务端故障 | `500` | `{"detail":{"error":"Internal Server Error"}}` | 文档 |
| **网络超时** | 无 HTTP 响应 | `urllib` 抛 `socket.timeout` / `URLError`（DNS、连接拒绝、TLS 失败同类） | — |

`432` / `433` 是 Tavily 自定义的非标准状态码，标准 HTTP 客户端不会特殊处理，但 `urllib` 会照常把它们包成 `HTTPError` 并保留 `.code`——**这正是不用 SDK 的理由之一**（见第 7 节）。

### 5.4 映射到四态

红线要求：`未配置 / 已配置未测试 / 已连接 / 错误`，且「功能尚未实现」不属于其中任何一态。

| 状态 | 判定条件 | 展示 |
|---|---|---|
| **未配置** | `TAVILY_API_KEY` 不存在或为空串。**不发任何请求。** | 「未配置」+ 一条指向 tavily.com 申请免费 key 的指引 |
| **已配置未测试** | 密钥存在，但本进程内**还没有过一次成功的健康检查**（刚启动、或刚改完 key 使缓存失效） | 「已配置未测试」+「测试连接」按钮。**不得显示为已连接** |
| **已连接** | `GET /usage` 返回 `200` | 「已连接」+ 可顺带展示 `plan_usage / plan_limit` 余额 |
| **错误** | 下列任一，且必须带上具体原因串 | 「错误：<原因>」 |

「错误」态的原因细分（**必须落到具体原因，不能笼统显示「错误」**）：

| 原因 | 触发 |
|---|---|
| 密钥无效 | `401` |
| 速率超限，请 N 秒后重试 | `429`（N 取 `retry-after`） |
| 计划额度已用尽 | `432` |
| PAYGO 额度已用尽 | `433` |
| Tavily 服务异常 | `5xx` |
| 网络不可达 / 超时 | `socket.timeout` / `URLError` |
| 响应无法解析 | 200 但 JSON 解析失败或缺 `results` |

**判断：配额耗尽（432/433）归入「错误」而非「已连接」。** 依据是四态描述的是「这个外部能力现在能不能真的干活」，而不是「凭据是否合法」。额度耗尽时密钥合法但搜索必然失败，若显示「已连接」，等于用一个乐观状态掩盖了一个必然的失败——正落在「不得伪造连接状态」这条红线上。同理，`429` 也归「错误」，只是它自带恢复时间，文案上应提示可重试。

**判断：状态不做持久化。** 「已配置未测试 → 已连接」的跃迁只在进程内内存中生效，重启后回落到「已配置未测试」。依据是磁盘上的一个陈旧「已连接」标记，正是「用静态数据冒充已连接服务」的典型形态——上次能连不代表这次能连。这与地图 Notes 第 3 条「目录缓存与价格缓存重启即失」是同一条原则。

## 6. 健康检查

**结论：用 `GET https://api.tavily.com/usage`。**

### 为什么是它

| 候选 | credit 成本 | 副作用 | 结论 |
|---|---|---|---|
| `GET /usage` | **0**（见下） | 无 | ✅ 采用 |
| `POST /search` with `max_results=0` | 1 credit | 真发一次搜索 | ❌ 每次探活烧配额 |
| `POST /search` 故意传非法参数看是否 422 | 0 | 无 | ❌ 422 无法区分「密钥有效」与「密钥无效但先撞上校验」（5.2 第 3 点实测已证），判不出健康 |

**`/usage` 不消耗 credit 的依据**：[API Credits](https://docs.tavily.com/documentation/api-credits) 逐条列出了 Search / Extract / Map / Crawl / Research 五类端点的 credit 单价，**`/usage` 不在其中**；且 [Rate Limits](https://docs.tavily.com/documentation/rate-limits) 把 `/usage` 单独拎出来限制为 10 次 / 10 分钟，而不是并入按 credit 计费的标准端点。两点合起来足以判定它免费。⚠️ 但**官方并未用一句话明写「/usage 免费」**，这是推断，不是引文。

### 响应结构

来源：[API Reference: usage](https://docs.tavily.com/documentation/api-reference/endpoint/usage)

- `key` 对象（本密钥维度）：`usage`（本计费周期已耗 credits）、`limit`（上限，无限则为 null）、`search_usage` / `extract_usage` / `crawl_usage` / `map_usage` / `research_usage`
- `account` 对象（账户维度）：`current_plan`（计划名）、`plan_usage`、`plan_limit`、`paygo_usage`、`paygo_limit`，以及与 `key` 同名的分端点用量字段

这比「能不能连上」多给了一样东西：**余额**。所以健康检查成功时，Dashboard 可以直接显示「已连接 · 本月已用 137 / 1000」，这是真实数据而非装饰。

### 实现约束（务必带进 #20）

**`/usage` 的速率限制是 10 次 / 10 分钟，即平均 1 分钟 1 次。** 这意味着：

1. **绝不能在每次 Dashboard 渲染 / 每次 SSE 心跳时探活**，九页 Dashboard 里只要有两个页面各自轮询就会瞬间打满。
2. 健康检查结果必须**在进程内缓存**，建议成功缓存 60 秒、失败缓存约 30 秒（与地图 Notes 第 3 条模型目录「成功 5 分钟 / 失败约 1 分钟」的缓存哲学一致，只是周期更短）。
3. 探活应当是**手动触发为主**（设置页的「测试连接」按钮）+ 缓存兜底，而非后台定时轮询。
4. 探活本身要有**独立的短超时**（建议 5 秒，短于搜索调用的 10 秒），因为它挡在 UI 交互路径上。
5. 讽刺但重要：**健康检查自己也可能撞 429**。撞上时应显示「已配置未测试（探活过于频繁，请稍候）」而不是「错误」——因为这不是集成本身的故障。

## 7. 依赖形态：`tavily-python` vs 裸 `urllib.request`

### 官方 SDK 的依赖树

`tavily-python` 最新版 **0.8.0**，`install_requires = ['requests', 'tiktoken>=0.5.1', 'httpx']`（[setup.py](https://github.com/tavily-ai/tavily-python/blob/master/setup.py)、[PyPI JSON API](https://pypi.org/pypi/tavily-python/json)，均为本次实取）。

展开传递依赖（各包 PyPI 元数据实取）：

```
tavily-python 0.8.0
├── requests          → charset_normalizer, idna, urllib3, certifi
├── tiktoken >=0.5.1  → regex, requests            ← 编译扩展（Rust）
└── httpx             → anyio, certifi, httpcore, idna
                        ├── httpcore → certifi, h11
                        └── anyio    → idna, sniffio/typing_extensions, exceptiongroup(<3.11)
```

去重后约 **13 个第三方包**，其中 `tiktoken` 与 `regex` 是**带编译扩展的二进制包**。而 Alfred 需要的全部能力是：**一个 POST + 一个 GET，带一个 Bearer 头，收发 JSON。**

### 明确推荐：**用标准库 `urllib.request`，不引入 SDK，不设 optional extra**

理由按权重排序：

1. **错误分支的可控性（决定性理由）。** 四态判定的全部信息量都在 HTTP 状态码里——尤其是 `432` / `433` 这两个 Tavily 自定义码。SDK 会把 HTTP 错误包装成自己的异常类型，是否为每个码保留可区分的类型、是否透传 `retry-after`，都是 SDK 的实现细节且随版本漂移；而 `urllib.error.HTTPError` 直接暴露 `.code`、`.headers`、`.read()`，四态映射是一段自己完全掌握的 `if code == 401 / 429 / 432 / 433` 。**为了不伪造状态，我们必须拿到原始状态码。** 这条压倒其余所有理由。
2. **`tiktoken` 与用途完全无关。** 它在 SDK 里服务于「按 token 数截断上下文」这类辅助函数，Alfred 的 token 计量走自己的 Ops 账本（地图 Notes 第 4 条）。为一个用不到的功能背一个 Rust 编译扩展是纯负债。
3. **打包门禁。** 终点要求「wheel+sdist 在干净临时环境验证」。零依赖的集成永远不会在这道门上失败；带编译扩展的依赖则可能在某个 Python 版本 / 平台上缺少预编译 wheel 而回退到源码构建。
4. **核心依赖极简是项目既定约束**，SDK 会把三个 HTTP 客户端（`requests`、`httpx`、外加标准库）同时拖进环境，三者语义各异，反而增加心智负担。
5. **API 面极窄且稳定。** 一个 POST 端点、一个 GET 端点、一个请求头。SDK 在这里没有封装掉任何复杂度——它不是一层深模块，只是一层薄转译。
6. **超时可控。** `urllib.request.urlopen(..., timeout=N)` 是显式参数；SDK 的默认超时是隐式的，且 `requests` 默认**永不超时**，一次网络抖动就能把 Agent 挂死。

### 由此得出的一个注册表设计结论

Tavily 作为第一个真实样例，**证明了「可选集成」不等于「可选依赖」**：它可选（默认未配置），但依赖 extra 为空。因此注册表的 `extra` 字段必须允许为 `None`，且「未配置」的判定只看**配置字段**，绝不能看「依赖是否已安装」——否则 Tavily 会因为没有 extra 而被误判成永远可用。

对照：真正需要 extra 的将是 OpenTelemetry 导出（地图 Out of scope 已提到「未安装依赖时提示并继续本地 JSONL」）。那才是 `extra` 非空的样例。**两者形态不同，注册表要同时容纳。**

> 反面成本：不用 SDK 意味着 Tavily 改响应结构时我们得自己跟。考虑到需要的字段只有 `results[].{title,url,content,score}` 和 `usage.credits`，且这些是 API 的核心契约，风险可接受。缓解办法是解析时对可选字段一律用 `.get()`，缺字段降级而不抛。

### 参考实现骨架

```python
import json, urllib.error, urllib.request

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_USAGE_URL = "https://api.tavily.com/usage"


def _call(url: str, api_key: str, payload: dict | None, timeout: float):
    """Return (status, parsed_body). Raises nothing for HTTP errors."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method="POST" if payload is not None else "GET",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        # .code carries 401 / 422 / 429 / 432 / 433 / 5xx verbatim -- this is
        # exactly the signal the four-state display is derived from.
        body = exc.read().decode(errors="replace")
        try:
            body = json.loads(body)
        except ValueError:
            pass
        return exc.code, body
    # urllib.error.URLError and socket.timeout propagate: network-level failure.
```

`detail` 的两种形状（对象 / 数组，见 5.2 第 2 点）在提取错误文案时要分别处理。

## 8. 五要素映射表

地图 issue #1 Notes 第 5 条把五要素定为：**字段 / 是否密钥 / 依赖 extra / 健康检查 / 重载范围**。

> 📌 **口径出入待 #20 确认**：本票的派工描述里五要素被写成六项（多了「启用条件」）。本文按 issue #1 的五项为准，并把「启用条件」作为**可推导项**单列——它完全由「字段 + 是否密钥」决定，不必单独存储。请在 #20 落地时敲定注册表 schema 到底存五项还是六项。

### Tavily `web_search` 的注册表条目

| 要素 | 值 |
|---|---|
| **字段** | 单字段 `TAVILY_API_KEY`（环境变量 / `.env`）。无 base_url、无 org id、无第二密钥。可选的非密钥调参（`search_depth`、`max_results`、`topic`）**不进注册表**，它们属于工具行为配置，走 Dashboard 设置页 |
| **是否密钥** | **是。** `TAVILY_API_KEY` 是密钥字段：只从环境 / `.env` 读，**永不落库、永不进 trace / 日志**，界面只显示末四位（与地图 Notes 第 2 条对模型 key 的处理完全一致） |
| **依赖 extra** | **无（`None`）。** 仅用标准库 `urllib.request`，见第 7 节。注册表必须支持该字段为空 |
| **健康检查** | `GET https://api.tavily.com/usage`，超时 5s，0 credit。成功缓存 60s / 失败缓存 30s。**手动触发为主**，因其速率限制仅 10 次 / 10 分钟。成功时可顺带渲染 `account.plan_usage` / `account.plan_limit` 作为真实余额 |
| **重载范围** | **两级，见下节** |

### 可推导项

| 项 | 值 |
|---|---|
| **启用条件** | `bool(os.environ.get("TAVILY_API_KEY", "").strip())`。**纯本地判定，不发网络请求**——依据是 5.2 第 1 点：401 无法区分「缺密钥」与「密钥错」，只能本地判 |

## 9. 重载范围：改了 API key 之后到底要重启什么

**结论：分两级，且任何一级都不需要重启 Gateway。**

| 变更 | 重载范围 | 依据 |
|---|---|---|
| **改 key 的值**（已配置 → 已配置，即密钥轮换） | **热更新。** 只需清掉健康检查缓存，把状态打回「已配置未测试」 | 密钥的值只出现在**每次 HTTP 请求的 `Authorization` 头里**。只要工具实现在**调用时**读取配置（而不是在构造时把 key 捕获进闭包），下一次搜索自然就用上新 key。没有任何长生命周期对象持有它 |
| **未配置 ⇄ 已配置**（工具在 / 不在模型的 tools 清单里） | **重建 Agent。** 不必重启 Gateway | 启用与否决定 `web_search` 是否出现在发给模型的 `tools` 数组里。该数组在**构造 Agent 时被烘焙**进请求模板，是一个不可变结构；改变它就必须重造持有它的对象 |
| **任何情况** | **不需要重启 Gateway** | Gateway（CLI 与 Web 两个）持有的应是 **Agent 工厂**而非 Agent 实例。只要重建 Agent 的能力在进程内可达，进程本身就不必重启。若某个实现里 Gateway 直接长持一个 Agent 单例，那是该实现的缺陷，应在 #20 修正而不是把重载范围上调到「重启 Gateway」 |

### 判断依据（可推广到注册表里的其他集成）

> **一个字段的重载范围，等于「这个值被烘焙进的最外层不可变结构」的重建范围。**

按此标尺：

- 值只在**请求发出的那一刻**被读 → **热更新**（Tavily 的 key 值、模型 key 值）
- 值决定**Agent 构造期就定型的结构**（tools 数组、system prompt、模型选型）→ **重建 Agent**（Tavily 的启用与否）
- 值决定**进程级资源**（监听端口、DB 文件路径、日志 sink）→ **重启 Gateway**

Tavily 一个集成同时踩中前两档，这恰恰说明**重载范围不是「每个集成一个值」，而是「每个字段一个值」**——注册表的 `reload_scope` 应挂在字段上，或者至少为「值变更」和「启用状态变更」分别记一个。这是这个首样例给注册表 schema 提出的最实质的要求。

## 10. 留档：为什么是 Tavily，而不是 Brave Search API 或 Serper

（用户已选定 Tavily，本节仅作决策留档。Brave / Serper 部分数据来自二手来源，标注见下。）

| | Tavily | Brave Search API | Serper |
|---|---|---|---|
| 免费额度 | 1,000 credits / 月，无需信用卡 <sup>[定价页](https://www.tavily.com/pricing)</sup> | 免费档已取消，改为每月 $5 抵扣额度（约 1,000 次）<sup>[Brave 定价页](https://api-dashboard.search.brave.com/documentation/pricing)（$5/月抵扣、$5/千次为一手；"免费档已取消"为二手报道）</sup> | 2,500 次免费，无需信用卡（二手来源） |
| 单价 | $0.008 / credit | $5.00 / 1,000 次 = $0.005 / 次 | 约 $0.30–$1.00 / 1,000 次（二手来源） |
| 返回形态 | **面向 LLM**：抽取好的相关片段 + score，可选直接答案 | 原始搜索结果（URL / 文本 / 新闻 / 图片），另有 LLM 上下文 | Google SERP 原样代理 |
| 余额查询 | **有 `/usage` 端点** | 无同类免费端点 | 无同类免费端点 |
| 配额状态码 | **432 / 433 专码可区分** | 标准 429 | 标准 429 |

**一段话结论：** 单看单价，Serper 便宜数倍，Brave 也略便宜；但 Alfred 需要的不是最便宜的 SERP 代理，而是**最省 token 的检索结果**。Serper 和 Brave 返回的是原始搜索结果，要么得把整页塞进上下文、要么得自己再写一层抽取，省下的那点 API 费会在模型 token 上加倍吐出来——而 Tavily 直接返回按查询抽取好的片段并附相关性 score，`content` 字段拿来就能进上下文。更关键的是**可观测性**：Tavily 是三者中唯一提供免费余额端点（`/usage`）和专用配额状态码（`432` / `433`）的，这让「已连接」和「配额耗尽」能被**如实区分**，而不必靠一次真实搜索去试探——对一个把「不伪造连接状态」写成红线的项目，这一条本身就足以定标。1,000 次/月的免费额度对私人助手的用量也绰绰有余。**代价**是绑定了一家相对小的供应商，且 `432`/`433` 是非标准状态码；缓解办法是把它藏在集成注册表这层抽象之后——换供应商时只需换注册表里的一条，这正是 #20 要建的东西。

## 11. 给 #20 的落地清单

1. 注册表 `extra` 字段允许为 `None`；「启用条件」只看配置字段，**不看依赖是否安装**（§7 末）。
2. `reload_scope` 挂到**字段**粒度，至少区分「值变更＝热更新」与「启用状态变更＝重建 Agent」（§9）。
3. `未配置` 本地判定，**不发网络请求**（§5.2-1）。
4. 错误解析要同时处理 `detail` 为**对象**和**数组**两种形状（§5.2-2）。
5. 错误态必须细分到具体原因串，`432`/`433`/`429` 归「错误」而非「已连接」（§5.4）。
6. 健康检查结果**只存内存、重启即失**，绝不持久化（§5.4 末）。
7. `/usage` 限 10 次 / 10 分钟：必须缓存 + 手动触发，禁止渲染期轮询（§6）。
8. 健康检查自身撞 `429` 时显示「已配置未测试（探活过于频繁）」，不算「错误」（§6）。
9. 搜索调用常开 `include_usage: true`，把 `usage.credits` 写进 Ops 账本（§2）。
10. `request_id` 记入 trace（§3）。
11. 显式超时：搜索 10s、探活 5s（§6、§7）。
12. 密钥永不落库 / 永不进 trace，界面只显示末四位（§8）。

---

## 来源

**一手（官方）**

- [Tavily Docs — Search API Reference](https://docs.tavily.com/documentation/api-reference/endpoint/search)
- [Tavily Docs — Usage API Reference](https://docs.tavily.com/documentation/api-reference/endpoint/usage)
- [Tavily Docs — Rate Limits](https://docs.tavily.com/documentation/rate-limits)
- [Tavily Docs — API Credits](https://docs.tavily.com/documentation/api-credits)
- [Tavily Docs — Quickstart](https://docs.tavily.com/documentation/quickstart)
- [Tavily 定价页](https://www.tavily.com/pricing)
- [tavily-python setup.py](https://github.com/tavily-ai/tavily-python/blob/master/setup.py) / [PyPI JSON API](https://pypi.org/pypi/tavily-python/json)（依赖树由各包 PyPI 元数据实取）
- [Brave Search API 定价页](https://api-dashboard.search.brave.com/documentation/pricing)

**实测**

- 2026-08-26 对 `api.tavily.com` 的 `/search` 与 `/usage` 发起真实请求（无密钥 / 无效密钥 / 缺必填字段），结果见 §5.1，原样粘贴未加工。

**二手（仅用于 §10 的 Brave/Serper 对比，已在文中标注）**

- Serper 免费额度与单价、Brave 免费档取消一事，来自搜索结果中的第三方汇总页，未经一手核实。
