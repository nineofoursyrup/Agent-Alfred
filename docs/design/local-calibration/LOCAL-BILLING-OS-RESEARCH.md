# 本地真实校准：供应商计费与 macOS 隔离研究

**DRAFT / NOT APPROVED — 2026-09-28。** 本文件提供合同草案所需的一手资料与工程推论，不构成新的信任边界批准、运行许可、阈值批准或验收 PASS。仅访问公开文档并读取本机 `sandbox-exec` 手册；未读取账户、凭据或 Keychain，未检查本机实际权限，未修改系统/网络，未安装、部署或调用产品/judge API。

## 1. 建议与证据边界

推荐机主为可信方的本地结构：**可信 broker / DecisionSource / 预算与审计存储 + 无网络 entitlement 的原生 App Sandbox runner**。runner 使用捆绑且绑定候选的 Python 与既有 Agent-Alfred 实现，通过匿名管道/限定 FD IPC 申请已批准的操作。无需远端审批服务、三个云账户、新增管理员或默认 VM。下面的 Apple 资料支持这个方向；尚未证明本仓库 Python 依赖在该边界内可运行，须先做离线可行性及负向权限验收。

USD25 继续作为统一模型金额目标。**本次公开资料研究未取得供应商侧、账户适用且可执行的 USD25 账单硬上限证据。** 可以设计本地支出授权与请求封顶，但不能将它改称已证明的供应商账单硬上限。下文的 USD16.86841344 只是带明确前提的上界演算，不是账单保证。

## 2. 模型身份、单价与 token 语义

主代理已从 #98 R04 核验：产品/辅助请求名 `deepseek-flash`，judge/reviewer 请求名 `deepseek-v4-pro`；`POST https://api.deepseek.com/chat/completions`，`thinking disabled`。本研究沿用这些字符串，不做模型替换。

公开官网当前映射如下。该映射没有实测本账户，亦不证明提供不可变模型版本锁定：

| 请求名 | 官网公布的当前服务版本 | 合同影响 |
|---|---|---|
| `deepseek-flash` | `DeepSeek-V4.1-Flash` | 精确保留已选请求名，另存公开版本映射与未来回包身份 |
| `deepseek-v4-pro` | `DeepSeek-V4-Pro-0813` | 同上 |
| `deepseek-v4-flash` | 旧名仍接受，原对应模型已退役，转由 V4.1-Flash 服务 | 不能把已选 `deepseek-flash` “归一化”为此旧名 |

下列均为 USD / 1M tokens；运行时应重新固定适用价表。官网保留调价权，优惠/缓存命中不能作为保守上界前提。[DeepSeek Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/)

| 模型 | 缓存命中输入：低谷 / 峰值 | 未命中输入：低谷 / 峰值 | 输出：低谷 / 峰值 |
|---|---:|---:|---:|
| Flash | 0.003 / 0.006 | 0.15 / 0.30 | 0.60 / 1.20 |
| Pro | 0.022 / 0.044 | 0.66 / 1.32 | 1.98 / 3.96 |

Chat Completions 的 `max_tokens` 为生成 token 上限，当前合法值为 1–393216；输入加生成受 context 限制。不要依赖服务默认输出上限。回包有 `model`、`system_fingerprint`、prompt/cache/completion usage；这些是服务声明，不能升级为不可伪造版本证明。[Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)

