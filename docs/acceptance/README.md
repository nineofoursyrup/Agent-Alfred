# v1 阶段 A 验收证据

入口：`python -m agent_alfred.evals.acceptance`。当前可执行的准备、模拟、导入和报告示例均为离线操作；下文保留的在线命令仅说明历史格式，当前不可执行。`offline_engineering` 与 `v1_release` 独立。缺真实质量证据、批准阈值、正式金标、当前平台门禁或逐项需求证据时，发布保持 `BLOCKED`。已证实违规优先显示 `FAIL`，同时保留所有 blockers。阶段 A 完成不等于 v1 发布通过。

## 离线完整使用路径

在隔离工作树安装锁定依赖；使用已验证支持共享 SQLite C API 的 CPython 3.14：

```sh
uv sync --python /opt/homebrew/bin/python3.14 --extra dev --extra mcp --locked
mkdir -p /tmp/alfred-acceptance-example
uv run python -m agent_alfred.evals.acceptance prepare \
  --candidate-root "$PWD" --batch example-a \
  --output /tmp/alfred-acceptance-example/manifest.json
uv run python -m agent_alfred.evals.acceptance validate \
  --input /tmp/alfred-acceptance-example/manifest.json
uv run python -m agent_alfred.evals.acceptance dry-run \
  --input /tmp/alfred-acceptance-example/manifest.json \
  --workspace /tmp/alfred-acceptance-example/runtime \
  --store /tmp/alfred-acceptance-example/evidence --candidate-root "$PWD"
uv run python -m agent_alfred.evals.acceptance report \
  --store /tmp/alfred-acceptance-example/evidence --batch example-a \
  --candidate-root "$PWD" --output /tmp/alfred-acceptance-example/report.json
uv run python -m agent_alfred.evals.acceptance verify \
  --store /tmp/alfred-acceptance-example/evidence --batch example-a \
  --candidate-root "$PWD"
```

`dry-run` 实际启动 Host、Session、SQLite、Registry，运行六个合成案例，再重启读取会话证明持久结果。只有模型边界被 ScriptedModel 替换。记忆、Skill、路由和文件仅位于新建的专用目录，不读取默认生产状态。它同时保存批次并发布模拟报告；因没有真实质量证据，`dry-run`、`report`、`verify` 的预期退出码均为 `2`（JSON 中 `v1_release=BLOCKED`），可继续执行下一条命令；`prepare`、`validate` 退出 `0`。`--output` 使用排他写入，需为每次结果选择新文件名。

同一批次不可覆盖。新一轮使用新 `batch_id`、新工作目录及 `parent`（`retry`）；一次产品样本不得在同批重复或从其它批次择优拼接。原始包不随产品 trace 或 CI 附件清理。

## 数据格式 v1

`prepare` 生成可校验的完整格式示例；精确校验实现位于 `src/agent_alfred/evals/acceptance/schema.py`。顶层 `schema_version=1`、`contract=V1-ACCEPTANCE-PHASE-A-SPEC-r1`；未知版本拒绝。JSON 哈希使用 UTF-8、sort_keys、紧凑 separators、不接受 NaN；哈希证明记录字节一致，不证明人或第三方真实性。

- `candidate`：commit、tree、全部纳入文件的 kind/mode/sha256、锁文件依赖、环境；`candidate_id` 为其规范 JSON 哈希。Git 已跟踪的删除和未提交文件均计入。排除 `tmp/`、`.scratch/`、`output/`、`dist/`、node_modules 与虚拟环境，评测配置/数据另封存在包中。
- `profiles`：固定产品模型（primary、可选 retrieval_gate）、独立 judge 模型、请求参数与 persona 等输入；每个模型记录 endpoint_id/model_id/wire_style/provider_version，未知版本明确写 `unknown`。profile id 是除 id 外内容的哈希。judge 与产品 model_id 相同会拒绝。
- `cases`：id/group/input/gold/forbidden/source/applicability/operation/setup/script/material_id。六组为 conversation、memory、tools、skills、routing、aggregation。`offline_fixture` 另算；calibration 每组至少 5；formal 每组至少 20。material_id 绑定 input+gold，不会因更名 case ID 变为独立样本。
- `rubric`：null 表示通用阈值未批准；支持 `scorer=case-fraction-v1` 的显式逐组、逐维 fraction 阈值及 version/id/approval。示例测试规则只允许标记 `approval.kind=test`，不代表人类批准。真实规则需要 `kind=approved`、批准人、时间及来源。通用值没有默认值。
- `results`：首次产品结果，包含 Run、实际采样/收尾时间、输出、outcome、recorded、实际 trace/Attempt/工具证据、来源。`grades`：原始 judge 判分，绑定 result_id/result_hash/rubric_id，每项 status/reason/evidence；产品失败不等于 judge 错误。
- `adjudications`：人工显式导入的 id/grade_id/human/at/reason/rubric_id/dimensions/prohibitions。争议或疑似安全违规在裁决前阻塞；程序不生成批准。
- `references`：具名嵌入文本和 sha256；`coverage`：来源版本→义务→公共路径→证据→适用性→缺口→归属；`gates`：命令、平台、Python、时间、退出码、原始日志、预定/实收案例及实际状态。
- `parent`：原批次 batch_id/sha256/relation；`calibration`：原校准 batch_id/sha256/material_ids。校准和正式材料不可交叉，judge/profile 变化需要重新校准。

