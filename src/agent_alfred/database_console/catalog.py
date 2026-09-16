"""Static diagnostic object catalog. These are projections, not production tables."""

from __future__ import annotations

from dataclasses import dataclass

REQUIRED_SOURCE_TABLES = (
    "sessions",
    "runs",
    "agent_log",
    "facts",
    "episodes",
    "memory_sources",
    "memory_provenance",
    "history_groups",
    "history_group_times",
    "memory_uses",
    "history_reads",
    "tool_metering",
    "tool_ledger",
    "external_tool_operations",
    "calendar_entries",
    "forget_operations",
    "forget_limits",
    "forget_cleanup",
    "memory_consolidation_batches",
    "memory_consolidation_sources",
    "memory_mirrors",
    "schema_migrations",
    "memory_revision",
)


@dataclass(frozen=True)
class Column:
    name: str
    sql_type: str
    nullable: bool
    identity: bool
    origin: str
    protection: str
    flag: bool = False


@dataclass(frozen=True)
class DiagObject:
    name: str
    columns: tuple[Column, ...]
    source: str
    notes: str

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    @property
    def identity_columns(self) -> frozenset[str]:
        return frozenset(column.name for column in self.columns if column.identity)

    @property
    def create_sql(self) -> str:
        parts = []
        for column in self.columns:
            null = "" if column.nullable else " NOT NULL"
            parts.append(f"{column.name} {column.sql_type}{null}")
        return f"CREATE TABLE {self.name} ({', '.join(parts)})"


def _col(
    name: str,
    sql_type: str,
    *,
    nullable: bool = True,
    identity: bool = False,
    origin: str,
    protection: str,
    flag: bool = False,
) -> Column:
    return Column(name, sql_type, nullable, identity, origin, protection, flag)


def _text(name: str, **kwargs: object) -> Column:
    return _col(name, "TEXT", **kwargs)  # type: ignore[arg-type]


def _int(name: str, **kwargs: object) -> Column:
    return _col(name, "INTEGER", **kwargs)  # type: ignore[arg-type]


def _flag(name: str, **kwargs: object) -> Column:
    return _col(name, "INTEGER", flag=True, **kwargs)  # type: ignore[arg-type]


_ID = "主键或引用身份，保护改变时失败"
_BODY = "开放字符串，保护后可见"
_RAW = "原物理类型保留"
_FLAG = "INTEGER 0/1"
_NULL = "合法缺失为 NULL"

