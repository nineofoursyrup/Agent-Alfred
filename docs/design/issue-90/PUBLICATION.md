# #90 文档归档与远端收尾授权

日期：2026-10-08（Asia/Shanghai）。来源为本票设计讨论之后的同一 live exchange。

在完整设计、验收路径、领域文档补充、resolution 草稿及文档检查结果已呈现后，用户针对以下明确列出的收尾范围回复「授权」：

> 是否授权仅文档收尾：commit/push 归档分支 → 发布 resolution → 关闭 #90 → 回写 #85？

本次授权覆盖本票文档提交、推送 `codex/issue-90-memory-behaviour-tools-design` 归档分支、发布已确认设计的 resolution、以 completed 关闭 #90，以及在父地图 #85 的 Decisions so far 添加标题链接和一句摘要。父地图继续保持开放。授权不包括产品实现、付费执行、合并 main 或产品发布；主工作区原有修改不纳入本次提交。

## 归档身份

- 分支：`codex/issue-90-memory-behaviour-tools-design`。
- 文档起点：#88 归档提交 `0e9915b5f020a756776f3e67fe13f352da3bb983`，包含 #86 视觉基线与 #87 壳层合同。
- 产品代码基线：`22c8720e1ec874fd012cc79b086cd8aace4c79b6`。
- 冻结设计：[MEMORY-BEHAVIOUR-TOOLS-DESIGN-r1](DESIGN.md)。SHA-256：`9788e488c7b32dfbe15876193c2fc4958f44b82961ae7f091b8284b073863697`。
- 设计依据：[DECISIONS.md](DECISIONS.md)、[APPROVAL.md](APPROVAL.md)、[FACTS.md](FACTS.md)；验收交接：[ACCEPTANCE.md](ACCEPTANCE.md)；既有检查：[VALIDATION.md](VALIDATION.md)。
- 领域补充：[人工保护](../../../CONTEXT.md)、[ADR-0046](../../adr/0046-panel-visibility-preserves-page-lifecycle.md)。
- 原始文件摘要：[manifest.json](manifest.json)。该清单的九个目标保留原字节，清单自身及本文件由归档提交与逐文件 Git blob 回读绑定，不扩大原清单的校验范围。
- 实际提交 SHA、固定文件链接、远端文件核验和关闭结果记录在 [#90 resolution](https://github.com/nineofoursyrup/Agent-Alfred/issues/90)，避免在提交内写入其自身 SHA。

## 记录与验证边界

原有设计材料、批准记录、检查记录、resolution 草稿和 manifest 均保留。其「未获收尾授权」「未提交／推送」「未发布」或「OPEN」表述描述各自成文时状态；本文件追加其后取得的归档授权，实际执行结果以 #90 resolution 和远端回读为准。两轮「全按建议」仍只代表各轮设计确认，不追溯扩张为交付授权。

归档范围共十一份文件：本票目录的九份材料、`CONTEXT.md` 和 ADR-0046。已完成且输入未变的静态核验沿用 [VALIDATION.md](VALIDATION.md)；新增本文件执行链接、空白与冲突标记检查，并在提交前核验原文件摘要、变更路径及产品／继承设计内容保持不变。推送后按固定提交逐文件读取远端 blob，再发布 resolution、关闭 #90、追加父地图摘要，最后回读议题和原生依赖状态。

当前 CI 仅由 main push、指向 main 的 pull request 或手动 dispatch 触发，独立文档归档分支的 push 不触发该工作流。本票只有文档变更，产品行为／浏览器测试及真实模型／外部工具执行均为 NOT RUN；静态检查和远端归档不构成产品验收。