提炼专项由 `specialist` 导入和公共报告独立核验，逐案原子数累计后的正确性与覆盖率各 ≥90%，空分母 N/A、禁止项与结构安全必须通过；此专项不为六组通用规则提供默认值。

## 判分导入与人工裁决

准备一个 JSON 数组，记录格式如上，然后用新身份追加：

```sh
uv run python -m agent_alfred.evals.acceptance import-grades \
  --store /tmp/alfred-acceptance-example/evidence --batch example-a \
  --new-batch example-graded --input /tmp/grades.json
uv run python -m agent_alfred.evals.acceptance adjudicate \
  --store /tmp/alfred-acceptance-example/evidence --batch example-graded \
  --new-batch example-adjudicated --input /tmp/adjudications.json
```

原包与原判分保留；重评分不改变产品输出或采样时间。原 judge 为 error/unknown 时也可导入显式人工裁决；有效裁决的确定失败仍为 FAIL，未裁决仍 BLOCKED。报告每次按明确时区重新校验七天时效；刚好七天未超期，未来/矛盾时间不可用。候选依赖闭包不明时不复用；本版本采取保守的全部纳入文件一致校验。

## 授权与在线接缝

当前生产 `execute` / `judge` 及 product / auxiliary / judge 公共执行路径在读取凭据和构造线上工厂前以 `approval_source_unverifiable` 拒绝；仓库没有真实可信的审批来源和 Dispatch。`validate` 只证明 schema，`proposal` 只生成不可执行的待确认材料。`authorization` 内的 `binding`、by/at、额度或 `accept_unknown_cost` 是历史格式字段，执行者自填、哈希或同权限评审均不能将其升级为真实许可。任何 ambient key 均不代替授权。完整当前合同见 [AUTHORIZATION-ADMISSION.md](AUTHORIZATION-ADMISSION.md)。

模拟正向仅通过 `SimulationAuthority` 签发的 `SimulationSession` 和精确的 `httpx2.MockTransport` 进入 `runner.execute` / `online_judge.grade_batch`。公共入口不接受任意 `product_factory_builder`、`factory_builder` 或 `http_client` 注入作为授权替代；单设 `simulation=True` 也不能执行。模拟 Dispatch 在实际发送前预留并记录 `SEND_INTENT`，共享 product / auxiliary / judge 请求账、原时限和停止状态。它不证明真实 broker、OS 身份、凭据隔离或网络出口控制。

未来真实付费执行还须另行建立独立可信 issuer / Authority / Dispatch、受保护身份与审计，以及经 broker 可验证的**硬金额上限**（明确币种、金额、计费口径和外部强制机制）；请求数、token 和时间限制不构成金额硬上限，unknown 费用不能由执行者选择接受。独立 judge 接缝是 `judge.judge_result`，只接必要数据与无工具的 ModelRequest；离线测试不声称真实 judge 的语义抗诱导能力已通过。当前 prepare/validate/dry-run/import/report/verify 均不调用在线模型；`report/verify` 会写新报告，封存库只做 `read` / 纯 `report`。

## 机械门禁

`collect --gate NAME --batch OLD --new-batch NEW` 在候选仍一致时运行现有门禁，保存完整命令日志，生成新的证据版本。例如：

```sh
uv run python -m agent_alfred.evals.acceptance collect \
  --store /tmp/alfred-acceptance-example/evidence --batch example-a \
  --new-batch example-ruff --gate ruff --candidate-root "$PWD" \
  --workspace /tmp/alfred-acceptance-example/checks
```

