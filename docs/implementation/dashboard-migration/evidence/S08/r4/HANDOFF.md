# S08 / #116 implementer handoff — r4

r4 有界同步已完成，固定候选等待 root 调度 **Standards 与 Spec 两轴 r3→r4 受影响绑定复核**，随后由 merger 集成。未自行评审、push、写 tracker/ledger 或 integration。

- 工作树：`/Users/nineofour/.codex/worktrees/dashboard-s08/Agent-Alfred`；`codex/dashboard-s08`；clean。
- **HEAD：`51159d3854db388a6c178f8fea0b560fcda0bd13`**
- **Tree：`a7779e06b2592939767764828fd7c80b5d13a904`**
- root 指定已接受 base：`87aab611e4a040ad0edc9956a43f6dff5d4944f3`；tree `e4fa378f6e161834d528769865ab97ee1bc4517f`。
- 已双审 r3：`2951bf6892d9c5a7b43dedcb412ee54c938b2977`；tree `22d321b255f0e0edde3e588381e1f5cb02282e43`；原 base `bba439d7faeedde71970a74cea146a5e896e5345`。
- must-retain：`c34d8462ae6cdf3eb332784972db9521ebf8c070`，仍为祖先，原 production blob 不变。

## 唯一增量

正常无冲突合入已接受 S02 r8。相对 r3 **只增加 `tests/browser/shell-startup.spec.js` 的 30 行真实 first-snapshot + breakpoint 测试**，生产代码、资源、依赖和配置全部 byte-identical。r3 已审的三处 Models 默认折叠定位适配完整保留。没有导入尚未通过的 S03。

[r3→r4 diff](r3-to-r4.diff) · [最终 base→candidate diff](candidate.diff) · [真实最终 21 文件集合](candidate-files.json) · [commit 清单及父关系](commits.json)

## 必要复核与继承

[受影响 startup 实跑](logs/02-startup-composition.log)：**5 PASS**。新增真实 native SSE 首快照后从 1100→1099→1100 的布局边界保留 MainBar 输入、焦点与选区；原 entry/SSE/history 首次挂页门、0 early GET/POST、唯一页面/Stream、合法同步后断连 HTTP 读取，以及真实进程重启同 revision 重连均保留。重启 fixture 退出码 `[0,0]`，没有新失败。完整候选和增量 diff check 均 PASS。

其余确定性检查按相同代码/环境继承 r3，不重复 25/10 套件、24 HTTP 或完整 source 大表。新测试没有新增资源；逐个核对 **24 资源字节仍等于 r3 inventory SHA-256**，继承已审真实 HTTP/MIME/200 身份。原 Models 当前值采用资格、r1 请求归属与焦点修复、Connections、后端清空及价格等保持原已审实现。

[scope-delta.json](scope-delta.json) 绑定 r3 完整 source/资源/检查/HTTP 文件的原文 SHA-256，并绑定 8 个相关原文文档字节；55 source/19 AC 仅继承，不重建或升级总体结果。[applicability.json](applicability.json) 列明本轮检查、复用依据、双审 PASS 起点与 r4 待复核范围。[evidence-validation.json](evidence-validation.json) 记录候选文件、源文档、资源、must-retain 和清理核验。

## 证据保全与未完成范围

r3 原 handoff/manifests/first FAIL 在原目录保持不变；固定候选 21 个文件原始字节、记录与 11 份评审文件另行保存在 [r3-candidate](r3-candidate/)。[ARCHIVE-MANIFEST.json](r3-candidate/ARCHIVE-MANIFEST.json) 校验 128 项保存文件，未复制环境。所有更早的 FAIL 与固定身份沿 r3 原归档保留。

原 r3 Standards / Spec 均 PASS，但只绑定其原候选；r4 两轴受影响绑定尚 **NOT RUN**。整体 source/AC/G、最终 S11 跨片组合、安装矩阵、全仓 gates、原生 200%／真实中文 IME 不升级；实际 BFCache 保持历史 BLOCKED。实际回退构建和验证仍由 S11 负责。

最终工作树 clean，fixture 正常结束，17920–17929 全部可绑定。本轮无真实外部 API、付费模型、用户凭据或业务数据操作。
