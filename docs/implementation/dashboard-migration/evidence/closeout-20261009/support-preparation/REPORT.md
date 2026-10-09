# 支持回退后继与 G08 执行报告

结果：**support-closeout-r1 未提交候选已准备；新 G08 第一次执行 PASS，exit 0，4.93 秒**。候选等待根协调器独立双轴核验及 Git 发布；本报告不擅自采用最终验收或关闭 Issue。

## 固定候选

- 工作树：`/Users/nineofour/.codex/worktrees/dashboard-rollback-closeout/Agent-Alfred`，初始 clean、detached HEAD `8d4adbc97cf1f5d55e3d36b65da7dbf2a1a1be2a`。
- 精确同步 integration 已审 manifest 的 7 路径（4 产品 JS、3 回归测试），未自行新增测试、未重跑 114 条回归。支持树中的回归文件与 integration 当前文件整份相同；其相对旧支持树的 diff 可以多于 integration 相对 11dcc 的 diff，原因是旧支持树原先没有 S11 的测试侧附加修改。
- 620 个产品文件已逐一 hash 对照：与当前 integration 仅 `ops/static/index.html` 和 `ops/static/shell.js` 的已批准 Inbox 默认入口差异；这两文件保持 8d4 原字节，四个修复产品文件与已审 integration 相同。
- 完整未提交候选 tree：`641dba43d5e468f2d62be709953765b42b4bcaca`；src tree：`534f2caae73702d69a57ddf54b9af20dd207b326`；package tree：`ac9c0adf3bd35595e8280ecc1876fd0a1d5d2929`。
- [candidate-manifest.json](candidate-manifest.json) SHA256 `54235b595fe2a096f1ebb33b1920efc8f5956dd36205c281fbfbd15833d0d60b`；含 7 路径文件 mode/type/hash、620 产品文件 hash、完整 diff hash、来源 manifest 和复用边界。[candidate.diff](candidate.diff) 包含 tracked 与纳入的 untracked 变化。
- tree 由独立临时 `GIT_INDEX_FILE` 计算；真实 index 保持 EMPTY，HEAD 未变。没有 commit/push/远端动作，没有写 integration、canonical ledger 或 acceptance。

## 产物与环境

| 角色 | wheel SHA256 | 安装源字节 |
| --- | --- | --- |
| old 22c8720 | `d688a7aebdcc9451003e12f316be9930fd9fb7ffff8b5c12557fdb513fec4a46` | 598 文件全部等于固定旧 Git 源码 |
| current 未提交 integration | `55648a28cdfae7ca479033739ace599ab8f22546ebd5d03b5214d7d40cc96e5a` | 620 文件全部等于当前已审源码 |
| supported 未提交后继 | `e42c4cf600f20186c2045868b0696a3c4c1f2e61f7b56e9a3d04e1dd2724d87e` | 620 文件全部等于本支持候选源码 |

支持包由 `uv build --out-dir /Users/nineofour/.codex/dashboard-migration-20261009/closeout-20261009/support-dist` 新构建；该目录此前不存在，恰有一件 wheel 和一件 sdist（另有 uv 自建 `.gitignore`）。sdist SHA256 `f9735d131e1e174086dba21ff2bab90b87326bc531196298054ab962f6ec79dc`。原 dist 与支持 8d4 产物未覆盖。

三组均新建独立 CPython 3.14.7 mcp 环境：`/Users/nineofour/.codex/dashboard-migration-20261009/closeout-20261009/lifecycle-{old,current,supported}-env`。验证运行 cwd 在证据目录，移除 PYTHONPATH/PYTHONHOME，禁用 dotenv。逐文件验证 wheel→所声称源码以及实际 site-packages→wheel；实际导入路径位于各 env，源码污染均 false。完整依赖版本、Python 路径和包内 hash 见 [installed-source-binding.json](installed-source-binding.json)，产物对源绑定见 [wheel-source-binding.json](wheel-source-binding.json)。

支持目标只执行本次授权的新构建和同状态生命周期安装；没有将其称作新的 wheel/sdist × base/mcp 四组完整检查。当前 integration 四安装由根协调器另外运行并登记。

## G08 新执行

直接复用 integration 的 `scripts/check_dashboard_lifecycle.mjs` 与 `scripts/dashboard_lifecycle_server.py`，没有改执行器、服务夹具或产品。命令和 hash 在 [lifecycle-manifest.json](lifecycle-manifest.json)、[lifecycle-01-summary.json](lifecycle-01-summary.json)；current 与 supported 均明确标成 base+manifest 的未提交候选，不把 base SHA 冒称为新产物 commit。根提交后可追加等价绑定，原执行身份不改写。

执行目录 `/Users/nineofour/.codex/dashboard-migration-20261009/closeout-20261009/support-lifecycle-01` 此前不存在；脚本又独立断言 state 不存在。old→current→supported 连续使用同一持久状态、同一个保留浏览器标签，每次正常 close 后才换进程。三组 instance 不同，实际服务 package/HTML SHA 与安装验证完全匹配；三次 close 均 code=0、signal=null、closed=true；结束后端口可再次绑定，未留运行服务。

实际通过：Session、旧/新消息、草稿、深链接、显式旧标签刷新、不自动重投；显示名和价格清空后的持久值、旧/新记忆及真实遗忘完成、Ops 账目、新版 Database 查询；支持根/品牌入口恢复 Inbox。current 刷新自动 POST=0，supported 刷新自动 POST=0。没有恢复数据库、清理状态、删除锁或并开写宿主；所有数据和模型均为离线隔离夹具。

本轮首次执行即 PASS；没有新的 FAIL 需要重试。此前修复 RED/FAIL、旧 G08 和 old/current/support 历史产物全部保留。未声称真实 provider 或用户业务状态验证。

## 证据与交接

- 原始结果（含三处短命夹具 CSRF token，不用于公开）：`/Users/nineofour/.codex/dashboard-migration-20261009/closeout-20261009/support-lifecycle-01/result.json`，SHA256 `d08ecec57c384b3f34df3ae593e0cbd59d105a6080301e3bb7b9934394e22a5f`。
- [lifecycle-01-portable.json](lifecycle-01-portable.json)：可公开结构副本，仅移除 `stages[0..2].entry.csrf_token`，记录原始 SHA 和具体删字段；其余行为、输入与候选信息保留。SHA256 `fadab8f1787c0cadf54743bd9f23d5b83a01c9930f6f402c54e4c5801957aa72`。
- [final-verification.json](final-verification.json)：最终无漂移、7 文件/620产品 hash、两处默认入口差异、真实 index empty、`git diff --check` PASS、三次 close 与端口释放证据。
- `preflight.json`、`build-result.json`/`build.log`、`installation-setup.json` 及六份安装日志保留真实命令/exit/SHA。`lifecycle-01.log` 为空，exit 和结构结果独立保存。

下一步由根协调器：对这个精确 support 候选及新的 G08/产物适用性作独立 Standards/Spec 增量核验；确认后精确暂存 7 路径，提交/推送支持分支并回读 head，追加 commit 与当前 tree 的等价关系。此执行者没有采用台账、写远端、提交或推送权限，已停在该技术交接点。