可用项：sync/ruff/skills/env/pytest/build/installations/typecheck/browser。浏览器前执行 `npm ci --ignore-scripts` 与 `npx playwright install chromium`；Ubuntu 使用 `--with-deps`。pytest 插件显式记录 collection、deselected 与 setup/call/teardown；skip/xfail/漏项不能因退出 0 自动 PASS。测试覆盖仍独立于计数。

Ubuntu/CPython3.14 必需全部项；macOS/CPython3.14 必需 wheel/sdist × base/mcp 真实安装公共路径。安装脚本在源码外独立 venv 校验 CLI、Dashboard、文件/SQLite/MCP 子进程，核对安装模块全部文件、实际解释器和导入位置。未运行平台保持 NOT RUN/BLOCKED；本地 macOS 不冒充 Ubuntu CI。

## 恢复、删除及覆盖缺口

批次目录中的 batch.json 与 complete.json 必须同时完整且哈希相符。中断包不可使用；完整包在 latest 更新失败后仍可按明确 batch ID 核验。旧 latest 从不代表指定的新批次。先核验已保存包再重建报告，不隐式重跑模型。显式 `delete --store PATH --batch ID` 保留安全身份删除标记，删除原正文；再次调用可完成未完成的删除。依赖该包的复用会失效。

敏感字段及已加载/ambient 凭据值在输入、保存和 judge 边界拒绝；错误不回显原文。默认报告仅安全摘要，不包含逐案正文，不上传包。不自动创建 Issue、提交、发布或关闭票。

`coverage-inventory.json` 保存历史来源引用、义务摘录与尚未核验的缺口；`sources/` 保存抓取时完整正文/评论以复核出处。同 ID 的多轮历史不能自行视为当前有效版本。“8 大类 30+”独立原始 brief 未找到，明确缺证据。本阶段不会凭空补全金标或将旧关闭/旧 CI 直接转为当前 PASS。

## 独立校准、专项导入和重评分

校准包与正式包必须在同一证据库中，绑定同一候选、产品配置、judge 和 rubric。校准包每组至少 5 案且每案保留产品结果与有效判分；它还需要显式 `calibration_approval`（kind/by/at/reference/evidence_sha256）。最后一项由 `schema.calibration_identity(calibration_batch)` 得到，绑定材料、结果、判分、裁决、规则和模型。校准中出现产品失败可以成为校准资料，不要求把失败改成 PASS。整批批准不能替代逐案争议/疑似安全违规裁决；每案的有效评分必须完整，judge 错误可由明确人工裁决处理。正式报告仍核验所引用校准样本的原始七天时效。校准源自己的 case/语义批准在独立导入、重读与正式引用时，须不晚于源原共享预算起点和最早采样；regrade 与后续 judge 预算不刷新这一上界。正式包的 `case_set_approval` 以相同批准身份字段及 `cases_sha256=digest(cases)` 绑定已批准金标；正式采样之后才批准的通用规则/金标不能追认为事先冻结。Schema3 正式证据导入、重读和执行预检统一核对 case、语义规则、汇总以及所引用 `calibration_approval.at`：各批准不得晚于已记录的共享预算起点或最早正式采样，汇总批准不得早于校准批准；未执行草案留待实际执行预检。公共 `report/verify` 会发布新报告，应仅对临时库或经授权可写的库运行；封存库只读核验见 [SCHEMA3.md](SCHEMA3.md)。A 不生成这两种真实批准。

提炼通过独立的 `specialist` 文档嵌入批次后使用 `import` 导入，公共报告输出独立 `consolidation` 轴并参与 release。字段为 schema_version=1、id（除 id 外规范 JSON 哈希）、candidate_id、profile_id、simulation、approval 和 rows。每行包含 case_id/material_id/run_id、实际 input/gold/output、sampled_at/finished_at/source、outcome/recorded、原始 evidence、review（批准身份字段）、correct_atoms/output_atoms/covered_items/expected_items、prohibitions 和 structure_safe。至少 30 个不同材料/Run；正确原子不能超过输出原子，正确表达项不能超过应提取项；累计两比率各 ≥90%，空分母 N/A，禁止项或结构安全失败不能被平均抵消。资料缺失、过期、配置不一致均阻塞。通用与专项结果都核对 evidence 中 run_id、recording_state=recorded、trace_status=available；outcome、recorded 的相互矛盾不会被摘要成功覆盖，已知失败与缺记录分别保留。历史提炼的原始资料应从既有真实提炼路径导出，不把普通聊天当成提炼；runner 明确拒绝 `operation=consolidate`，本接口使用原始专项证据导入。

