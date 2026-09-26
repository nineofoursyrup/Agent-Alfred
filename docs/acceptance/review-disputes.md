# 外部复核争议 v1

落实 R05、AC-11、R03/R09：原 judge 即使 `disputed=false`，外部复核者也可追加待人工裁决意见。意见不证明产品违规，不改分，不运行产品或 judge。原 judge、产品结果、采样时间、批次和报告均保留。

## 公共路径

```sh
python -m agent_alfred.evals.acceptance import-reviews \
  --store /path/to/evidence --batch original-batch \
  --new-batch reviewed-batch --input /path/to/agent-reviews.json
python -m agent_alfred.evals.acceptance verify \
  --store /path/to/evidence --batch reviewed-batch --candidate-root /path/to/candidate
python -m agent_alfred.evals.acceptance report \
  --store /path/to/evidence --batch reviewed-batch --candidate-root /path/to/candidate
```

输入是下表记录的 JSON 数组。公共 Python `reviews.binding(batch, grade_id)` 读取现存身份，以 `schema.digest(record_without_id)` 得到新记录 ID；schema 和 store 独立核对。`EvidenceStore.revise(..., reviews=[record])` 与 CLI 使用同一路径。原批次有效读取是前提。

| 字段 | 内容与校验 |
| --- | --- |
| `id/version/kind` | 内容哈希；整数 1；固定 `agent_review_dispute` |
| `binding` | 原 batch ID/内容 hash、candidate ID、contract、case ID/hash、result ID/hash、grade ID/hash、rubric ID、judge ID、完整 judge profile hash |
| `item` | `kind=dimensions/prohibitions` 与原评分项 `name`，必须存在 |
| `reason/evidence/at` | 非空理由、合法且非空的本案引用列表、有时区复核时间且不早于产品完成 |
| `source` | `kind=agent`、复核者 name、reference、原复核文档 content 及字节 SHA-256 |

schema 1/2 均可携带这个独立版本化扩展；没有扩展的历史包不补字段。旧程序不理解新扩展，不能用旧程序验证新包；新验证程序的候选身份须另列证据。绑定目标须是可重读祖先批次；同名案例或结果不能串接，规则漂移也拒绝。来源正文随记录保存，重启不依赖另一个 Markdown 文件。哈希证明字节一致，不证明作者身份或语义正确。

旧 grade 缺身份仍按原校验拒绝，不能补造字段；另存旧包字节 hash 和来源说明可作 provenance，不能冒充 active parent。r3 已有有效 c6 绑定，可以原样复制完整父链后追加；r1/r2 不在本次导入。

新复核包保留原样本 candidate，因为没有新产品样本。新代码验证旧样本可能列出 `candidate_changed/runtime_candidate_mismatch`，应保留；新门禁不能附会成旧样本已验证新代码。`relation=regrade` 仅沿用不可变修订关系，本次追加没有第二次 judge 请求。

## 待裁决与历史

`quality.review_disputes` 逐项输出来源、绑定、评分项、理由、引用、`pending` 和裁决记录。未解决意见在 quality/v1_release 列出 `adjudication_required:<case_id>`；同案多项分别保留记录，共用该案 blocker。已有 FAIL 和其它 BLOCKED 保留，不把弱证据当成产品违规。无证据库的纯报告另列 `review_source_not_verified`。

修订继承复核和裁决记录的完整前缀，拒绝删除、改写、重排、重复。相同 grade/项/复核来源只改时间或理由不能制造独立意见。新独立来源可另加意见；旧人工裁决不自动处理后来意见。既有 grade 裁决也不能默默解决新复核。

携带复核历史的修订链固定 grade、case、result 和规则，移除 grade 或规则漂移会拒绝。后续确需新规则/新 grade，须独立版本、重新校准及可核验 provenance，不移植旧裁决。批准校准的身份包含扩展，待裁决校准包不能成为有效校准来源。已证实的产品失败可作为校准反例，不能变为正式 PASS。

## 显式人工处理

只有用户实际提供/确认的裁决才能执行。本轮仅使用 `OFFLINE TEST HUMAN` 模拟夹具，没有任何 r3 真实裁决。认为证据仍不足时保留 pending，不要求硬选 pass/fail。

```sh
python -m agent_alfred.evals.acceptance adjudicate-reviews \
  --store /path/to/evidence --batch reviewed-batch \
  --new-batch human-reviewed-batch --input /path/to/confirmed-human-rulings.json
```

每条输入含 `id`（其它字段内容 hash）、`version=1`、`kind=human_review_adjudication`、精确 `review_id`、非空 `human/at/reason/evidence`、`outcome=dismissed/confirmed_violation`。时间不早于复核，未来裁决不能解锁当前报告。引用限定于该复核绑定案例。每条复核最多一个最终裁决，重复/断裂引用拒绝。接口保存显式身份声明，不提供登录鉴权或签名；agent 不得填人名冒充用户决定。

`dismissed` 仅解除这一条意见，不改原评分，不清除其它 blocker/已确认失败。`confirmed_violation` 追加 `review_violation:<review_id>` FAIL，也不改原评分。原 judge 自报争议的整案判分仍走既有 `adjudicate`。两者不能相互冒充。需要纠正已最终导入的人工裁决时，当前扩展不支持覆盖，应另行明确版本化方案。

## 验证

`test_acceptance_disputes.py` 经临时文件、真实 store、CLI 子进程验证公共导入、重启、继承、重复/断链/跨案/漂移拒绝、人工处理、未来时间及校准来源阻塞，与既有 acceptance 用例一起执行。r3 六份 judge/70 次合法引用仅为结构事实；四条意见待人工处理，不代表语义校准、批准金标或发布验收。
