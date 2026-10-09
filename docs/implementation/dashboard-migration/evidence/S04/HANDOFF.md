# S04 修复后候选 r4

Spec r3 的新 P2 N1 已真实复现并修复，既有 F1/F2 的约束保留。当前等待 root 对同一固定 tree 独立复审；全部源项和整体 AC 仍为 **NOT RUN**。

- Worktree：`/Users/nineofour/.codex/worktrees/dashboard-s04/Agent-Alfred`
- Branch：`codex/dashboard-s04`
- Base：`77a51d08917dd37de9c98ae677aa7196b043d4e0`（integration 未变化，无需重复 merge）
- Head：`267a17ea73e353c6425e004c47e06fb319f49c83`
- Tree：`0de2736d42da93209172cb3d05b3796088ca1072`
- 当前工作树 clean；本轮仅本地 commit，无 push/PR/tracker/main merge。

## N1 修复与真实证据

历史五条在成功读取时固定的规则没有改变。新增私有 `visibleRuns` 只派生可见历史和 current＋history 的显示身份集合，不从第六候选补位。历史与当前卡使用同一完整 ID 集合判断短 ID 碰撞；当前槽的绘制签名包含 peers ID，且在成功来源已接受后重算，所以当前 Run/recording 状态不变时，新 peers 仍能更新当前链接标签。

扩展原 recorded-overlap 真实 Host/SQLite/HTTP 用例。新增 fixture 命令只把已有历史 Run 的合法不透明 ID 设为与真实当前 Run 相同的前十二字符；没有改生产分类、读取或返回预设结果。首轮 recent 网络失败、真实保存屏障、native EventSource idle 暂缓沿既有测试边界控制。

- `r4-red-visible-id-collision.log`：修改生产前 FAIL，当前链接仍显示冲突短 ID；截图、trace 和输入 hash 保存在 `r4-red-visible-id-collision/`。
- `r4-green-visible-id-collision.log`：窄修复后 1 PASS。
- `r4-affected-browser.log`：当前槽/普通完成、来源焦点、历史长 ID/布局、recorded-overlap 共 4 PASS（6.8s）。
- 随后强化同一个 recorded-overlap：先真实读取确认当前已 recorded 且短名无冲突，再只变更历史 peer 输入并显式读取；当前 identity/recording 不变，标签必须扩展。`r4-stable-current-peer-refresh.log` 1 PASS，替代上一条四项结果中的 recorded-overlap，其他三项输入未变。
- 该用例还断言可见六个链接名称唯一、完整 href 不变、当前槽释放前后历史五条 ID 不变且 GET 增量 0；只有用户显式刷新才纳入原当前 Run，GET 恰增 1。
- `r4-typecheck.log`、`r4-ruff.log`、当前 `git diff --check base HEAD`：PASS。

独立问题来源为 `../reviews/S04-r3-Spec.md`、`S04-r3-Spec-details.md` 与 `S04-r3-collision-repro.log`。原 F1（历史成员）和 F2（准确来源焦点）没有回退；没有读取本轮 Standards 结论。

## 固定候选与复用适用性

`candidate.diff` 为当前 base → r4 的完整九文件差异；`r3-to-r4.diff` / `r4-fix-only.diff` 是本轮窄修复；`r1-to-r4.diff` 保留完整修复与 S01 三测试同步路径。

`candidate-hash-impact.json` 给出原九文件 r1/r2/r3/r4 hash：相对 r3 **6 个文件不变，3 个变化**。唯一生产变化是 `overview.js`；另两个是现有浏览器用例和 Python fixture。共享 app.js/projection/readGapRevision、HTML、CSS、assets、registry 和后端均未改变。因而共享 shell/MainBar/Ops、原 40 Python、20 GET/HEAD assets 和未受影响的十项 Overview 证据继续复用；受影响四项用上列 r4 证据替代。不把多轮数量相加称完整 CI。

`source-coverage.json` 按原 39 source IDs / 22 AC 保留 hash、owners 和责任，更新本候选与碰撞修复关联；整体结果仍 NOT RUN。`candidate-files.json` 9/9 文件 hash 与当前源码一致；`resource-inventory.json` 从 HTML → import → CSS url 反查并核登记/bytes/MIME，20 资源通过。详见 `r4-evidence-validation.json`、`checks.json`、`failure-chain.json`。

r3 全部交付物、日志、附件已封存于 `r3-candidate/`（103 文件及 `archive-sha256.json`）；r1/r2 归档与所有 first FAIL 均保留，不 amend 原固定提交。正常视觉与长 ID 参考截图位于 `screenshots-r4/`，来自当前生产代码的受影响检查；CSS 未改。

## 边界与后继

运行环境仍为 macOS 27.2 arm64 / Python 3.14.7 / Node 22.23.2；隔离端口 17830、fixture +3/+4，临时 SQLite/Host 与离线 ScriptedModel。没有真实外部模型或用户业务数据。

完整实现、SourceContext 和回退规则见 `r3-candidate/HANDOFF.md` 及其引用；本轮只修正跨 current/history 的可见短 ID 区分。来源 schema 和完整 href 保持，历史五条仍只在成功显式读取时选定。

待 root / S11：独立复审，S03/S05/S09 两端来源组合，G01/G02/G04/G05/G07，全量 CI、包与四组隔离安装、升级回退、最终默认入口，原生 200% 缩放和中文 IME。移动仅 Chromium 模拟；iPhone/iOS/真实软键盘 excluded_by_user；历史 BFCache BLOCKED 保持。