更换 judge、金标或 rubric 时，先以 `regrade` 建立新版本并清空旧评分/裁决/活动授权，保留原产品输出、输入和采样时间。产品原 profile 不变；独立 `judge_profile` 覆盖由 model 与 id 组成，id 是除 id 外规范 JSON 哈希。需有匹配新规则/judge 的新校准包。配置 JSON 只允许 rubric、calibration、judge_profile、cases（只许调整金标/来源，原实际输入不变）、case_set_approval、calibration_approval：

```sh
uv run python -m agent_alfred.evals.acceptance regrade \
  --store /tmp/alfred-acceptance-example/evidence --batch example-a \
  --new-batch example-regrade --input /tmp/regrade-configuration.json
```

以下命令是历史在线包的入口形状，**当前生产路径必定拒绝**，并非取得一份本地 `authorization` 后即可运行。待未来真实可信通道、可验证硬金额上限及新批次授权全部具备后，须按届时接口重新核对；阶段 A 未运行：

```sh
uv run python -m agent_alfred.evals.acceptance execute \
  --input /tmp/approved-batch.json --candidate-root "$PWD" \
  --store /tmp/approved-evidence --workspace /tmp/approved-runtime
uv run python -m agent_alfred.evals.acceptance judge \
  --input /tmp/approved-judge-authorization.json --candidate-root "$PWD" \
  --store /tmp/approved-evidence --batch approved-product --new-batch approved-judged
```

`budget.binding` 还绑定当前 rubric 和独立 judge_profile。`runner.execute` 与 `online_judge.grade_batch` 均要求 candidate_root/store；judge 另需 new_batch。当前公共入口先核验可信来源，再核验候选并排他预占批次；无来源时在任何线上工厂和凭据前拒绝。除核对 candidate_root 的工作树，还核对当前 Python 进程实际导入的 agent_alfred 包文件闭包；声明另一份旧 checkout 不能证明正在执行的代码身份。报告与门禁采集同样检查，完全相同的已安装包可通过字节核验。模拟正向只接受上述 `SimulationSession` 与 `MockTransport`，CLI 不提供模拟 Authority 附着。

## 请求账与中断恢复

每次实际传输 preflight 在发送之前写入并 fsync 请求事件；完成后补记真实 Attempt 用量，未收到的信息保留 unknown。`.execution-*` 预占永久保留，同批改工作目录不会重发；`.journal-*` 追加带连续身份/前序哈希的事件，保留已完成产品结果和原始 judge 输出。最终包写入失败不会抹掉已花额度。同一执行批的产品和 judge 共享请求账与开始时刻。显式 regrade 配置命令建立新预算域：旧请求移入不可删改的 request_history，并保留原包；新域尚无授权，另获该新批授权后才开始新的时限/请求计数。普通 judge 或报告修订不重置原预算，也不能重复执行同一父批。

从中断事件恢复只重建证据，不调用模型：

```sh
uv run python -m agent_alfred.evals.acceptance recover \
  --store /tmp/approved-evidence --batch approved-product --operation product \
  --candidate-root "$PWD"
uv run python -m agent_alfred.evals.acceptance recover \
  --store /tmp/approved-evidence --batch approved-product --operation judge \
  --new-batch recovered-judge --candidate-root "$PWD"
```

若同名产品包已经半写，显式 recover 只有在已写字节与完整事件一致时才补齐完成标记，不改原正文；字节冲突或事件损坏保持阻塞。judge 可从原始完整产品包用新身份恢复。完整包可按明确 batch ID 重建报告。报告追加于 reports/，latest-report.json 携带明确 report_id/batch_id；发布 latest 中断留下旧入口时，旧入口不代表新的指定批次。读取有效结果始终重读原包与依赖并重新计算，而非信任旧 PASS 摘要。显式删除同时清除该批的原始执行日志，保留安全预占/删除身份，依赖该批的后继包不可继续复用。

门禁使用未筛选的独立 pytest 收集计划，再根据仓库唯一默认 `not requires_key` 规则排除在线项；实际 selection/outcomes/setup-call-teardown 必须完整匹配。`PYTEST_ADDOPTS`/`PYTEST_PLUGINS` 等隐藏选项会明确拒绝。macOS 若 Git 指向未完成许可的 Xcode，使用已有 CommandLineTools；本机离线验证使用 `NO_PROXY=localhost,127.0.0.1`、同值 no_proxy，以及 Node22 的 PATH。这些是执行环境修正，原始失败日志仍保留。

