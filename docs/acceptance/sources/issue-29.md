# #29 决定：Models 与 Connections 设置页（唯一模型入口）

来源：https://github.com/nineofoursyrup/Agent-Alfred/issues/29

## Question

**Models 与 Connections 两页**：v1 **唯一**的模型选择入口（地图前提 #2 写死——不是 `.env`、不是 CLI 向导）。
它是里程碑④的前置：没有它，④a 之后的每一片都没有可选的模型。

- **两页的分界**：Connections 管 `ModelEndpoint`（`endpoint_id` / `base_url` / `auth_ref`）
  与密钥；Models 管目录、钉选、主模型与检索门小模型。分界线画在哪。
- **连接四态 × 支持三态两个正交维度如何分开渲染**（地图前提 #1）。
  ⚠️ 红线：`未配置 / 已配置未测试 / 已连接 / 错误` 四态**只描述连通性与认证**；
  「功能尚未实现」**不是**其中之一，属支持三态。两维混用即违红线。
- **在线目录拉取**（`catalog_url` 或 `{base_url}/models`，10 s，进程内缓存成功 5 min /
  失败约 1 min）**失败时的退回路径与错误呈现**——拉取失败**不得影响聊天**。
  唯一持久化的本地文件只存**用户钉选的模型**；目录与价格缓存重启即失。
- `unsupported` 须带 `unsupported_wire_style: responses` 如实标注（不静默隐藏、
  不伪装成连接错误）；`unknown`（默认态）须**用户手选 style 后方可使用**。
