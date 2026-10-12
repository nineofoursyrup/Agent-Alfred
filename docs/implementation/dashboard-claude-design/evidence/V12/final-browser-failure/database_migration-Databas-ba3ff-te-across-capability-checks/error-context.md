# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: database_migration.spec.js >> Database catalog preserves every approved field and expanded state across capability checks
- Location: tests/browser/database_migration.spec.js:14:1

# Error details

```
Test timeout of 15000ms exceeded.
```

```
Error: expect(locator).toBeVisible() failed

Locator: getByText('可执行', { exact: true })
Expected: visible
Timeout: 4000ms
Error: element(s) not found

Call log:
  - Expect "toBeVisible" getByText('可执行', { exact: true }) with timeout 4000ms
  - waiting for getByText('可执行', { exact: true })

```

```yaml
- banner "导航面板":
  - link "Alfred 本地助手":
    - /url: /overview
  - paragraph: 已同步
  - paragraph: 127.0.0.1:18120
  - navigation "主导航":
    - paragraph: 工作区
    - link "总览":
      - /url: /overview
    - link "收件箱":
      - /url: /inbox
    - link "运行":
      - /url: /runs
    - link "记忆":
      - /url: /memory
    - paragraph: Agent 配置
    - link "模型":
      - /url: /models
    - link "连接":
      - /url: /connections
    - link "行为":
      - /url: /behaviour
    - link "工具":
      - /url: /tools
    - paragraph: 诊断
    - link "用量账本":
      - /url: /ops
    - link "数据库":
      - /url: /database
  - paragraph: 本地优先 · 共享运行宿主
- main:
  - paragraph: DATABASE
  - heading "数据库" [level=1]
  - paragraph: 只读查询当前实例的诊断数据，结果遵守脱敏与遗忘边界。
  - button "打开主对话" [expanded]
  - region "运行与连接摘要":
    - paragraph: 尚未选择会话
  - paragraph: 查询受保护的临时数据。SQL 的 WHERE／LIMIT 不缩小准备范围。结果有上限，不是数据库总量。
  - region "诊断对象目录":
    - paragraph: 当前实例固定。记忆修订 20 · 保护规则版本 5
    - paragraph: 可用性：核验中
    - paragraph: 执行预算 5 秒 · 最多 1000 行／64 列／2 MiB JSON
    - group:
      - text: 对象目录
      - group:
        - text: diag_sessions
        - paragraph: 来源 sessions
        - paragraph: 无虚构 title
        - term: session_id
        - definition: TEXT · NOT NULL · 身份字段；来源 sessions.session_id；主键或引用身份，保护改变时失败
        - term: created_at
        - definition: TEXT · NOT NULL · 非身份字段；来源 sessions.created_at；原物理类型保留
        - term: activity_revision
        - definition: INTEGER · NOT NULL · 身份字段；来源 sessions.activity_revision；主键或引用身份，保护改变时失败
        - text: SELECT session_id, created_at, activity_revision FROM diag_sessions LIMIT 20
        - button "写入编辑器"
      - group:
        - text: diag_runs
        - paragraph: 来源 runs
        - paragraph: 不输出 prompt_preview 或 telemetry
        - term: run_id
        - definition: TEXT · NOT NULL · 身份字段；来源 runs.run_id；主键或引用身份，保护改变时失败
        - term: purpose
        - definition: TEXT · NOT NULL · 非身份字段；来源 runs.purpose；原物理类型保留
        - term: session_id
        - definition: TEXT · 可空 · 身份字段；来源 runs.session_id；主键或引用身份，保护改变时失败
        - term: gateway
        - definition: TEXT · NOT NULL · 非身份字段；来源 runs.gateway；原物理类型保留
        - term: entry_surface_id
        - definition: TEXT · 可空 · 身份字段；来源 runs.entry_surface_id；主键或引用身份，保护改变时失败
        - term: phase
        - definition: TEXT · NOT NULL · 非身份字段；来源 runs.phase；原物理类型保留
        - term: outcome
        - definition: TEXT · 可空 · 非身份字段；来源 runs.outcome；合法缺失为 NULL
        - term: accepted_at
        - definition: TEXT · NOT NULL · 非身份字段；来源 runs.accepted_at；原物理类型保留
        - term: started_at
        - definition: TEXT · 可空 · 非身份字段；来源 runs.started_at；合法缺失为 NULL
        - term: finished_at
        - definition: TEXT · 可空 · 非身份字段；来源 runs.finished_at；合法缺失为 NULL
        - term: activity_revision
        - definition: INTEGER · NOT NULL · 身份字段；来源 runs.activity_revision；主键或引用身份，保护改变时失败
        - term: admission_state
        - definition: TEXT · NOT NULL · 非身份字段；来源 runs.admission_state；原物理类型保留
        - text: SELECT run_id, purpose, session_id, gateway, entry_surface_id, phase, outcome, accepted_at, started_at, finished_at, activity_revision, admission_state FROM diag_runs LIMIT 20
        - button "写入编辑器"
      - group:
        - text: diag_messages
        - paragraph: 来源 agent_log
        - paragraph: 仅 user/assistant；text 为保护后 TextBlock 按原顺序拼接
        - term: message_id
        - definition: INTEGER · NOT NULL · 身份字段；来源 agent_log.id；主键或引用身份，保护改变时失败
        - term: session_id
        - definition: TEXT · NOT NULL · 身份字段；来源 agent_log.session_id；主键或引用身份，保护改变时失败
        - term: run_id
        - definition: TEXT · 可空 · 身份字段；来源 agent_log.run_id；主键或引用身份，保护改变时失败
        - term: role
        - definition: TEXT · NOT NULL · 非身份字段；来源 agent_log.role；原物理类型保留
        - term: source
        - definition: TEXT · NOT NULL · 非身份字段；来源 agent_log.source；原物理类型保留
        - term: created_at
        - definition: TEXT · NOT NULL · 非身份字段；来源 agent_log.created_at；原物理类型保留
        - term: consolidated
        - definition: INTEGER · NOT NULL · 非身份字段；来源 agent_log.consolidated；INTEGER 0/1
        - term: text
        - definition: TEXT · NOT NULL · 非身份字段；来源 agent_log.content TextBlock；开放字符串，保护后可见
        - text: SELECT message_id, session_id, run_id, role, source, created_at, consolidated, text FROM diag_messages LIMIT 20
        - button "写入编辑器"
      - group:
        - text: diag_attempts
        - paragraph: 来源 runs.telemetry.attempts
        - paragraph: 仅已知字段；损坏 fail-closed
        - term: run_id
        - definition: TEXT · NOT NULL · 身份字段；来源 runs.run_id；主键或引用身份，保护改变时失败
        - term: attempt_id
        - definition: TEXT · NOT NULL · 身份字段；来源 runs.telemetry.attempts.attempt_id；主键或引用身份，保护改变时失败
        - term: outcome
        - definition: TEXT · 可空 · 非身份字段；来源 attempts.outcome；合法缺失为 NULL
        - term: endpoint_id
        - definition: TEXT · 可空 · 非身份字段；来源 attempts.model.endpoint_id；合法缺失为 NULL
        - term: model_id
        - definition: TEXT · 可空 · 非身份字段；来源 attempts.model.model_id；合法缺失为 NULL
        - term: total_input_tokens
        - definition: INTEGER · 可空 · 非身份字段；来源 usage.total_input_tokens；合法缺失为 NULL
        - term: uncached_input_tokens
        - definition: INTEGER · 可空 · 非身份字段；来源 usage.uncached_input_tokens；合法缺失为 NULL
        - term: cache_read_tokens
        - definition: INTEGER · 可空 · 非身份字段；来源 usage.cache_read_tokens；合法缺失为 NULL
        - term: cache_write_tokens
        - definition: INTEGER · 可空 · 非身份字段；来源 usage.cache_write_tokens；合法缺失为 NULL
        - term: output_tokens
        - definition: INTEGER · 可空 · 非身份字段；来源 usage.output_tokens；合法缺失为 NULL
        - term: reasoning_tokens
        - definition: INTEGER · 可空 · 非身份字段；来源 usage.reasoning_tokens；合法缺失为 NULL
        - term: endpoint_reported_cost_usd
        - definition: TEXT · 可空 · 非身份字段；来源 usage.endpoint_reported_cost_usd；合法缺失为 NULL
        - text: SELECT run_id, attempt_id, outcome, endpoint_id, model_id, total_input_tokens, uncached_input_tokens, cache_read_tokens, cache_write_tokens, output_tokens, reasoning_tokens, endpoint_reported_cost_usd FROM diag_attempts LIMIT 20
        - button "写入编辑器"
      - group:
        - text: diag_attempt_coverage
        - paragraph: 来源 runs
        - paragraph: 每个 Run 一行：recorded / recorded_empty / unrecorded
        - term: run_id
        - definition: TEXT · NOT NULL · 身份字段；来源 runs.run_id；主键或引用身份，保护改变时失败
        - term: state
        - definition: TEXT · NOT NULL · 非身份字段；来源 telemetry 覆盖规则；原物理类型保留
        - term: recorded_count
        - definition: INTEGER · 可空 · 非身份字段；来源 attempts 列表长度；合法缺失为 NULL
        - term: run_finished
        - definition: INTEGER · NOT NULL · 非身份字段；来源 phase=finished；INTEGER 0/1
        - text: SELECT run_id, state, recorded_count, run_finished FROM diag_attempt_coverage LIMIT 20
        - button "写入编辑器"
      - group:
        - text: diag_facts
        - paragraph: 来源 facts + memory_provenance
        - paragraph: 后五列派生；不恢复已删除事实
        - term: id
        - definition: TEXT · NOT NULL · 身份字段；来源 facts.id；主键或引用身份，保护改变时失败
        - term: record_version
        - definition: INTEGER · NOT NULL · 身份字段；来源 facts.record_version；主键或引用身份，保护改变时失败
        - term: subject
        - definition: TEXT · NOT NULL · 非身份字段；来源 facts.subject；开放字符串，保护后可见
        - term: fact
        - definition: TEXT · NOT NULL · 非身份字段；来源 facts.fact；开放字符串，保护后可见
        - term: origin_kind
        - definition: TEXT · NOT NULL · 非身份字段；来源 facts.origin_kind；原物理类型保留
        - term: origin_batch_id
        - definition: TEXT · 可空 · 身份字段；来源 facts.origin_batch_id；主键或引用身份，保护改变时失败
        - term: origin_source
        - definition: TEXT · 可空 · 非身份字段；来源 facts.origin_source；原物理类型保留
        - term: origin_call_id
        - definition: TEXT · 可空 · 身份字段；来源 facts.origin_call_id；主键或引用身份，保护改变时失败
        - term: created_at
        - definition: TEXT · NOT NULL · 非身份字段；来源 facts.created_at；原物理类型保留
        - term: modified_at
        - definition: TEXT · 可空 · 非身份字段；来源 facts.modified_at；合法缺失为 NULL
        - term: human_protected
        - definition: INTEGER · NOT NULL · 非身份字段；来源 facts.human_protected；INTEGER 0/1
        - term: last_change_origin_type
        - definition: TEXT · 可空 · 非身份字段；来源 facts.last_change_origin；原物理类型保留
        - term: last_change_origin_source
        - definition: TEXT · 可空 · 非身份字段；来源 facts.last_change_origin；原物理类型保留
        - term: last_change_origin_call_id
        - definition: TEXT · 可空 · 身份字段；来源 facts.last_change_origin；主键或引用身份，保护改变时失败
        - term: last_change_origin_batch_id
        - definition: TEXT · 可空 · 身份字段；来源 facts.last_change_origin；主键或引用身份，保护改变时失败
        - term: provenance_state
        - definition: TEXT · NOT NULL · 非身份字段；来源 memory_provenance.state；原物理类型保留
        - text: SELECT id, record_version, subject, fact, origin_kind, origin_batch_id, origin_source, origin_call_id, created_at, modified_at, human_protected, last_change_origin_type, last_change_origin_source, last_change_origin_call_id, last_change_origin_batch_id, provenance_state FROM diag_facts LIMIT 20
        - button "写入编辑器"
      - group:
        - text: diag_episodes
        - paragraph: 来源 episodes + memory_provenance
        - paragraph: 派生规则同 facts
        - term: id
        - definition: TEXT · NOT NULL · 身份字段；来源 episodes.id；主键或引用身份，保护改变时失败
        - term: record_version
        - definition: INTEGER · NOT NULL · 身份字段；来源 episodes.record_version；主键或引用身份，保护改变时失败
        - term: summary
        - definition: TEXT · NOT NULL · 非身份字段；来源 episodes.summary；开放字符串，保护后可见
        - term: occurred_at
        - definition: TEXT · 可空 · 非身份字段；来源 episodes.occurred_at；合法缺失为 NULL
        - term: occurred_until
        - definition: TEXT · 可空 · 非身份字段；来源 episodes.occurred_until；合法缺失为 NULL
        - term: origin_kind
        - definition: TEXT · NOT NULL · 非身份字段；来源 episodes.origin_kind；原物理类型保留
        - term: origin_batch_id
        - definition: TEXT · 可空 · 身份字段；来源 episodes.origin_batch_id；主键或引用身份，保护改变时失败
        - term: origin_source
        - definition: TEXT · 可空 · 非身份字段；来源 episodes.origin_source；原物理类型保留
        - term: origin_call_id
        - definition: TEXT · 可空 · 身份字段；来源 episodes.origin_call_id；主键或引用身份，保护改变时失败
        - term: created_at
        - definition: TEXT · NOT NULL · 非身份字段；来源 episodes.created_at；原物理类型保留
        - term: modified_at
        - definition: TEXT · 可空 · 非身份字段；来源 episodes.modified_at；合法缺失为 NULL
        - term: human_protected
        - definition: INTEGER · NOT NULL · 非身份字段；来源 episodes.human_protected；INTEGER 0/1
        - term: last_change_origin_type
        - definition: TEXT · 可空 · 非身份字段；来源 episodes.last_change_origin；原物理类型保留
        - term: last_change_origin_source
        - definition: TEXT · 可空 · 非身份字段；来源 episodes.last_change_origin；原物理类型保留
        - term: last_change_origin_call_id
        - definition: TEXT · 可空 · 身份字段；来源 episodes.last_change_origin；主键或引用身份，保护改变时失败
        - term: last_change_origin_batch_id
        - definition: TEXT · 可空 · 身份字段；来源 episodes.last_change_origin；主键或引用身份，保护改变时失败
        - term: provenance_state
        - definition: TEXT · NOT NULL · 非身份字段；来源 memory_provenance.state；原物理类型保留
        - text: SELECT id, record_version, summary, occurred_at, occurred_until, origin_kind, origin_batch_id, origin_source, origin_call_id, created_at, modified_at, human_protected, last_change_origin_type, last_change_origin_source, last_change_origin_call_id, last_change_origin_batch_id, provenance_state FROM diag_episodes LIMIT 20
        - button "写入编辑器"
      - group:
        - text: diag_memory_sources
        - paragraph: 来源 memory_sources
        - paragraph: 版本化来源关系
        - term: kind
        - definition: TEXT · NOT NULL · 身份字段；来源 memory_sources.kind；主键或引用身份，保护改变时失败
        - term: memory_id
        - definition: TEXT · NOT NULL · 身份字段；来源 memory_sources.memory_id；主键或引用身份，保护改变时失败
        - term: record_version
        - definition: INTEGER · NOT NULL · 身份字段；来源 memory_sources.record_version；主键或引用身份，保护改变时失败
        - term: source_group_id
        - definition: TEXT · NOT NULL · 身份字段；来源 memory_sources.source_group_id；主键或引用身份，保护改变时失败
        - text: SELECT kind, memory_id, record_version, source_group_id FROM diag_memory_sources LIMIT 20
        - button "写入编辑器"
      - group:
        - text: diag_history_groups
        - paragraph: 来源 history_groups LEFT JOIN history_group_times
        - paragraph: 不开放 evidence_id；时间缺失为 NULL
        - term: group_id
        - definition: TEXT · NOT NULL · 身份字段；来源 history_groups.group_id；主键或引用身份，保护改变时失败
        - term: kind
        - definition: TEXT · NOT NULL · 非身份字段；来源 history_groups.kind；原物理类型保留
        - term: container_id
        - definition: TEXT · 可空 · 身份字段；来源 history_groups.container_id；主键或引用身份，保护改变时失败
        - term: evidence
        - definition: TEXT · NOT NULL · 非身份字段；来源 history_groups.evidence；原物理类型保留
        - term: occurred_at
        - definition: TEXT · 可空 · 非身份字段；来源 history_group_times.occurred_at；合法缺失为 NULL
        - text: SELECT group_id, kind, container_id, evidence, occurred_at FROM diag_history_groups LIMIT 20
        - button "写入编辑器"
      - group:
        - text: diag_memory_uses
        - paragraph: 来源 memory_uses
        - paragraph: 不把 consumer 伪称 run_id
        - term: kind
        - definition: TEXT · NOT NULL · 身份字段；来源 memory_uses.kind；主键或引用身份，保护改变时失败
        - term: memory_id
        - definition: TEXT · NOT NULL · 身份字段；来源 memory_uses.memory_id；主键或引用身份，保护改变时失败
        - term: record_version
        - definition: INTEGER · NOT NULL · 身份字段；来源 memory_uses.record_version；主键或引用身份，保护改变时失败
        - term: consumer
        - definition: TEXT · NOT NULL · 身份字段；来源 memory_uses.consumer；主键或引用身份，保护改变时失败
        - term: attempt_id
        - definition: TEXT · NOT NULL · 身份字段；来源 memory_uses.attempt_id；主键或引用身份，保护改变时失败
        - term: purpose
        - definition: TEXT · NOT NULL · 非身份字段；来源 memory_uses.purpose；原物理类型保留
        - text: SELECT kind, memory_id, record_version, consumer, attempt_id, purpose FROM diag_memory_uses LIMIT 20
        - button "写入编辑器"
      - group:
        - text: diag_history_reads
        - paragraph: 来源 history_reads
        - paragraph: 组到组的实际读取关系
        - term: source
        - definition: TEXT · NOT NULL · 身份字段；来源 history_reads.source；主键或引用身份，保护改变时失败
        - term: consumer
        - definition: TEXT · NOT NULL · 身份字段；来源 history_reads.consumer；主键或引用身份，保护改变时失败
        - term: attempt_id
        - definition: TEXT · NOT NULL · 身份字段；来源 history_reads.attempt_id；主键或引用身份，保护改变时失败
        - term: purpose
        - definition: TEXT · NOT NULL · 非身份字段；来源 history_reads.purpose；原物理类型保留
        - text: SELECT source, consumer, attempt_id, purpose FROM diag_history_reads LIMIT 20
        - button "写入编辑器"
      - group:
        - text: diag_tool_metering
        - paragraph: 来源 tool_metering
        - paragraph: reason_code 与 cost_* 为安全派生，不开放 cost 原 JSON
        - term: run_id
        - definition: TEXT · NOT NULL · 身份字段；来源 tool_metering.run_id；主键或引用身份，保护改变时失败
        - term: step_index
        - definition: INTEGER · NOT NULL · 身份字段；来源 tool_metering.step_index；主键或引用身份，保护改变时失败
        - term: call_id
        - definition: TEXT · NOT NULL · 身份字段；来源 tool_metering.call_id；主键或引用身份，保护改变时失败
        - term: ordinal
        - definition: INTEGER · NOT NULL · 非身份字段；来源 tool_metering.ordinal；原物理类型保留
        - term: tool_name
        - definition: TEXT · NOT NULL · 非身份字段；来源 tool_metering.tool_name；开放字符串，保护后可见
        - term: source_id
        - definition: TEXT · 可空 · 非身份字段；来源 tool_metering.source_id；主键或引用身份，保护改变时失败
        - term: capability_id
        - definition: TEXT · 可空 · 非身份字段；来源 tool_metering.capability_id；主键或引用身份，保护改变时失败
        - term: effect
        - definition: TEXT · 可空 · 非身份字段；来源 tool_metering.effect；原物理类型保留
        - term: requested_at
        - definition: TEXT · NOT NULL · 非身份字段；来源 tool_metering.requested_at；原物理类型保留
        - term: start_confirmation
        - definition: TEXT · NOT NULL · 非身份字段；来源 tool_metering.start_confirmation；原物理类型保留
        - term: result
        - definition: TEXT · NOT NULL · 非身份字段；来源 tool_metering.result；原物理类型保留
        - term: reason_code
        - definition: TEXT · 可空 · 非身份字段；来源 tool_metering.reason 安全映射；原物理类型保留
        - term: finished_at
        - definition: TEXT · 可空 · 非身份字段；来源 tool_metering.finished_at；合法缺失为 NULL
        - term: operation_id
        - definition: TEXT · 可空 · 身份字段；来源 tool_metering.operation_id；主键或引用身份，保护改变时失败
        - term: model_delivery
        - definition: TEXT · NOT NULL · 非身份字段；来源 tool_metering.model_delivery；原物理类型保留
        - term: cost_kind
        - definition: TEXT · NOT NULL · 非身份字段；来源 tool_metering.cost.kind；原物理类型保留
        - term: cost_units
        - definition: TEXT · 可空 · 非身份字段；来源 tool_metering.cost.units；合法缺失为 NULL
        - term: cost_unit
        - definition: TEXT · 可空 · 非身份字段；来源 tool_metering.cost.unit；合法缺失为 NULL
        - term: cost_source
        - definition: TEXT · 可空 · 非身份字段；来源 tool_metering.cost.source；合法缺失为 NULL
        - term: cost_service
        - definition: TEXT · 可空 · 非身份字段；来源 tool_metering.cost.service；合法缺失为 NULL
        - term: cost_reason
        - definition: TEXT · 可空 · 非身份字段；来源 tool_metering.cost.reason 安全映射；原物理类型保留
        - text: SELECT run_id, step_index, call_id, ordinal, tool_name, source_id, capability_id, effect, requested_at, start_confirmation, result, reason_code, finished_at, operation_id, model_delivery, cost_kind, cost_units, cost_unit, cost_source, cost_service, cost_reason FROM diag_tool_metering LIMIT 20
        - button "写入编辑器"
      - group:
        - text: diag_tool_ledger
        - paragraph: 来源 tool_ledger
        - paragraph: 不开放 summary／fingerprint，不虚构 step_index
        - term: id
        - definition: INTEGER · NOT NULL · 身份字段；来源 tool_ledger.id；主键或引用身份，保护改变时失败
        - term: tool_name
        - definition: TEXT · NOT NULL · 非身份字段；来源 tool_ledger.tool_name；开放字符串，保护后可见
        - term: effect
        - definition: TEXT · NOT NULL · 非身份字段；来源 tool_ledger.effect；原物理类型保留
        - term: status
        - definition: TEXT · NOT NULL · 非身份字段；来源 tool_ledger.status；原物理类型保留
        - term: call_id
        - definition: TEXT · 可空 · 身份字段；来源 tool_ledger.call_id；主键或引用身份，保护改变时失败
        - term: run_id
        - definition: TEXT · 可空 · 身份字段；来源 tool_ledger.run_id；主键或引用身份，保护改变时失败
        - term: session_id
        - definition: TEXT · 可空 · 身份字段；来源 tool_ledger.session_id；主键或引用身份，保护改变时失败
        - term: created_at
        - definition: TEXT · NOT NULL · 非身份字段；来源 tool_ledger.created_at；原物理类型保留
        - term: updated_at
        - definition: TEXT · 可空 · 非身份字段；来源 tool_ledger.updated_at；合法缺失为 NULL
        - text: SELECT id, tool_name, effect, status, call_id, run_id, session_id, created_at, updated_at FROM diag_tool_ledger LIMIT 20
        - button "写入编辑器"
      - group:
        - text: diag_tool_operation_links
        - paragraph: 来源 external_tool_operations
        - paragraph: 已持久关联，不开放 receipt／fingerprint
        - term: run_id
        - definition: TEXT · NOT NULL · 身份字段；来源 external_tool_operations.run_id；主键或引用身份，保护改变时失败
        - term: step_index
        - definition: INTEGER · NOT NULL · 身份字段；来源 external_tool_operations.step_index；主键或引用身份，保护改变时失败
        - term: call_id
        - definition: TEXT · NOT NULL · 身份字段；来源 external_tool_operations.call_id；主键或引用身份，保护改变时失败
        - term: ledger_id
        - definition: INTEGER · NOT NULL · 身份字段；来源 external_tool_operations.ledger_id；主键或引用身份，保护改变时失败
        - text: SELECT run_id, step_index, call_id, ledger_id FROM diag_tool_operation_links LIMIT 20
        - button "写入编辑器"
      - group:
        - text: diag_calendar_entries
        - paragraph: 来源 calendar_entries
        - paragraph: 正文经保护
        - term: id
        - definition: INTEGER · NOT NULL · 身份字段；来源 calendar_entries.id；主键或引用身份，保护改变时失败
        - term: title
        - definition: TEXT · NOT NULL · 非身份字段；来源 calendar_entries.title；开放字符串，保护后可见
        - term: starts_at
        - definition: TEXT · NOT NULL · 非身份字段；来源 calendar_entries.starts_at；原物理类型保留
        - term: ends_at
        - definition: TEXT · 可空 · 非身份字段；来源 calendar_entries.ends_at；合法缺失为 NULL
        - term: iana_time_zone
        - definition: TEXT · 可空 · 非身份字段；来源 calendar_entries.iana_time_zone；原物理类型保留
        - term: participants
        - definition: TEXT · 可空 · 非身份字段；来源 calendar_entries.participants；开放字符串，保护后可见
        - term: notes
        - definition: TEXT · 可空 · 非身份字段；来源 calendar_entries.notes；开放字符串，保护后可见
        - term: created_at
        - definition: TEXT · NOT NULL · 非身份字段；来源 calendar_entries.created_at；原物理类型保留
        - text: SELECT id, title, starts_at, ends_at, iana_time_zone, participants, notes, created_at FROM diag_calendar_entries LIMIT 20
        - button "写入编辑器"
      - group:
        - text: diag_forget_operations
        - paragraph: 来源 forget_operations
        - paragraph: current_state 为纯读推导，不记录观察
        - term: operation_id
        - definition: TEXT · NOT NULL · 身份字段；来源 forget_operations.operation_id；主键或引用身份，保护改变时失败
        - term: kind
        - definition: TEXT · NOT NULL · 非身份字段；来源 forget_operations.kind；原物理类型保留
        - term: memory_id
        - definition: TEXT · NOT NULL · 身份字段；来源 forget_operations.memory_id；主键或引用身份，保护改变时失败
        - term: completeness
        - definition: TEXT · NOT NULL · 非身份字段；来源 forget_operations.completeness；原物理类型保留
        - term: created_at
        - definition: TEXT · NOT NULL · 非身份字段；来源 forget_operations.created_at；原物理类型保留
        - term: current_state
        - definition: TEXT · NOT NULL · 非身份字段；来源 operation_state() 纯读；原物理类型保留
        - text: SELECT operation_id, kind, memory_id, completeness, created_at, current_state FROM diag_forget_operations LIMIT 20
        - button "写入编辑器"
      - group:
        - text: diag_forget_limits
        - paragraph: 来源 forget_limits
        - term: operation_id
        - definition: TEXT · NOT NULL · 身份字段；来源 forget_limits.operation_id；主键或引用身份，保护改变时失败
        - term: group_id
        - definition: TEXT · NOT NULL · 身份字段；来源 forget_limits.group_id；主键或引用身份，保护改变时失败
        - term: mode
        - definition: TEXT · NOT NULL · 非身份字段；来源 forget_limits.mode；原物理类型保留
        - text: SELECT operation_id, group_id, mode FROM diag_forget_limits LIMIT 20
        - button "写入编辑器"
      - group:
        - text: diag_forget_cleanup
        - paragraph: 来源 forget_cleanup
        - paragraph: error_code 从实际 error 映射安全标记
        - term: operation_id
        - definition: TEXT · NOT NULL · 身份字段；来源 forget_cleanup.operation_id；主键或引用身份，保护改变时失败
        - term: target_id
        - definition: TEXT · NOT NULL · 身份字段；来源 forget_cleanup.target_id；主键或引用身份，保护改变时失败
        - term: revision
        - definition: INTEGER · NOT NULL · 非身份字段；来源 forget_cleanup.revision；原物理类型保留
        - term: state
        - definition: TEXT · NOT NULL · 非身份字段；来源 forget_cleanup.state；原物理类型保留
        - term: error_code
        - definition: TEXT · 可空 · 非身份字段；来源 forget_cleanup.error 安全映射；原物理类型保留
        - text: SELECT operation_id, target_id, revision, state, error_code FROM diag_forget_cleanup LIMIT 20
        - button "写入编辑器"
      - group:
        - text: diag_consolidation_batches
        - paragraph: 来源 memory_consolidation_batches
        - paragraph: 不使用同名近似旧表，不输出 receipt
        - term: batch_id
        - definition: TEXT · NOT NULL · 身份字段；来源 memory_consolidation_batches.batch_id；主键或引用身份，保护改变时失败
        - term: session_id
        - definition: TEXT · NOT NULL · 身份字段；来源 memory_consolidation_batches.session_id；主键或引用身份，保护改变时失败
        - term: revision
        - definition: INTEGER · NOT NULL · 身份字段；来源 memory_consolidation_batches.revision；主键或引用身份，保护改变时失败
        - term: status
        - definition: TEXT · NOT NULL · 非身份字段；来源 memory_consolidation_batches.status；原物理类型保留
        - term: created_at
        - definition: TEXT · NOT NULL · 非身份字段；来源 memory_consolidation_batches.created_at；原物理类型保留
        - term: updated_at
        - definition: TEXT · NOT NULL · 非身份字段；来源 memory_consolidation_batches.updated_at；原物理类型保留
        - term: finished_at
        - definition: TEXT · 可空 · 非身份字段；来源 memory_consolidation_batches.finished_at；合法缺失为 NULL
        - term: error_code
        - definition: TEXT · 可空 · 非身份字段；来源 error_code 安全映射；原物理类型保留
        - term: generation_run_id
        - definition: TEXT · 可空 · 身份字段；来源 memory_consolidation_batches.generation_run_id；主键或引用身份，保护改变时失败
        - text: SELECT batch_id, session_id, revision, status, created_at, updated_at, finished_at, error_code, generation_run_id FROM diag_consolidation_batches LIMIT 20
        - button "写入编辑器"
      - group:
        - text: diag_consolidation_sources
        - paragraph: 来源 memory_consolidation_sources
        - paragraph: 按 batch_id+revision 关联
        - term: batch_id
        - definition: TEXT · NOT NULL · 身份字段；来源 memory_consolidation_sources.batch_id；主键或引用身份，保护改变时失败
        - term: revision
        - definition: INTEGER · NOT NULL · 身份字段；来源 memory_consolidation_sources.revision；主键或引用身份，保护改变时失败
        - term: run_id
        - definition: TEXT · NOT NULL · 身份字段；来源 memory_consolidation_sources.run_id；主键或引用身份，保护改变时失败
        - term: session_id
        - definition: TEXT · NOT NULL · 身份字段；来源 memory_consolidation_sources.session_id；主键或引用身份，保护改变时失败
        - term: ordinal
        - definition: INTEGER · NOT NULL · 非身份字段；来源 memory_consolidation_sources.ordinal；原物理类型保留
        - term: accepted_at
        - definition: TEXT · NOT NULL · 非身份字段；来源 memory_consolidation_sources.accepted_at；原物理类型保留
        - term: finished_at
        - definition: TEXT · NOT NULL · 非身份字段；来源 memory_consolidation_sources.finished_at；原物理类型保留
        - text: SELECT batch_id, revision, run_id, session_id, ordinal, accepted_at, finished_at FROM diag_consolidation_sources LIMIT 20
        - button "写入编辑器"
      - group:
        - text: diag_memory_mirrors
        - paragraph: 来源 memory_mirrors
        - paragraph: 仅持久观测，不开放路径／intent／fingerprint，不提供 ready
        - term: target_id
        - definition: TEXT · NOT NULL · 身份字段；来源 memory_mirrors.target_id；主键或引用身份，保护改变时失败
        - term: required_generation
        - definition: INTEGER · NOT NULL · 非身份字段；来源 memory_mirrors.required_generation；原物理类型保留
        - term: generated_generation
        - definition: INTEGER · NOT NULL · 非身份字段；来源 memory_mirrors.generated_generation；原物理类型保留
        - term: verified_generation
        - definition: INTEGER · NOT NULL · 非身份字段；来源 memory_mirrors.verified_generation；原物理类型保留
        - term: covered_cleanup_generation
        - definition: INTEGER · NOT NULL · 非身份字段；来源 memory_mirrors.covered_cleanup_generation；原物理类型保留
        - term: generated_at
        - definition: TEXT · 可空 · 非身份字段；来源 memory_mirrors.generated_at；合法缺失为 NULL
        - term: verified_at
        - definition: TEXT · 可空 · 非身份字段；来源 memory_mirrors.verified_at；合法缺失为 NULL
        - term: conflict
        - definition: INTEGER · NOT NULL · 非身份字段；来源 memory_mirrors.conflict；INTEGER 0/1
        - term: error_code
        - definition: TEXT · 可空 · 非身份字段；来源 memory_mirrors.error 安全映射；原物理类型保留
        - text: SELECT target_id, required_generation, generated_generation, verified_generation, covered_cleanup_generation, generated_at, verified_at, conflict, error_code FROM diag_memory_mirrors LIMIT 20
        - button "写入编辑器"
    - group:
      - text: 完整限制与寿命
      - term: 当前实例
      - definition: d6334c6eea3c450eada417107380a32a
      - term: SQL UTF-8
      - definition: 64 KiB
      - term: 源单值／源行
      - definition: 2 MiB／2 MiB
      - term: 保护后输入
      - definition: 64 MiB
      - term: SQLite heap
      - definition: 128 MiB
      - term: 返回上限
      - definition: 1000 行／64 列／2 MiB JSON
      - term: 本地分页
      - definition: 100 行；翻页不重发 SQL
      - term: 执行预算
      - definition: 5 秒
      - term: 活动查询
      - definition: 一次一个；句柄签发不等于执行获准
      - term: 句柄
      - definition: 未执行 30 秒到期；终态保留 60 秒；容量 64
      - term: 取消／HTTP IO
      - definition: 各 1 秒预算（既有服务合同，非目录 DTO 字段）
      - paragraph: 句柄期限不是结果 TTL。已显示结果没有固定倒计时；读取时刻持续可见，遗忘、保护变化、断连或真正离页会使其失效。
  - text: SQL 草稿
  - textbox "SQL":
    - /placeholder: SELECT * FROM diag_sessions LIMIT 20
  - paragraph: Enter 换行。仅点击执行才提交；查询中可编辑下一份草稿。
  - button "执行" [disabled]
  - button "取消" [disabled]
  - button "核验查询与可用性"
  - button "复制 SQL" [disabled]
  - paragraph: 数据库能力尚未确认可用；请重新核验。
  - paragraph: 记忆或保护版本变化：已清除结果及提交关联；草稿保留，核验后仍需显式执行。
  - region "查询与资源状态":
    - heading "执行" [level=3]
    - paragraph: 核验中
    - heading "正文" [level=3]
    - paragraph: 尚未收到查询正文
    - heading "清理" [level=3]
    - paragraph: 尚无查询清理责任
  - region "已提交查询"
  - button "复制本页结果" [disabled]
  - status: 尚无有效结果可复制。
  - region "查询结果"
- region "MainBar":
  - heading "主对话" [level=2]
  - button "收起主对话"
  - text: 尚未选择会话
  - button "新建会话"
  - region "主对话"
  - text: 消息
  - textbox "消息" [disabled]:
    - /placeholder: 写下消息；/skills 指定流程…
  - button "发送" [disabled]
  - paragraph: 请显式新建或继续会话。
- status: 就绪
- alert
```

