# S11 CI r1 — 时序、合同和责任映射

所有时间为相应 Playwright trace 的 monotonic 毫秒，只在同一 trace 内比较。没有新增复现或 instrumentation；代码路径推断与已观察结果分列。源合同使用固定候选中的冻结原文。

## 1. Inbox：未采用的继续操作不等于草稿丢失

PR trace：A=`6aaeec54bcc045e2984d002125975800`，B=`c75f65d57e1542d79afe42651d8aacad`。`264339.599` 开始 B 的精确定位；`264354.243` 测试调用继续 A；`264360.071` DOM 中出现“连接尚未同步，请等待状态核验”。此后没有 A 的 `/api/mainbar` 请求。A 草稿在 reload 后的检查已经通过。

Push trace：A=`5fdafc9b424e468a81dfc0a03e3d2cc7`，B=`cfd7f43d98424c32a3a826ec89b89e73`。`237592.404` 普通 B 历史、`237592.628` 精确 B 定位；`237615.321` 测试继续 A；`237621.382` DOM 出现相同的未同步错误。此后没有 A 的历史请求。最后输入为空，符合仍处于 B，不能证明 A 的存储被清除。

固定 `app.js:138–150,803–809` 明确拒绝未同步的 continue；`inbox-run-navigation.spec.js:106–109` 的 evaluate 包装忽略结果，末尾直接检查输入。需要等待目标 B 的当前 Host 状态已经接受、显式继续 A 成功，再核对实际选中 A、A 的 sessionStorage 草稿和可见输入。仍应覆盖未同步时拒绝、不排队自动切换的行为，不能放宽守卫。

合同：#87 R05（93 行）“刷新后先恢复身份／输入，再核验服务端”；R07（121 行）明确继续才切换、恢复对应草稿且沿用限制；#89 D08（150 行）被阻止保持原选择和输入。直接源责任为 `issue-87:R05/R07`、`issue-89:D04/D08` 的 S02／S03 浏览器组合；G01 末尾失败保留。不是 S01 数据删除或草稿跨 Session 污染的已确认缺陷。

## 2. reconnect：计数窗口与旧刷新重叠

PR trace：初始普通历史 `352452.700` 起、约 `352498.047` 完成；测试 `352462.681` 发出 `run.finished`；计数 route `352498.193` 安装；offline/online `352501.425`；被计数 GET #1 `352511.035`，返回 `other-recorded-run`；reconnect state `352514.266`；GET #2 `352517.378`，返回目标 `r1`。第一个 GET 早于本次 reconnect snapshot 是确证。

`app.js:391–477` 的在途历史读取会保留 refresh 请求，`623–646` 的正式终态事件触发历史刷新，`548–552` 的首次 idle snapshot 对 pending 状态发起核对。因此旧刷新进入新计数窗口是合理、尚待确定性屏障验证的原因。此 trace 没有证明 `other-recorded-run` 被误合并为 `r1` 已保存；最后已保存发生在 fixture 第二次返回 r1 后。

应先完成 setup 的真实终态／相关读取，再只计明确 reconnect 动作的读取；保留“其他 Run 不证明 r1 已保存”和“实际 r1 权威回读才解除未核对”断言。不得把预期改成 2、删除精确身份断言，或全局延迟掩盖竞态。

合同：#87 R08（136、143 行）同身份合并及内容／持久事实分开；R09（152、159 行）无权威结果待核对；I05（127 行）同身份旧 pending 不降级已确认记录。`issue-87:R08/R09`、`issue-89:D08` 的 S02 reconnect 行为需后继有效证据；此首败本身保留 FAIL，不能把已保存单调性直接判为产品 FAIL。

## 3. 三条新建失败：同一 Mainbar 呈现应单独验证

| 用例 | POST 开始／持续 | Mainbar toolbar click | 已观察结果 |
| --- | --- | --- | --- |
| aggregation.spec.js:491 | 122960.117／111.434 ms | 122962.638 | 未采用新 Session，显式 created receipt，消息框禁用 |
| database.spec.js:21 | 230451.615／79.077 ms | 230454.355 | 同上 |
| integrations.spec.js:64 | 302747.994／365.033 ms | 302750.298 | 第二个 Tools 标签页在 303524.150 出现同样 receipt，未选择 Session |