- 密钥只从环境 / `.env` 读，界面**只显示末四位**。
- 主模型由用户指定；**检索门小模型不指定时回落主模型**。
- 所有写入必须过 **MutationGate**（[ADR-0016](https://github.com/nineofoursyrup/Agent-Alfred/blob/main/docs/adr/0016-concurrent-mutation-returns-409.md)），
  且「改模型」落在哪一档 `reload_scope`——注意该属性是**字段粒度**不是集成粒度（调研 #10）。

### 验收

- 四态与三态各自的渲染用例，含二者矛盾组合（端点已连接 + 模型 unsupported）。
- 目录拉取失败时聊天照常可用，且错误原因如实呈现。
- 无密钥时**不伪造**「已连接」。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/29#issuecomment-5437791122

## 决议

### 1. 两页的分界：Connections 只读

Connections 只展示**随包内置**的 `ModelEndpoint`（`endpoint_id` / `base_url` / `catalog_url` / `api_key_env`）、密钥状态、连接四态与两个探测动作。**不增、不删、不改**。Models 独占模型的选择与用法。

「加一家供应商 = 配置表加一行」描述的是**开发者的扩展机制**，不等于用户可编辑数据。让用户填任意 URL 会一次性引入 SSRF 面、端点身份与唯一性、被引用时的删除语义、认证变量名校验四组新决定，且会撞上「唯一持久化文件只保存钉选模型」这条硬约束。自定义端点因此**出 v1 范围**（见 §14）。

### 2. 钉选是持久化记录，不是收藏夹

键 **`(endpoint_id, model_id)`**。设置文件 = `schema_version` + `revision` + 钉选记录 + 顶层 `assignments`。

钉选记录**只存用户事实**：`wire_style_override`、`price_override`、可选显示名覆盖。**不复制**目录里会过期的元数据（上下文长度、目录价）——本地存一份永不更新却看起来同样权威的假事实，比不存更坏。

不变式三条：`primary` 必须引用一条钉选记录；`retrieval_gate` 为空即指向 `primary`，非空时指向的也必须已钉选；**目录拉取失败绝不改变指派**。「退回」只意味着 Models 页改用钉选记录 + 内置候选继续呈现。

于是「目录拉不到照样能聊天」是**结构上的结果**，而不是一条特判——能聊的模型天然全在本地。

### 3. 连接四态是观测，不持久化

进程内、不落盘。启动时有密钥即 `已配置未测试`，无密钥 `未配置`。**页面加载不探测**。每次取值随附 `checked_at`、`checked_via`、机器可读原因。密钥或端点配置一变，立即打回 `已配置未测试`。存下「已连接」等于凭旧记录声称此刻连得上，是红线。

**目录健康是独立一维**：`catalog_url` 可以不是推理端点，目录 200 只证明目录服务可达，证明不了模型调用可用。

### 4. 密钥

缺失**或**去空白后为空串 → `未配置`（空串拿去发请求只会换回一个假的「错误」态，把配置问题伪装成连接问题）。长度 ≥ 8 才显示末四位，短密钥全掩码，**前端永远收不到完整值**。界面永不提供密钥输入框。

有效来源 = **进程环境优先于启动时解析的 `.env` 路径**。可经 MutationGate 重读**同一份** `.env`，**不得重新搜索路径、不得覆盖进程环境**；进程环境变更只能重启生效。重读后须使相关连接观测与目录缓存失效。

### 5. 声明不是验证（→ ADR-0021）

用户手选 style 后，支持三态**仍是 `unknown`**，以 `wire_style_source = user_declared` 呈现「未验证，按你选择的形状尝试」。`support_basis` 只记系统依据（`builtin_table` / `probe_evidence`）。

```
assignable = pinned && (support == supported
                        || (support == unknown && wire_style_override ∈ {anthropic, openai}))
```

**连接观测不参与指派。** 四态只放端点组头，三态只放模型行，禁用原因按维度给机器码。矛盾组合（端点已连接 + 模型 unsupported）两处各说各的，不合成一句话。

### 6. 候选合并与目录呈现

按 `(endpoint_id, model_id)` 合并，保存 **`sources` 集合**而非单个 `source`——一条模型可以同时来自多处。钉选覆盖用户字段，`catalog` 提供动态元数据，`builtin` 兜底。

目录四态 **`unfetched / fresh / stale / unavailable`**。过期刷新失败时保留最后成功快照，并**同时**展示 `last_success_at`、最新失败原因与重试时刻。

stale 目录价仍可作 `estimated`，须携带 `catalog_fetched_at` 与 `stale=true`。理由不是「价格很少变」，而是**带时间与降级标记的旧观测，通常比更旧的静态表或「未知」更有信息**。

### 7. reload_scope：所有字段都在下一次获准的 Run 生效

`assignments.primary` / `retrieval_gate` / `wire_style_override` / `price_override` / 显示名 / 钉选与取消钉选 / `.env` 重读——**全部无需重启 `RuntimeHost`**。

结构：`RuntimeHost` 长期持有可注入的 **`ModelClientFactory`** 与**按不可变配置版本管理的传输池**；Run 准入时一次性捕获 `assignments` + style + 凭据快照并取得客户端。设置 mutation 因 ADR-0016 不可能与 Run 并发。密钥轮换使该端点的连接观测、目录缓存与**旧客户端缓存**失效。

这**顶掉**调研 [#10](https://github.com/nineofoursyrup/Agent-Alfred/issues/10) §9 举例里的「模型选型 → 重建 Agent」：那条的前提是值被烘焙进 Agent 构造期，而烘不烘焙是我们自己选的。但调研那条「值只在请求发出时读」**不可照搬到模型侧**——SDK 客户端在构造时就捕获了密钥。

取消钉选**当前被指派**的模型 → 拒绝，要求先改指派。

### 8. 失败处置：不自动修，不自动换模型

**启动期**读到语法错误、引用破坏或**更新版本写下的 schema** → 保留原文件，进入 `settings_invalid` / `settings_schema_newer`，**禁止聊天也禁止写回**（用旧 schema 写回会静静抹掉新字段）。

**运行期**发现磁盘版本被外部改变 → 拒绝本次 mutation，继续使用内存中的 last-known-good 快照，报 `settings_conflict`。

密钥被移除、或模型被判 `unsupported`：这**不是**不变式破坏，只是运行事实。**保留指派**，下一次发送分别明确失败为 `endpoint_unconfigured` / `model_unsupported`。悄悄换模型意味着用户以为在用 A、账却记在 B 上。

### 9. 词汇肃清

`(provider_id, model_id)` → **`(endpoint_id, model_id)`**；`provider_reported` → **`endpoint_reported`**；`provider_static` → **`endpoint_static`**。地图前提、`CONTEXT.md`、价格来源枚举与 [#15](https://github.com/nineofoursyrup/Agent-Alfred/issues/15) 一次性统一。依据：调研 [#8](https://github.com/nineofoursyrup/Agent-Alfred/issues/8) 实测同一主体不同端点不同价，`provider` 心智模型已被证伪。

### 10. 两个探针

**凭据探针**：只对配置表**声明**了「需认证且无副作用」端点的行可用，该声明必须冻结成可执行契约——`method` / URL 或 path / 成功状态码 / 超时 / 错误映射**五项齐全**。按钮名「验证凭据」，`checked_via=auth_probe`，**必须绕过目录缓存重新发请求**。没有声明就明确显示「无免费认证探针」，**不伪造结果**：`{base_url}/models` 并非每个端点都有，有些目录还允许匿名读，200 什么也证明不了。

**推理探针**：挂在 **Models 页的具体模型行**上（两个指派位可能同属一个端点，「该端点已指派的模型」并不唯一），只能用该端点**已钉选且已指派**的模型，没有就禁用——**不猜候选**。明示可能计费，走全局准入，是一次独立 Run：记正常 Attempt / usage / trace 与 Run 级 `purpose=inference_probe`，但**不写会话消息**；凭据快照只在内存，不进事件、不持久化。`checked_via=inference_probe`。

真实业务往返记 `checked_via=real_run`。

目录拉取**永不**更新连接四态，哪怕请求与凭据探针逐字相同——按**动作语义**分离，比按 URL 是否偶然相等去推断，可预测得多。

### 11. 目录拉取时机：刻意的不对称

打开 Models **只自动拉当前指派端点**的目录；其余端点在展开分组或被选中时懒加载。**绝不能打开一页就并发请求全部内置端点。** 无密钥时不发请求，直接用 pinned + builtin 呈现。

端点级「刷新目录」绕过 5 min / 1 min 缓存，但须**合并同端点的并发请求**，进行中禁用按钮，且仍不影响连接四态。

连接探测仍然**不自动**。不对称的理由是失败代价不同：目录拉取失败的代价是一行 `unavailable`，推理探测的代价是真金白银与限流。

### 12. 设置写入的双重冲突判据（→ ADR-0022）

浏览器 mutation 必须携带 **`expected_revision`**，与 `RuntimeHost` 内存中的 `revision` 比对（挡陈旧标签页）；`ModelSettingsStore` 另将加载时的 `{exists, sha256(raw_bytes)}` 与写前磁盘状态比对（发现进程外改动）。任一不符 → `settings_conflict`，分因 `stale_revision` / `external_change`，**一个字节不写**。

`revision` 每次成功写入单调加一，供界面与诊断；摘要只在内部使用。

落盘顺序：同目录独占创建临时文件 → 强制 `0600` → 写入并 `fsync` → 再次缩小窗口地核对目标 → 原子 `rename` → `fsync` 父目录 → **而后**才发布新的内存快照与摘要。

`RuntimeHost` 独占 `ModelSettingsStore`，一切 mutation 先过 MutationGate。

**明确记下的局限**：摘要比较只对不守本系统锁的外部编辑提供**乐观**检测，消灭不了「最终核对与 rename 之间」的文件系统竞态。设置文件**不是受支持的人工编辑接口**，唯一的模型入口仍是设置页。

### 13. 价格覆盖是逐维的，来源是分项的

`price_override` 四维（uncached input / `cache_read` / `cache_write` / output）**全部可缺省**：**缺省 = 继续沿价格链向下查；显式 `0` = 用户声明该维免费。** 值为**非负十进制定点数，单位固定 USD / 百万 Token**。

不采用「四维必填的完整包」：绝大多数人只知道 prompt / completion 两个数，强制填四维等于逼他们替缓存价瞎编——那正是本项目废除「未知 Provider 默认 $3/$15」时判定过的那类有害数字。

**不把标量 `price_source` 原地改成类型不稳定的「向量」**，而是新建 **`price_components`** 映射：每个实际参与计费的维度各带 `tokens` / `unit_price` / `source` / `amount`，以及来源元数据（`stale`、目录快照时刻、阶梯价标记）。顶层的「同档 / 混合」只是**渲染派生值**。

费用状态按**闭合判别式**算，不叫「取最弱维」：

1. 有效 `endpoint_reported_cost` 存在 → **`exact`**，且**不再生成任何估算分项**（两套数字并列必然互相矛盾）；
2. 否则任一必要 Token 为 `None`，或某维 Token > 0 却找不到该维价格 → 整笔 **`unknown`**，不输出任何金额；
3. 其余 → **`estimated`**。

Token 为 0 的维度不要求价格，但适配器**只有能从协议语义证明未发生该类用量时**才可写 0，**不得把「未报告」改写成 0**。

`usage.jsonl` 派生快照除来源外还必须保存**本次实际采用的逐维单价**与计算时刻——否则目录价一变，历史金额再也无法复核。

（也否决了「保持标量 + 取最弱档」：它引入一套与价格链**优先级不同构**的隐性可信度排序，`user_override` 优先级在 `catalog` 之上、可信度却未必。）

### 14. 自定义端点：v1 出范围

六个里程碑均不依赖它；任意 URL、SSRF、无密钥认证、端点唯一性与引用删除语义足以自成一条独立纵向切片。「本地优先」承诺的是**状态本地**，并不承诺 v1 本地推理。未来版本可重新立票。

---

## 验收（对应本票原文三条）

- **四态 × 三态各自的渲染用例，含矛盾组合**：端点 `已连接` + 模型 `unsupported` → 组头显示已连接（带 `checked_at` / `checked_via`），模型行显示 `unsupported` + `unsupported_wire_style: responses`，指派控件禁用且原因码指向**支持维**。反向用例：端点 `未配置` + 模型 `supported` → **仍可指派**，发送时失败为 `endpoint_unconfigured`。
- **目录拉取失败时聊天照常可用**：断掉 `catalog_url`，目录健康转 `unavailable`（带 `last_success_at` + 原因 + 重试时刻），Models 页仍列出 pinned + builtin，指派不变，发消息成功。
- **无密钥时不伪造「已连接」**：移除环境变量 → `未配置`，两个探针按钮各自禁用或明示「无免费认证探针」，且**不发任何请求**。
- 追加：`unknown` + `user_declared` 的模型在界面上不得出现「支持」字样；`settings_conflict` 的两个分因各有一个测试；四维 `price_override` 缺省与显式 `0` 的行为差异有测试。

## 产出

- `CONTEXT.md`：新增 `wire_style_source` / `连接观测` / `凭据探针与推理探针` / `support_basis` / `钉选模型` / `模型指派` / `可指派` / `目录健康` / `price_components`；修订 `ModelEndpoint`（内置只读）、`连接四态`、`支持三态`、`price_source`、`费用三态`（改闭合判别式）、`派生导出`。
- **ADR-0021**（模型指派与证据语义：连接观测不参与指派，用户声明不是系统验证）、**ADR-0022**（设置写入用双重冲突判据）。
- 回写修订：[#13](https://github.com/nineofoursyrup/Agent-Alfred/issues/13)（`ModelClientFactory` + Run 级 `purpose`）、[#14](https://github.com/nineofoursyrup/Agent-Alfred/issues/14)（键改名 + `auth_probe` 契约 + 两个字段分名 + 翻转规则）、[#15](https://github.com/nineofoursyrup/Agent-Alfred/issues/15)（三处改名 + 四维覆盖 + `price_components` + 闭合判别式 + stale 价 + 导出 schema）、地图前提 1 与 4。


## Comment https://github.com/nineofoursyrup/Agent-Alfred/issues/29#issuecomment-5437792208

决议已记于上方评论；`CONTEXT.md` 与 ADR-0021 / ADR-0022 同步落地。
