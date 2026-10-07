# Issue #84 P2 环境落点与部署准备 — r1

状态：`PLAN ONLY / NOT DEPLOYED / NO REAL GRANT`。本目录是独立的本地追加规划材料；不是 `b7826a12144ec77e8656b01b02e49e0ce740f01e` 的一部分，也不修改 #84 阶段 A 验收或 #83 发布决定。本轮没有部署、购买、充值、修改 IAM/网络、读取真实密钥、调用模型或 Git 交付。

## 依据与已核查环境事实

- 基线：合并 SHA `b7826a12144ec77e8656b01b02e49e0ce740f01e`，tree `301bd93f2ae42debf6f13df4d44eacff379392b0`；现有 issue-84 工作树的 tree 相同。[阶段 A 摘要](https://github.com/nineofoursyrup/Agent-Alfred/blob/b7826a12144ec77e8656b01b02e49e0ce740f01e/docs/acceptance/phase-a-closeout.md)和[授权准入](https://github.com/nineofoursyrup/Agent-Alfred/blob/b7826a12144ec77e8656b01b02e49e0ce740f01e/docs/acceptance/AUTHORIZATION-ADMISSION.md)明确：P1 只有离线协议接缝，生产仍以 `approval_source_unverifiable` 拒绝。`AuthorityDispatch` 是四操作接口，`SimulationAuthority`/`MockTransport` 不构成系统隔离。
- 本地既有材料：`../trusted-authorization-p1-c34-r1/P2-ENVIRONMENT.md` 要求独立审批宿主、受保护 Authority 状态与不同管理域的锚、独立 Dispatch 身份、受控 worker、外部 egress 拒绝、合成 canary。该文件位于被 `tmp/.gitignore` 排除的本地目录，未进入合并树。
- 可见的仓库部署元数据：指定树只有 `.github/workflows/ci.yml`，没有 IaC/部署清单；只读 GitHub API 返回 deployments `[]`、repository environments `[]`、repository variables `[]`。这些观察**不能证明**云账户里没有资源。AWS 账户、Region、VPC、域名、管理员、账单项目及任何资源 ID 均为 **未提供／未验证访问权**。
- 当前 Codex 本机具备同权限 shell 与网络能力，不是受控 P2 worker 或可证明的受限 agent。P2 的越权/egress 验收须在下面的远端受限运行身份上执行；本机仍只能准备 unsigned proposal，不能被记为已通过网络拒绝的运行身份。
- 已批准的产品信任假设：独立远端审批宿主；agent 无管理权的独立环境及独立审计锚；未来付费只允许可验证硬金额上限，不接受 unknown 模式。选择 AWS 是本文件的**推荐拟建方案**，不是已存在资源或已批准的云供应商购买行为。

## 推荐落点：三个管理域的 AWS P2 实验环境

采用单 Region、单可用区的短时 P2 实验部署，明确不宣称高可用。账户 A（审批）由用户/独立审批管理员管理；账户 B（broker 与运行）由独立平台管理员管理；账户 C（审计锚）由另一管理员管理，建议不在 B 管理员可控制的 AWS Organization/跨账户管理员角色之下。**三个账号名称本身不构成隔离**：需读回根账号、Organizations/SCP、role trust、permission boundary、资源策略、VPC 路由与审计账户的实际控制关系。若同一执行者能修改 A 的签发策略、B 的运行策略或 C 的保留锚，P2 不通过。

| 拟建落点 | 运行身份与管理者 | P2 功能／信任约束 |
| --- | --- | --- |
| A：API Gateway + issuer Lambda + 非生产 KMS 签名 key | `p2-issuer-role`，仅 A 审批管理员能部署和改 key policy | Lambda 在 A 私有 VPC 中仅访问 KMS/SQS 私有 endpoint；P2 可只读呈现完整 proposal 条款，合成事件触发接口只允许 A 管理员的 `AWS_IAM` 身份调用，agent 不能调用。固定 issuer/key allowlist，经独立受控通道交给 B。真实用户登录、主动确认事件属 P3。issuer 没有 B 的 DB/Dispatch 或供应商凭据。 |
| B：private REST API + Authority Lambda + DynamoDB | `p2-authority-role`，B 平台管理员部署 | Authority 装入无公网默认路由的 broker VPC；仅它能事务写 grant、撤回、隔离、请求账和事件链；每次版本提交后调用 C 的 anchor API 并读回，确认前不激活/派发。外部只有 `submit_job/invoke/finish/status` 的受限投影。 |
| B：Dispatch Lambda，broker VPC 独立子网/安全组 | `p2-dispatch-role`，B 平台管理员部署；合成 secret 由独立 secret 管理员注入 | 只可取 Secrets Manager 中**合成** canary secret；仅能访问 B 的固定 private canary API。拒绝 worker 提供的 URL、Authorization header、重定向和代理；P2 无 NAT/Internet 路由或真实 provider key。 |
| B：ECS Fargate 受控 worker 与越权测试 executor | 不同 task role；镜像/任务定义由 B 平台管理员固定，agent 无 `RunTask/PassRole` | worker image digest、输入 digest、`readonlyRootFilesystem`、非 root、无 public IP；仅可调用 Authority private API 并写受限输出。测试 executor 仅可提交 unsigned proposal/读受限状态。两者无 issuer/Dispatch 身份、管理权或 secret/DB/anchor 权限。 |
| B：private REST API canary Lambda | `p2-canary-role`，B 平台管理员部署 | 模拟供应商响应/usage，留独立接收日志；仅 Dispatch VPC endpoint 与身份能调用。它不是模型供应商，也不产生模型费用。 |
| C：anchor Lambda + DynamoDB 条件更新表 + S3 Object Lock Compliance bucket | C 审计管理员独占代码、表、bucket/retention 与读回管理权；B Authority 仅可调用特定 `commit/read` anchor 入口 | Lambda 在 C 私有 VPC 中仅访问 C 的 DynamoDB/S3 gateway endpoint。C 内部用条件更新维护单调 revision 和前序哈希；每版再写锁定对象留追加审计。B 不能直接写 C 表或 bucket。C 在确认前核验表和对象链；不一致就拒绝。合规保留期内旧对象版本连 C root 也不能删除，但**新版本和 delete marker 仍可能出现**，读回须核验完整版本链，不能只看 latest key。 |

AWS 官方说明了 [Fargate task role 与 execution role 的分离](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/security-iam-roles.html)、[Fargate 私网 ENI 与 VPC endpoints](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-task-networking.html)、[DynamoDB 事务的原子性与跨账户限制](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TransactWriteItems.html)、[S3 Object Lock Compliance 的版本保护及限制](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html)。上述组合是待实施的设计推断，不能由单项云产品说明推出端到端隔离已成立。

### 请求与网络方向

1. A issuer Lambda 的**合成**签名事件经 A VPC 的 KMS/SQS interface endpoint 与限定跨账户 SQS 送至 B Authority；A 私有子网无公网默认路由。签发 key 的公钥/指纹由 A 管理员经独立受信通道安装到 B allowlist；agent 提交的文件、哈希和自签 receipt 不装入信任根。
2. 远端 executor/worker 所在 VPC 无 IGW/NAT 路由，Fargate `assignPublicIp=DISABLED`。任务安全组只允许到 Authority private REST API 的指定 `execute-api` VPC endpoint，以及运行必需的 ECR/Logs endpoint；S3 使用限定 gateway endpoint/endpoint policy。worker 不可调用 canary API。API 资源策略同时限制 `aws:SourceVpce` 与 IAM principal。[private REST API 仅支持 REST、可绑定具体 VPC endpoint](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-private-apis.html)。
3. B Authority 在事务中预留请求，调用并读回 C 的独立 `commit` 锚；发送前再次事务化 `SEND_INTENT` 并调用/读回 C 锚。仅 Authority 身份可调用 Dispatch Lambda，Dispatch 在派发前向 Authority 核验一次性 attempt 与实际 payload/profile。SQS/Lambda 重试或重复投递不得重发同一 attempt；`SEND_INTENT` 后结果丢失必须记 `MAY_HAVE_SENT/UNKNOWN_USAGE` 并停新请求。
4. B 的 broker VPC（Authority 与 Dispatch 各用独立 subnet/安全组）无 IGW/NAT 默认路由。Authority 安全组只到指定 SQS、Lambda Invoke interface endpoint、DynamoDB gateway endpoint 及必要的 private API endpoint；endpoint policy 和跨账户 Lambda resource policy 都精确限到 C `commit/read` 与 B Dispatch ARN。Dispatch 安全组只到 Secrets Manager 私有 endpoint、Authority 核验入口及 canary private API endpoint。canary API 资源策略只接受 Dispatch endpoint 与身份。P2 **没有**通往真实模型域名的路由；P4 若需外网，应重新设计并独立授权供应商专用出口，不能在 P2 中预开。
5. A issuer、B worker、B broker、C anchor 四个 VPC 的 Route 53 Resolver DNS Firewall 均采用 allowlist + block-all，故障模式 `FAIL_CLOSED`，记录 DNS query；安全组不能过滤 AWS VPC Resolver，故不能把 SG 出站规则当 DNS 拒绝证据。[AWS DNS Firewall 故障关闭说明](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/resolver-dns-firewall-vpc-configuration.html)。同时核验 IPv4/IPv6、代理、VPN、端口转发、未授权本地端口、容器 socket 与身份提升。实际名单需各账户管理员在部署前校验其服务及 endpoint 正常运行需求。

## 状态一致性与停止规则

Authority 每个变更先用 B 的 DynamoDB `TransactWriteItems` 条件推进 revision、账与事件，再向 C 的独立 anchor Lambda 提交 `(job, revision, previous_digest, event_digest)`。C 用其自己的 DynamoDB 条件更新高水位/前序摘要并写 `anchors/<job>/<revision>-<digest>.json` Object Lock 版本；C 表与 S3 对象链一致且 B 强一致读回同版后，Authority 才对外确认或调用 Dispatch。C 内部的表与 S3 也**不是同一事务**；任何部分提交都要停机并由独立管理员核对。B/C **不能跨账户原子提交**：任一步失败、当前 B DB 与 C 高水位不一致、C 表与锁定对象不一致、锚不可读或时钟失信时，整个受影响 grant `SUSPENDED`，不从本地文件/receipt 恢复。若在 B DB 提交后 C 锚确认前崩溃，保留保守占额并由 C/B 管理员按完整日志人工恢复；任何可能已发送的 attempt 不退款、不自动重试。部署时必须通过实际故障注入验证这条顺序，不以设计文字代替证明。

## 实施顺序与回滚

1. 环境所有者补齐 `config.example.yaml` 的账户/Region/管理员/预算占位符并独立授权 P2 部署；先由各管理员读回组织关系、现有 IAM、配额、账单与区域可用性。若任何管理域无法独立控制，停止。
2. C 管理员先建 C 私有 VPC、独立高水位表、anchor Lambda、DynamoDB/S3 gateway endpoint、审计 bucket/保留/只读观察与拒绝删除策略；A 管理员建 A 私有 VPC、KMS/SQS interface endpoint 与**合成** signer；B 管理员安装可信公钥指纹。先验证 agent/部署者不能改变 A/C 信任根。
3. B 管理员建无公网路由的 worker/broker 两个 VPC、endpoint/策略、DNS Firewall/日志、DynamoDB/SQS/private API、受限 role；静态核对所有允许边，不使用默认全开放安全组。定价计算和基础设施预算确认在创建前完成。
4. B 管理员部署 Authority、Dispatch、canary 与按 digest 固定的 worker/test-executor；合成 secret 值只由授权管理员写入 secret manager，绝不进入本目录、agent 环境或日志。验证运行身份不能修改任务定义、路由、IAM、DB、锚或 secret policy。
5. 运行 `PERMISSIONS-ACCEPTANCE.md` 中的真实身份/网络/故障验收；外部审计管理员读回策略、CloudTrail/Flow/DNS/canary/锚，记录原始失败与时间。任一拒绝可绕过、日志可由执行者改写、状态不一致仍派发，立即停用并保持 `ISOLATION NOT IMPLEMENTED`。
6. 完成或停止时先拒绝新 job、撤回合成 grant、停 Fargate/Lambda 派发、移除合成 secret/可调用入口，再保存审计读回。由各资源所有者按逆序撤销临时角色与可收费资源；C 的 Compliance 保留对象在期限届满前不能删除，成本继续计入。交付后 A/B/C 管理员各自保留其管理权；临时部署身份收回，Codex agent 始终无这些权力。

## 当前不可宣布通过的条件

实际 AWS 账户/Region/VPC/项目、三方管理员与独立管理关系、基础设施预算、签发根安装渠道、日志/锚保留期、P2 部署授权均未提供；本轮没有云资源读回、没有执行任何 P2 拒绝测试。P4 计费方与硬金额证明另列于 `COSTS.md`，不阻止准备 P2 合成 canary 实验，也不能被当作 P2 完成或真实执行许可。#84 保持 `CLOSED/COMPLETED`，#83 保持 `OPEN`；c25 隔离、c21 `v1_release=FAIL`、r3 pending 与历史保护 FAIL 不改。
