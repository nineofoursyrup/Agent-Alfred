# #92 与 Dashboard 地图的文档归档

日期：2026-10-09（Asia/Shanghai）。本文件记录已确认设计之后取得的归档授权、输入补齐及新会话交接；产品实现和验收仍未开始。

## 本次授权

用户在获知 #89、#92 和 #85 的剩余工作均为归档／决策收尾后，明确回复：

> 归档收尾，我将使用新会话to spec

该回复覆盖 #89／#92 文档 commit／push、发布固定来源的 resolution、以 completed 关闭两票、回写 #85 并在地图终点满足后收口，以及准备新会话的 to-spec 入口。它没有启动产品实现、创建实现票、合并 main、运行个人实例或付费模型；新会话由用户另行开启。

## 来源补齐与原始记录

- #89 已归档于 [0120f0aff56b7c8b29854c5d9be5fe475b8b06b7](https://github.com/nineofoursyrup/Agent-Alfred/commit/0120f0aff56b7c8b29854c5d9be5fe475b8b06b7)，[resolution](https://github.com/nineofoursyrup/Agent-Alfred/issues/89#issuecomment-6065550390) 已发布，议题读回为 CLOSED／COMPLETED。其原八份材料保留字节，10 份归档文件已逐文件读取远端 blob 核对。
- 本分支在原 #91 文档基线 `1a343ae1894554ad0167273a34b1f6bce171fb5b` 上接入 #89 固定提交。`CONTEXT.md` 自动合并新增的收件箱／会话运行／系统运行词汇和运行详情定义，同时保留 #90／#91 的词汇；产品基线仍是 `22c8720e1ec874fd012cc79b086cd8aace4c79b6`。
- [source-index.json](source-index.json) 将 #89 从本地未归档输入更新为固定提交、永久文档链接和已关闭的 native dependency；[acceptance-map.json](acceptance-map.json) 只更新该输入条件，原条文、原 ID、责任和 NOT RUN 结果保持。
- 归档前十二份文件在 [history/before-archive](history/before-archive/README.md) 逐字节保留，包括当时的 #89 BLOCKED、原 manifest、首次文档 FAIL 及后续修正记录。当前规范／来源状态更新不覆盖这些历史。
- [VALIDATION.md](VALIDATION.md) 仍保留设计整理阶段的原始核对；本次归档核对见 [ARCHIVE-VALIDATION.md](ARCHIVE-VALIDATION.md)。原始材料中未提交／未授权／OPEN 的表述按其成文时点理解，不用后来授权改写早期记录。

## 当前交接入口

[HANDOFF-TO-SPEC.md](HANDOFF-TO-SPEC.md) 给出新会话的目录、固定来源、继续有效的决定及工作范围。[SPEC.md](SPEC.md)、[SLICES.md](SLICES.md)、[ACCEPTANCE.md](ACCEPTANCE.md) 与 [VALIDATION-PLAN.md](VALIDATION-PLAN.md) 共同组成 #92 的归档输入，沿用已确认 Q1–Q7，包括移动模拟覆盖 Q4 原真机要求。

归档分支为 `codex/issue-92-migration-contract`。当前文件及历史副本身份由 [manifest.json](manifest.json) 记录；实际归档提交 SHA、远端字节核验、#92 关闭和地图收口结果由 [#92 resolution](https://github.com/nineofoursyrup/Agent-Alfred/issues/92) 及 [#85 收口结论](https://github.com/nineofoursyrup/Agent-Alfred/issues/85) 记录，避免在提交中写入自身 SHA。

文档只在归档分支保存，未合并 main。独立文档分支 push 不触发现有 main-only CI；文档静态检查和远端回读分别报告，产品 Python／browser、构建／隔离安装、桌面原生／移动模拟、升级回退和产品独立评审仍均 NOT RUN。
