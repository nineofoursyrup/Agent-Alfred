# 初版 CI 安装核验首次失败

运行：[38109147161](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/38109147161)，head `87d3fb68cfb3e61e962ea4c0bef7f40c666a3a2d`，job `114380870842`。在本轮推送后的远端读取中发现该历史运行已失败。

离线确定性测试、构建发行包完成；失败步骤为「Verify base and MCP artifact installations」。`scripts/check_mcp_installations.py` 调用隔离 base 环境 smoke.py 返回 1；日志只有 CalledProcessError，没有底层 smoke 的详细 stdout/stderr，根因 UNKNOWN。后续 Dashboard typecheck／browser 步骤均 SKIPPED。

远端 artifact 数为 0，因此没有浏览器失败用例目录、截图或 trace 可归档；只保存 run 元数据及失败 job 日志，不制造缺失材料，不提交整个 artifact。job-log.json 的 content 可恢复原始日志字节，SHA-256 保留。run.json 绑定该历史 head 的 overview.js／overview.css 哈希。

没有修改 Python 安装检查、工作流或依赖。本轮本地 543 passed 与该历史失败分别记录；新 head 的远端 CI 状态只以交付回报和 PR 读取为准。