覆盖基准来自候选内 coverage-inventory.json 的实际字节。批次通过 references.coverage_inventory 嵌入它的 content/sha256；coverage 每行 id 对应 inventory_id，来源/义务/适用性/归属不可替换，evidence 列表以 `{ "gate": "Ubuntu:pytest", "test_ids": ["路径::test_name"] }` 指向同候选的实际通过项。历史证据不填入这里；缺来源、缺测试或原清单未决 gap 均阻塞发布，不能删除 gap 字段或塞一空行宣告全覆盖。


### 判分原文与证据定位

scored grade 若有 raw，必须是 JSON，且其中 dimensions/prohibitions/disputed/suspected_safety 与解析后记录完全一致；不一致或断引用在导入时拒绝。真实评分没有 raw 时报告保持 BLOCKED；显式 simulation 的纯判定夹具允许只提供结构评分。judge 非法输出原文留在 error 记录中，不冒充有效评分。

每个评分项的 evidence 为一个定位字符串或非空定位列表。支持本案结果 `result:<result_id>#/output`、`result:<result_id>#/evidence/attempts/0`、金标 `case:<case_id>#/gold`、规则 `rubric:<rubric_id>#/groups/conversation/correctness`。片段遵循 JSON Pointer（`~0`、`~1` 转义）；没有片段表示完整对象。兼容直接写本案 result_id。不能引用别案、未知对象或不存在的字段。比如：

```json
{"status":"fail","reason":"输出与本案金标不符（仅格式示例）","evidence":["result:RESULT_ID#/output","case:CASE_ID#/gold"]}
```

将占位 ID 替换为已保存本案 ID。定位校验只证明所引字节存在，不代替语义判分或人工批准。原始 pytest 三阶段状态同样是核验输入：任一已知 failed 保留为 FAIL，摘要与阶段不一致另列 blocker。


### 真实校准包的引用资格

外层 `simulation=false` 不足以成为有效校准。正式包引用时核查每案批准来源及 case_set_approval、采样之前的批准、逐结果 online 来源、Run/trace/持久记录一致性、原始有效 judge 评分与实际 judge 身份。产品和 judge 的 Attempt 通过 `evidence.attempts[].attempt_id` / `grades[].attempt_ids` 关联 `requests` 和 `request_history`；授权沿不可变父链核对绑定、模型、额度与请求起始时间。

资格校验不要求校准质量全过：失败产品 Run 可没有 assistant 回复，保留已保存 Run 的原始失败证据；judge 构造前故障可没有原文或请求，但需保留明确错误和有效人工裁决。不能将缺实际记录解释成这种例外。空输出在通用及提炼专项中都记必需完成失败，即使其余案例高分。


产品 Attempt 的 started_at 必须位于对应首次 sampled_at/finished_at 窗口内；judge Attempt 不早于该产品收尾、不晚于本版校准批准。补判不能用新的结果时间掩盖旧请求。judge 收到 ModelCallInterrupted 时先保存已发生的 Attempt、错误评分和请求账，以 stop_reason=judge_interrupted 结束批次；余案仍保留且不再发请求。

## 六案真实试跑扩展（TRIAL-PLAN-r1）

原阶段 A 格式 `schema_version=1` 保持不变。用户单独批准的六案试跑使用
`schema_version=2`、`phase=trial`、`simulation=false`，合同仍引用阶段 A；
此版本只接受 trial，六个既有组各一案。它既不是 30 案校准，也不是 120 案正式验收，
不能作为 formal 的校准来源；report 明确加入 `trial_not_release_evidence`，永不发布 PASS。
质量失败仍显示 FAIL，缺证据仍显示 BLOCKED。版本 1 不接受 trial，未知版本拒绝。

仍从公共 `execute` / `judge` / `report` / `verify` 入口执行，授权绑定具体候选、材料、
profile 和 rubric；先取得用户批准，再生成 authorization，不能靠环境中的 key 启动。
真实合成案例经用户批准后用 source.kind=approved 并保留合成材料出处；测试传输不是真实评测。

trial profile 必须明确 `local_tool_allowlist=["draft_message"]`。运行器使用新状态目录，
只给 Host 注册该本地工具，白名单外调用按未知工具拒绝，且不启动 MCP。
检索、显式 Skill 选择和 aggregate 内部的只读来源路径仍使用隔离状态。
Host 构建的 `local_tool_allowlist` 参数省略时保留常规产品行为；指定时只允许本地声明。
模型设置切换保留旧 pin 而改变指派，避免同一变更删除尚被引用的 pin，不发生模型回退。