`app.js:201–204` 将所有 pointerdown 和大多数 focusin 计作 creationIntent；`863–893` 在回执到达后用其变化拒绝自动采用。`shell.js:157–168` 的打开 Mainbar 本身还会移焦标题或输入。这三条真实时序已经观察到 POST 201 前只有该呈现路径就导致成功创建未被采用。

冻结合同：I06（138 行）openPanel“仅改呈现”；#87 R03 把呈现与会话选择分开；#89 D04（66 行）“点击新建成功后打开新 Session 的 MainBar”。合同没有逐字规定所有 pointerdown 均代表放弃新建。由此得到的有界解释是：同一 Mainbar 的打开动作，若无会话改变、业务导航、新编辑或独立阅读意图，不宜独自撤销原创建。应作为产品窄风险验证，不能只把三条测试改为等待 201。

已读完整 `session-create-race.spec.js`：已有真实服务＋受控成功回执覆盖新编辑、其他标签真实 Run、进程更换、首次挂载期间导航／主动 Tab、窄窗回执和键盘可达。修复须保留这些保护，并用同样真实 POST 201 屏障加入“仅打开同一 Mainbar”路径的行为证据。

直接责任：`issue-87:R03/R07`、`issue-89:D04` 的 S02／S03 创建消费；对应 AC04。三条在消息提交前失败，不能归因聚合记录／SQL join／搜索错误优先级，亦不能用这次未执行步骤否定独立、仍适用的 AC17/18/20/21/22 或 G04/G05 局部证据。

## 4. 空身份：服务器正确，早期 UI 消费未收敛

Push trace：reload 后普通 `/api/mainbar?session_id=` 于 `307278.810` 开始，按测试要求被 abort；核对点击实际执行快照 `307311.196` 时连接仍未同步。`307316.958` 的 `/api/mainbar/locate` 含 `process_instance_id=dd0626fefa3740dc94a5d89447c1a991&session_id=&run_id=`，HTTP 200，2.644 ms；实际 JSON 中 session/run 都为空，完整用户块、`reply_text=离线模型回复`、`recording_state=recorded`、`history_contiguous=false`，均可核验。一次 abort 没有误命中 locate。

最终 Inbox 已挂载，目标仍“正在定位指定记录…”、0 个 `[data-run-id=""]`；草稿仍为“空ID会话草稿”。`app.js:262` 在同步前暴露可点击核对；`289–300` 持有 shell.generation，若发生变化直接返回 retired；`shell.js:175–181,280` 首次挂载也推进 generation。初次 mount 与读取应用竞争是强候选原因，但没有记录该条件的运行时值，不能写成已证实分支。无论根因，允许用户动作后无限保持 loading 的本次可见失败需要后继处理。

合同：#89 D03（50 行）缺参区别合法空 ID；D08（149、157、163 行）有界精确读取、字段／读失败分别表达、有效动作与退休责任；#87 R09（163 行）刷新后已知未读仍可核对。S01 空 ID HTTP 合同继续有实际成功证据；S02 核对动作及首次同步的 UI 消费／退休收敛责任暂停 PASS。不能靠跳过空 ID、删除 initial ordinary-history failure 或断言存在按钮来闭环。

## 5. 建议的结果写入边界

- 确定 FAIL：当前 CI gate、AC25；7 次原始失败观察全部保留，G01 当前组合终点失败；AC23 不能因旧局部绿灯继续整体 PASS。
- AC04／AC06／AC07：对应上文浏览器子责任挂接本次 FAIL，并暂停无条件整体 PASS；“产品根因未确认”保留在诊断字段，不能把实际执行失败改为 NOT RUN。被覆盖的旧 PASS 是历史证据，不能删除。
- source row 只在相关 owner／场景追加失败观察，不把同一源项 S01 后端成功或其他仍适用片段一并改成实现失败。上文给出精确原始 source IDs；不改原 frozen acceptance-map。
- NOT RUN 仅适用于三条 setup 失败后确实未到达的领域步骤；不用于把整条测试 FAIL 藏掉。G04/G05 既有独立证据可保留其原范围，完整 gate 仍 FAIL。
- AC03 真实 IME BLOCKED、历史 BFCache BLOCKED 与本次 CI FAIL 独立；本地 527 PASS、Python／安装矩阵 PASS 不冲销远端浏览器失败。
- 本报告不判定修复成功。后继先验证上述独立行为／屏障，再由原 owner 决定必要回归、固定候选和评审；不要求机械重跑所有历史确定性证据。
