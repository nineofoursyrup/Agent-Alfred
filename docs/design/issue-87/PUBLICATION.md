# #87 文档归档与远端收尾授权

日期：2026-10-08（Asia/Shanghai）。来源为本票设计讨论之后的同一 live exchange。

在完整壳层规格、resolution 草稿及文档检查结果已呈现后，用户对下列收尾请求明确回复「授权」：

> 是否授权 文档 commit/push → 发布 resolution → 关闭 #87 → 回写 #85？

该授权覆盖本票的文档提交、推送独立归档分支、发布已确认设计的 resolution、以 completed 关闭 #87，以及在父地图 #85 的 Decisions so far 添加标题链接和一句摘要。父地图继续保持开放；产品实现、产品验收、合并 main 和产品发布仍遵守独立授权边界。

## 归档身份

- 分支：`codex/issue-87-shell-design`。
- 文档起点：#86 获批视觉基线提交 `541befcbe4f771d6f0d7ce3dfc820357c91c7cbc`。
- 产品代码基线：`22c8720e1ec874fd012cc79b086cd8aace4c79b6`。
- 冻结规格：[DASHBOARD-SHELL-SPEC-r1](SPEC.md)。SHA-256：`60a5342a1c2bdd3d6b7f7891c7268aa922898bf09203c81ffa1a26917c804f71`。
- 归档提交的实际 SHA、固定文档链接和远端核验结果由 [#87 resolution](https://github.com/nineofoursyrup/Agent-Alfred/issues/87) 记录。

`SPEC.md` 保留冻结时的完整字节，末尾待收尾授权的表述描述其成文时状态；本文件补充其后新增授权，不改写设计决定。[RESOLUTION-DRAFT.md](RESOLUTION-DRAFT.md) 同样保留为授权前草稿，实际发表内容以 #87 resolution 为准。

文档验证采用差异检查、相对文件链接检查、规格指纹和改动范围核对。产品代码与 #86 资产保持原样；文档检查不等于产品或浏览器验收。当前 CI 配置由 main push、指向 main 的 pull request 或手动 dispatch 触发，独立文档归档分支的 push 不触发该工作流。
