# P2 所需环境与权限（不属于 c34 实施或授权）

P1/c34 仅建立离线协议接缝。P2 要在执行者无法控制的真实系统边界上，使用合成 issuer key、合成供应商 secret、非付费 canary endpoint 验证隔离。环境所有者需先提供以下具体落点及相应受限权限；若任一项无法提供，保持 `ISOLATION NOT IMPLEMENTED / offline-only`。

1. **独立审批宿主**：与 Codex 工作区及同权限进程隔离的宿主、域名、账户认证与用户验证方案；交互事件 API、固定 issuer allowlist、不可由 agent 调用的签发身份及受保护密钥存储。审批 UI 能完整呈现候选、批次、模型、请求/token/期限、输出范围及费用条件；事件含 challenge、nonce、subject、事件时间、proposal digest 与 decision。P2 先用合成事件及合成密钥，真实用户事件留到 P3。
2. **Authority 控制面**：独立服务身份、可事务写入的 grant/撤回/隔离/请求账 DB，以及不同管理域的单调高水位锚和追加审计日志。仅 Authority 身份可写；提供 agent 只读的受限状态投影和运维可读的策略/审计读回。需有删除、替换、回滚、崩溃、重启和网络分区的故障注入环境与恢复权限。
3. **Dispatch 身份与网络**：Dispatch 专属 service identity、secret manager 权限、只允许已签 endpoint 的 egress 策略和非付费 canary。仅 Dispatch 可读合成供应商 secret；拒绝任意 URL、重定向、代理和执行者提供的 Authorization header。P2 不配置真实 provider key。
4. **受控 worker**：独立非特权 UID/工作负载身份、broker 管理的只读固定代码与输入包、完整 manifest/运行包摘要校验、限定 evidence namespace 与输出挂载。agent 不能切换该身份、修改镜像/策略或启动具有更高权限的 worker；worker 不挂载 keychain、home、SSH agent、secret、Authority DB、容器 socket。
5. **外部强制的网络拒绝**：在 worker/agent 进程外默认拒绝任意外网派发；worker 只到 Authority 相互认证入口及必要受控本地服务。验证直达供应商、DNS/代理/VPN/端口转发、未授权本地端口、容器 socket 与身份提升均被 OS/网络策略拒绝。策略和拒绝日志须从执行者不可写的控制面读回。
6. **P2 操作权限与证据**：允许在隔离测试环境创建/部署上述合成身份、策略、服务与 canary，运行 ACL/egress 拒绝测试和故障注入，导出只读策略、服务身份、拒绝日志、DB/锚版本与测试输出。禁止读取/配置真实 issuer 或 provider 凭据、真实供应商请求、历史批次追认及 Git/发布操作。环境所有者应给出宿主/项目 ID、服务地址、管理员协作人与最小部署/观测权限，不能由执行者在当前同权限本机自行宣布完成。
7. **未来付费的独立前提**：提供外部可强制的硬金额上限机制及其可证明的币种、含税/附加费口径、价格依据版本、所有可计费变量的上界与停机读回。请求/token/时间限制或本地估算不算金额硬封顶。具体金额和计费条件待另行产品裁决与单批独立确认；无法证明则始终不可执行，不能改用 unknown 费用模式。

P2 完成只说明真实身份、状态与网络隔离通过；P3 的真实宿主事件及非付费端到端、P4 的单批硬金额确认与真实凭据接入仍需分别授权。
