# 调研：MCP stdio 服务器接入与子进程生命周期

> 对应 issue [#9](https://github.com/nineofoursyrup/Agent-Alfred/issues/9)，是 [#21 实现：MCP 桥接](https://github.com/nineofoursyrup/Agent-Alfred/issues/21) 的前置。
> 调研日期 2026-08-26。所有"实测"结论都在本机（macOS / Python 3.12.13 / uv 0.11.7）跑过，脚本与输出见文末。

## 一句话结论

**不引官方 `mcp` 包，自己写约 150 行纯 stdlib 的 stdio 客户端进核心**；SDK 的 28 个依赖里有 21MB 是我们完全用不到的服务端 HTTP 栈和 OAuth 加密栈，而它真正值钱的部分——子进程清理策略——是可以照抄思路而不是照抄代码的。

---

## 0. 先说一个会影响全盘的背景：MCP 现在有两个"世代"

这是本次调研最容易踩坑的地方，必须先讲清楚。

| 世代 | 版本 | 握手方式 |
|---|---|---|
| **Modern** | `2026-07-28` | **没有握手**。协议版本、客户端能力、身份都塞在每个请求的 `_meta.io.modelcontextprotocol/*` 里，服务器逐请求接受或拒绝 |
| **Legacy** | `2025-11-25` 及更早（`2025-06-18` / `2025-03-26` / `2024-11-05`） | `initialize` 请求 + `notifications/initialized` 通知建立会话 |

来源：[Versioning and Compatibility](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)。

新世代把 MCP 明确定义为 **无状态协议**——"Servers **MUST NOT** rely on prior requests over the same connection to establish context"，甚至写明"an open connection, such as a STDIO process, is not a conversation or session"（[Base Protocol · Statelessness](https://modelcontextprotocol.io/specification/2026-07-28/basic)）。

**生态现状（实测）**：官方 Python SDK `mcp` 2.1.1 的版本注册表里，`MODERN_PROTOCOL_VERSIONS` 只有 `("2026-07-28",)`，而它的 `initialize` 客户端默认发的是 `LATEST_HANDSHAKE_VERSION = "2025-11-25"`（`mcp_types/version.py`）。实测拉起官方参考服务器 `@modelcontextprotocol/server-everything` v2.0.0，它协商回来的也是 `2025-11-25`。

**对 #21 的决定**：

- v1 **只实现 legacy 握手**，目标版本 `2025-11-25`，版本协商按规范——服务器回什么就用什么，不认识就断开报错。
- v1 **不实现** modern 世代（`server/discover` 探测、per-request `_meta`）。但请求构造代码里给 `params._meta` 留一个可注入的空位，将来加 modern 支持不用重构消息层。
- 这条要在 `CONTEXT.md` 或一条 ADR 里记一笔，因为它是有保质期的判断。

---

## 1. stdio 传输：帧格式与硬规则

规范原文（[stdio transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)，[2025-06-18 版本](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)措辞一致）：

- 客户端把 MCP 服务器**当子进程拉起**；服务器从 `stdin` 读 JSON-RPC 消息，往 `stdout` 写。
- 每条消息是一个独立的 JSON-RPC request / notification / response。
- **消息以换行分隔，且消息内不得含嵌入换行**（`MUST NOT contain embedded newlines`）。
- 消息 **MUST** 是 UTF-8。
- 服务器 **MAY** 往 `stderr` 写任意 UTF-8 日志。客户端 **MAY** 捕获、转发或忽略，并且 **"SHOULD NOT assume `stderr` output indicates error conditions"**。
- 服务器 **MUST NOT** 往 `stdout` 写任何非 MCP 消息；客户端 **MUST NOT** 往服务器 `stdin` 写任何非 MCP 消息。
- 客户端 **MUST NOT** 往 stdin 写 JSON-RPC *response*；服务器 **MUST NOT** 往 stdout 写 JSON-RPC *request*。

### 实现要点

1. **写侧天然安全**：Python 的 `json.dumps` 会把字符串里的换行转义成 `\n` 两个字符，永远不会产生裸换行，所以 `json.dumps(msg) + "\n"` 直接满足"消息内不得含嵌入换行"。
2. **读侧要自己缓冲**：不能假设一次 read 恰好一行。按 chunk 读、`split("\n")`、把最后一段留作 buffer（SDK `client/stdio.py` 的 `stdout_reader` 就是这么写的）。用文本模式 `readline()` 也可以，但要设 `bufsize=1` 且注意行超长。
3. **stderr 必须持续抽干**，用独立线程或 selector。否则管道缓冲区一满，服务器写 stderr 就阻塞，进而不再读 stdin，整条链路死锁。同理 stdout：SDK 在关闭流程里专门有个 `_drain_stdout()`，注释写得很直白——"Keeps a server flushing buffered output from blocking on a full pipe and missing its chance to exit"。
4. **保留 stderr 尾部**（比如最后 50 行）。服务器起不来的时候，这是唯一的诊断信息，四态里的"错误"必须能把它展示出来。
5. 解析失败的行**不要 crash**，当成一条错误值往上抛给会话层（SDK 的 `_parse_line` 就是 return 异常而不是 raise）。

---

## 2. 最小可用实现需要哪些方法

### 2.1 握手（legacy）

请求（[Lifecycle 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)）：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2025-11-25",
    "capabilities": {},
    "clientInfo": { "name": "agent-alfred", "version": "0.1.0" }
  }
}
```

`capabilities` 我们可以给空对象——我们不提供 `roots` / `sampling` / `elicitation`，就别声明。规范只要求服务器不使用未协商的能力。

响应里我们只关心三件事：`protocolVersion`（协商结果）、`capabilities.tools`（有没有工具）、`serverInfo`（`name` / `version`，给 Dashboard 显示）。`instructions` 字段可选，若有可以拼进系统提示。

握手完必须发通知（**没有 id**）：

```json
{ "jsonrpc": "2.0", "method": "notifications/initialized" }
```

版本协商规则：客户端发自己支持的最新版；服务器支持就原样回，不支持就回它自己支持的另一个版本；**客户端不支持服务器回的版本时 SHOULD 断开**。

### 2.2 列工具

```json
{ "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": { "cursor": "..." } }
```

**必须实现游标分页**：响应里有 `nextCursor` 就要继续拉，直到没有。响应中每个 tool 有 `name` / `title?` / `description` / `inputSchema`（JSON Schema，默认 2020-12 方言）/ `outputSchema?` / `annotations?`。
规范提醒：`annotations` **MUST** 被当作不可信输入，除非来自可信服务器（[Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)）。

### 2.3 调工具

```json
{
  "jsonrpc": "2.0", "id": 3, "method": "tools/call",
  "params": { "name": "get_weather", "arguments": { "location": "New York" } }
}
```

结果里 `content` 是内容块数组（`text` / `image` / `audio` / `resource_link` / `resource`），可能还有 `structuredContent`（对应 `outputSchema`）和 `isError`。
v1 只需要处理 `text`，其他类型降级成一句占位描述——**但要如实说明"该内容类型暂不支持"，不能伪造内容**。

### 2.4 JSON-RPC 层的坑

- `id` **MUST** 是 string 或 integer，**MUST NOT** 是 `null`（这一点和裸 JSON-RPC 2.0 不同）。
- `id` **MUST NOT** 与任何尚未收到响应的请求撞号。
- notification **MUST NOT** 带 `id`。
- 收到不认识的 method / 无关 id 的消息，**跳过而不是报错**（服务器可能主动推 `notifications/message` 日志、`notifications/progress` 进度）。
- 错误码：MCP 划走了 `-32020`~`-32099`。`-32022` = 协议版本不支持，`-32021` = 缺必需客户端能力，`-32020` = header 不匹配（[Base Protocol · Error Codes](https://modelcontextprotocol.io/specification/2026-07-28/basic)）。

### 2.5 两类错误，必须区分对待

规范把工具错误分成两类（[Tools · Error Handling](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)）：

| | 形态 | 例子 | 我们该怎么办 |
|---|---|---|---|
| **协议错误** | JSON-RPC `error` 对象 | 未知工具、请求格式不合法、服务器内部错 | 客户端 **MAY** 给模型；建议记 trace，**不**降级服务器状态（除非是连接层错） |
| **工具执行错误** | `result.isError == true`，内容在 `content` 里 | API 失败、参数校验失败、业务错误 | 客户端 **SHOULD** 原样回灌给模型让它自我纠正。**服务器状态仍然是"已连接"** |

这条很容易做错：`isError: true` 是**正常的协议行为**，不是连接故障。

### 2.6 超时与取消

规范 Timeouts 一节：实现 **SHOULD** 给所有请求设超时；超时后 **SHOULD** 发一条取消通知然后停止等待。

```json
{ "jsonrpc": "2.0", "method": "notifications/cancelled",
  "params": { "requestId": 3, "reason": "client timeout" } }
