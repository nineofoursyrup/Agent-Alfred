# PR #120 独立 Spec 评审

Candidate：`11dcc66f18ceb264ade1c1bcc1b48287dc4f1516`；tree `41adbb9ad2479d16228c68f8c228882c6cd6ae91`；base `22c8720e1ec874fd012cc79b086cd8aace4c79b6`。审查前后身份一致、worktree 干净。使用 `git diff base...head`；未修改仓库或 GitHub。

覆盖 S01–S11 的规格与集成边界：共享读取、十页壳层/MainBar、来源恢复、Overview、Memory/Behaviour/Tools、Models/Connections、Ops/Database；资源安装及升级回退核对现有证据，不重复执行。重点独立读取新增/改变的实现，不使用旧评审结论代替代码。结论不是全部 309 源责任的产品验收 PASS。

1. **SP-120-01 / P2：断连未退休精确定位读取。** `src/agent_alfred/ops/static/app.js:314–327`（响应资格检查在 316）；断连入口 `675–685`、`852–861` 均未退休该请求。规范 `docs/design/dashboard-implementation/INTERFACES.md:21`：“连接中断…按源规则退休请求”；另见 `issue-92/ACCEPTANCE.md:18` AC06。延迟真实 `/api/mainbar/locate` 响应，触发 offline 后放回，实际 `connected=false` 仍返回 `applied` 并插入此前未读目标正文；预期请求退休、不回写。关闭检查：覆盖 SSE error 和 offline 两入口、断连后恢复及较新定位，旧请求不应用，新显式读取可正常执行。

2. **SP-120-02 / P2：连续定位把旧目标混入“最新”历史。** `app.js:349–354` 只过滤最后一个 `locatedRecord`；每次 `322–327` 又覆盖它并积累 `replies`。规范 `INTERFACES.md:129`：“回到最新清除定位呈现”，`issue-89/DESIGN.md:159` 禁止将不相邻目标拼成连续历史；AC07 在 `issue-92/ACCEPTANCE.md:19`。真实 27 条 chat Run，普通历史为后 25 条；依次定位此前未加载的 A、B，再回到最新，实际 A 残留、记录变 26 条、定位提示为空；预期只有 25 条合法普通历史。关闭检查：连续多个早期目标、返回最新、普通补页与新 SSE 回复交错，定位片段不伪装连续历史，合法记录不丢失。

验收缺证单列：真实中文 IME 仍 BLOCKED（`ci-repair-r1/native/successor-065-applicability.json`），违反 AC03/AC26 的完成条件；不是本轮新发现的代码缺陷，合成 composition 不替代它。未确认额外范围扩展。

**Spec：2 条代码 findings（均 P2）；1 项既知验收阻塞。**
