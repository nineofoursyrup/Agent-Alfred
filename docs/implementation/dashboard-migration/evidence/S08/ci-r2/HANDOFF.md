# S08 ci-r2 — accepted-tip compatibility handoff

ci-r2 已固定，供根安排独立增量绑定。此轮仅合入已验收的 S05 Memory/listener tip；S08 产品实现及 ci-r1 原 4 行测试修复均未改变。

- Worktree：`/Users/nineofour/.codex/worktrees/dashboard-s08/Agent-Alfred`
- Branch：`codex/dashboard-s08`
- Head：`abb3817cee1c6491094f589c2c25c5257d5a1ac8`
- Tree：`cbe04daa248ffe4020316544f9c7ee5630fadafd`
- Accepted base：`da7d6b84899b19d690d574c74397a45b0239078b`
- Base tree：`20821980c0bb7977c157416a52db5ac80327b307`
- Previous ci-r1：`e6a91d0d8c5eae5d75829e543a79140f9e060935`，保留为祖先。
- Must retain：`c34d8462ae6cdf3eb332784972db9521ebf8c070`，祖先及清空修复 blob 均未变。

## 范围与当前证据

正常 merge 无冲突。相对 accepted base 仍仅 `tests/browser/mcp.spec.js` **+4/-0**，与 ci-r1 测试 blob 完全相同；整个 `src` 与 accepted base 完全相同。ci-r1→ci-r2 的 8 个文件变化均来自根指定的已接受 S05 Memory/listener 集成。listener 将 stdlib 默认 backlog 改为 `socket.SOMAXCONN`；本实现者没有修改该实现，也没有导入其他 private 候选。

ci-r1 已独立 Standards / Spec **PASS、0 findings**，报告与验证材料复制于 `ci-r1-candidate/reviews/`。原 ci-r1 的 63 项证据原地 hash 保全；原根镜像在更新前复制于 `ci-r1-candidate/mirrors-before/`。本轮当前 `source-coverage.json`、`resource-inventory.json`、`resource-http-identity.json`、`candidate-files.json`、`candidate.diff`、`checks.json`、`ownership-and-applicability.json`、`evidence-validation.json`、`HANDOFF.md` 在 `S08/` 与 `S08/ci-r2/` 双镜像一致。

原 **55 source / 19 AC** 的 source 原文、行号、hash、owners、integration chains 等 metadata 与冻结图核对；历史 r3 fragment/evidence candidate 身份按原值保留，不把旧证据改写为 ci-r2 新跑结果，整体仍 **NOT RUN**。

当前资源从实际 `index.html` 的 href/src，递归 JS import/export/dynamic import 与 CSS url 发现，再与 ASSETS 比对，得到 **33 资源 / 77 边**，没有缺失引用或未达的注册资源。完整归属及 hash 见 `resource-inventory.json`、`ownership-and-applicability.json`，包括新接受的 Memory CSS、已有 S03/S06/S10 及 S08 私有资源。S07/S09 旧资源的存在不代表其新页面已验收。

## 本轮验证及继承

- 原丢失 MCP 回执 target **一次 PASS**（2.8s，retries=0），由真实 listener 变化触发。完整保留两份相等 payload、同 operation_id、仅新增一次 initialize、四 viewport 的滚动/焦点/详情/无横溢出。没有重跑 5 次或全套。
- 当前真实隔离 Host 对全部 **33 资源 HTTP 200 / MIME / SHA-256** 核对通过，均等于固定候选源码；`model_calls=[]`，fixture exit 0。
- accepted base→candidate 与 ci-r1→ci-r2 两种 diff check 均 PASS；工作区干净。
- 所有 Host/MCP/browser 正常结束；后续独立清理核验中，`17920–17929` 原始 bind 与 SO_REUSEADDR bind 全 PASS，connect_ex 均拒绝，无本任务 fixture 进程。lsof 的无关 SMB stat warning 原样记录，清理结论以 bind/进程证据为主。
- ci-r1 的受控 GET 探针 1 PASS 与稳定性 5 PASS，以及历史 S08 后端/Models/Connections 检查按 unchanged source/inputs 的范围继承，不计为本轮新跑。新 listener 兼容性由本轮唯一 target 覆盖。

证据脚本首次使用不存在的 fixture `port` 字段，改读实际 `origin`；之后资源 HTTP 核对与 exit0 已完成，但紧接着的原始 bind 短暂返回 address-in-use。原失败均保留，未假设原因；没有再次跑资源检查，独立稍后清理审计证明端口全部可绑定。证据汇总另有 collection key、相对路径、冻结空 fragment 与历史 fragment 区分的三次校验脚本错误，均保留原脚本及 logs/07–09，最终 logs/10 校验成功。这些不属于产品测试失败，也没有覆盖原件或改弱产品检查。

## 未完成与归属

两次原远端 CI `37871594920` / `37871590592` 均保持 **FAIL**（各 418 browser PASS / 1 FAIL，6422 Python PASS）；原 PR 28 项 / push 27 项 artifact hash 本轮重新核验。本轮不宣称最终 CI、完整 source/AC/G、安装矩阵或 native 验证通过。

ci-r2 的独立双轴增量、最终集成和完整 CI 由根执行。S07 以后被接受时，仍须合并其 MCP tests 的 details 展开及 nested cleanup，同时保留本处 4 行完成状态等待；当前未导入其 private 候选。S11 的 native 200% / IME、真实移动端及 BFCache 边界不在此升级。没有自行评审、push、ledger/tracker/GitHub 写入，可释放实现者 slot。

详细入口（相对 `S08/ci-r2/`）：`candidate-files.json`、`candidate.diff`、`ci-r1-to-ci-r2.diff`、`source-coverage.json`、`checks.json`、`resource-inventory.json`、`resource-http-identity.json`、`ownership-and-applicability.json`、`evidence-validation.json`、`MANIFEST.json`。