```

不发这条，服务器还在傻干活。stdio 是单条共享通道，没有"关掉这个请求的流"这种操作，取消通知是唯一手段（[stdio · Cancellation](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)）。

### 2.7 最小方法集清单

**必须**：`initialize` · `notifications/initialized` · `tools/list` · `tools/call` · `notifications/cancelled`（发）
**建议**：忽略 `notifications/message`、`notifications/progress`、`notifications/tools/list_changed`（v1 不做热重载，但至少不能被这些消息噎住）
**不做**：`resources/*` · `prompts/*` · `roots/*` · `sampling/*` · `elicitation/*` · `ping` · `completion/*` · `logging/setLevel`

---

## 3. 官方 Python SDK 的体积与依赖树（实测）

### 3.1 实测数据

```
$ uv venv --python 3.12 venv-mcp
$ uv pip install mcp
Resolved 28 packages in 13.12s
Installed 28 packages
 + mcp==2.1.1  + mcp-types==2.1.1
 + anyio + annotated-types + attrs + cffi + click + cryptography + h11
 + httpcore2 + httpx2 + idna + jsonschema + jsonschema-specifications
 + opentelemetry-api + pycparser + pydantic + pydantic-core + pyjwt
 + python-multipart + referencing + rpds-py + sse-starlette + starlette
 + truststore + typing-extensions + typing-inspection + uvicorn

$ du -sh venv-mcp/lib/python3.12/site-packages
 28M
$ find ... -type f | wc -l
 1008
```

`mcp` 2.1.1 的 `Requires-Dist`（全部是**无条件必需**，不是 extra）：

```
anyio, httpx2>=2.5.0, jsonschema>=4.20.0, mcp-types==2.1.1,
opentelemetry-api>=1.28.0, pydantic>=2.12.0, pyjwt[crypto]>=2.10.1,
python-multipart>=0.0.9, sse-starlette>=3.0.0, starlette>=0.27,
typing-extensions, typing-inspection, uvicorn (非 emscripten),
pywin32>=311 (win32)
```
只有两个 extra：`cli`（typer + python-dotenv）和 `rich`。

### 3.2 这 28MB 里有多少是我们要的

| 包 | 体积 | 我们跑 stdio 客户端用得上吗 |
|---|---|---|
| `cryptography` | **13 MB** | ❌ 被 `pyjwt[crypto]` 拖进来，纯为 HTTP 传输的 OAuth |
| `pydantic` + `pydantic_core` | 6.1 MB | ⚠️ 只用来做消息模型；我们用 dict + 少量校验就够 |
| `starlette` / `uvicorn` / `sse-starlette` / `python-multipart` / `httpcore2` / `httpx2` / `h11` | ~1.9 MB | ❌ 全是**服务端 / HTTP 传输**栈 |
| `jsonschema` + `referencing` + `rpds-py` + `attrs` | 1.7 MB | ⚠️ 校验 tool 的 inputSchema；v1 可以不校验（模型给的参数直接透传，让服务器自己校验） |
| `opentelemetry-api` | 0.24 MB | ❌ 地图里 OTel 明确是可选、v1 不接线 |
| `mcp` + `mcp_types` | 1.8 MB | ✅ 这才是本体 |

粗算：**28MB 里约 21MB 与 stdio 客户端毫无关系**，而且没有任何 extra 开关能去掉它们。

对照组（实测）：
- `uv pip install mcp-types` 单独装 = **6 包 / 7.1 MB**（只拖 pydantic 全家）。可以拿类型定义不拿运行时。
- 自写最小客户端 = **0 个新依赖**。

### 3.3 自写客户端的可行性（实测跑通）

本次调研写了一个 **110 行、纯 stdlib**（`json` / `os` / `signal` / `subprocess` / `threading` / `time`）的客户端，对官方参考服务器实跑：

```
$ python3 minimal_client.py npx -y @modelcontextprotocol/server-everything
initialize: {"protocolVersion": "2025-11-25", "capabilities": {"tools": {"listChanged": true}, ...},
             "serverInfo": {"name": "mcp-servers/everything", "title": "Everything Reference Server",
                            "version": "2.0.0"}, ...}
tools (13): ['echo', 'get-annotated-message', 'get-env', 'get-resource-links',
             'get-resource-reference', 'get-structured-content', 'get-sum', 'get-tiny-image',
             'gzip-file-as-resource', 'toggle-simulated-logging', 'toggle-subscriber-updates',
             'trigger-long-running-operation', 'simulate-research-query']
handshake+list took 9.58s     ← 其中约 9s 是 npx 首次下载包，与协议无关
exit code: 0                   ← 清理干净
```

握手、分页列表、优雅关闭全部走通。加上取消通知、错误分类、四态映射，落地估计 **150–200 行**。

### 3.4 推荐：不引 `mcp`，自写最小客户端

**理由**

1. **依赖极简是项目红线**。核心依赖只有模型 SDK、dotenv、终端富文本。为一个 stdio 客户端引入 starlette + uvicorn + cryptography，方向完全相反。
2. **"透明可读"**。自写的客户端是一个文件、一条同步的读写循环，任何人五分钟读完。SDK 的等价路径要穿过 `anyio` 的 task group、memory object stream、cancel scope shield、`TextReceiveStream`——功能上更强，但可读性上是反向的。
3. **SDK 的价值几乎全在我们不用的地方**：HTTP/SSE 传输、OAuth 授权、服务端框架（FastMCP）、Tasks/Subscriptions 扩展。我们只要 stdio 客户端这一小块。
4. **离线确定性测试更好写**。地图里每张施工票的默认验收项是"依赖可注入 + 同一份业务代码能被 `ScriptedModel` 驱动"。自己的 JSON-RPC 层可以直接把一对 `BytesIO` 当传输喂进去，构造任意握手/超时/崩溃场景，完全不需要真拉进程。用 SDK 的话要么去 mock anyio，要么真起进程——两条路都会让"全离线确定性测试"这道门禁变难。

**同时必须记的两条**

- **SDK 的子进程清理策略要照抄**（照抄思路，不是代码）。`mcp/client/stdio.py` 和 `mcp/os/posix/utilities.py` 是这个问题上写得最细的公开实现，第 6 节整节都建立在它之上。它的注释里有好几条别处查不到的经验，比如"Never use `getpgid()`: it fails once the leader is reaped"。
- **给未来的 optional extra 留位置**。哪天要接 remote MCP（Streamable HTTP + OAuth），再把 `mcp` 放进 `[project.optional-dependencies] mcp = ["mcp>=2.1"]`，走"可选集成注册表"那五要素。v1 不预先加。

---

## 4. 本地 MCP 配置文件的事实标准格式

没有正式规范，只有事实标准，而且各家有分歧。

### 4.1 各家实际形状

**Claude Desktop** — `~/Library/Application Support/Claude/claude_desktop_config.json`（macOS）/ `%APPDATA%\Claude\claude_desktop_config.json`（Windows）
（[连接本地 MCP 服务器](https://modelcontextprotocol.io/docs/develop/connect-local-servers)）

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem",
               "/Users/username/Desktop", "/Users/username/Downloads"]
    },
    "brave-search": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-brave-search"],
      "env": { "BRAVE_API_KEY": "..." }
    }
  }
}
```
它的日志约定也值得抄：`mcp.log` 记连接与失败，`mcp-server-<NAME>.log` 单独存该服务器的 stderr。

**Claude Code** — `.mcp.json`（项目，进版本控制）/ `~/.claude.json`（local & user scope）
（[Claude Code MCP 文档](https://code.claude.com/docs/en/mcp)）

根键同样是 `mcpServers`，stdio 项字段 `type: "stdio"` / `command` / `args` / `env`，另外：
- 支持环境变量展开：`${VAR}` 和 `${VAR:-default}`。变量没设且没默认值时**配置照样加载**，只是在列表里报一个 missing-variable 警告，并按字面 `${VAR}` 使用——这个"降级但如实告警"的处理值得抄。
- 支持 per-server `"timeout"`（毫秒），覆盖全局 `MCP_TOOL_TIMEOUT`。
- 会给子进程注入 `CLAUDE_PROJECT_DIR`。

**VS Code / Copilot** — `.vscode/mcp.json`
根键是 **`servers`**，不是 `mcpServers`：

```json
{
  "servers": {
    "playwright": { "command": "npx", "args": ["-y", "@microsoft/mcp-server-playwright"] }
  }
}
```

**Codex CLI** — TOML，表名 `mcp_servers`（下划线），没有外层包裹对象。

### 4.2 给 Agent-Alfred 的建议

最大公约数是 **根键 `mcpServers` + 每项 `{command, args, env}`**。建议：

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "${HOME}/Documents"],
      "env": { "SOME_TOKEN": "${SOME_TOKEN}" },
      "cwd": "${HOME}",
      "enabled": true,
      "timeout_ms": 60000
    }
  }
}
```

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `command` | ✅ | — | 空字符串或缺失 ⇒ 状态"未配置" |
| `args` | | `[]` | |
| `env` | | `{}` | 叠加在继承白名单之上 |
| `cwd` | | 继承 | |
| `enabled` | | `true` | `false` ⇒ 不拉起，状态"已配置未测试" |
| `timeout_ms` | | 60000 | 只覆盖 `tools/call` |

约定：

1. **同时接受 `servers` 作为 `mcpServers` 的别名**，方便用户从 VS Code 配置直接粘。两个都存在时以 `mcpServers` 为准并告警。
2. **忽略而不是拒绝未知字段**（`type` / `disabled` / `alwaysAllow` 之类别家的字段），这样从别家粘过来的配置能直接用。但如果出现 `"type": "http"` / `"url"`，要明确报"v1 只支持 stdio"，**不能装作能连**。
3. **支持 `${VAR}` / `${VAR:-default}` 展开**，作用于 `command` / `args` 每一项 / `env` 的值 / `cwd`。密钥只从环境 / `.env` 读——这跟地图里"密钥仍只从环境 / `.env` 读，界面只显示末四位"的既定前提一致。**配置文件里不应出现明文密钥**，界面上编辑这个文件时要提示。
4. **子进程环境用白名单继承**。SDK 的 POSIX 默认白名单是 `HOME LOGNAME PATH SHELL TERM USER`（Windows 是 `APPDATA HOMEDRIVE HOMEPATH LOCALAPPDATA PATH PATHEXT PROCESSOR_ARCHITECTURE SYSTEMDRIVE SYSTEMROOT TEMP USERNAME USERPROFILE`），并且会跳过以 `()` 开头的值（bash 导出函数，安全风险）。这是个好默认，建议照抄，再叠加配置里的 `env`。
5. **文件位置**：跟人格文件 / Skill 目录的落地位置一起定（地图 "Not yet specified" 里还挂着）。倾向 `~/.agent_alfred/mcp.json`，用户单机、无需进版本控制。

---

## 5. 多服务器时把工具命名空间化为 `server_tool`

### 5.1 规范怎么说

[Tools · Tool Names](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)：

- 工具名 **SHOULD** 1–128 字符。
- **SHOULD** 只用大小写 ASCII 字母、数字、下划线 `_`、连字符 `-`、点 `.`。
- **SHOULD** 大小写敏感。
- **唯一性只在单个服务器范围内保证**。

规范还专门有一条 Note：

> 聚合多服务器工具的客户端或代理 **MAY** 遇到命名冲突（例如两个服务器都有 `search`），**SHOULD** 实现消歧策略，比如给工具名加服务器标识前缀。
> **`serverInfo` 里的服务器 `name` 不保证跨服务器唯一，SHOULD NOT 拿来消歧。**

第二句很重要：**前缀必须用我们自己配置文件里的 key，不能用服务器自报的 `serverInfo.name`。**

### 5.2 真正的约束不是规范，是下游模型 API

| | 允许字符 | 最大长度 |
|---|---|---|
| MCP 规范 | `A-Za-z0-9_-.` | 128 |
| **Anthropic Messages API** | **`^[a-zA-Z0-9_-]{1,64}$`** | **64** |
| OpenAI 兼容 API | `^[a-zA-Z0-9_-]{1,64}$` | 64 |

Anthropic 的是一手来源：[Define tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools) 的参数表原文——"`name`: The name of the tool. Must match the regex `^[a-zA-Z0-9_-]{1,64}$`."
OpenAI 的官方 function-calling 指南没有明写；这个正则来自 API 的 400 报错文本，属于**二手来源**（[OpenAI 社区讨论](https://community.openai.com/t/openai-badrequesterror-error-code-400-does-not-match-a-za-z0-9-1-64/709823)），落地前值得实测一次。

**结论：实现必须按 `[A-Za-z0-9_-]` + ≤64 字符来做，不能按规范的 128 + 点号。** 规范允许的点号在 Anthropic 和 OpenAI 上都会 400。

（旁证：Claude Code 自己就踩过这个坑——[anthropics/claude-code#2485 "Tool Name Length Exceeds 64 Character Limit"](https://github.com/anthropics/claude-code/issues/2485)。）

### 5.3 建议的命名规则

```
sanitize(s)  = s 中所有 [^A-Za-z0-9_-] 替换为 "_"
alias(server, tool) = sanitize(server) + "_" + sanitize(tool)
```

**超长截断**（`len(alias) > 64` 时）——绝不能盲截，会撞名：

1. server 段截到 ≤ 16 字符
2. 保留 5 字符给后缀 `_` + 4 位 hex
3. tool 段截到 `64 - len(server段) - 1 - 5`
4. 拼上 `_` + `sha256(f"{server}\x00{tool}").hexdigest()[:4]`

长度确定、碰撞概率可忽略、且不同原名一定得到不同 alias。

**冲突处理**：sanitize 之后仍可能撞（`my-server` 和 `my_server` 都变成 `my_server`）。构建映射表时若 alias 已存在，追加 `_2` / `_3`（追加后仍要满足 ≤64），并且**必须在日志和 Dashboard Tools 页上显式报出这次改名**——静默改名会让用户找不到自己的工具。

**最关键的一条：调用永远走映射表，不解析名字。**

桥接层维护 `alias -> (server_key, original_tool_name)` 的字典。模型返回 alias，我们查表拿到原始工具名再发 `tools/call`。**绝不能**把 alias 直接当 MCP 工具名发出去，也**绝不能**靠"按第一个下划线切开"来还原——因为服务器 key 和工具名里本来就可能有下划线（实测 `server-everything` 的工具名是 `get-annotated-message` 这种带连字符的）。

> 对比：Claude Code 用 `mcp__<server>__<tool>`，双下划线做分隔符，正是为了降低歧义（[Claude Code MCP 文档](https://code.claude.com/docs/en/mcp) 里插件版形式是 `mcp__plugin_<plugin>_<server>__<tool>`，并明确说"任何 `A-Z a-z 0-9 _ -` 之外的字符替换为 `_`"）。
> 但双下划线要多吃 6 个字符的 64 预算。本项目 brief 已定 `server_tool`，那就接受"分隔符有歧义"，靠映射表兜底——反正我们从不解析。

**描述也要带上来源**：拼给模型的 `description` 前面加一句「来自 MCP 服务器 `<server_key>`」。多服务器时这能显著降低模型选错工具的概率，也符合"不伪造"的要求。

---

## 6. 子进程清理：三条路径

### 6.1 规范给的关闭序列

[stdio · Shutdown](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)：

1. 关闭子进程的输入流（stdin）
2. 等它退出
3. 合理时间内不退，用操作系统机制强制终止

POSIX 上典型是 `SIGTERM` → `SIGKILL`；Windows 上用 `TerminateProcess` 或 Job Objects。规范同时对服务器方要求：

> Servers **SHOULD** exit promptly when their standard input is closed or reads return end-of-file. **This is the primary graceful-shutdown signal and the only portable one.**

这句话决定了我们的兜底策略（见 6.4）。

### 6.2 必须做的两件事：新会话 + 按进程组杀

**`start_new_session=True`**（Python 文档：会在 exec 前调 `setsid()`）。理由有两个，都很硬：

1. **能杀干净孙子进程。** MCP 服务器几乎都是被 wrapper 拉起的——`npx -y ...`、`uv run ...`、`docker run ...`。`proc.terminate()` 只杀 wrapper，真正的服务器进程会变成孤儿留在系统里。有了独立进程组，`os.killpg(pgid, sig)` 一次覆盖整棵树。
2. **Ctrl-C 的行为变得确定。** 见 6.3。

**pgid 直接用 `process.pid`**，因为 `start_new_session` 让它成了组长。**不要用 `os.getpgid(pid)`**——SDK 的注释写得很直接：

> Never use `getpgid()`: it fails once the leader is reaped, even with live members left.

**实测验证。** 造一个"顽固服务器"：忽略 `SIGTERM`、从不读 stdin、还 fork 一个 `sleep(300)` 的孙子进程。跑完整梯度：

```
close() took 2.01s, returncode=-9      ← -9 = SIGKILL
leader still present? (gone)
group survivors: (none)                ← 孙子进程也被 killpg 带走了
```

### 6.3 三条路径

#### 路径 A — 正常退出

```
1. proc.stdin.close()                          # 规范的首选、也是唯一可移植的信号
2. proc.wait(timeout=GRACE)                    # SDK: PROCESS_TERMINATION_TIMEOUT = 2.0s
3. 超时 → os.killpg(pgid, SIGTERM)             # 注意：不是 proc.terminate()
4. proc.wait(timeout=FORCE)                    # SDK: FORCE_KILL_TIMEOUT = 2.0s
5. 还活着 → os.killpg(pgid, SIGKILL)
6. 再等 REAP 确认被回收                          # SDK: _KILL_REAP_TIMEOUT = 2.0s
7. 还不死 → 记 warning 放弃，不要无限等
```

`terminate()` / `kill()` 的语义（Python 文档原文）：terminate 在 POSIX 上发 `SIGTERM`、Windows 上调 `TerminateProcess()`；kill 在 POSIX 上发 `SIGKILL`、Windows 上是 terminate 的别名。

两个容易被忽略的细节：

- **等待要轮询 `returncode` 而不是 `proc.wait()`**（如果用 asyncio）。SDK 的 `_wait_for_process_exit` 注释解释了原因：asyncio 3.11+ 的 `wait()` 还会等管道 EOF，而一个继承了管道的孙子进程会让**已经退出的**服务器看起来像卡住了。同步 `subprocess` 没这个问题，但如果 #21 走 asyncio 就必须注意。
- **别用 `wait()` 干等还挂着 `stdout=PIPE`**。Python 文档明确警告：子进程输出足够多、管道缓冲区满时会死锁。所以关闭流程里也要继续抽 stdout（SDK 的 `_drain_stdout`）。

#### 路径 B — Ctrl-C（SIGINT）

先看 POSIX 的定义（[Open Group General Terminal Interface](https://pubs.opengroup.org/onlinepubs/9699919799/basedefs/V1_chap11.html)）：

> INTR 字符 "Generates a SIGINT signal which is sent to **all processes in the foreground process group** for which the terminal is the controlling terminal."

这意味着**是否用 `start_new_session` 决定了两种完全不同的世界**：

| | 服务器收到 Ctrl-C 吗 | 后果 |
|---|---|---|
| 不用 `start_new_session` | 收到 | 服务器可能在我们还没走完关闭流程时就自己死了/半死，退出码乱七八糟，状态难判定 |
| 用 `start_new_session` | **收不到** | 服务器在独立组里，Ctrl-C 只打到我们。清理责任 100% 在我们身上——但**行为是确定的** |

**选后者。** 确定性比省事重要，而且我们本来就为了杀孙子进程要用它。

配套要求：

1. Python 默认把 SIGINT 变成 `KeyboardInterrupt`，所以只要清理逻辑在 `try/finally` 或 `contextlib.ExitStack` 里就会执行。
2. **清理过程必须扛得住第二次 Ctrl-C。** 用户不耐烦会连按。SDK 的做法是把整个 shutdown 塞进 `anyio.CancelScope(shield=True)`。stdlib 版的等价做法：进 shutdown 前 `signal.signal(SIGINT, signal.SIG_IGN)`，出来再恢复；或者做个计数器——第二次 Ctrl-C 直接跳过梯度上 `SIGKILL`。**不要**让第二次 Ctrl-C 把清理打断在中间，那正好留下僵尸。
3. 多个服务器要**并行**关，不要串行。串行时 N 个服务器最坏要等 N × 6 秒。

#### 路径 C — 异常崩溃

`atexit` **不是**可靠防线。Python 文档原文：

> The functions registered via this module are not called when the program is killed by a signal not handled by Python, when a Python fatal internal error is detected, or when `os._exit()` is called.

所以要分四层：

| 层 | 手段 | 覆盖 |
|---|---|---|
| 1 | `try/finally` / `ExitStack` / 上下文管理器 | 普通异常、正常返回 |
| 2 | `atexit.register` | `sys.exit()`、主流程被绕开 |
| 3 | `signal.signal(SIGTERM, ...)` 转成正常退出路径 | 被 `kill` / 系统关机。**Python 默认对 SIGTERM 直接终止且不跑 atexit**，不装 handler 这条就漏了 |
| 4 | **让服务器自己发现孤儿** | 我们被 `SIGKILL` / OOM kill —— 任何 Python 代码都跑不了 |

第 4 层是唯一真正的兜底，而且**它对我们有硬性要求**：

- 必须 `stdin=subprocess.PIPE`，并且**我们持有唯一的写端**。
- 我们的进程一死，内核关掉管道写端 → 服务器 `stdin` 读到 EOF → 按规范它 SHOULD 立刻退出。
- 所以**绝不能**把服务器的 stdin 设成继承（`stdin=None`），那会让它挂在我们的终端上，我们死了它也毫无察觉。

**顺手拿一条测试断言**：Python 3.6 起，`Popen` 对象被 GC 而子进程还在跑会发 `ResourceWarning`。在离线确定性测试里把 warning 打成 error（`-W error::ResourceWarning`），就得到一个零成本的进程泄漏检测器。建议直接写进 #21 的验收项。

### 6.4 建议的超时值

对齐 SDK 常量，全部可配置：

| 阶段 | 默认 | 出处 |
|---|---|---|
| spawn → `initialize` 响应 | 10s | 自定（SDK 未定，Claude Code 走 `timeout` 字段） |
| `tools/list` | 10s | 自定 |
| `tools/call` | 60s，可被 per-server `timeout_ms` 覆盖 | 对齐 Claude Code 的 `timeout` 字段语义 |
| stdin close → 放弃等待 | 2.0s | SDK `PROCESS_TERMINATION_TIMEOUT` |
| SIGTERM → SIGKILL | 2.0s | SDK `FORCE_KILL_TIMEOUT` |
| SIGKILL → 确认回收 | 2.0s | SDK `_KILL_REAP_TIMEOUT` |

---

## 7. 启动失败 / 握手超时如何映射到四态

四态：`未配置` / `已配置未测试` / `已连接` / `错误`。
地图红线：不得用静态假数据冒充已连接或成功；**「功能尚未实现」不是这四态之一**。

| 情形 | 状态 | 界面/日志显示 |
|---|---|---|
| `mcpServers` 里没有这一项 | **未配置** | — |
| 有 key 但 `command` 为空或缺失 | **未配置** | 「未配置启动命令」 |
| `enabled: false` | **已配置未测试** | 「已禁用」 |
| 配置完整，本次会话还没拉起过（懒启动 / 刚保存配置） | **已配置未测试** | 「已配置，未连接」+「测试连接」按钮 |
| 进程起来了 + `initialize` 成功 + `tools/list` 返回过 | **已连接** | 工具数 · `serverInfo.name`/`version` · 协商到的 `protocolVersion` |
| spawn 失败（`FileNotFoundError` / `PermissionError`） | **错误** | `命令未找到: npx` / `无执行权限: ./server` |
| 进程起来了但 `initialize` 超时 | **错误** | `握手超时（10s）` + stderr 尾部 |
| `initialize` 返回 JSON-RPC error | **错误** | 服务器给的 `error.message`（+ `error.code`） |
| 版本协商失败：服务器回一个我们不支持的 `protocolVersion` | **错误** | `协议版本不兼容：服务器 2026-07-28，本地支持 2025-11-25` |
| 服务器没声明 `capabilities.tools` | **已连接**，工具数 0 | 「已连接，该服务器不提供工具」 |
| 进程握手后中途退出 / stdout 读到 EOF | **错误** | `服务器已退出（code=…）` + stderr 尾部 |
| 服务器 stdout 吐出非法 JSON | **错误** | `协议错误：无法解析服务器输出` + 原始行片段 |
| `${VAR}` 展开时变量缺失且无默认值 | **错误** | `环境变量未设置：BRAVE_API_KEY`（照 Claude Code：仍加载配置，但如实告警） |
| 配置写了 `"type": "http"` / `url` | **错误** | `v1 仅支持 stdio 传输` |
| `tools/call` 返回 `isError: true` | **仍是「已连接」** | 不改服务器状态；结果回灌给模型 |
| `tools/call` 超时 | **仍是「已连接」** | 发 `notifications/cancelled`；本次调用记为失败，服务器状态不变 |
| 服务器往 stderr 写了很多东西 | **不影响状态** | 只作诊断展示 |

### 四条容易做错的判定规则

1. **`isError: true` 不降级连接状态。** 规范把它定义成给模型自我纠正用的反馈，是正常协议行为。
2. **stderr 有输出 ≠ 出错。** 规范原文："clients ... **SHOULD NOT** assume `stderr` output indicates error conditions"。很多服务器把全部日志都写 stderr。stderr 只进诊断面板，不参与状态判定。
3. **状态是观测出来的，不是从配置推断的。** 没成功连过就是「已配置未测试」。配置看起来完全正确也**不能**显示「已连接」——这正是红线针对的情形。
4. **「错误」必须带可操作的原因**，而不只是一个红点。三样东西：一句人话的分类（命令未找到 / 握手超时 / 协议错误 / 进程退出）、原始错误文本、stderr 尾部若干行。

对照 Claude Code 的状态词表（`✔ Connected` / `✘ Failed to connect` / `⏸ Pending approval` / `not configured`），它也是把 `not configured` 单列的，并且会把失败细节附在状态行后面——同时明确**对敏感字段做脱敏、不回显展开后的 URL**。我们展示 stderr 时也要过一遍脱敏（`env` 里配的值不回显）。

---

## 8. 给 #21 的落地清单

**做**

1. `McpStdioClient`：纯 stdlib，`start_new_session=True`，stderr 独立线程抽干并保留尾部 50 行，请求超时后发 `notifications/cancelled`。
2. 消息层与传输层分离——传输层只认"一对读写流"，测试里塞 `BytesIO` 就能全离线跑完握手、超时、崩溃、非法 JSON 四类场景。
3. 关闭梯度：`stdin.close()` → 2s → `killpg(SIGTERM)` → 2s → `killpg(SIGKILL)` → 2s 确认。pgid 用 `proc.pid`。shutdown 期间屏蔽/计数 SIGINT。多服务器并行关。
4. 四层清理防线：`ExitStack` + `atexit` + `SIGTERM` handler + 依赖 stdin EOF 兜底。
5. `McpRegistry`：读配置（`mcpServers`，兼容 `servers` 别名）、`${VAR}` 展开、白名单环境继承、alias 映射表、四态。
6. 工具名 alias：`sanitize(server) + "_" + sanitize(tool)`，`[A-Za-z0-9_-]`，≤64，超长按 5.3 的规则带 hash 截断，冲突显式告警。**调用只走映射表。**
7. 验收项：离线测试用 `-W error::ResourceWarning` 跑，进程泄漏直接失败。

**不做（v1）**

- modern 世代（`2026-07-28`）与 `server/discover` 探测
- HTTP / SSE / WebSocket 传输，OAuth
- `resources/*` `prompts/*` `roots/*` `sampling/*` `elicitation/*` `ping` `completion/*`
- `tools/list_changed` 热重载、tools 列表缓存
- 依赖 `mcp` 包（连 optional extra 也先不加）

**留给决策的问题**

1. **懒启动 vs 启动即连？** 懒启动（配置进来先是「已配置未测试」，首次调用才拉进程）省启动时间，但首次调用要吃握手延迟。倾向懒启动 + Dashboard 上一个显式的「测试连接」按钮，正好把「已配置未测试」这个状态用活。
2. **同步阻塞 vs asyncio？** 核心不用框架，但 Dashboard 是 HTTP+SSE。如果 Dashboard 走 async，桥接层要么整体 async（就要处理 6.3 里 asyncio `wait()` 的坑），要么放线程池。需要跟 Observer / Dashboard 骨架票对齐。
3. **配置文件落在哪？** 跟人格文件 / Skill 目录的位置一起定（地图 "Not yet specified" 里挂着）。
4. **OpenAI 兼容 API 的 64 字符限制要实测确认一次**（当前是二手来源）。首个 provider 是 OpenCode Go，走 OpenAI 兼容风格，正好顺手验。

---

## 来源

**MCP 规范（一手）**

- [stdio 传输（2026-07-28）](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio) — 帧格式、关闭序列、向后兼容探测
- [传输总览（2025-06-18）](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports) — stdio 规则的早期措辞
- [Lifecycle（2025-11-25）](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle) — `initialize` 握手、版本协商、关闭、超时
- [Versioning and Compatibility（2026-07-28）](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning) — modern / legacy 两个世代与兼容矩阵
- [Base Protocol（2026-07-28）](https://modelcontextprotocol.io/specification/2026-07-28/basic) — JSON-RPC 约束、错误码分区、无状态性、`_meta`
- [Tools（2026-07-28）](https://modelcontextprotocol.io/specification/2026-07-28/server/tools) — `tools/list` / `tools/call` 形状、工具名规则、两类错误
- [连接本地 MCP 服务器](https://modelcontextprotocol.io/docs/develop/connect-local-servers) — Claude Desktop 的 `mcpServers` 与日志布局

**官方 Python SDK（一手，本机 `mcp` 2.1.1 实读）**

- `mcp/client/stdio.py` — 关闭序列、超时常量、`_drain_stdout`、`_wait_for_process_exit`、环境白名单
- `mcp/os/posix/utilities.py` — `terminate_posix_process_tree`，killpg 策略与 `getpgid` 的坑
- `mcp_types/version.py` — `KNOWN_PROTOCOL_VERSIONS` / `HANDSHAKE_PROTOCOL_VERSIONS` / `MODERN_PROTOCOL_VERSIONS`
- `mcp-2.1.1.dist-info/METADATA` — `Requires-Dist` 全量与仅有的两个 extra
- 仓库：https://github.com/modelcontextprotocol/python-sdk

**模型 API 的工具名约束**

- [Anthropic · Define tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools) —（一手）`^[a-zA-Z0-9_-]{1,64}$`
- [OpenAI 社区：400 报错正则](https://community.openai.com/t/openai-badrequesterror-error-code-400-does-not-match-a-za-z0-9-1-64/709823) —（二手，待实测）
- [anthropics/claude-code#2485](https://github.com/anthropics/claude-code/issues/2485) — 64 字符限制的真实踩坑

**配置格式**

- [Claude Code · MCP](https://code.claude.com/docs/en/mcp) — `.mcp.json` / `~/.claude.json`、三种 scope 与优先级、`${VAR:-default}` 展开、`timeout`、`mcp__<server>__<tool>` 命名、服务器状态词表与脱敏
- [VS Code · MCP servers](https://code.visualstudio.com/docs/copilot/customization/mcp-servers) — 根键是 `servers`

**Python / POSIX（一手）**

- [`subprocess`](https://docs.python.org/3/library/subprocess.html) — `terminate`/`kill` 语义、`start_new_session`、`process_group`、`ResourceWarning`、`wait()` 管道死锁警告
- [`atexit`](https://docs.python.org/3/library/atexit.html) — 三种不执行的情形
- [Open Group · General Terminal Interface](https://pubs.opengroup.org/onlinepubs/9699919799/basedefs/V1_chap11.html) — INTR 把 SIGINT 发给整个前台进程组

**本次实测**

- `uv pip install mcp` → 28 包 / 28 MB / 1008 文件；`uv pip install mcp-types` → 6 包 / 7.1 MB
- 110 行纯 stdlib 客户端 vs `npx -y @modelcontextprotocol/server-everything` v2.0.0：协商到 `2025-11-25`，列出 13 个工具，退出码 0
- 顽固服务器（忽略 SIGTERM + 带孙子进程）走完 killpg 梯度：2.01s，returncode `-9`，整组无残留