字符到 token 的换算只是近似；官方提供离线 tokenizer，但实际 token 以回包 usage 为准。因此，计费上界需要与当前服务 tokenizer、完整消息/工具/协议开销相匹配的可验证上界；只统计业务文本、使用平均比例或旧 tokenizer 不能证明。[Token & Token Usage](https://api-docs.deepseek.com/quick_start/token_usage/)

## 3. USD25 可证明到什么程度

本轮沿用主代理核验的 #98 R04 限额：

- 全流程请求 ≤576；Flash ≤480、Pro ≤96；并发 1。
- 完整输入 payload：Flash ≤20000 tokens，Pro ≤24000 tokens。
- 每请求输出：Flash ≤8192 tokens，Pro ≤16384 tokens。
- 全流程 ≤10800 秒、每 Attempt ≤120 秒、每 Run ≤900 秒。

若 **完整可计费输入和输出确被上述限额约束、全部重试与诊断/复核均在同一请求池、供应商按所列峰值单价计费、没有其他费用或追补**，则按所有输入均未命中缓存计算：

```text
Flash = 480 × (20000 × 0.30 + 8192 × 1.20) / 1,000,000
      = USD 7.598592
Pro   =  96 × (24000 × 1.32 + 16384 × 3.96) / 1,000,000
      = USD 9.26982144
合计  = USD 16.86841344；距 USD25 余量 = USD 8.13158656
```

这是使用官方价表与已批额度所做的离线 `Decimal` 演算。它说明目标有数值余量，**不证明所列前提已满足，也不抵消下表证据空白**。

| 事项 | 公开一手资料能证明的内容 | 仍缺少的强保证证据 |
|---|---|---|
| 余额、预充值 | 费用从充值/赠送余额扣除，赠送余额优先；公开 FAQ 说明充值入口 | 仅充值 USD25 不证明绝不出现超过 USD25 的收费责任；账户其它使用也需归属 |
| `GET /user/balance` | `is_available` 与 CNY/USD、总余额/赠送/充值余额 | 只是余额读数；无原子预留、该批次总额上限或在途最大扣款承诺 |
| HTTP 402 | 文档解释为余额不足 | 未说明检查与最终扣款的原子性、透支/负余额、何时截断已开始的请求 |
| 自动充值/授信 | 本次查询的公开定价、余额、错误与 FAQ 页未取得适用承诺 | 是否存在自动充值、授信或其它扣款来源；若有，如何由机主关闭并留证 |
| 并发、重试、超时 | 平台并发按账户而非 API key 统计；请求发送至完成均计为并发连接 | 并发 1 仅是本地调度约束；超时/断连不证明未发送、不计费或服务器已停止 |
| 最终金额口径 | 公开美元 token 价表 | 当前账户币种/适用税费、汇率、支付手续费、舍入与追补规则；USD25 是模型价款还是含这些费用的最终支出，仍须明确 |

依据：[定价与扣款规则](https://api-docs.deepseek.com/quick_start/pricing/)、[余额接口](https://api-docs.deepseek.com/api/get-user-balance/)、[402 错误定义](https://api-docs.deepseek.com/quick_start/error_codes/)、[账户级并发](https://api-docs.deepseek.com/quick_start/rate_limit/)、[官方 FAQ](https://static.deepseek.com/faq/index.html?lang=en)。FAQ 的页面内容来自其公开前端资源；未登录账户。未检索到某项承诺，不等于已证明供应商没有该功能。

建议保留三种独立状态，名称可按仓库惯例调整：

1. **本地支出授权可执行**：broker 对每次物理发送先作持久化预留；总额统一覆盖产品、辅助、judge、诊断、重试和复核；未知发送继续占用最坏金额，不凭超时释放；停止/撤回后禁止新发送。
2. **条件计费上界已核验**：有完整可计费 token 上界、适用且有效的价格上界、全部可能收费路径、在途与重试处理的证据。若任一未知，状态保持 BLOCKED，不把演算当证明。
3. **供应商账单硬上限已核验**：账户适用的强制上限或等价可核验计费责任上界，含当前余额/授信/自动充值及费用口径，且受限 runner 不能改变或绕过。当前未取得。

合同选择应单独呈现：保留原账单强保证就继续阻塞真实付费运行，等待一手适用证据；若机主愿意接受“本地 USD25 支出授权控制 + 公开费率条件上界 + 最终账单仍有已列残余风险”，必须明确批准为合同保证变化。研究不替机主作此选择，也不为填缺口默认新增云基础设施。

## 4. macOS 各层保证

| 层次 | 可提供的保证 | 不能据此承诺 |
|---|---|---|
| 进程内 capability、ModelClient 检查、预算代码 | 正常调用路径拒绝越权、错候选、超限与撤回后请求 | 同进程任意代码执行、猴子补丁、直接 socket、修改本进程凭据不可绕过 |
| POSIX/DAC 与不同标准 UID | 对用户/组按配置限制文件读写；标准用户无管理员设置能力 | 普通 UID 自带出站域名限制；同 UID 拆目录/两个 Python 进程就是强边界 |
| App Sandbox | kernel 支持的应用资源限制，runner 与其继承沙箱的子进程受约束 | 只可见单个业务目录、禁止一切 exec、自动隔离所有 Keychain 项、按域名出站白名单 |
| VM | 客体磁盘/内存和显式共享目录提供额外边界；共享可只读 | 默认 NAT 是无网络、只允许 DeepSeek 或防护宿主管理员 |
| 机主/管理员 | 管理批准、broker、配置、审计副本与运行许可 | 本地系统能对机主自己的管理操作提供独立不可篡改承诺 |

不同用户的文件权限由 POSIX/ACL 控制；Apple 也警告同 UID 的进程可以通过配置或 IPC 相互干扰。这里是选择隔离边界的依据，不是对本机现有权限的验收。[macOS 权限](https://support.apple.com/en-ca/101914)、[用户类型](https://support.apple.com/en-mn/guide/mac-help/mchl3e281fc9/mac)、[Secure Helpers](https://developer.apple.com/library/archive/documentation/Security/Conceptual/SecureCodingGuide/DesigningSecureHelpers/DesigningSecureHelpers.html)

App Sandbox 默认仍允许自身 container 与若干系统资源；它限制任意 AppleEvents、辅助功能、修改网络等行为。实际 entitlement、动态授权和子进程都必须核对，不能把“未声明某功能”推广为所有旁路已封闭。[Protecting user data with App Sandbox](https://developer.apple.com/documentation/security/protecting-user-data-with-app-sandbox)

`com.apple.security.network.client` 是允许建立出站连接的布尔值，包含连接本机服务；不是 host/domain allowlist，TCP 连接建立后双向数据流均可用。因此 runner 不开 client/server entitlement，IPC 使用匿名管道而非为了 loopback HTTP 打开网络权限。broker 的固定 URL、禁止重定向/任意代理、TLS 校验属于可信 broker 的软件策略；若合同要求连被攻陷的 broker 也只能访问指定域名，当前方案并未提供该强保证。[network.client](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.network.client)、[network.server](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.network.server)

Keychain 权限由具体项目的访问控制与受信应用身份决定。应声明“provider secret 仅由可信 broker 读取且永不传入 runner”，并用无价值 sentinel 项验证 runner 无权读；不要声明 App Sandbox 会禁止一切 Keychain 调用，也不要用允许任意 app 或通用解释器读取的 Keychain 项来声称凭据隔离。[Keychain Access Control Lists](https://developer.apple.com/documentation/security/access-control-lists)、[Code Signing In Depth](https://developer.apple.com/library/archive/technotes/tn2206/)

VM 不作为默认方案。Apple 的 NAT attachment 将流量路由到外部网络；只读共享只保护明确配置的共享目录。把 runner 放入有 NAT、host home 共享和凭据的 VM 并不能自动实现本合同。[VZNATNetworkDeviceAttachment](https://developer.apple.com/documentation/virtualization/vznatnetworkdeviceattachment)、[Virtio 文件共享](https://developer.apple.com/documentation/virtualization/vzvirtiofilesystemdeviceconfiguration/)

`sandbox-exec` 不作为长期默认：本机系统手册直接标记 `DEPRECATED`；Apple DTS 解释其 SBPL 策略语言未向第三方文档化，不宜作为产品依赖。此处仅引用该弃用/支持范围说明，未沿用 2020 年帖子中已可能过时的虚拟化能力描述。[Apple DTS 说明](https://developer.apple.com/forums/thread/661939)

## 5. 原生 wrapper + Python CLI 的最小落点

Apple 官方支持在 sandboxed macOS app 内嵌 command-line helper，包括由外部构建系统生成的二进制。helper 按文档只带 `com.apple.security.app-sandbox` 与 `com.apple.security.inherit`；添加其它 entitlement 可能导致签名错误，`get-task-allow` 与继承模式不兼容。这是可靠的官方入口，不是任意 Homebrew Python/venv 已获兼容的证明。[Embedding a command-line tool](https://developer.apple.com/documentation/xcode/embedding-a-helper-tool-in-a-sandboxed-app)

通过 `posix_spawn` / `NSTask` 启动的子进程可继承 sandbox；主 app 不应带 `inherit=true`。静态权利可继承，运行后选文件获得的动态权利不能假定自动继承，应显式传本案数据或受控 bookmark。Apple 更推荐 XPC 做权限分离；小型匿名管道方案的协议安全仍需自己验证。[Enabling App Sandbox Inheritance](https://developer.apple.com/library/archive/documentation/Miscellaneous/Reference/EntitlementKeyReference/Chapters/EnablingAppSandbox.html)

方案的工程推论如下，全部待后续实施验证：

1. 机主启动可信 broker；broker 校验精确候选、批准对象、停止状态与预算后，启动独立启用 App Sandbox 的原生 runner。runner 再启动捆绑 Python helper。两类信任主体通常会有 broker、native launcher、Python 三个 OS 进程，不能把“两域”写成恰好两个进程。
2. 冻结 wrapper、解释器、标准库、native extensions、Python 包、entitlements 和启动环境的整体 manifest；不从 agent 可写目录导入代码，不把机主工作区/整个 home 交给 sandbox。采用当前签名/打包路径，无需因个人本地试验默认要求 App Store 上架或远端公证服务；是否需要特定签名身份仍由离线可行性测试确认。
3. broker 与 runner 仅继承指定匿名管道端点，关闭其余 FD；不传 provider socket、凭据 FD、审计数据库 FD、SSH agent、外部 terminal/stdin 或机主环境变量。`Pipe` 与 `Process.standardInput/Output` 是 Apple 支持的相关进程 IPC，但对本项目组合的可行性尚未实测。[Pipe](https://developer.apple.com/documentation/foundation/pipe)、[Process.standardInput](https://developer.apple.com/documentation/foundation/process/standardinput)
4. IPC 只接受固定操作、角色和类型化 payload；runner 不能传任意 URL、HTTP header、文件路径、SQL 或 shell 命令让 broker 代执行。broker 重新核对候选、case/attempt、operation、预算、序号、撤回及输出 namespace，避免沦为无条件代办工具。[Apple 对 helper 独立决策的指导](https://developer.apple.com/library/archive/documentation/Security/Conceptual/SecureCodingGuide/DesigningSecureHelpers/DesigningSecureHelpers.html)
5. 本案 workspace 位于 runner container 的专用目录；container 不得同时装入其它案例隐藏材料、gold、原评分、批准记录或历史账单。App Sandbox container 本身可写，不能用它保存 runner 不得篡改的审批/审计锚。每案或每阶段独立进程、必要时由可信 broker 在启动前准备容器；跨案残留须做负向检查。
6. 受限 agent 不暴露任意 shell/exec、AppleEvents、UI 自动化或通用网络工具。若攻击代码仍启动子进程，验收要求子进程继承同等 sandbox，不能扩大权限；这比“操作系统禁止一切 exec”的不实表述准确。

若捆绑 Python 依赖只能靠开放 home、通用临时例外、网络 client 或共享凭据才能运行，离线可行性判为 BLOCKED，先缩小/替换适配；不得为跑通真实调用把 sandbox 关掉，也不得改名 SQLite fixture。若匿名 FD 在实际启动路径不可可靠继承，可评估窄 XPC，但不自动演变为云服务或额外管理员。

## 6. 后续离线与运行前验收应采集什么

本研究只提出检查，不执行本机权限、凭据或出口探测：

- **候选/启动证据**：最终 bundle 与所有 helper 的签名、entitlements、manifest；确认每个实际进程启用 sandbox，Python 不从宿主路径加载代码。签名检查单独不等于行为隔离 PASS。
- **文件/秘密负向测试**：无价值 sentinel 模拟机主文件、另案材料、gold、批准记录、预算库、审计锚、Keychain 项；验证 runner/子进程及路径穿越、symlink/FD 路径均无权读取/改写。当前正用资料绝不为测试暴露。
- **出口负向测试**：runner/子进程直接 TCP/UDP、IPv4/IPv6、loopback 与外部地址均不得成为旁路；检查不存在已继承 socket、可调用通用代理或系统代发通道。先使用受控无计费目标；后续真实允许出口检查要另有授权。
- **broker 边界**：非法角色、旧批准对象、任意 URL/path/header、重放、超大帧、停止/撤回、未知发送与崩溃恢复均 fail closed；未知在途保留请求与金额预留，不自动补发。
- **盲复核**：同一机主可以管理独立进程/上下文与隔离材料，先锁定盲意见再比较。其“独立”保证是对受限 agent 与信息暴露的隔离；不保证机主无法查看材料，也不保证同供应商模型统计独立。
- **金额预检**：机主后续提供不含 secret 的账户适用计费/上限证明，核对自动充值/授信/其它消费者、完整 token 上界、峰值价/生效期、币种费用口径。缺项按对应保证 BLOCKED；不因余额充足或 402 演示就开放真实 30 案。

机主可修改批准和策略、重签/更换程序、修改本地文件、恢复旧磁盘或绕过 broker 直接使用其凭据，这些能力明确处于新的信任假设内；本地哈希链可以帮助发现受限 agent 或意外修改，不能向机主本人提供第三方不可否认、独立时间锚或不可回滚存储承诺。该变化必须由用户确认后写入修订合同。