# Test source

```ts
  1   | import {test, expect} from '@playwright/test';
  2   | import {writeFile} from 'node:fs/promises';
  3   |
  4   | const run = page => page.getByRole('button', {name:'执行', exact:true});
  5   | const result = page => page.getByRole('region', {name:'查询结果', exact:true});
  6   | const editor = page => page.getByRole('textbox', {name:'SQL', exact:true});
  7   | async function open(page) {
  8   |   await page.goto('/database');
  9   |   await expect(page.getByText('可执行', {exact:true})).toBeVisible();
  10  | }
  11  |
  12  | // Check the API-to-view boundary, including fields that the previous catalog
  13  | // omitted, without creating a second source of production catalog definitions.
  14  | test('Database catalog preserves every approved field and expanded state across capability checks', async ({page}) => {
  15  |   await open(page);
  16  |   const catalog = await (await page.request.get('/api/database')).json();
  17  |   await expect(page.locator('.db-catalog > details').first()).not.toHaveAttribute('open');
  18  |   await page.getByText('对象目录', {exact:true}).click();
  19  |   const details = page.locator('.db-catalog details[data-object]');
  20  |   expect(catalog.objects).toHaveLength(21);
  21  |   await expect(details).toHaveCount(21);
  22  |   for (const object of catalog.objects) {
  23  |     const item = page.locator(`[data-object="${object.name}"]`);
  24  |     await item.locator('summary').click();
  25  |     await expect(item.locator('dt')).toHaveText(object.columns.map(column => column.name));
  26  |     await expect(item.locator('dd')).toHaveText(object.columns.map(column =>
  27  |       `${column.type} · ${column.nullable ? '可空' : 'NOT NULL'} · ${column.identity ? '身份字段' : '非身份字段'}；来源 ${column.origin}；${column.protection}`));
  28  |     await expect(item.locator('pre')).toHaveText(object.example);
  29  |     await expect(item).toContainText(object.source);
  30  |     if (object.notes) await expect(item).toContainText(object.notes);
  31  |   }
  32  |   await page.getByText('完整限制与寿命', {exact:true}).click();
  33  |   const limits = page.locator('.db-catalog > details').last();
  34  |   await expect(limits).toContainText('64 KiB');
  35  |   await expect(limits).toContainText('64 MiB');
  36  |   await expect(limits).toContainText('128 MiB');
  37  |   await expect(limits).toContainText('2 MiB');
  38  |   await expect(limits).toContainText('各 1 秒预算');
  39  |   await expect(limits).toContainText('未执行 30 秒到期；终态保留 60 秒；容量 64');
  40  |   await expect(limits).toContainText('句柄期限不是结果 TTL');
  41  |   await page.getByRole('button', {name:'核验查询与可用性',exact:true}).click();
> 42  |   await expect(page.getByText('可执行', {exact:true})).toBeVisible();
      |                                                     ^ Error: expect(locator).toBeVisible() failed
  43  |   await expect(page.locator('.db-catalog details[data-object][open]')).toHaveCount(21);
  44  |   await expect(limits).toHaveAttribute('open');
  45  | });
  46  |
  47  | for (const [width,height] of [[1440,900],[1280,800],[390,844],[320,800]]) {
  48  |   test.describe(`${width}x${height}`, () => {
  49  |   test.use({viewport:{width,height},hasTouch:width<1100,deviceScaleFactor:1});
  50  |   test(`Database types, long content and keyboard actions remain reachable at ${width}x${height}`, async ({page}, testInfo) => {
  51  |     await page.setViewportSize({width,height});
  52  |     await page.emulateMedia({reducedMotion:'reduce'});
  53  |     await open(page);
  54  |     const long = '长文本 complete-value-'.repeat(40) + 'LAST-VALUE';
  55  |     await editor(page).fill(`SELECT NULL AS v, '' AS v, 1 AS v, 1.0 AS v, X'01ff' AS v, '${long}' AS long_value`);
  56  |     if (width<1100) await run(page).tap(); else await run(page).click();
  57  |     await expect(result(page).locator('th')).toHaveText(['v','v','v','v','v','long_value']);
  58  |     await expect(result(page).locator('td')).toHaveText(['NULL','""','1','1.0','BLOB 01ff (2 字节)',JSON.stringify(long)]);
  59  |     const scroll = page.getByRole('region', {name:'数据表横向滚动区',exact:true});
  60  |     await scroll.focus();
  61  |     await expect(scroll).toBeFocused();
  62  |     await page.keyboard.press('ArrowRight');
  63  |     await expect.poll(() => scroll.evaluate(el => el.scrollLeft)).toBeGreaterThan(0);
  64  |     const cell = page.getByRole('region', {name:'完整单元格值',exact:true});
  65  |     await cell.focus();
  66  |     await expect(cell).toBeFocused();
  67  |     await expect(cell).toContainText('LAST-VALUE');
  68  |     expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  69  |     expect(await page.locator('#page').evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
  70  |     for (const button of [run(page), page.getByRole('button',{name:'取消',exact:true}),
  71  |       page.getByRole('button',{name:'核验查询与可用性',exact:true}),
  72  |       page.getByRole('button',{name:'复制 SQL',exact:true}),
  73  |       page.getByRole('button',{name:'复制本页结果',exact:true})]) {
  74  |       await button.scrollIntoViewIfNeeded();
  75  |       const box = await button.boundingBox();
  76  |       expect(box.x).toBeGreaterThanOrEqual(0);
  77  |       expect(box.x+box.width).toBeLessThanOrEqual(width);
  78  |     }
  79  |     if (width < 1100) {
  80  |       await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
  81  |       await expect(editor(page)).not.toBeVisible();
  82  |       await expect(page.locator('[aria-label="查询结果"] td').last()).toHaveText(JSON.stringify(long));
  83  |       await page.getByRole('button',{name:'收起主对话',exact:true}).click();
  84  |       await expect(editor(page)).toHaveValue(/LAST-VALUE/);
  85  |     }
  86  |     const metrics = await page.evaluate(() => {
  87  |       const style = selector => {
  88  |         const computed = getComputedStyle(document.querySelector(selector));
  89  |         return {fontSize:computed.fontSize,color:computed.color,background:computed.backgroundColor};
  90  |       };
  91  |       return {viewport:[innerWidth,innerHeight], dpr:devicePixelRatio,
  92  |         central:document.querySelector('.page-body').getBoundingClientRect().width,
  93  |         body:style('.db-console'), table:style('.db-console table'), meta:style('.db-result-meta'),
  94  |         activeAnimations:document.getAnimations().length, browser:navigator.userAgent,
  95  |         maxTouchPoints:navigator.maxTouchPoints,
  96  |         scope:innerWidth<1100?'Chromium viewport simulation, no real mobile keyboard':'Chromium desktop viewport'};
  97  |     });
  98  |     expect(metrics.maxTouchPoints > 0).toBe(width < 1100);
  99  |     expect(metrics.body.fontSize).toBe('13px');
  100 |     expect(metrics.table.fontSize).toBe('13px');
  101 |     expect(metrics.meta.fontSize).toBe('11px');
  102 |     expect(metrics.activeAnimations).toBe(0);
  103 |     const metricFile = testInfo.outputPath('geometry-and-environment.json');
  104 |     await writeFile(metricFile,JSON.stringify(metrics,null,2));
  105 |     await testInfo.attach('geometry-and-environment', {path:metricFile,contentType:'application/json'});
  106 |     await page.locator('#page').evaluate(el => el.scrollTop=0);
  107 |     await page.screenshot({path:testInfo.outputPath(`database-${width}x${height}-top.png`)});
  108 |     await result(page).scrollIntoViewIfNeeded();
  109 |     await page.screenshot({path:testInfo.outputPath(`database-${width}x${height}-long-result.png`)});
  110 |   });
  111 |   });
  112 | }
  113 |
  114 | test('Database row, column and byte results follow actual worker boundaries', async ({page},testInfo) => {
  115 |   await open(page);
  116 |   await editor(page).fill('SELECT '+Array.from({length:64},(_,i)=>`${i} AS col_${i}`).join(','));
  117 |   await run(page).click();
  118 |   await expect(result(page).locator('th')).toHaveCount(64);
  119 |   await expect(result(page).locator('th').last()).toHaveText('col_63');
  120 |   await editor(page).fill('SELECT '+Array.from({length:65},(_,i)=>i).join(','));
  121 |   const rejected = page.waitForResponse(r=>r.url().endsWith('/execute'));
  122 |   await run(page).click();
  123 |   expect((await rejected).status()).toBe(400);
  124 |   await expect(page.locator('[data-state]')).toContainText('请求被拒绝');
  125 |   await expect(result(page)).toBeEmpty();
  126 |   await page.screenshot({path:testInfo.outputPath('database-65-columns-rejected.png')});
  127 |   const values = Array.from({length:11},(_,i)=>`SELECT ${i} AS n`).join(' UNION ALL ');
  128 |   const long = 'x'.repeat(50000)+'BYTE-END';
  129 |   await editor(page).fill(`WITH digits AS (${values}) SELECT '${long}' AS body FROM digits a CROSS JOIN digits b`);
  130 |   const completed = page.waitForResponse(r=>r.url().endsWith('/execute'));
  131 |   await run(page).click();
  132 |   const response = await completed;
  133 |   const body = await response.json();
  134 |   expect(response.status()).toBe(200);
  135 |   expect(body.truncation_reasons).toEqual(['bytes']);
  136 |   expect(body.returned_rows).toBeLessThan(121);
  137 |   expect(Buffer.byteLength(await response.text())).toBeLessThanOrEqual(2097152);
  138 |   await expect(page.locator('[data-state]')).toHaveText('结果截断 · 字节上限');
  139 |   await expect(result(page)).toContainText('字节上限 (bytes)');
  140 |   await expect(result(page).locator('td').first()).toHaveText(JSON.stringify(long));
  141 | });
  142 |
```
