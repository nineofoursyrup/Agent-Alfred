# S02 r5 壳层、唯一 MainBar 与公共呈现交接

固定候选 `5ceee180cf9f54f336017fb88998d2b11f91a186`，tree `2b90301c4ea0bea96a1b2ca377163177c86def75`；集成 base 仍为 `a4168881bd9057a900e8d353002307adff714c42`，最初 base `7d87243ff715eaecc42159f35aa9f223c4ef8ce7`。分支 `codex/dashboard-s02`，工作树 `/Users/nineofour/.codex/worktrees/dashboard-s02/Agent-Alfred`。本轮提交 `5ceee18`；没有新增 merge，没有 push、tracker 写入或整合到 integration。

r4 的 `HANDOFF.md`、source/resource、PAGE-PORTS、生成脚本和校验结果已原样归档 `r4-candidate/`；r3 仍在 `r3-candidate/`。当前片内检查 PASS，独立 r5 Standards/Spec 等 coordinator 增量复核；所有整体 source/AC/迁移组合仍保留 NOT RUN。

## r5 空身份兼容修复

冻结 #89 D03（DESIGN.md:50）和真实 S01 guarded HTTP 允许历史合法空 Session/Run ID，缺参数才是缺身份。独立发现的恢复未读按钮因 `if(session&&unread)` 对空 Session 不发请求已修复，改为 null sentinel 判断。同一 S02 MainBar 接缝检查另修：过程缺口 Run 链接按 string 存在性判断，消息 `data-run-id` 保留空字符串。产品仅 `app.js` 三行判断；不改变 csrf/action_id/process identity、忙态、确认、发送或保存协议。

检查 app/shell/stream/progress/notices 的全部 Session/Run 判断与 URL/Map/DOM 转换，详细结论见 `EMPTY-IDENTITY-AUDIT-r5.md`。已有 stream URLSearchParams、session draft、selectSession、定位响应与历史身份合并都用 null/string/严格等值，无需其他产品修改。

真实回归经隔离 Host 完成实际 Run，再在 fixture 持久数据边界准备合法历史空 ID；只 abort 初次普通 history IO，使未读需核对。核对按钮随后走真实 S01 HTTP/Host/SQLite，断言完整的 `session_id=`/`run_id=` 参数、目标正文与已保存、保留空 ID 消息身份与草稿、一次定位 GET、零 POST。普通 history 失败不会被精确定位冒充连续历史；显式同 Session 回读后才核验回到最新的 unread 确认。

| 本轮证据 | 实际结果 |
| --- | --- |
| `empty-identity-first-fail.log` / artifacts | **2 FAIL** 首次证据：空 Session 点击核对请求数0；空 Run 缺口链接丢失。 |
| `r5-mainbar.log` / artifacts | **23 PASS / 1 FAIL**；修复后的主对话/真实组合/recording/streaming 等通过，新增 empty 测试在刻意失败普通 history 后过早要求“回到最新”清 unread。保留此失败；测试补显式同 Session 普通历史回读，产品未弱化 unread 确认。 |
| `r5-empty-successor.log` / artifacts | **11 PASS**；真实空 Session/Run、真实 S01 组合、精确定位、缺口 absent/empty 区分、preview、recording identity/unknown 等。 |
| `r5-empty-http-contracts.log` | **2 PASS / 13 deselected**；S01 原有真实 guarded HTTP 空/特殊字符 ID 契约。 |
| `typecheck-r5.log`、`ruff-r5.log`、`git diff --check` | PASS；r5 未改依赖、原有 schema/服务行为和共享 fixture 默认路径。 |

下面 r4 及更早的确定性 PASS 只在未变责任范围内复用，不把分轮计数相加为完整 suite。r5 唯一测试 fixture 新命令仅准备已完成调度后的历史 ID，不替换生产定位、保存、分类或返回内容。

## r4 修复的保留与适用证据

1. **Run 缓存恢复所有权**：`restoreRunPage` 持有原 Run controller 和 shell generation；真正离页的 dispose 立即退休资格，重连首状态只重建仍匹配的原 owner。`/runs → synthetic persisted pageshow → /models → 新草稿 → 迟到首状态` 不再重建 Models，原 DOM、焦点和新草稿保持，下一次实际离页仍有 dirty 守卫。原页面的 export 恢复能力仍通过。首次失败：`cache-owner-first-fail.log` 与 artifacts；独立源证据 `reviews/S02-r3-Standards.md`、`S02-r3-standards-probe.{cjs,json}`。
2. **漏收保存补丁后的核对**：同实例首个 idle 重连时，对先前 pending 标记“记录状态待核对”并发起当前 Session 只读历史核对；idle 自身不推断 saved。接收真实持久 `run_pair` 后，仅匹配 Session/Run 的 reply 与 terminal summary 推进到 recorded，解除旧 pending。其他 Run 的持久记录不会把目标标成已保存；明确再次读取也沿同一合并规则。首失败：`recording-recheck-first-fail.log` 与 artifacts；独立源证据 `reviews/S02-r3-Spec.md`、`S02-r3-Spec-details.md`、`S02-r3-recording-repro-{before,after}.log`。

r3 已修的 Models A→提交B→编辑C→B回执→A dirty、回B clean，以及聚合 NoAction 两种原因/“历史聚合记录”、request preview 完整性、hidden 错误、阅读/未读身份均保留并回归。

## 共有实现和接缝

