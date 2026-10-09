# PR120 四项修复独立 Spec 复核

结论：Spec finding **0**（缺失／部分 0、未要求扩展 0、错误实现 0；无优先级）。仅审本轮四项修复及直接回归，不是整个 PR 的重新批准。

候选：worktree `/Users/nineofour/.codex/worktrees/dashboard-integration/Agent-Alfred`，base＝HEAD＝`11dcc66f18ceb264ade1c1bcc1b48287dc4f1516`。最终 manifest SHA256 `274775393b6f209350e45f2920b8abb5daa814f31160246421fbe84d1c822984`；7 文件完整路径、hash 和变更集合首尾均一致，no-drift。逐文件证据见 `candidate-verification.json`。

| 修复 | 核对合同与实现 | 复核边界 |
| --- | --- | --- |
| SP-120-02 | INTERFACES.md:127–129；issue-89/DESIGN.md:159–163；app.js:327–328、350–355、439–442、573–576、643–646 | 连续定位分别保留身份，只有当前目标进入定位呈现；普通历史和 live 正式回复独立接纳同 Run，回到最新清除纯定位呈现，普通游标不受定位改变。 |
| SP-120-01 | INTERFACES.md:21；AC06；app.js:297、304–336、678–689、856–879 | navigation、SSE error、offline、实例/Session 替换退休在途定位；异步结果须匹配实例、Session、代次与导航所有者。合法跨 Session 待首个 Stream snapshot 状态没有被粗略 connected=false 拒绝。 |
| STD-120-01 | issue-91/DESIGN.md:66–72；models.js:70–72、117–141、194–201、232–235、347–373 | 未确认 unpin 保留回执；旧读、离线读、失败读不能解除核对锁；当前连接上后发起并读到 pinned 的匹配读取可恢复编辑。旧请求／实例不能清新输入，不自动重投。 |
| STD-120-02 | issue-91/DESIGN.md:98；contracts.py:50；overview.js:264；run-fields.js:40–48 | 四个已知用途采用真实 inference_probe 枚举；未知用途仍保留未知。 |

复用原始确定性结果：`green-location-final.log` 16 项、`green-models-mainbar.log` 中 Models 16 项、`green-purposes-final.log` 1 项，共 33 个相关绿灯；未重跑。读取用例实现确认受控 HTTP/SSE 与真实 Host/SQLite 集成各自边界；没有把合成传输当原生验收。独立运行 `git diff --check` 通过。未读取修复者 REPORT/REVIEW 主观结论，未修改仓库、GitHub、提交或推送。

已知真实中文 IME 缺证仍保留，不记作新的代码 finding，也不宣称整个产品已验收。