产品及 judge 的模型记录可指定 `thinking="disabled"`，仅 schema 2 的 DeepSeek/OpenAI
组合接受此选项。它进入 profile 内容身份并经共享预算客户端透传，覆盖 primary、辅助模型
和 judge；省略则保留端点默认。请求参数仍由 ModelRequest 和 wire 编码器显式表达，
不接受任意 extra_body。初轮为非流式；非流式响应中的供应商 model ID 另存到请求账本，
未报告则 null，不能把请求 ID 当作供应商后端版本证据；不一致则停机且不消费其产品输出。

认证/协议错误等基础设施失败在 trial 中停止后续请求。产品和 judge 共享原始预算起点与
所有 Attempt，传输重试也计数。最大请求数、每请求 max_tokens、总时限是规模限制，
以下 `cost` / `accept_unknown_cost` 是当时 trial 包的历史字段说明，不提供当前准入：`cost` 只表示有出处的估算，不是账单硬上限；历史格式即使写 `accept_unknown_cost=true`，当前执行仍因 `approval_source_unverifiable` 拒绝。未来付费要求可验证硬金额上限。
逐案 rubric 可以明确 groups={} 来保留原始判分，但通用阈值缺失仍阻塞发布。

本次试跑不改变 Ubuntu 历史 NOT RUN、未完成的覆盖、校准或正式金标；macOS 试跑成功
也不能代替完整 v1 发布验收。重采样、换模型、加额度、修订金标/阈值需新的明确范围与批次。

trial 工具案例还会封存实际 outbox 正文、SHA-256 与观测时间，位于
`result.evidence.local_artifacts`；仅从专用状态的固定 outbox 读取普通文件，拒绝符号链接，
最多 16 项、每项 64 KiB，读不完整或发生变化时如实 unavailable 并停止后续调用。
未生成草稿显示 missing。公共 Run evidence 同时保留真实 node/graph 的 route_label，
不从分类文本或最终回答反推实际选择路径。以上字段仍经过统一敏感数据检查。

trial 的逐案适用维度一旦有确定 fail，报告列出 `trial_dimension_failed`；
通用阈值缺失、trial 非正式证据等 blockers 仍保留。unknown 仍只构成阻塞，
不假装已知失败；此逐案义务检查不为 schema 1 校准/正式组分数设置默认阈值。

## r1 后离线修复与 r2 材料

r1 原始结果、judge 和失败历史保持不可变。新的 judge 请求包含完整嵌套
`response_schema`，严格拒绝 fences、重复/额外字段和不合法的 N/A；schema2
允许 DeepSeek 独立 judge profile 显式设置 `response_format: "json_object"`。
该字段进入 profile 身份，不改变产品默认请求；JSON mode 不保证语义判分正确，
仍保留原始字节且不自动补判。

需要机械验证精确格式的新材料，在 `gold` 中预声明并冻结：

```json
{"exact_format":{"version":1,"kind":"exact_lines","value":["第一行","第二行"]}}
```

`exact_lines` 比较整个回复与 `\n` 连接后的文本，额外段落、Markdown、空白或
尾随换行均不被裁剪。`json_object` 的 value 为预期对象，允许 JSON 的排版空白
与对象键顺序变化，拒绝重复键、额外正文及值类型差异。这些是明确的格式合同，
不是通用质量阈值；旧材料缺少 `exact_format` 时不会推断或追溯添加。

judge 记录中的 `format_check` 和 `judge_conflicts` 分别保留机械事实与冲突维度，
不修改模型原判分及原始文本。公共 report 从原始输出和声明重新计算，不能用手填
检查结果覆盖事实。明确格式违规保留 FAIL；judge 同时宣称通过则另列
`adjudication_required`，不能用协议合格或人工注释把格式失败变成通过。
未知版本或非法格式声明在执行前拒绝。

聚合验收只在专用状态中捕获实际模型请求，与持久的已发送 aggregation Attempt
核对后提供 `evidence.aggregation_inputs`，包含当次 `provided_sources` 正文。
未发送或捕获不完整显示 unavailable，不能从 setup 重建正文冒充发送证据。
该机制不改变生产 trace/telemetry 的正文留存规则。

