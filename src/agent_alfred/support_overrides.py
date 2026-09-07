"""Process-local shape evidence. This module contains no supplier rules."""

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from threading import Lock
from typing import Literal

from agent_alfred.clock import Clock, format_instant
from agent_alfred.model import ClientSnapshot, ModelError, ModelResult
from agent_alfred.redact import Redactor


@dataclass(frozen=True)
class SupportOverride:
    endpoint_id: str
    model_id: str
    wire_style: str
    reason: str
    flip_rule: Literal["endpoint_not_found_404", "shape_mismatch_400"]
    attempt_id: str
    status_code: int
    body_excerpt: str
    run_id: str
    config_version: str
    observed_at: str
    support: Literal["unsupported"] = field(default="unsupported", init=False)
    support_basis: Literal["probe_evidence"] = field(
        default="probe_evidence", init=False
    )

    @property
    def key(self) -> tuple[str, str, str]:
        return self.endpoint_id, self.model_id, self.wire_style


class SupportOverrides:
    """Publish a complete immutable record once per endpoint/model/style."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._records: dict[tuple[str, str, str], SupportOverride] = {}

    def record(self, evidence: SupportOverride) -> bool:
        with self._lock:
            if evidence.key in self._records:
                return False
            self._records[evidence.key] = evidence
            return True

    def get(
        self, endpoint_id: str, model_id: str, wire_style: str
    ) -> SupportOverride | None:
        with self._lock:
            return self._records.get((endpoint_id, model_id, wire_style))

    def for_run(self, run_id: str) -> tuple[SupportOverride, ...]:
        with self._lock:
            return tuple(
                item for item in self._records.values() if item.run_id == run_id
            )


# This callable is an injection seam for mechanism tests. Production assembly
# supplies None: no supplier rule is approved and there is no config switch.


SupportRule = Callable[[ClientSnapshot, ModelError], str | None]


class SupportRecorder:
    def __init__(
        self,
        store: SupportOverrides,
        redactor: Redactor,
        clock: Clock,
        rule: SupportRule | None = None,
    ):
        self._store = store
        self._redactor = redactor
        self._clock = clock
        self._rule = rule

    def observe(
        self, snapshot: ClientSnapshot, run_id: str, result: ModelResult, events
    ) -> None:
        if self._rule is None:
            return
        from agent_alfred.endpoints import resolve_model
        from agent_alfred.events import Notice

        support = resolve_model(
            snapshot.endpoint_id,
            snapshot.model_id,
            wire_style_override=snapshot.wire_style,
        )
        if support.support == "unsupported":
            return
        for attempt in result.attempts:
            error = attempt.error
            if error is None:
                continue
            rule = self._rule(snapshot, error)
            if (rule, error.status_code) not in {
                ("endpoint_not_found_404", 404),
                ("shape_mismatch_400", 400),
            }:
                continue
            values = dict(
                endpoint_id=snapshot.endpoint_id,
                model_id=snapshot.model_id,
                wire_style=snapshot.wire_style,
                reason=rule,
                flip_rule=rule,
                attempt_id=error.attempt_id,
                status_code=error.status_code,
                body_excerpt=error.body_excerpt or "",
                run_id=run_id,
                config_version=snapshot.config_version,
                observed_at=format_instant(self._clock.wall_utc()),
            )
            try:
                safe = self._redactor.redact_jsonable(values)
                safe["body_excerpt"] = safe["body_excerpt"][:500]
                record = SupportOverride(**safe)
            except Exception:
                if events is not None:
                    events.emit(Notice(code="redaction_failed"))
                return
            if self._store.record(record) and events is not None:
                events.emit(
                    Notice(
                        code="model_support_flipped",
                        detail=tuple(
                            (key, str(value))
                            for key, value in asdict(record).items()
                            if key != "body_excerpt"
                        ),
                        evidence=record.body_excerpt,
                    )
                )
