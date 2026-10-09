# S11 窄语义修复 Spec 审计详情

审阅只使用固定 Git 对象、既有交接与日志／trace，未读取可变 S11 工作树或当前 Standards 结论，未写产品、Git、冻结合同、canonical ledger 或 GitHub。主报告的 PASS 只覆盖其明列修复单元。

## 合同与实现

| 要求 | 固定源码核查与结论 |
| --- | --- |
| #87 `SPEC.md:110–113`：「由原页面判断此次离开是否会丢失尚未保存的用户输入」；「提示须区分未提交输入与已提交请求」。I06 `INTERFACES.md:139` 同样区分两者。 | `models.js:33–34,81,219–226,307,356` 统一以待确认请求的 submitted 为未提交输入参照；其他状态仍比较 baseline。`memory.js:1394–1410` 在存在编辑提交身份时对比冻结 request，否则维持普通 dirty。填写旧保存值 A 不会掩盖已提交 B 之后的新意图；重新填写 B 不会产生虚假未提交警告。 |
| #91 `DESIGN.md:64–68`：「请求固定点击时的模型身份、字段、值和预期 revision」；「由用户核对后明确采用当前版本再提交，不自动重试旧写入」。 | `models.js:147–193` 保存时捕获 value、revision 与 request identity，submitted 不取代 baseline/revision。明确非成功返回清除 submitted；不可信回执／连接中断继续 unknown。核验过的成功响应才更新实际字段基线。`247` 的显式采用动作清除 submitted、采用当前 revision，但仍不提交。原 comparison 的 generation/instance/read-sequence 资格未改。 |
| #90 `DESIGN.md:49–54`：「回执不应用到新目标，也不能清掉提交后新增的输入」；`80–84` 保持 expected_version 与记录保护。I08 `171,174` 保持原 operation／目标。 | `memory.js:1164–1207` 保持原 request、operation_id、expected_version、编辑对象比较。新 predicate 不改命令、回执、删除／失效协议。明确返回清除 editAttempt；unknown 保留恢复身份。成功后当前值不同于原提交时保留输入，以实际提交值及回执 record_version 更新编辑基线。 |
| #87 `111,113` 默认留页／Esc 取消、旧响应不夺焦；#91 `72` 拒绝旧页面／实例／请求回填。 | 原 shell 守卫与 settings-focus 未改。STD02 successor 在真实提交后加一行新输入，再执行既有放弃离页与新页面焦点断言；未删弱原断言。 |

路径均相对仓库，产品文件位于 `src/agent_alfred/ops/static/`，规格位于 `docs/design/issue-*` 或 `docs/design/dashboard-implementation/`；行号来自修复／测试 successor 固定对象。

## 实际行为证据与适用性

- 新 Models 两用例（`models-migration.spec.js:22–44`）经真实 `/api/settings`，只在收到真实响应后延迟／丢弃交付。A 先真实保存；B 请求只提交一次且绑定 `expected_revision=1`。填写 A 后离页默认焦点为留页，Esc 保留 A；unknown 分支显式读回当前保存 B 仍保留 A。恢复为 B 可直接离页，无对话框；GET `/api/models` 核实持久字段 B，断言只一个 B POST、零模型调用。
- 原 SPEC F1 和 STD01 用例保留 stale revision、读失败／过早读无资格、明确采用后才以新 revision 重发、旧实例／迟到响应不污染 successor；相关用例在 `submitted-green.log` 为 PASS。各字段独立保存、清空、失败 unpin、探针不代存／不自动重放原覆盖仍通过。
- Memory `memory-migration.spec.js:102–143` 使用原真实 `memoryServer` 与带真实入口 CSRF 的 command helper。持久版本 1 A → 请求 B → 后续输入 A，先验证留页取消；原回执到达时仍保留 A 与焦点，独立 GET 核实 B；后续显式保存才产生版本 3 A。现有 `402–428` 另验证提交相同值可离页、只一个写请求、原回执仍可查。普通 dirty 目标切换、拒绝／取消、删除核验等既有 Memory 用例亦在 24 PASS 中。
- Fixtures 使用实际本地 Host、HTTP、Settings/Memory 服务和 store；仅外部目录／模型为离线实现，不是线上 provider 验收。后端、锁文件、配置和 fixture 相对 accepted base 没有修复变更；机器文件核对了 15 个适用性输入。

## 原始失败与 successor

四份 log 全文已读且 SHA256 与 `semantic-repair-evidence.json` 一致。原 3 FAIL 都停在预期真实离页 guard 不出现；不改写为成功。第一次修复运行是 **24 PASS / 1 FAIL**，原 STD02 trace 精确显示第三次保存后未产生新输入，已点击 Connections，再等待不应出现的「放弃并离开」；保留其失败与 trace。

`0758e46` 仅在第三次保存后添加 `input.fill('new unsaved input before retirement')`。后续 3 PASS 覆盖两个新 Models 回归与 STD02，保留 refresh/action/MainBar/new-page 焦点、3 次 POST 和零模型调用断言。产品两模块与第一次修复运行一致，故复用其余 24 项适用结果；不声称另跑过一整轮 25 项绿色套件。

原 Memory RED trace 的测试源比固定修复源少两行后续 focus 操作／断言；差异在机器文件完整保存。它们位于原失败 guard 断言之后，属于加强焦点验证，没有改变 RED 触发路径。两份 Models RED 与 STD02 失败 trace 的测试源均字节匹配修复对象。

成功用例未保留 trace；现有失败 trace 中静态 JS 响应未附正文，故不宣称拥有运行时 JS 哈希。证据采用固定源码、原始日志、trace 测试源及输入／环境适用性组合。日志所述 Node 22 / CPython 3.14.7 不提升为本审阅者另行实测的版本。

首轮审计曾错误断言 shell 在排除的入口 commit 前后也字节相等，已保留在 `S11-semantic-Spec-first-comparison.json`。实际 A→修复的 shell 不变；修复→测试上下文只有根 `/` 路径判定／规范化变更。本组从 `/models`、`/memory` 进入并使用显式非根路由，未经过该差异条件，故不妨碍本单元复用；入口变更本身留待最终 S11 review。

## 裁决边界

未发现修复单元缺失／部分满足／越界的要求；无需新增产品 probe 或重复已适用检查。支持回滚闭包必须由 root 在两轴结论与实际安装／状态验证后裁决。本报告不提升最终入口、四安装、升级／支持回滚、309 source、26 AC、12 MCE、G01–G08、原生 zoom/IME 或 BFCache 状态。