中文检索在原有 FTS 搜索为空时才补充字面汉字二字片段召回。查询最多128字符、
最多32个不同片段，超过任一限制则不启用；单字、纯西文不启用。每 Store 只检查
最近1000条候选（semantic subject 条件先应用），按匹配片段数、既有稳定时间/行
顺序排序并保留原 limit。不会因权限过滤或 episodic 时间过滤将已有 FTS 命中
清空后再补搜。新结果仍经原权限检查、整条选择、长度预算与删除失效规则。
这个有界策略可能漏掉更早记录或增加弱相关候选，不承诺语义搜索完整性。
命中以 `relevance=han-bigram-fallback-v1` 标明，Host gate 的 Store 证据记录
`retrieval_method`；不持久化查询片段或新正文索引。

Skill 通用指令强调格式约束作用于整个正式回复，不能夹带引言或附言；产品仍
保留实际模型输出，不裁剪、重写或额外重试。离线回放只能验证接线与反例，真实
格式遵循、召回后的答案质量和 judge 语义改善必须经另获批准的 r2/校准验证。

真实 trial 的产品与 judge 默认工厂均关闭传输自动重试（SDK 隐式重试亦关闭），
每次失败保留首次请求与错误，不为协议或质量补采、补判。非 trial 的产品默认策略
保持原值。离线注入工厂只用于控制测试边界，不能作为已运行的真实试跑证据。

## r2 后引用协议：judge-citations-v1

新在线判分必须显式声明 `judge_profile.protocol`；`prepare` 自动创建当前声明。
已有未判分材料可在获准的配置准备阶段用
`judge_protocol.current_profile(model)` 生成整个 `judge_profile`，随后重新生成
`budget.binding(batch)` 并取得该绑定的授权。已有评分改协议应通过显式 `regrade`
配置入口新建版本，清空活动评分/裁决/授权并保留旧包；本轮没有授权重判 r1/r2。
未声明协议的历史包仍可导入、报告、核验，但新 `judge` 在构造工厂前拒绝它，
不会默默换提示词。未知、null 或内容身份不符的协议明确拒绝。

`judge_protocol.descriptor()` 包含版本、提示词和响应 schema 的规范 JSON SHA-256、
来源策略、目录策略及容量；其 id 绑定全部字段。新 grade 的 `judge_id` 绑定
模型及协议，另存 `protocol_id`、`catalog_id`、`material_id`，分别绑定协议、
完整引用列表和实际 sources；既有 result_hash/rubric_id 继续核验。
配置、输入正文、结果、规则、目录或模型漂移不能靠旧评分通过导入/报告/校准。
未来协议升级须保留历史版本定义或明确提供历史读取路径，不能原地改写此版本。

## c4 离线修订：judge-citations-v2

`judge-citations-v2` 是本轮引用可靠性修复后的新协议身份，不能把
`judge-citations-v1` 的 profile、授权或判分当作 v2 结果。公开 `judge_result` 先用
现有 canonical JSON `digest` 严格比较传入案例与 `result.case_id` 对应案例；同 ID
但 JSON 类型或内容漂移也在构造材料、调用 client 前拒绝，不做归一化或替换。

当前 grade 的 `format_check` 也属于受保护的引用记录：导入时按 canonical 案例和结果
重算并核对，并要求其中每个 JSON Pointer 出现在本批实际生成的目录中。目录/结构检查
仍只说明“引用可解析”；它不证明引用支持结论，语义校准和真实模型可靠性继续由后续
明确授权的材料承担。

judge 请求只含本案实际 result、本案 id/input/gold/applicability/forbidden、rubric，
以及格式事实、响应 schema、协议和 `reference_catalog`。不加入 setup、其它案例、
旧评分、默认生产状态或未发送的重建材料。全部来源复用现有安全检查；目录逐节点
从这些来源生成，对象键按字典序、数组按位置，空容器和根节点也列出。
每个目录项先经过与评分相同的身份/JSON Pointer 解析器，再交给模型原样选择。
新协议根引用采用 `identity#`，不提供旧 bare result_id 别名。

目录最多 10000 项；超量在发请求前明确 `reference_catalog_limit`，不截断来源、
不删目录项、不裁剪模型输出。非法转义、数组越界/负索引/前导零、不存在路径、
跨案、空引用、未提供来源的引用均拒绝。模型非法引用保留原始 raw 和 error；
没有自动修正、模糊匹配、补判或重试。目录增加输入 token 成本，需列入试跑估算。

目录只帮助定位。它不证明证据支持结论，更不完成语义校准。离线反例仍包括：
引用真实 output 却误判完整格式、用结果摘要推断工具效果、用 Skill selected
推断遵循、用回答文字推断未发生外部行为。预声明格式的机械冲突仍触发 FAIL
和裁决 blocker；其它语义错误需要后续独立校准，不能靠目录查找判为可靠。
原 r1/r2 raw、报告、错误及身份保持原样；夹具通过不表示真实模型引用率改善。