OBJECTS: tuple[DiagObject, ...] = (
    DiagObject(
        "diag_sessions",
        (
            _text(
                "session_id",
                nullable=False,
                identity=True,
                origin="sessions.session_id",
                protection=_ID,
            ),
            _text(
                "created_at",
                nullable=False,
                origin="sessions.created_at",
                protection=_RAW,
            ),
            _int(
                "activity_revision",
                nullable=False,
                identity=True,
                origin="sessions.activity_revision",
                protection=_ID,
            ),
        ),
        "sessions",
        "无虚构 title",
    ),
    DiagObject(
        "diag_runs",
        (
            _text(
                "run_id",
                nullable=False,
                identity=True,
                origin="runs.run_id",
                protection=_ID,
            ),
            _text("purpose", nullable=False, origin="runs.purpose", protection=_RAW),
            _text(
                "session_id", identity=True, origin="runs.session_id", protection=_ID
            ),
            _text("gateway", nullable=False, origin="runs.gateway", protection=_RAW),
            _text(
                "entry_surface_id",
                identity=True,
                origin="runs.entry_surface_id",
                protection=_ID,
            ),
            _text("phase", nullable=False, origin="runs.phase", protection=_RAW),
            _text("outcome", origin="runs.outcome", protection=_NULL),
            _text(
                "accepted_at",
                nullable=False,
                origin="runs.accepted_at",
                protection=_RAW,
            ),
            _text("started_at", origin="runs.started_at", protection=_NULL),
            _text("finished_at", origin="runs.finished_at", protection=_NULL),
            _int(
                "activity_revision",
                nullable=False,
                identity=True,
                origin="runs.activity_revision",
                protection=_ID,
            ),
            _text(
                "admission_state",
                nullable=False,
                origin="runs.admission_state",
                protection=_RAW,
            ),
        ),
        "runs",
        "不输出 prompt_preview 或 telemetry",
    ),
    DiagObject(
        "diag_messages",
        (
            _int(
                "message_id",
                nullable=False,
                identity=True,
                origin="agent_log.id",
                protection=_ID,
            ),
            _text(
                "session_id",
                nullable=False,
                identity=True,
                origin="agent_log.session_id",
                protection=_ID,
            ),
            _text("run_id", identity=True, origin="agent_log.run_id", protection=_ID),
            _text("role", nullable=False, origin="agent_log.role", protection=_RAW),
            _text("source", nullable=False, origin="agent_log.source", protection=_RAW),
            _text(
                "created_at",
                nullable=False,
                origin="agent_log.created_at",
                protection=_RAW,
            ),
            _flag(
                "consolidated",
                nullable=False,
                origin="agent_log.consolidated",
                protection=_FLAG,
            ),
            _text(
                "text",
                nullable=False,
                origin="agent_log.content TextBlock",
                protection=_BODY,
            ),
        ),
        "agent_log",
        "仅 user/assistant；text 为保护后 TextBlock 按原顺序拼接",
    ),
    DiagObject(
        "diag_attempts",
        (
            _text(
                "run_id",
                nullable=False,
                identity=True,
                origin="runs.run_id",
                protection=_ID,
            ),
            _text(
                "attempt_id",
                nullable=False,
                identity=True,
                origin="runs.telemetry.attempts.attempt_id",
                protection=_ID,
            ),
            _text("outcome", origin="attempts.outcome", protection=_NULL),
            _text("endpoint_id", origin="attempts.model.endpoint_id", protection=_NULL),
            _text("model_id", origin="attempts.model.model_id", protection=_NULL),
            _int(
                "total_input_tokens",
                origin="usage.total_input_tokens",
                protection=_NULL,
            ),
            _int(
                "uncached_input_tokens",
                origin="usage.uncached_input_tokens",
                protection=_NULL,
            ),
            _int(
                "cache_read_tokens", origin="usage.cache_read_tokens", protection=_NULL
            ),
            _int(
                "cache_write_tokens",
                origin="usage.cache_write_tokens",
                protection=_NULL,
            ),
            _int("output_tokens", origin="usage.output_tokens", protection=_NULL),
            _int("reasoning_tokens", origin="usage.reasoning_tokens", protection=_NULL),
            _text(
                "endpoint_reported_cost_usd",
                origin="usage.endpoint_reported_cost_usd",
                protection=_NULL,
            ),
        ),
        "runs.telemetry.attempts",
        "仅已知字段；损坏 fail-closed",
    ),
    DiagObject(
        "diag_attempt_coverage",
        (
            _text(
                "run_id",
                nullable=False,
                identity=True,
                origin="runs.run_id",
                protection=_ID,
            ),
            _text(
                "state", nullable=False, origin="telemetry 覆盖规则", protection=_RAW
            ),
            _int("recorded_count", origin="attempts 列表长度", protection=_NULL),
            _flag(
                "run_finished",
                nullable=False,
                origin="phase=finished",
                protection=_FLAG,
            ),
        ),
        "runs",
        "每个 Run 一行：recorded / recorded_empty / unrecorded",
    ),
    DiagObject(
        "diag_facts",
        (
            _text(
                "id", nullable=False, identity=True, origin="facts.id", protection=_ID
            ),
            _int(
                "record_version",
                nullable=False,
                identity=True,
                origin="facts.record_version",
                protection=_ID,
            ),
            _text("subject", nullable=False, origin="facts.subject", protection=_BODY),
            _text("fact", nullable=False, origin="facts.fact", protection=_BODY),
            _text(
                "origin_kind",
                nullable=False,
                origin="facts.origin_kind",
                protection=_RAW,
            ),
            _text(
                "origin_batch_id",
                identity=True,
                origin="facts.origin_batch_id",
                protection=_ID,
            ),
            _text("origin_source", origin="facts.origin_source", protection=_RAW),
            _text(
                "origin_call_id",
                identity=True,
                origin="facts.origin_call_id",
                protection=_ID,
            ),
            _text(
                "created_at", nullable=False, origin="facts.created_at", protection=_RAW
            ),
            _text("modified_at", origin="facts.modified_at", protection=_NULL),
            _flag(
                "human_protected",
                nullable=False,
                origin="facts.human_protected",
                protection=_FLAG,
            ),
            _text(
                "last_change_origin_type",
                origin="facts.last_change_origin",
                protection=_RAW,
            ),
            _text(
                "last_change_origin_source",
                origin="facts.last_change_origin",
                protection=_RAW,
            ),
            _text(
                "last_change_origin_call_id",
                identity=True,
                origin="facts.last_change_origin",
                protection=_ID,
            ),
            _text(
                "last_change_origin_batch_id",
                identity=True,
                origin="facts.last_change_origin",
                protection=_ID,
            ),
            _text(
                "provenance_state",
                nullable=False,
                origin="memory_provenance.state",
                protection=_RAW,
            ),
        ),
        "facts + memory_provenance",
        "后五列派生；不恢复已删除事实",
    ),
    DiagObject(
        "diag_episodes",
        (
            _text(
                "id",
                nullable=False,
                identity=True,
                origin="episodes.id",
                protection=_ID,
            ),
            _int(
                "record_version",
                nullable=False,
                identity=True,
                origin="episodes.record_version",
                protection=_ID,
            ),
            _text(
                "summary", nullable=False, origin="episodes.summary", protection=_BODY
            ),
            _text("occurred_at", origin="episodes.occurred_at", protection=_NULL),
            _text("occurred_until", origin="episodes.occurred_until", protection=_NULL),
            _text(
                "origin_kind",
                nullable=False,
                origin="episodes.origin_kind",
                protection=_RAW,
            ),
            _text(
                "origin_batch_id",
                identity=True,
                origin="episodes.origin_batch_id",
                protection=_ID,
            ),
            _text("origin_source", origin="episodes.origin_source", protection=_RAW),
            _text(
                "origin_call_id",
                identity=True,
                origin="episodes.origin_call_id",
                protection=_ID,
            ),
            _text(
                "created_at",
                nullable=False,
                origin="episodes.created_at",
                protection=_RAW,
            ),
            _text("modified_at", origin="episodes.modified_at", protection=_NULL),
            _flag(
                "human_protected",
                nullable=False,
                origin="episodes.human_protected",
                protection=_FLAG,
            ),
            _text(
                "last_change_origin_type",
                origin="episodes.last_change_origin",
                protection=_RAW,
            ),
            _text(
                "last_change_origin_source",
                origin="episodes.last_change_origin",
                protection=_RAW,
            ),
            _text(
                "last_change_origin_call_id",
                identity=True,
                origin="episodes.last_change_origin",
                protection=_ID,
            ),
            _text(
                "last_change_origin_batch_id",
                identity=True,
                origin="episodes.last_change_origin",
                protection=_ID,
            ),
            _text(
                "provenance_state",
                nullable=False,
                origin="memory_provenance.state",
                protection=_RAW,
            ),
        ),
        "episodes + memory_provenance",
        "派生规则同 facts",
    ),
    DiagObject(
        "diag_memory_sources",
        (
            _text(
                "kind",
                nullable=False,
                identity=True,
                origin="memory_sources.kind",
                protection=_ID,
            ),
            _text(
                "memory_id",
                nullable=False,
                identity=True,
                origin="memory_sources.memory_id",
                protection=_ID,
            ),
            _int(
                "record_version",
                nullable=False,
                identity=True,
                origin="memory_sources.record_version",
                protection=_ID,
            ),
            _text(
                "source_group_id",
                nullable=False,
                identity=True,
                origin="memory_sources.source_group_id",
                protection=_ID,
            ),
        ),
        "memory_sources",
        "版本化来源关系",
    ),
    DiagObject(
        "diag_history_groups",
        (
            _text(
                "group_id",
                nullable=False,
                identity=True,
                origin="history_groups.group_id",
                protection=_ID,
            ),
            _text(
                "kind", nullable=False, origin="history_groups.kind", protection=_RAW
            ),
            _text(
                "container_id",
                identity=True,
                origin="history_groups.container_id",
                protection=_ID,
            ),
            _text(
                "evidence",
                nullable=False,
                origin="history_groups.evidence",
                protection=_RAW,
            ),
            _text(
                "occurred_at",
                origin="history_group_times.occurred_at",
                protection=_NULL,
            ),
        ),
        "history_groups LEFT JOIN history_group_times",
        "不开放 evidence_id；时间缺失为 NULL",
    ),
    DiagObject(
        "diag_memory_uses",
        (
            _text(
                "kind",
                nullable=False,
                identity=True,
                origin="memory_uses.kind",
                protection=_ID,
            ),
            _text(
                "memory_id",
                nullable=False,
                identity=True,
                origin="memory_uses.memory_id",
                protection=_ID,
            ),
            _int(
                "record_version",
                nullable=False,
                identity=True,
                origin="memory_uses.record_version",
                protection=_ID,
            ),
            _text(
                "consumer",
                nullable=False,
                identity=True,
                origin="memory_uses.consumer",
                protection=_ID,
            ),
            _text(
                "attempt_id",
                nullable=False,
                identity=True,
                origin="memory_uses.attempt_id",
                protection=_ID,
            ),
            _text(
                "purpose", nullable=False, origin="memory_uses.purpose", protection=_RAW
            ),
        ),
        "memory_uses",
        "不把 consumer 伪称 run_id",
    ),
    DiagObject(
        "diag_history_reads",
        (
            _text(
                "source",
                nullable=False,
                identity=True,
                origin="history_reads.source",
                protection=_ID,
            ),
            _text(
                "consumer",
                nullable=False,
                identity=True,
                origin="history_reads.consumer",
                protection=_ID,
            ),
            _text(
                "attempt_id",
                nullable=False,
                identity=True,
                origin="history_reads.attempt_id",
                protection=_ID,
            ),
            _text(
                "purpose",
                nullable=False,
                origin="history_reads.purpose",
                protection=_RAW,
            ),
        ),
        "history_reads",
        "组到组的实际读取关系",
    ),
    DiagObject(
        "diag_tool_metering",
        (
            _text(
                "run_id",
                nullable=False,
                identity=True,
                origin="tool_metering.run_id",
                protection=_ID,
            ),
            _int(
                "step_index",
                nullable=False,
                identity=True,
                origin="tool_metering.step_index",
                protection=_ID,
            ),
            _text(
                "call_id",
                nullable=False,
                identity=True,
                origin="tool_metering.call_id",
                protection=_ID,
            ),
            _int(
                "ordinal",
                nullable=False,
                origin="tool_metering.ordinal",
                protection=_RAW,
            ),
            _text(
                "tool_name",
                nullable=False,
                origin="tool_metering.tool_name",
                protection=_BODY,
            ),
            _text("source_id", origin="tool_metering.source_id", protection=_ID),
            _text(
                "capability_id", origin="tool_metering.capability_id", protection=_ID
            ),
            _text("effect", origin="tool_metering.effect", protection=_RAW),
            _text(
                "requested_at",
                nullable=False,
                origin="tool_metering.requested_at",
                protection=_RAW,
            ),
            _text(
                "start_confirmation",
                nullable=False,
                origin="tool_metering.start_confirmation",
                protection=_RAW,
            ),
            _text(
                "result", nullable=False, origin="tool_metering.result", protection=_RAW
            ),
            _text(
                "reason_code", origin="tool_metering.reason 安全映射", protection=_RAW
            ),
            _text("finished_at", origin="tool_metering.finished_at", protection=_NULL),
            _text(
                "operation_id",
                identity=True,
                origin="tool_metering.operation_id",
                protection=_ID,
            ),
            _text(
                "model_delivery",
                nullable=False,
                origin="tool_metering.model_delivery",
                protection=_RAW,
            ),
            _text(
                "cost_kind",
                nullable=False,
                origin="tool_metering.cost.kind",
                protection=_RAW,
            ),
            _text("cost_units", origin="tool_metering.cost.units", protection=_NULL),
            _text("cost_unit", origin="tool_metering.cost.unit", protection=_NULL),
            _text("cost_source", origin="tool_metering.cost.source", protection=_NULL),
            _text(
                "cost_service", origin="tool_metering.cost.service", protection=_NULL
            ),
            _text(
                "cost_reason",
                origin="tool_metering.cost.reason 安全映射",
                protection=_RAW,
            ),
        ),
        "tool_metering",
        "reason_code 与 cost_* 为安全派生，不开放 cost 原 JSON",
    ),
    DiagObject(
        "diag_tool_ledger",
        (
            _int(
                "id",
                nullable=False,
                identity=True,
                origin="tool_ledger.id",
                protection=_ID,
            ),
            _text(
                "tool_name",
                nullable=False,
                origin="tool_ledger.tool_name",
                protection=_BODY,
            ),
            _text(
                "effect", nullable=False, origin="tool_ledger.effect", protection=_RAW
            ),
            _text(
                "status", nullable=False, origin="tool_ledger.status", protection=_RAW
            ),
            _text(
                "call_id", identity=True, origin="tool_ledger.call_id", protection=_ID
            ),
            _text("run_id", identity=True, origin="tool_ledger.run_id", protection=_ID),
            _text(
                "session_id",
                identity=True,
                origin="tool_ledger.session_id",
                protection=_ID,
            ),
            _text(
                "created_at",
                nullable=False,
                origin="tool_ledger.created_at",
                protection=_RAW,
            ),
            _text("updated_at", origin="tool_ledger.updated_at", protection=_NULL),
        ),
        "tool_ledger",
        "不开放 summary／fingerprint，不虚构 step_index",
    ),
    DiagObject(
        "diag_tool_operation_links",
        (
            _text(
                "run_id",
                nullable=False,
                identity=True,
                origin="external_tool_operations.run_id",
                protection=_ID,
            ),
            _int(
                "step_index",
                nullable=False,
                identity=True,
                origin="external_tool_operations.step_index",
                protection=_ID,
            ),
            _text(
                "call_id",
                nullable=False,
                identity=True,
                origin="external_tool_operations.call_id",
                protection=_ID,
            ),
            _int(
                "ledger_id",
                nullable=False,
                identity=True,
                origin="external_tool_operations.ledger_id",
                protection=_ID,
            ),
        ),
        "external_tool_operations",
        "已持久关联，不开放 receipt／fingerprint",
    ),
    DiagObject(
        "diag_calendar_entries",
        (
            _int(
                "id",
                nullable=False,
                identity=True,
                origin="calendar_entries.id",
                protection=_ID,
            ),
            _text(
                "title",
                nullable=False,
                origin="calendar_entries.title",
                protection=_BODY,
            ),
            _text(
                "starts_at",
                nullable=False,
                origin="calendar_entries.starts_at",
                protection=_RAW,
            ),
            _text("ends_at", origin="calendar_entries.ends_at", protection=_NULL),
            _text(
                "iana_time_zone",
                origin="calendar_entries.iana_time_zone",
                protection=_RAW,
            ),
            _text(
                "participants", origin="calendar_entries.participants", protection=_BODY
            ),
            _text("notes", origin="calendar_entries.notes", protection=_BODY),
            _text(
                "created_at",
                nullable=False,
                origin="calendar_entries.created_at",
                protection=_RAW,
            ),
        ),
        "calendar_entries",
        "正文经保护",
    ),
    DiagObject(
        "diag_forget_operations",
        (
            _text(
                "operation_id",
                nullable=False,
                identity=True,
                origin="forget_operations.operation_id",
                protection=_ID,
            ),
            _text(
                "kind", nullable=False, origin="forget_operations.kind", protection=_RAW
            ),
            _text(
                "memory_id",
                nullable=False,
                identity=True,
                origin="forget_operations.memory_id",
                protection=_ID,
            ),
            _text(
                "completeness",
                nullable=False,
                origin="forget_operations.completeness",
                protection=_RAW,
            ),
            _text(
                "created_at",
                nullable=False,
                origin="forget_operations.created_at",
                protection=_RAW,
            ),
            _text(
                "current_state",
                nullable=False,
                origin="operation_state() 纯读",
                protection=_RAW,
            ),
        ),
        "forget_operations",
        "current_state 为纯读推导，不记录观察",
    ),
    DiagObject(
        "diag_forget_limits",
        (
            _text(
                "operation_id",
                nullable=False,
                identity=True,
                origin="forget_limits.operation_id",
                protection=_ID,
            ),
            _text(
                "group_id",
                nullable=False,
                identity=True,
                origin="forget_limits.group_id",
                protection=_ID,
            ),
            _text("mode", nullable=False, origin="forget_limits.mode", protection=_RAW),
        ),
        "forget_limits",
        "",
    ),
    DiagObject(
        "diag_forget_cleanup",
        (
            _text(
                "operation_id",
                nullable=False,
                identity=True,
                origin="forget_cleanup.operation_id",
                protection=_ID,
            ),
            _text(
                "target_id",
                nullable=False,
                identity=True,
                origin="forget_cleanup.target_id",
                protection=_ID,
            ),
            _int(
                "revision",
                nullable=False,
                origin="forget_cleanup.revision",
                protection=_RAW,
            ),
            _text(
                "state", nullable=False, origin="forget_cleanup.state", protection=_RAW
            ),
            _text(
                "error_code", origin="forget_cleanup.error 安全映射", protection=_RAW
            ),
        ),
        "forget_cleanup",
        "error_code 从实际 error 映射安全标记",
    ),
    DiagObject(
        "diag_consolidation_batches",
        (
            _text(
                "batch_id",
                nullable=False,
                identity=True,
                origin="memory_consolidation_batches.batch_id",
                protection=_ID,
            ),
            _text(
                "session_id",
                nullable=False,
                identity=True,
                origin="memory_consolidation_batches.session_id",
                protection=_ID,
            ),
            _int(
                "revision",
                nullable=False,
                identity=True,
                origin="memory_consolidation_batches.revision",
                protection=_ID,
            ),
            _text(
                "status",
                nullable=False,
                origin="memory_consolidation_batches.status",
                protection=_RAW,
            ),
            _text(
                "created_at",
                nullable=False,
                origin="memory_consolidation_batches.created_at",
                protection=_RAW,
            ),
            _text(
                "updated_at",
                nullable=False,
                origin="memory_consolidation_batches.updated_at",
                protection=_RAW,
            ),
            _text(
                "finished_at",
                origin="memory_consolidation_batches.finished_at",
                protection=_NULL,
            ),
            _text("error_code", origin="error_code 安全映射", protection=_RAW),
            _text(
                "generation_run_id",
                identity=True,
                origin="memory_consolidation_batches.generation_run_id",
                protection=_ID,
            ),
        ),
        "memory_consolidation_batches",
        "不使用同名近似旧表，不输出 receipt",
    ),
    DiagObject(
        "diag_consolidation_sources",
        (
            _text(
                "batch_id",
                nullable=False,
                identity=True,
                origin="memory_consolidation_sources.batch_id",
                protection=_ID,
            ),
            _int(
                "revision",
                nullable=False,
                identity=True,
                origin="memory_consolidation_sources.revision",
                protection=_ID,
            ),
            _text(
                "run_id",
                nullable=False,
                identity=True,
                origin="memory_consolidation_sources.run_id",
                protection=_ID,
            ),
            _text(
                "session_id",
                nullable=False,
                identity=True,
                origin="memory_consolidation_sources.session_id",
                protection=_ID,
            ),
            _int(
                "ordinal",
                nullable=False,
                origin="memory_consolidation_sources.ordinal",
                protection=_RAW,
            ),
            _text(
                "accepted_at",
                nullable=False,
                origin="memory_consolidation_sources.accepted_at",
                protection=_RAW,
            ),
            _text(
                "finished_at",
                nullable=False,
                origin="memory_consolidation_sources.finished_at",
                protection=_RAW,
            ),
        ),
        "memory_consolidation_sources",
        "按 batch_id+revision 关联",
    ),
    DiagObject(
        "diag_memory_mirrors",
        (
            _text(
                "target_id",
                nullable=False,
                identity=True,
                origin="memory_mirrors.target_id",
                protection=_ID,
            ),
            _int(
                "required_generation",
                nullable=False,
                origin="memory_mirrors.required_generation",
                protection=_RAW,
            ),
            _int(
                "generated_generation",
                nullable=False,
                origin="memory_mirrors.generated_generation",
                protection=_RAW,
            ),
            _int(
                "verified_generation",
                nullable=False,
                origin="memory_mirrors.verified_generation",
                protection=_RAW,
            ),
            _int(
                "covered_cleanup_generation",
                nullable=False,
                origin="memory_mirrors.covered_cleanup_generation",
                protection=_RAW,
            ),
            _text(
                "generated_at", origin="memory_mirrors.generated_at", protection=_NULL
            ),
            _text("verified_at", origin="memory_mirrors.verified_at", protection=_NULL),
            _flag(
                "conflict",
                nullable=False,
                origin="memory_mirrors.conflict",
                protection=_FLAG,
            ),
            _text(
                "error_code", origin="memory_mirrors.error 安全映射", protection=_RAW
            ),
        ),
        "memory_mirrors",
        "仅持久观测，不开放路径／intent／fingerprint，不提供 ready",
    ),
)

