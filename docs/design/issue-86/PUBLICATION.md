# 视觉决策收尾授权与档案范围

2026-10-08（Asia/Shanghai），用户在两轮设计确认后明确回复：

> 授权收尾#86grilling

该授权覆盖[决定：Dashboard 视觉规范与参考验收基线](https://github.com/nineofoursyrup/Agent-Alfred/issues/86)的档案提交与推送、resolution comment、关票及父地图摘要回写。设计确认沿用 [APPROVAL.md](APPROVAL.md) 的 Q1–Q6；本次授权没有重新选择或修改原型。

## 发布方式

- 保存于专用分支 `codex/issue-86-visual-baseline`，基础代码为 `22c8720e1ec874fd012cc79b086cd8aace4c79b6`。resolution 使用实际推送提交的完整 SHA 构造固定链接。
- 发布本目录中的参考来源、A/B/C 原型与历史、获批 A 的 `baseline/r1/`、tokens、截图、检查和批准记录，以及本票的 `CONTEXT.md` 术语澄清和 ADR-0045。
- 获批原型 SHA-256：`44936e6b85558dfb78d1df6e6aec08ea300575e598cfa3e284c187cc9492bfdf`。
- 冻结基线 manifest SHA-256：`95af762d98d31d878369602d9fb8bc2028db83d0507c6477c2a6483859f0cd5e`。本次收尾不修改其文件；发布前文档保存在 `history/before-publication/`。
- 原型在 main 之外归档，不创建产品实现 PR 或合并；后续实现依据本票固定链接读取并落实相关决定。

## 核验与边界

提交前校验资产清单、封存基线、文档差异和源文件语法；复用对同一获批原型已完成的浏览器检查。保留首次失败与修正证据。推送后先读回提交、Git tree 与冻结基线摘要，再发布 resolution；关票与父地图回写后核对 REST / GraphQL 的状态和链接。

产品代码、真实数据连接、付费调用、全产品测试和产品发布不属于本次决策收尾。后续壳层交互与 Overview 数据口径分别由其对应决策票冻结。历史批准记录中的授权边界描述当时状态，本文件记录后续新增的明确授权。