## c5 离线修订：judge-citations-v3

`judge-citations-v3` 在 v2 身份上继续升级，保留 v1/v2 与 r1/r2 的历史文件和判分，
不把旧结果重判成新协议结果。公开 `judge_result` 先用 canonical JSON `digest` 同时
核对传入的 case 和同 `result.id` 的 canonical result；同 ID 的结果 JSON 类型或内容
漂移在生成材料、调用 client 前以 `result_identity_mismatch` 拒绝。未改变的深拷贝仍可
继续使用，拒绝不做自动替换、归一化或重试。

每个新 grade 另外保存 `case_hash` 与 `case_material_id`，并和 `result_hash` 一起绑定
实际案例。没有 protocol 的 legacy grade 若缺少这两个绑定则以
`legacy_grade_unbound` fail-closed；已有绑定但案例、金标或结果漂移以
`judge_material_mismatch` 拒绝。校准包和显式 regrade 仍先经过各自父链、案例批准和
原样本链接检查；这些包的专用错误不会被新的绑定检查重写。旧 r1/r2 文件不改写、不补
字段、不重新评分。

v3 仍只保证“引用能解析”：目录、JSON Pointer、格式事实和身份检查都不等于证据支持
结论。合法 output 引用却误判完整格式、用结果摘要推断工具效果、用 Skill selected
推断遵循、用回答文字推断外部行为缺席等语义反例继续保留为后续校准材料。夹具通过不
 能宣称真实模型引用可靠性已经改善。

## c6 离线修订：judge-citations-v3 绑定收紧

c5 的独立 clean-context 复审发现，校准包、显式 regrade 及其 formal 输入仍可能绕过
`validate_grade` 的案例、结果、材料绑定；无 protocol 的 legacy grade 也可能伪造已保存的
`format_check`。c6 在所有包类型统一执行 `case_hash`、`case_material_id`、`result_hash`
和 rubric 身份检查，并对已有 `format_check` 重新计算；不因父链、校准链接或 package-linked
路径跳过检查。漂移分别以 `judge_material_mismatch` 或 `format_check_mismatch` fail-closed，
不自动修复、重试、补判或改写旧 grade。

c6 的 red/green 测试覆盖 legacy format 事实漂移、伪造 calibration grade 绑定及伪造
regrade grade 绑定；兼容性夹具仍保持父链专用错误顺序。协议身份继续明确记录为
`judge-citations-v3`，c5 候选、报告、门禁与历史 r1/r2 文件保持不可变。测试通过只能证明
所有入口执行了相同的机械绑定；引用可解析仍不证明证据支持结论，语义反例、真实模型引用
可靠性、完整金标、通用阈值、30 案校准和 120 案正式验收仍需后续授权。

## r3 后外部复核争议

新增 `import-reviews` 与 `adjudicate-reviews`，通过不可变修订包保留原判分、agent 复核意见和显式人工裁决的独立身份；未决意见阻塞报告。数据格式、兼容边界、命令与人工处理约束见 [外部复核争议 v1](review-disputes.md)。

## c22 离线修复：截断投影与 judge-citations-v4

当工具的完整审计文本是 JSON 数组且超过模型投影上限时，能安全解析的数组只保留上限内完整数组项的原始文本片段，不重编码数值或转义内容；解析超出安全上限、解析失败、或数组项解码后触发中央脱敏时，从该项起隐藏并标记其余内容未知。数组形状但无法安全解析时不展示半条记录。非数组文本预览仍可能在字段中间结束，并明确标记。原始 audit、哈希和工具执行事实不变。这个离线可验证的输入边界不证明模型之后不会推断不可见内容。

新判分请求使用 `judge-citations-v4`。保留 v3 的完整 `reference_catalog` 和逐字引用校验，另给出从同一目录机械筛选的 `reference_guide`，指向任务、首次结果、实际工具/聚合证据和独立恢复观察的常用入口。guide 不改写 raw、不能把错误引用自动纠正为合法引用，引用可解析也不证明语义支持。新指令明确首次观察与后续恢复、命令文字与实际动作、unknown 与成功、无记录与已证明未发生的区别。历史 v3 包及原判仍按原协议身份只读验证；新在线 judge 执行要求当前 v4 身份、新材料和独立授权，旧校准批准不得沿用。ScriptedModel 只验证 wire、校验和拒绝行为，真实 judge 可靠性仍待新校准。
