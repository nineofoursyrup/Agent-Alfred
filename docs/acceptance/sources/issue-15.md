# #15 实现：模型目录、价格链与费用账本语义

来源：https://github.com/nineofoursyrup/Agent-Alfred/issues/15

## Question

模型目录、价格链与费用账本语义。

**目录**（建图已锁定的行为，照此实现）：

- `catalog.list_models()`：走 provider 指定的 `catalog_url`，否则 `{base_url}/models`；携带该 provider 的 key；超时 10s。
- 解析模型、价格、上下文长度、工具支持。
- 进程内 `_models_cache`：成功缓存 5 分钟，失败缓存约 1 分钟。
- 响应含 `pricing.prompt` / `pricing.completion` 时调用 `remember_price()` 写入进程内价格缓存。
- **拉取失败不影响聊天**：退回内置默认模型，并把错误原因如实展示。
- 唯一持久化的本地文件**只保存用户钉选的模型**；目录缓存与价格缓存重启即失。

**价格链**（按序，缺一档往下落）：

1. `provider_reported` —— 端点自报实付（调研 [#7](https://github.com/nineofoursyrup/Agent-Alfred/issues/7) 实测 xAI 直接返回 `cost_in_usd_ticks`）。**独此一档给 `exact`**。
2. `user_override` —— 用户手填价。**位置必须在目录价之上**，否则不叫覆盖。
3. `catalog` —— 在线目录缓存。
4. `free_rule` —— 免费模型规则。
5. `model_static` —— 模型级静态价。
6. `provider_static` —— provider 级估算价。
7. **未知** —— 以上全部落空。

⚠️ ~~未知 Provider 默认 `$3 / $15`~~ **已废除**：凭空猜出的数字混进「估算」，
比诚实地说「未知」更有害。落到第 7 档就是 `unknown`，**不得再兜一个数字出来**。

价格查表键是 **`(provider_id, model_id)` 二元组**——调研
[#8](https://github.com/nineofoursyrup/Agent-Alfred/issues/8) 实测 `opencode` 与
`opencode-go` 有 6 处价格不一致，单用 `model_id` 查表必然串价。

**账本语义**：

- **Token 永远是唯一事实来源，费用在渲染层算**，不随用量存储。
- 费用三态：
  - `exact` —— **`provider_reported` 独占**。
  - `estimated` —— 其余有价者一律如此，且**必须携带 `price_source`**。
  - `unknown` —— Token 缺失或无可接受价格。**不出任何金额**，只单列调用次数与 Token 量。
- 未知模型**不得**按零费用或均价处理。
- 订阅制（OpenCode Go）**照样按 token 计费**。

trace 文件（用量事实的落盘载体）的布局、切分与保留策略**不属本票**，归
[决定：trace 文件布局、切分与保留策略](https://github.com/nineofoursyrup/Agent-Alfred/issues/22)。

### 依赖

[调研：models.dev 目录结构与内置静态价表的生成方案](https://github.com/nineofoursyrup/Agent-Alfred/issues/8)
必须先给出第 5 档 `model_static` 静态价表的内容来源——否则 `deepseek-v4-flash`
会一路落到 `provider_static` 乃至「未知」，费用要么按整个 provider 的粗估串价，要么根本出不来。


---

⚠️ **由 [#29](https://github.com/nineofoursyrup/Agent-Alfred/issues/29) 修订，六处：**

- **三处改名**：`(provider_id, model_id)` → **`(endpoint_id, model_id)`**；`provider_reported` → **`endpoint_reported`**；`provider_static` → **`endpoint_static`**。理由同上：同一主体不同端点不同价，已被调研 [#8](https://github.com/nineofoursyrup/Agent-Alfred/issues/8) 实测证伪。
- **`price_override` 是四维可选**（uncached input / `cache_read` / `cache_write` / output，维度取自 [#2](https://github.com/nineofoursyrup/Agent-Alfred/issues/2) 定的 `Usage`），值为**非负十进制定点数，单位固定 USD / 百万 Token**。**缺省 = 继续沿价格链向下查；显式 `0` = 用户声明该维免费。** 二者不可混——强制四维必填等于逼用户替缓存价瞎编，那正是本项目废除「未知 Provider 默认 $3/$15」时判定过的那类有害数字。
- **`price_source` 挂分项、不挂整笔**：新增 **`price_components`** 映射，每个**实际参与计费**的维度各带 `tokens` / `unit_price` / `source` / `amount`，以及来源元数据（`stale`、目录快照时刻、阶梯价标记）。顶层的「同档 / 混合」只是**渲染派生值**，不是存储形态。（不采用「标量 + 取最弱档」：那需要另立一套与价格链**优先级不同构**的隐性可信度排序。）
- **费用三态改为闭合判别式**：有效 `endpoint_reported_cost` 存在 → `exact`，且**不再生成任何估算分项**（两套数字并列必然互相矛盾）；否则任一必要 Token 为 `None`、或某维 Token > 0 却找不到该维价格 → 整笔 `unknown` 且**不输出任何金额**；其余 `estimated`。Token 为 0 的维度不要求价格，但适配器**只有能从协议语义证明未发生该类用量时**才可写 0，**不得把「未报告」改写成 0**。
- **stale 目录价仍可作 `estimated`**，但必须携带 `catalog_fetched_at` 与 `stale=true`。依据不是「价格很少变」，而是**带时间与降级标记的旧观测，通常比更旧的静态表或「未知」更有信息**。
- **派生导出 `usage.jsonl` 的 schema 随之改**：含金额时除来源外还须保存**本次实际采用的逐维单价**与计算时刻，否则目录价一变，历史金额再也无法复核。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/15#issuecomment-5425931745

📌 上游调研 #8 已关闭，四条**硬约束**直接落到本票：

1. **价格查表键必须是 `(provider_id, model_id)` 二元组**，不能只用 model_id。`opencode` 与 `opencode-go` 共有 17 个模型，其中 **6 个价格不一致**，`deepseek-v4-pro` 差 2.6 倍；且方向反直觉——Go 的输入输出比 Zen **贵**，缓存读却便宜 4 倍。
2. **默认 `$3/$15` 兜底的高估幅度是两个数**，票面写的「约 15 倍」不够准：输入高估 **13.6 倍**，输出高估 **22.7 倍**。Dashboard 上标注估算误差时用这两个数。
3. **407 个模型有阶梯价**（`cost.tiers` / `context_over_200k`），扁平单价会**低估**账单。方案：存基础档 + 打 `tiered = true` 标记，UI 注明「按基础档估算」，**不得假装精确**。
4. **`api.json` 没有任何生成时间戳字段**，快照日期只能由生成脚本自己记录（取 HTTP `Date` 头避开时钟偏差，并存 ETag）。且它每小时同步（cron `17 * * * *`），**快照发布当天即过期**——这反过来印证了「运行时目录必须在线拉取」这条既定决策。

⚙️ 一个已踩过的坑：models.dev 在 Cloudflare 后面，默认 `Python-urllib/3.x` UA 会吃 **HTTP 403**（curl 正常）。生成脚本里已修，实现时别改回去。

📄 合规：models.dev 为 **MIT**（`anomalyco/models.dev`），可随包分发，须保留版权与许可声明。备选源均不可用：LiteLLM 的 3213 条记录**零收录 opencode**；OpenRouter 只含自营且数据许可未声明。

裁剪后的静态价表约 **17 KB / 123 模型**（全量 4.3 MB / 7303 模型），已用 `tomllib` 验证往返解析。

草案脚本：`docs/research/gen_prices_draft.py`；详情：`docs/research/model-catalog-and-prices.md`。

⚠️ 调研票自陈的一处未验证：未持 key 复验 `zen/go/v1/models` 不含价格这一前提（沿用了建图 session 的实测结论——该结论确由无鉴权请求得到，可信，但本票实现时顺手用真 key 复验一次更稳）。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/15#issuecomment-5425951592

📌 再补一条：前置调研 #7（`docs/research/provider-protocol-diff.md`）也影响本票的**费用计算**。

1. **价格链新增第 0 档 = 网关自报实付**。xAI 的 usage 直接返回 `cost_in_usd_ticks`。这是对建图时你原话「优先采用网关实付」的具体化，地图 Notes 前提 #4 已同步。有实付就用实付，不要再走目录价。
2. **缓存 token 口径三家互不相同，照抄公式必然算错账**：
   - Anthropic：`cache_creation_input_tokens` / `cache_read_input_tokens` 与 `input_tokens` **分开计**
   - OpenAI：`prompt_tokens_details.cached_tokens` **已含在** `prompt_tokens` 内
   - DeepSeek：另用 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`

   ⇒ `usage.jsonl` 里必须记**口径标记**，否则同样的 token 数在三家会算出三种费用。这条与本票"Token 是唯一事实来源"的原则直接相关：事实要连口径一起记，否则它不是事实。
3. **不能靠「没报错」推断特性生效**（兼容层会静默忽略不认识的参数）。涉及 prompt caching 之类需要显式开启的能力时，费用页不得假定它已生效。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/15#issuecomment-5569943219

#15 已完成并合入 `main`。

关联 [PR #43](https://github.com/nineofoursyrup/Agent-Alfred/pull/43)。merge commit：`d3c7187c02ab6debead4fda2ce5641fb0446ba1c`（第一父节点 / 固定基线 `ea9e4b8e1a1581995c94bf26a893f7dc86194019`，第二父节点 / 已验收提交 `dee4398376a4d0cae058fb444e2e92ceaa232d6d`）。合并结果 tree `86590762a4cf53deb743b14fa08e2ae114915f5c` 与已验收 tree 相同；远端 `main` 当前即该 merge，包含已验收提交。

候选内容摘要：`ea6d1183b9909a7ddc58ecab6c152f6c1c2fe4edb06795b2b88060456c04ecae`。算法：对基线与已验收 HEAD 之间固定 19 路径排序；每行 `oct(mode & 0o777)<TAB>sha256(file bytes)<TAB>path`；Git `100644` 规范化为 `0o644`；实际 tab；末尾换行；再对 UTF-8 清单做 SHA-256。内容 SHA-256 不是 git blob OID。

合并后 CI（归属 merge SHA `d3c7187c…`，event=`push`，attempt 1 **success**）：https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34116088109 。这与 PR head CI https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/34112799354 不同，后者是合入前的 pull_request 检查。

本地 exact HEAD `dee43983…` 门禁（不是 GitHub CI）：非 keyed pytest **2610 passed / 1 requires_key deselected**；Dashboard browser **52 passed**；Ruff、skills/env、whitespace、Dashboard typecheck、wheel/sdist 资产一致性与隔离资源加载通过。独立 Standards / Spec 对同一 HEAD 与同一摘要均为 PASS。19 路径均纳入审查；`prices.toml` 为抽样/结构核验，不是逐行通读全表；`host.py` 为相关 hunk 与上下文，不是全文。

实现范围：模型目录接口与进程内缓存；按 `(endpoint_id, model_id)` 定价链；随包 2026-09-07 静态快照（137 模型）与 MIT NOTICE（快照不是实时价格保证）；费用 exact/estimated/unknown 闭合投影；派生 schema 纯函数。阶梯价存基础档并打 `tiered=true`，Dashboard 在 estimated 且分项含 `tiered` 时注明「按基础档估算」，已闭合。

未完成、也不应读成已完成：生产尚未调度 `list_models`（后续 #34）；无 user override store；无 CLI/HTTP 导出入口；keyed 验证未跑。没有生产目录拉取 → HTTP evidence → UI 真阶梯端到端。全部非阻断建议、范围限制与验证限制仍完整保留在 [PR #43 正文](https://github.com/nineofoursyrup/Agent-Alfred/pull/43)，未冒称修复。
