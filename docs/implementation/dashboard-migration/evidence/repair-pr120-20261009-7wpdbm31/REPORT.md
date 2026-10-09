# PR #120 四项 P2 修复记录

日期：2026-10-09（Asia/Shanghai）。工作树：`/Users/nineofour/.codex/worktrees/dashboard-integration/Agent-Alfred`。

结果：四项 P2 已修复；相关检查通过，独立局部复核 Standards 0 / Spec 0。真实中文 IME 仍为 **BLOCKED**，本记录不宣称整个 PR 或 S01–S11 产品验收完成。

## 候选与范围

基线及当前 HEAD 均为 `11dcc66f18ceb264ade1c1bcc1b48287dc4f1516`；本轮改动为未提交的 7 个文件，包含新增 `tests/browser/run-purposes.spec.js`。未提交、推送、合并、关票或发布。GitHub 只读核验 PR #120 仍为 OPEN / Draft、远端 head 同基线。

开始时工作树和 index 均干净，详情见 [INITIAL-STATE.md](INITIAL-STATE.md)。最终 tracked diff 见 [final.diff](final.diff)，完整文件副本在 `final-candidate/`（含未跟踪测试）。[final-manifest.json](final-manifest.json) SHA256：`274775393b6f209350e45f2920b8abb5daa814f31160246421fbe84d1c822984`；已逐项核对当前工作树文件与该清单一致。

## 修复与回归

| Finding | 修复 | 直接验证 |
| --- | --- | --- |
| SP-120-02 连续定位污染普通历史 | 每条回复保留 `locationOnly` 来源标记，仅由精确定位取得的片段只在对应目标展示；普通补页或真实 SSE/projection 可独立将同身份记录纳入普通呈现。 | 原有 25 条历史先定位 A、B，再回到最新仍为 25 条；补页和新 SSE 交错后合法记录保留并去重。 |
| SP-120-01 断连迟到定位 | SSE error、offline 及已有恢复同步入口调用 `retireLocation()`，递增请求代次并取消在途读取；旧请求不能在重连后恢复资格。 | 两个断连入口均在 offline 阶段得到 retired；重连后新显式定位可用，迟到旧响应不覆盖它。保留初始同步、空 Session/Run ID 及跨 Session HTTP 先于新 SSE 快照的合法定位。 |
| STD-120-01 unknown unpin 锁编辑 | 区分在途锁与未知回执；只有新的、同实例同代次、成功且连接已同步的模型读取确认仍 pinned，才释放未知 unpin 编辑锁；回执和草稿保留。 | pending 仍锁定；迟到旧读、离线读、失败重连读均不解锁；显式成功核验恢复显示名、线路和价格编辑；旧写回执不覆盖新草稿，无自动重送。 |
| STD-120-02 inference_probe 映射 | Run 公共字段和 Overview 都以实际值 `inference_probe` 映射“推理探针”，保留未知用途分支。 | 四个闭集用途及 future-purpose 在 Overview、Run 列表、Run 详情中正确区分。 |

## 验证

- `npm run typecheck`：PASS（最终代码）。
- `git diff --check`：PASS；index 未改动。
- 16 个 browser spec、114 个去重用例已有适用于最终改动的 PASS；详见 [validation-summary.json](validation-summary.json)。未受后续改动影响的确定性结果沿用，定位生命周期在移除过严检查后重新验证。
- [green-models-mainbar.log](green-models-mainbar.log)：28 PASS，其中 Models 16 项在最终代码上仍适用。
- [green-focused.log](green-focused.log)：MainBar reading 两项 PASS 可沿用；此轮其他失败保留，不将整轮标为 PASS。
- [affected-browser.log](affected-browser.log)：扩展范围 80 PASS、3 FAIL；失败的空身份两项和用途一项已分别修复并重跑，原日志保留。
- [green-location-final.log](green-location-final.log)：最终定位、真实空身份和真实 HTTP 集成共 16 PASS。
- [green-purposes-final.log](green-purposes-final.log)：用途跨三处渲染的最终回归 PASS。
- 未重跑完整 Python suite、全部浏览器 suite 或分发包构建：本次仅修改前端 JavaScript 和回归测试，无后端、依赖或打包规则变更。
- 所有验证使用隔离离线 fixture / 受控 HTTP-SSE 边界；无真实 provider 调用、无用户业务状态操作。中文 IME 未作真人原生验收。

## 首次失败与复核

[red-browser.log](red-browser.log) 及对应 trace/screenshot 保留了修复前的多目标历史污染和 offline/SSE error 未退休问题。首轮修复的 blanket `connected` 检查又引入首次/跨 Session 同步期间定位回归，由扩展测试及独立复核发现；最终移除这些检查，使用真实断连事件驱动的请求代次退休，新增跨 Session 时序回归并通过。

测试夹具的普通历史初始 cursor、错误返回链接与缺失 source envelope 也曾造成失败；已修正夹具边界，用途测试改为直接读取各 Run 详情，不把来源恢复夹具问题误判为生产用途映射失败。Models 初版两个 TypeScript 可空性错误已修复。

独立复核见 [REVIEW.md](REVIEW.md)：限定本次四项 P2 及直接回归，Standards 0、Spec 0、可选 smell 0。复核报告出具时未引用最后一轮用途运行；该运行现在已 PASS，结果在上面的最终用途日志中。
