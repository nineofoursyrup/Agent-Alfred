> 当前工程关闭合同为 [V1-AGENT-ONLY-CLOSEOUT-20261007-r1](AGENT-ONLY-CLOSEOUT.md)。本页定义 schema4 的版本化证据与真实质量／运行守卫；这些守卫不因工程票关闭而放宽。缺真实质量、材料或批准时 `quality` / `v1_release` 仍如实 FAIL / BLOCKED / UNKNOWN，不能用 Issue completed 改写批次。当前离线义务及历史条款归档见[补遗](agent-only-obligations.md)。

# Schema4：关键义务、裁判检验与用户摘要决定

合同：`V1-ACCEPTANCE-OFFLINE-SUPPLEMENT-SPEC-r1`，对应 [#95](https://github.com/nineofoursyrup/Agent-Alfred/issues/95)。这是离线机制；合成响应、合成用户事件和同模型复核一致都不证明真实材料或 judge 质量。schema1/2/3 保持各自的校验及批准含义，不能仅改版本升级。

## 准备与公共命令

所有路径必须指向**新建的隔离目录**。下列命令不访问真实模型或凭据：

```sh
python -m agent_alfred.evals.acceptance prepare --schema-version 4 \
  --candidate-root "$PWD" --batch supplement --output /tmp/new-95-draft.json
python -m agent_alfred.evals.acceptance validate --input /tmp/new-95-draft.json
python -m agent_alfred.evals.acceptance dry-run --input /tmp/new-95-draft.json \
  --store /tmp/new-95-evidence --workspace /tmp/new-95-runtime
python -m agent_alfred.evals.acceptance summary --store /tmp/new-95-evidence \
  --batch supplement --summary-stage results --output /tmp/new-95-summary.json
```

`prepare --phase calibration` / `--phase formal` 产生 30/120 案**合成清单**，仅演示分布校验，不是真实独立金标，也不能交给 `dry-run` 冒充完整采样。`offline_fixture` 为六组小型机制测试。`prepare` 默认 schema1，旧命令不改变合同。

一个包含真实离线 Host、脚本判分、独立实例记录、合成来源批准、关键 FAIL 和未决争议的完整示例：

```sh
python -m agent_alfred.evals.acceptance.examples_v4 --output /tmp/new-95-demo
```

输出包含 `prepared.json`、`runtime/`、不可变 `evidence/`、`user-summary.json` 和 `report.json`。脚本显式制造一个关键义务 fail；这个 FAIL 是机制证明，不是对实际模型的质量判断。裁判检验材料同时列出，尚未运行的检验明确为缺口。

继续使用现有公共入口：

| 命令 | 输入与效果 |
|---|---|
| `import` | 导入完整版本化包；拒绝未知字段、错绑摘要、断链与事后批准。 |
| `import-grades --batch B --new-batch C --input grades.json` | `grades.json` 是完整活动判分列表；保留原判分，不覆盖已有记录。 |
| `import-reviews` | 追加封存的 `independent_content_review` 数组。既可复核材料，也可复核产品判分或裁判检验结果。 |
| `adjudicate` / `adjudicate-reviews` | 追加同一种来源可核验的用户争议决定数组；Agent 意见不能占用此角色。 |
| `regrade --input configuration.json` | 提交允许的材料修订、摘要、用户事件、检验结果等；使用原完整列表追加历史。 |
| `summary --summary-stage ...` | 纯生成摘要，不自动批准、不保存为决定，不发模型请求。 |
| `report` / `verify` | 追加报告并更新 `latest-report.json`；退出码 2 仍可能写报告。 |

CLI 没有可信用户来源适配器，因此即使文件含 `approved`，也不能成为有效批准。测试中显式构造的 `EvidenceStore(path, decision_source=authority)` 可核验合成事件。纯读使用 `store.read(id)` 和 `report(batch, store=store)`，不会发布报告。

## 闭合数据合同

`schema_version=4`，顶层延续 schema3 的单一 profile、语义规则、汇总政策、请求史和执行策略。`rubric` 为 null；`semantic_rubric.version=semantic-rubric-v2`、`scorer=obligation-labels-v1`，`approval=null`。`review_policy` 由 `supplement_schema.review_policy()` 生成，允许相同模型、要求不同实例和先盲评后对照。旧 `case_set_approval` / `calibration_approval` 不能成为新版批准。

新增字段全部必须提供，集合缺材料时为空；合法草案可以保存但保持阻塞：

| 字段 | 精确对象 |
|---|---|
| `manifest` | 有序 `{case_id, case_sha256}` 列表，必须等于完整 cases 的身份；不能中止后删案。 |
| `seen_materials` | `{family_id, content_sha256, reference}` 列表，与既有 `seen_families` 一起登记已见来源。 |
| `judge_tests` | 下面定义的独立裁判材料。 |
| `judge_test_results` | 独立响应原文及解析结果，无产品 Run/Attempt 身份。 |
| `summaries` | `make_summary(batch, stage, store=store)` 返回的完整内容对象；须逐字保存，不能自行筛选失败。 |
| `user_decisions` | 独立来源的用户摘要决定事件。 |

每个 case 新增：

- `scene`：唯一值 `normal`、`boundary`、`failure_or_misleading`。calibration 每组 **5 案、2/2/1**；formal 每组 **20 案、8/8/4**，严格计数。
- `lineage`：非空 `{family_id, content_sha256, reference}` 列表。更名或改数字不证明语义独立，仍需材料复核。正式案拒绝登记的同内容/家族重叠。
- `drafter`：`{instance_id, model, reference}`；与内容复核实例区分。
- `obligations`：非空列表，每项恰含 `{id, meaning, dimension, kind, applies, expected, evidence_required, check}`。`dimension` 是既有三个维度；`kind` 为 `critical|secondary`；`applies` 为布尔；`check` 为 `semantic|exact_format`。后者必须已有合法 `gold.exact_format`。
- `absent_categories`：缺少适用关键/次要类别时，分别用 `critical` / `secondary` 键说明理由；理由也进入材料摘要与批准。

`material_id` 绑定除 case ID、脚本和自身字段外的全部案例材料；manifest 另绑定完整 case（含脚本）。脚本只属于离线替身，不是 judge 输入。每维所有适用义务和维度判断通过才贡献一案通过；增加次要条目不会增加分母。未运行和 unknown 留在预声明分母中。Run 自身失败、空输出或机械格式失败不能由用户文字决定改成成功。

## 判分、独立复核与争议

新 judge 协议为 `judge-obligations-v1`，旧 `judge-citations-v3/v4` 身份保持不变。响应沿用 `dimensions/prohibitions/disputed/suspected_safety`，另须 `obligations`，按全部义务 ID 返回 `{status, reason, evidence}`。状态闭合为 `pass|fail|unknown|na`，N/A 只能来自预声明。非法/重复 JSON 字段、错引用保留原 raw，形成 error，不修正或重试。汇总政策不进入 judge 输入。

新版 grade 还保存 `producer={instance_id,model,reference}`、`started_at/completed_at`。`judge_result(..., producer=..., clock=...)` 接缝用于记录实际实例和时刻。省略 producer 时仅记录注入 client 的进程实例标识，不能将其视为真实外部身份认证。

`supplement_reviews.target_for` 绑定材料、grade 或独立裁判结果的完整 hash；`blind_input` 返回允许暴露的实际输入；`make_review` 保存输入、原始输出、各自 hash 和时间，不替调用者执行复核。复核记录恰含：

`id/version/kind/target/actor/started_at/completed_at/compared_at/input/input_sha256/output/output_sha256/comparison_input/comparison_input_sha256/comparison_output/comparison_output_sha256`。

`kind=independent_content_review`；`target={type,id,sha256}`，type 为 `materials|grade|judge_test`；`actor={instance_id,model,reference}`。材料意见覆盖 `coverage/classification/source_independence`；grade 意见覆盖每个 `obligations:ID`、`dimensions:NAME`、`prohibitions:NAME`；裁判检验为 `judgment`。每项为 `{status,support,reason,evidence}`，support 为 `supported|unsupported|unknown`。原标签不在盲评输入中；盲评输出封存后，`comparison_input` 才包含原 raw、解析结论、实际引用与已封存盲评。`comparison_output` 保留盲评的 status，并记录对原始引用支持性的检查。两个阶段形成的争议均保留：对照 support 改为 supported 不能消除盲评的 unsupported/unknown；新保留的独立盲评争议标记 `stage=blind`，既有对照争议身份不变。材料准入、产品关键门禁和裁判资格均等待适用的用户裁决。同实例、错输入、改写封存输出或时间倒序拒绝。

分歧、支持不足、疑似违规、原 judge fail/unknown、机械矛盾和裁判预期不一致生成稳定 hash 的争议。原意见不改写，用户普通摘要批准不解除争议。`supplement_decisions.dispute_request(batch, dispute)` 生成精确决定目标；最终事件可为 `confirmed_violation|dismissed|insufficient_evidence`，必须有理由和可解析的非空证据引用。后者保持 pending。机械事实独立于裁决，原 judge 字节和原 Run 不变。

## 裁判检验

材料由 `supplement_schema.signed()` 内容寻址，恰含：

`id/version/kind/task/gold/answer/prohibition/expected/failure_mode/source_family_id/source/drafter/semantic_rubric_id`。

固定失效方式为 `missed_prohibition`（预期 fail）、`false_prohibition`（预期 pass，正确对照）、`unsupported_citation`（预期 unknown）。kind 为 `constructed_answer|correct_control`。构造答案、对照、金标和预期需事前材料复核及用户摘要批准。

`supplement_judge.run_test` 只接受 `simulation=true` 和 `ScriptedModel`，输入不含预期标签或目标失效方式。结果保存 `raw/parsed/error`、输入 hash、配置/语义身份、producer 和时刻。非法响应不裁剪。同材料/配置/规则不重复采样择优。报告分别显示标签一致性、引用结构、语义支持、各失效组分子分母；它们从不进入产品六组分数。已确认漏判不能由总体一致率抵消，准入数值不预置。

## 摘要、来源与派生

阶段为 `materials|results|calibration|thresholds`。材料摘要包含案例/金标/来源/义务/规则/材料复核与裁判预期；结果和校准摘要另包含原样本、原判分、复核、争议、全部缺口；阈值摘要绑定政策及校准源，不绑定尚未出现的正式采样。每个摘要覆盖全部 manifest、六组分布和固定分母，携带逐案原件入口。正文先经既有敏感数据检查；不能安全导入的材料报错，不能删掉不安全片段后假装完整。

`make_summary(batch, stage, decision_source=source, now=..., store=store)` 对非正式批结果及校准生成 `version=2` 摘要：`decision_snapshot` 保存当前来源可核验且不晚于 now 的裁决及材料/阈值前置决定，`assessment` 和顶层 `disputes` 展示裁决后的状态与全部失败。前置批准参与漏判及阈值失败的判断，结果/校准自身批准另行计算以避免循环。

正式批结果及校准摘要为 `version=3`。通过 `store` 重读、核对绑定校准源及其祖先；未显式传入 `decision_source` 时使用该 store 的来源。摘要保存与引用 hash 完全对应的 `calibration_snapshot` 及来源核验后的 `calibration_decision_snapshot`。本批评估继承校准 judge 检验的失败和缺口，同时在 `assessment.calibration_assessment` 完整展示校准批自身的质量、裁决和批准状态；校准产品结果不混入正式批分母。确定性评估时刻及批准截止覆盖本批和校准批的原结果、原判分、复核、裁决及适用决定。未提供 store 的正式摘要明确显示 `calibration_package_not_verified`，即使其摘要决定来源可信也不能成为有效结果/校准批准。CLI 使用证据库解析校准依赖，仍不提供可信用户来源。

材料/阈值摘要仍为 version 1，旧版摘要按原语义核验并保留原身份；当前报告仍保留新识别的争议。历史快照只用于完整性回放；当前批准每次以重新核验的校准依赖和当前决定来源生成摘要并精确匹配。来源缺失、失联或事件不匹配时不继承快照中的权限，旧版正式批摘要不能替代包含校准评估的 version 3 摘要。摘要获批不消除质量 FAIL，也不授予执行或发布权限。

用户事件恰含 `id/version/kind/subject/source_ref/at/decision/object_type/object_id/object_sha256/manifest/reason/evidence`。摘要决定为 `approved|rejected`。`DecisionSource.read_decision(source_ref)` 是独立只读接缝，必须返回权威侧记录的**完全相同事件**；未配置、来源失联、错对象、未来时刻或将 synthetic 用于真实包均不生效。

`SimulationAuthority.issue_decision` 仅签发 `simulation:` 主体的测试事件，复用其活动状态锚点；它与执行 `issue/activate` grant 分开。只保存 JSON、复制名字、确认文本或 hash 无法重建此权限。重启证据库会重算；未重新提供独立可核验来源时保持 `BLOCKED`，不缓存旧批准。真实执行仍在凭据读取/工厂构造之前因 `approval_source_unverifiable` 拒绝。

材料/预期决定不得晚于原预算起点或最早采样；结果决定不得早于所引用原结果和复核；正式引用校准批准、阈值批准不得晚于正式原起点，阈值批准不早于校准批准，正式材料冻结不得早于这两项批准。等时及等价时区合法。公共 import/read/reference/preflight 共用规则。正式执行同时要求完整校准证据（含产品判分的独立复核、语义支持和裁决）及已批准的非空阈值政策；正式报告从绑定校准源复用 judge 资格。

校准源及其祖先的授权隔离沿正式引用传播。源包仍可供审计读取；明确为 `QUARANTINED_AUDIT_ONLY` 的来源不能进入正式包的 import/read/report/material-preflight，统一拒绝为 `execution_authorization_invalid`。

`revise` 保留原结果、请求史和预算起点；义务、金标、语义规则或 judge 改变使活动判分清空，原件仍在父包，原批准因内容身份变化失效。历史复核/摘要/事件只追加，旧总结不会自动适用于新对象。已采样正式批不能改阈值；不能删案、改首次结果或刷新七天时效。

所有报告显式带 schema/contract、candidate/batch；`offline_engineering`、产品 Run outcome、quality 和 `v1_release` 分开。合成证据始终含 `simulation_not_release_evidence`，缺真实环境隔离、真实材料、judge 质量批准、在线授权或数值门槛时不宣称发布通过。
