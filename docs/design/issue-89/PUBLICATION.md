# #89 文档归档与远端收尾授权

日期：2026-10-09（Asia/Shanghai）。本文件追加设计确认之后取得的交付授权，不改写原设计及首次核对记录。

用户在收到 #89、#92 和父地图 #85 的当前状态，以及“归档收尾 → to-spec → to-tickets”的建议后，明确回复：

> 归档收尾，我将使用新会话to spec

该回复授权完成已确认设计的文档归档、推送独立归档分支、发布 resolution、以 completed 关闭 #89／#92、回写并核对 #85 地图终点后收口。用户会在新会话执行 to-spec；本轮不启动该流程、创建实现票、实施产品或合并 main。之前的设计确认仍只代表设计确认，不追溯扩张授权。

## 固定来源与保留内容

- 归档分支：`codex/issue-89-inbox-runs-design`；起点为 #88 归档 `0e9915b5f020a756776f3e67fe13f352da3bb983`，包含 #86／#87。
- 产品基线：`22c8720e1ec874fd012cc79b086cd8aace4c79b6`。
- [DESIGN.md](DESIGN.md) 的 `INBOX-RUNS-MIGRATION-DESIGN-r1` 已获整体确认，当前 SHA-256 为 `35ba39e52415b8e349d3658970932bcbd23019ef561f55a41642369afbe0af84`；[APPROVAL.md](APPROVAL.md) 保留呈送版本身份与确认原文。
- 原八份材料保留字节：`CONTEXT.md`、[ADR-0048](../../adr/0048-exact-run-reply-location-stays-in-mainbar.md) 及本票六份文档。新增本文件和 [manifest.json](manifest.json) 记录本次归档身份与边界。
- 原文中的“未获收尾授权”“未提交／推送”“OPEN”描述成文时状态；本文件记录后续授权，实际执行身份及远端状态由 [#89 resolution](https://github.com/nineofoursyrup/Agent-Alfred/issues/89) 记录，避免在提交中写入自身 SHA。

## 核对和交付范围

沿用 [QA.md](QA.md) 中输入未变的设计与反例核对。归档前核对原文件摘要、新增文件链接／空白、实际暂存路径及产品／继承设计无差异；推送后按固定提交逐文件读取远端 blob，再发布 resolution、关闭议题和回写父地图。

当前 CI 不由独立文档归档分支的 push 自动触发。没有产品代码或配置变更，产品 Python／browser、构建／隔离安装和真实模型执行均为 NOT RUN；文档检查与远端归档不能替代产品验收。主工作区原有 `CONTEXT.md` 和 `tmp/` 保留，其他设计材料按原固定身份沿用。

后续 #92 的归档会接入本票固定提交并与 #90／#91 文档合并，供新会话读取完整地图。该文档合并只整合已确认词汇与设计，不改变产品基线。