OBJECT_BY_NAME = {item.name: item for item in OBJECTS}
assert len(OBJECTS) == 21
assert len(OBJECT_BY_NAME) == 21


def manifest() -> list[dict[str, object]]:
    return [
        {
            "name": item.name,
            "source": item.source,
            "notes": item.notes,
            "columns": [
                {
                    "name": column.name,
                    "type": column.sql_type,
                    "nullable": column.nullable,
                    "identity": column.identity,
                    "origin": column.origin,
                    "protection": column.protection,
                }
                for column in item.columns
            ],
            "example": (
                f"SELECT {', '.join(item.column_names)} FROM {item.name} LIMIT 20"
            ),
        }
        for item in OBJECTS
    ]


def limits() -> dict[str, int]:
    from agent_alfred.database_console.budget import (
        EXECUTE_BUDGET_S,
        HANDLE_CAPACITY,
        HANDLE_TTL_S,
        PROTECTED_INPUT_LIMIT,
        RESULT_COLUMN_LIMIT,
        RESULT_JSON_LIMIT,
        RESULT_ROW_LIMIT,
        SOURCE_ROW_LIMIT,
        SOURCE_VALUE_LIMIT,
        SQL_TEXT_LIMIT,
        SQLITE_HEAP_LIMIT,
        TERMINAL_TTL_S,
    )

    return {
        "sql_utf8_bytes": SQL_TEXT_LIMIT,
        "source_value_bytes": SOURCE_VALUE_LIMIT,
        "source_row_bytes": SOURCE_ROW_LIMIT,
        "protected_input_bytes": PROTECTED_INPUT_LIMIT,
        "sqlite_heap_bytes": SQLITE_HEAP_LIMIT,
        "result_utf8_bytes": RESULT_JSON_LIMIT,
        "result_rows": RESULT_ROW_LIMIT,
        "result_columns": RESULT_COLUMN_LIMIT,
        "page_rows": 100,
        "handle_ttl_seconds": int(HANDLE_TTL_S),
        "terminal_ttl_seconds": int(TERMINAL_TTL_S),
        "handle_capacity": HANDLE_CAPACITY,
        "execute_budget_ms": int(EXECUTE_BUDGET_S * 1000),
    }