- `shell.js` 唯一 registry、导航/dirty、版本化单层面板历史、page controller 生命周期；旧九页独立 root；显隐不 dispose，真导航才释放。232/320 wide rails、1100 breakpoint、narrow panel 互斥。同地址不重建；Back/Forward/reload 与异步 scroll 恢复均有实际浏览器证据。
- `app.js` 唯一 MainBar/Stream，首个 Host 权威状态后挂页。保留发送/忙态/记录/重启协议；每标签/Session draft 与安全存储、迟到响应焦点归属、keyed 消息身份和显式回最新。hidden 状态保留 outcome/recording/过程缺口/读取错误；unread 只按同一 Run 完整末尾及记录状态可见确认。
- 公共 `dashboard.locateReply` 使用 immutable instance/Session/Run/action、单次只读定位，普通 history cursor 独立；preview/full 与 recorded 单调，退休请求不得夺焦/回写；只读 retry 不提交 Run。r4 已合入真实 S01，新增集成测试没有替换定位/分类/去重/保存逻辑。
- 旧九页 dirty/source/dispose 接缝、Memory/Database 隐藏保护、原动作回执归属维持；S08 持久清空修复仍单独负责。页面接入端口见 `PAGE-PORTS.md`。S04 添加真实 Overview，S11 最后切 `/` 和品牌入口。
- `resource-inventory.json` 从 HTML → static JS imports → CSS url 闭包反查登记，18 文件、38 边全部登记，绑定最终 head/tree 与每文件 hash。`source-coverage.json` 包含全部 164 个 S02 source IDs、25 AC 的原身份/hash/owners、具体片段证据及待组合责任；不会把本片 PASS 提升成源项或 AC PASS。

## 继续适用的 r4 检查

环境沿 r3：macOS 27.2 (26B5101f)，Python 3.14.7，Node 22.23.2，Playwright 1.63.0 Chromium，dev+mcp locked deps。隔离临时 Host/SQLite/worker/离线模型；17830–17839 端口，没有真实 provider/付费执行/用户业务状态。S01 merge 未改依赖锁。

| 证据 | 范围与结果 |
| --- | --- |
| `r4-owner-recording.log` | **26 PASS**；S01 merge 前的新 owner/真实漏收保存回归、全部 shell 和真实 trace export，包括原页 cache 恢复。用于修复直接后继，依赖组合以合并后结果为准。 |
| `r4-integrated.log` | **45 PASS**；合并 S01 后 MainBar/定位/聚合/阅读、shell/表单、发送、streaming、真实 recording/restart/tabs。新增真实 S01 持久记录和 pending projection 定位、同 Run 合并、跨 Session draft，以及“不匹配 Run 不得判 saved”。 |
| `r4-read-contracts.log` | **50 PASS**；合并后 `test_dashboard_shared_reads.py`、`test_reply_recovery.py`、`test_dashboard_assets.py`。 |
| `r4-read-pages.log` | **44 PASS**；合并后 runs/inbox/pagination/trace export 受影响消费者，包括真实原生 ZIP 下载、cancel、保护/过期、离页清理和原页缓存恢复。 |
| `typecheck-r4.log`、`ruff-r4.log` | PASS；`git diff --check` PASS。check-skills/env 复用 r3 未变配置输入的 PASS。 |

执行命令和选中的文件在各日志开头；浏览器命令使用 `PATH=/opt/homebrew/opt/node@22/bin:$PATH ALFRED_BROWSER_TEST_PORT=17830 npm run test:browser -- ... --output=<单独证据目录>`。只有未变范围复用以前确定性结果。

历史 **browser-r2 351 PASS / 18 FAIL**、browser-r3 successor **16 PASS / 1 FAIL**、final-targeted-r1 **50 PASS**、r2 **47 PASS** 均保留，不相加冒充最终完整 browser suite。旧 FAIL 的映射、命令失误/环境错误和修复历史详见 `r3-candidate/HANDOFF.md`。本轮两个 first FAIL 也保留，修复不删除原记录。完整 Python、最终构建及四组安装仍由 S11 执行，不把 source HTTP 当安装后 HTTP。

## 四张未跟踪截图保全

只处理 parent 指定的 `tmp/agent-work/issue-76/{narrow,statistics}.png` 和 `issue-27/{behaviour,aggregation-combinations}.png`。先复制到 `generated-png-r3/`、核对目标与来源 SHA-256 相等、落盘 `manifest.json`，再仅 unlink 四个已保全来源文件。记录原路径、目标路径、大小、mtime、两端 hash 与观察候选 r3；**原生成运行/生成候选无法确认，因此不是绑定 r3 的 PASS 截图证据**。未 clean 或删除其他路径。

## 待组合范围与真实缺口

- 已完成：真实 S01 公共记录/投影读取与 S02 逻辑端口直接组合。仍 NOT RUN：S03 UI 入口、很早 Session/Run 来源锚点、完整长短历史与跨页返回；不能宣告完整 AC07。
- S03–S10 迁移后的页面 source anchor/680 模式/字段完整性、回执及保护，及 S11 G01/G02/G03/G04/G05/G07 两端组合仍单独验收。所有 source 全责片段与组合未齐前整体 NOT RUN。
- S11 最终十页四视口、默认入口、全部 309 source、最终CI、wheel/sdist × base/mcp 字节和HTTP、升级/支持回退仍待验。
- 原生 Chromium desktop zoom 200%、真实中文 IME **NOT RUN**；当前 viewport/字体放大/合成 composition 只为补充。移动只报告 Chromium viewport/touch 模拟；iPhone/iOS Safari/真实移动软键盘 `excluded_by_user / NOT RUN`。
- 历史真实 BFCache **BLOCKED** 保留，no-store 未改；本轮 synthetic persisted owner 回归不证明实际 BFCache 命中。
