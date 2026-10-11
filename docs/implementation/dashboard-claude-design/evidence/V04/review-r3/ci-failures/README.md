# 两轮已结束的安装 CI 失败

- `d87ccc7b5c98b92a653cb45f7738215225f3a8eb`：run `38114280185`，job `114395951210`，FAIL。
- `6aca860852e07bf5dfb7c91ce64edf28ef3088c1`：run `38114532043`，job `114396686875`，FAIL。

两轮均在 Verify base and MCP artifact installations 的 base smoke 返回 1；Lint、Skill、环境、Python 确定性测试与构建通过，后继 typecheck 和浏览器测试 SKIPPED。日志没有打印被捕获的内部 stderr；根因经本轮本地未修复 wheel 的同脚本复现确认。

仅归档失败 job 日志、运行元数据与 artifact 列表（两轮计数均为 0）。没有浏览器失败用例目录可归档，没有下载或提交整个 artifact 压缩包。旧 first-ci-installation-failure 目录字节原样保留。
