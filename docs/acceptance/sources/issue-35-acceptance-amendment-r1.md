# Issue #35 验收补充 r1

记录时间：2026-09-15T23:34:50+08:00

用户原文：

> linux标记仅适配未测试，不做任何测试，进行收尾直到closed/completed

适用解释：

- 对 `SCHEMA-SPEC-r1` 的 V01 第 5 项作本票验收范围补充：不要求另行取得 Linux 专项验证证据，不为关闭 `STD-01` / `SPEC-01` 启动 Linux 环境或 Linux 专项测试。
- Linux 兼容性在本地技术验收中标记为 `NOT RUN — adapted, not independently tested`，不得把 macOS 结果冒充 Linux 结果。
- 已有 macOS 门禁、CE-01–CE-08 证据、构建和四种隔离安装仍为必需证据；付费模型仍非默认门禁。
- 用户同时授权本票从当前候选继续提交、推送、创建 PR、等待仓库既有 CI、合并、回写验收并关闭 Issue，终点为准确 Issue `CLOSED / COMPLETED`。
- 仓库既有 PR 与 `main` 工作流运行于 `ubuntu-latest`；该自动 CI 属于发布链门禁，不作为本补充要求的“另行 Linux 专项验证”。其实际结果必须如实回读。

产品范围、公开接口、历史 DDL、事务行为、R01–R07、D01、CE-01–CE-08 及其非 Linux 证据要求均不变。产品字节无变化，因此候选仍为 `schema-c3-31043dbb6a1f`；评审身份更新为 `SCHEMA-SPEC-r1 + ACCEPTANCE-AMENDMENT-r1`。
