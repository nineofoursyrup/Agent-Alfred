# V04 范围

初始 worktree 干净，基线为 #123 合并后的 `645c4549bb5bea9f79ddb4625f17c5a8a6af58c8`。主工作区 `docs/dashboard.md`、`README.md`、`tmp/` 为已有用户改动。未修改主工作区或其他 worktree。产品和测试仅归属 `overview.js`、`overview.css`、`overview.spec.js`；其余新增文件为用户明确要求的 V04 证据。

原稿 Overview 预览可联网读取 unpkg；生产页面无新增网络来源。历史最近运行 API 明确剔除 `prompt_preview`（`gateway/web/shared_reads.py`），因此行预览仍使用既有用途事实，不增加读取或复制演示消息。
