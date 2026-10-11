# 安装阻断首次复现

未修改安装脚本、资产发现规则或断言。先在原 head `6aca860` 的源树调用 `scripts.dashboard_installation_probe.closure`，得到 `FileNotFoundError: src/agent_alfred/ops/static/overview.css#overview-arrow`。

随后 rebase 到 `76c16bd`，未修复产品时在候选 `9485dee` 上构建 wheel／sdist。使用既有 `scripts/check_mcp_installations.py` 执行 base smoke；临时 `installation-debug.py` 仅打印 `CalledProcessError` 已捕获的 stdout／stderr，让 CI 未展示的内部错误可见。

实际安装在 site-packages 的 wheel 在 `verify_dashboard_http → closure → Path.read_bytes()` 报 `FileNotFoundError`，文件尾部仍为 `overview.css#overview-arrow`。外层返回 1，与 CI base smoke 的失败入口一致。原始构建／执行输出与被测文件 SHA-256 在 JSON 中，两个产品源文件保留失败版本；没有修改 Python 产品、脚本、工作流或永久测试。

修复仅删除 CSS 的 marker 引用，将同一 fragment 放到六条 SVG path 的 presentation attribute 中。绿色检查直接执行原安装脚本，没有使用诊断包装器。
