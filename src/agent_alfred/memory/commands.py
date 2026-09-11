"""One transaction owner for memory writes, their ledger, and replay receipts."""

from __future__ import annotations

import hmac
import json
import sqlite3
import sys
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any

from agent_alfred.memory.audit import AuditKey
from agent_alfred.memory.episodic import SQLiteEpisodicStore
from agent_alfred.memory.forget_ports import CleanupPort, ProjectionParticipant
from agent_alfred.memory.semantic import SQLiteSemanticStore
from agent_alfred.memory.types import (
    AlreadyAbsent,
    ConsolidationOrigin,
    Deleted,
    DuplicateConflict,
    ManualOrigin,
    MemoryId,
    NotFound,
    Origin,
    ToolOrigin,
    UpdateApplied,
    VersionConflict,
    origin_json,
)
from agent_alfred.resource_rollback import (
    ResumableRollback,
    dominant_error,
    raise_if_rollback_pending,
)
from agent_alfred.runtime.recording import RecordingStore, RecordingUnavailable


@dataclass(frozen=True)
class CommandContext:
    origin: Origin
    source: str
    run_id: str | None = None
    session_id: str | None = None
    call_id: str | None = None
    permission: object | None = None
    source_groups: tuple[str, ...] = ()
    checkpoint: Callable[[], None] | None = None


class _CommandDeadline(TimeoutError):
    """Our own pre-commit checkpoint expired, not an ambiguous storage error."""


def _check_deadline(context):
    if context.checkpoint is not None:
        try:
            context.checkpoint()
        except TimeoutError as error:
            raise_if_rollback_pending(error)
            raise _CommandDeadline from error


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _error(code: str, **metadata: Any) -> dict:
    return {"error": {"code": code, **metadata}}


class MemoryCommandService:
    def __init__(
        self,
        *,
        recording_store: RecordingStore,
        audit_key: AuditKey,
        clock,
        admission=None,
        delete_barrier=None,
        projection_participants: tuple[ProjectionParticipant, ...] = (),
        projection_inventory_evidence_id: str | None = None,
        cleanup_port: CleanupPort | None = None,
        memory_notifier=None,
        process_instance_id=None,
        file_state=None,
    ):
        self._db = recording_store
        self._key = audit_key
        self._clock = clock
        self._admission = admission
        self._delete_barrier = delete_barrier
        self._projection_participants = tuple(projection_participants)
        self._projection_inventory_evidence_id = projection_inventory_evidence_id
        self._cleanup_port = cleanup_port
        self._memory_notifier = memory_notifier
        self.notification_failed = False
        self._last_notified_revision = None
        self._process_instance_id = process_instance_id
        self._mutation_lock = threading.RLock()
        self._mutation_thread = None
        from agent_alfred.memory.consolidation_service import ConsolidationService
        from agent_alfred.memory.forgetting import ForgettingService

        self.forgetting = ForgettingService(self)
        self.consolidation = ConsolidationService(self)
        self._projection_participants = tuple(projection_participants) + (
            self.consolidation,
        )
        self.mirrors = None
        if file_state is not None:
            from agent_alfred.memory.mirrors import MarkdownMirrors

            self.mirrors = MarkdownMirrors(self, file_state)
            self._projection_participants += (self.mirrors,)
            self._cleanup_port = self.mirrors
            self._projection_inventory_evidence_id = "managed-markdown-v1"

    def _notify_memory(self, revision, change=None):
        if (self._memory_notifier is None or
                self._last_notified_revision is not None and
                revision <= self._last_notified_revision):
            return
        payload = {
            "schema_version": 1,
            "process_instance_id": self._process_instance_id,
            "memory_revision": revision,
            "change": change,
        }
        try:
            if self._memory_notifier(payload) is not False:
                self._last_notified_revision = revision
        except Exception as error:
            raise_if_rollback_pending(error)
            # Transport delivery cannot undo a durable command. The #46 adapter
            # owns disconnect-on-loss; readers always revalidate the DB revision.
            self.notification_failed = True

    def _stores(self, conn):
        options = {"clock": self._clock, "fingerprint": self._key.fingerprint}
        return SQLiteSemanticStore(conn, **options), SQLiteEpisodicStore(
            conn, **options
        )

    @contextmanager
    def reading_stores(self):
        with self._db.reading() as conn:
            yield self._stores(conn)

    def get(self, kind: str, id: str):
        if kind not in ("semantic", "episodic") or not isinstance(id, str):
            raise ValueError("invalid_input")
        with self.reading_stores() as stores:
            return stores[0 if kind == "semantic" else 1].get(MemoryId(id))

    @property
    def memory_revision(self) -> int:
        with self.reading_stores() as stores:
            return stores[0].memory_revision

    def get_operation(self, operation_id: str) -> dict | None:
        with self._db.reading() as conn:
            row = conn.execute(
                "SELECT receipt FROM memory_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            return None if row is None else json.loads(row[0])

    def get_provenance(self, kind: str, memory_id: str, version: int) -> dict:
        with self._db.reading() as conn:
            row = conn.execute(
                "SELECT state FROM memory_provenance "
                "WHERE kind=? AND memory_id=? AND record_version=?",
                (kind, memory_id, version),
            ).fetchone()
            groups = conn.execute(
                "SELECT source_group_id FROM memory_sources "
                "WHERE kind=? AND memory_id=? AND record_version=? "
                "ORDER BY source_group_id",
                (kind, memory_id, version),
            ).fetchall()
            return {
                "state": "unknown" if row is None else row[0],
                "source_groups": [group[0] for group in groups],
            }

    def get_records(self, **query):
        from agent_alfred.memory.queries import MemoryQueryService

        return MemoryQueryService(self.reading_stores).get_records(**query)

    def statistics(self, **query):
        from agent_alfred.memory.statistics import read_statistics

        return read_statistics(self._db, clock=self._clock, **query)

    def execute(self, command: dict, context: CommandContext) -> dict:
        try:
            command = json.loads(_json(command))
            parsed = self._validate(command)
            canonical = _json(
                {
                    "version": 1,
                    "command": command,
                    "origin": origin_json(context.origin),
                    "source": context.source,
                    "run_id": context.run_id,
                    "session_id": context.session_id,
                    "call_id": context.call_id,
                    "source_groups": context.source_groups,
                }
            ).encode("utf-8")
        except (ValueError, TypeError, KeyError) as error:
            raise_if_rollback_pending(error)
            return _error("invalid_input")

        def execute():
            _check_deadline(context)
            # A durable replay never starts another barrier or mutation.
            if command["action"] == "delete" and (
                context.run_id or self._delete_barrier is not None
            ):
                old = self.get_operation(command["operation_id"])
                if old is None:
                    current = self.get(command["kind"], parsed["id"])
                    if current is None:
                        return self._execute_transaction(
                            command, context, parsed, canonical
                        )
                    if current.record_version != command["expected_version"]:
                        return _error(
                            "version_conflict", current_version=current.record_version
                        )
                    if self._delete_barrier is None:
                        return _error("trace_barrier_failed")
                    try:
                        failed, _ = self._delete_barrier(context.run_id)
                    except Exception as error:
                        raise_if_rollback_pending(error)
                        return _error("trace_barrier_failed")
                    if failed:
                        return _error("trace_barrier_failed")
            return self._execute_transaction(command, context, parsed, canonical)

        return self._run_mutation(execute, context)

    def _run_mutation(self, operation, context):
        # RLock's native owner is observable even when interruption precedes
        # capture of acquire()'s return. Still reject recursive public mutations.
        if self._mutation_lock._is_owned():
            return _error("busy")
        admission_owner = ResumableRollback()
        cleanup = ResumableRollback()
        cleanup.own(self._mutation_lock, self._release_mutation_lock)
        if self._admission is not None:
            cleanup.own(
                admission_owner,
                partial(
                    self._admission.end_mutation,
                    owner=admission_owner,
                ),
            )
        try:
            if not self._mutation_lock.acquire(blocking=False):
                return _error("busy")
            self._mutation_thread = threading.get_ident()
            if self._admission is not None:
                trusted = (
                    context.permission is not None
                    and context.run_id is not None
                    and self._admission.validate_memory_permission(
                        context.permission, context.run_id
                    )
                )
                if isinstance(context.origin, ToolOrigin) or trusted:
                    if not trusted:
                        return _error("busy")
                else:
                    reason = self._admission.try_begin_mutation(owner=admission_owner)
                    if reason is not None:
                        return _error(
                            "busy"
                            if reason
                            in ("busy", "run_in_progress", "mutation_in_flight")
                            else "unavailable"
                        )
            # Independent executors cannot borrow a caller's open transaction.
            # Reject before any recording context, trace checkpoint or file IO.
            if self._db.transaction_in_progress:
                return _error("transaction_required")
            result = operation()
            if self.mirrors is not None:
                try:
                    self.mirrors.refresh_admitted()
                except Exception as error:
                    raise_if_rollback_pending(error)
                    # A committed command receipt is independent of projection
                    # refresh. Transactional generations retain retry duty.
            try:
                self._notify_memory(self.memory_revision)
            except Exception as error:
                raise_if_rollback_pending(error)
                # Notification failure cannot rewrite a committed receipt.
            return result
        except _CommandDeadline as error:
            raise_if_rollback_pending(error)
            return _error("deadline_exceeded")
        except (ValueError, TypeError) as error:
            raise_if_rollback_pending(error)
            return _error("invalid_input")
        except (sqlite3.Error, RecordingUnavailable) as error:
            raise_if_rollback_pending(error)
            return _error("storage_write_failed")
        finally:
            original = sys.exception()
            if not cleanup.retry():
                # Retry an interrupted idempotent release while preserving the
                # original control exception and an owner for incomplete work.
                failure = (
                    dominant_error(original, cleanup.process_control)
                    or cleanup.errors[0]
                )
                cleanup.raise_failure(failure)

    def _release_mutation_lock(self):
        if self._mutation_lock._is_owned():
            self._mutation_thread = None
            self._mutation_lock.release()

    def _execute_transaction(self, command, context, parsed, canonical):
        _check_deadline(context)
        fingerprint, key_id = self._key.fingerprint(canonical)
        operation_id = command["operation_id"]
        source_changed = False
        with self._db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute(
                "SELECT fingerprint,key_id,receipt FROM memory_operations "
                "WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if old is not None:
                if old[1] != key_id:
                    return _error("operation_unverifiable")
                if not hmac.compare_digest(old[0], fingerprint):
                    return _error("operation_mismatch")
                return json.loads(old[2])
            store = self._stores(conn)[0 if command["kind"] == "semantic" else 1]
            action = command["action"]
            payload = dict(parsed)
            if action == "save":
                result = store.save_with_result(**payload, origin=context.origin)
                status = "saved" if result.created else "already_exists"
                memory_id, version = result.id, result.version
                affected = int(result.created)
            else:
                memory_id = MemoryId(payload.pop("id"))
                if action == "update":
                    result = store.update(
                        memory_id,
                        expected_version=command["expected_version"],
                        origin=context.origin,
                        **payload,
                    )
                else:
                    result = store.delete(
                        memory_id, expected_version=command["expected_version"]
                    )
                match result:
                    case VersionConflict(current_version):
                        return _error(
                            "version_conflict", current_version=current_version
                        )
                    case DuplicateConflict(existing_id):
                        return _error("duplicate_conflict", existing_id=existing_id)
                    case NotFound():
                        return _error("not_found")
                    case UpdateApplied(_, changed, version):
                        status, affected = (
                            ("updated" if changed else "unchanged"),
                            int(changed),
                        )
                        if changed:
                            self.consolidation.invalidate(
                                conn,
                                memories=((command["kind"], memory_id),),
                                isolated=(),
                            )
                    case Deleted():
                        status, affected, version = "deleted", 1, None
                    case AlreadyAbsent():
                        status, affected, version = "already_absent", 0, None
                    case _:
                        raise RuntimeError("unsupported_memory_outcome")
            committed_at = self._clock().isoformat()
            receipt = {
                "operation_id": operation_id,
                "action": action,
                "kind": command["kind"],
                "status": status,
                "memory_id": memory_id,
                "record_version": version,
                "affected_count": affected,
                "committed_at": committed_at,
            }
            if action == "delete":
                receipt.update(
                    fingerprint=result.fingerprint
                    if isinstance(result, Deleted)
                    else None,
                    key_id=result.key_id if isinstance(result, Deleted) else None,
                )
            if action == "delete" and isinstance(result, Deleted):
                from agent_alfred.memory.forget_graph import start_forgetting

                start_forgetting(
                    conn,
                    operation_id,
                    command["kind"],
                    memory_id,
                    command["expected_version"],
                    committed_at,
                )
                self.forgetting.invalidate(
                    conn, operation_id, memories=((command["kind"], memory_id),)
                )
            encoded = _json(receipt)
            if not isinstance(context.origin, ConsolidationOrigin):
                conn.execute(
                    (
                        "INSERT INTO tool_ledger\n                    "
                        "(tool_name,fingerprint,effect,status,call_id,"
                        "run_id,session_id,summary,created_at,updated_"
                        "at)\n                    "
                        "VALUES (?,?,'local_write','succeeded',?,?,?,?,?,?)"
                    ),
                    (
                        f"memory_{action}",
                        fingerprint,
                        context.call_id,
                        context.run_id,
                        context.session_id,
                        _json({"receipt": receipt, "key_id": key_id}),
                        committed_at,
                        committed_at,
                    ),
                )
            if action != "delete":
                groups = context.source_groups
                if (
                    not groups
                    and isinstance(context.origin, ToolOrigin)
                    and context.run_id
                ):
                    groups = (context.run_id,)
                provenance_state = (
                    "known"
                    if groups
                    else "known_none"
                    if isinstance(context.origin, ManualOrigin)
                    else "unknown"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO memory_provenance VALUES (?,?,?,?)",
                    (command["kind"], memory_id, version, provenance_state),
                )
                if groups:
                    conn.execute(
                        "UPDATE memory_provenance SET state='known' "
                        "WHERE kind=? AND memory_id=? AND record_version=? "
                        "AND state='known_none'",
                        (command["kind"], memory_id, version),
                    )
                    sourced = self.forgetting.register_sources(
                        command["kind"],
                        memory_id,
                        version,
                        groups=groups,
                        evidence_id="command:" + operation_id,
                        context=context,
                        transaction=conn,
                    )
                    if "error" in sourced:
                        return sourced
                    source_changed = True
            conn.execute(
                "INSERT INTO memory_operations VALUES (?,?,?,?)",
                (operation_id, fingerprint, key_id, encoded),
            )
            forgetting_changed = source_changed or (
                action == "delete" and isinstance(result, Deleted)
            )
            if forgetting_changed:
                read_revision = self.forgetting.prepare_commit(conn)
            else:
                read_revision = conn.execute(
                    "SELECT revision FROM memory_revision WHERE singleton=1"
                ).fetchone()[0]
            _check_deadline(context)
            conn.commit()
        if affected or source_changed:
            change = {
                "kind": command["kind"],
                "id": memory_id,
                "record_version": version,
                "operation_id": operation_id,
                "action": "deleted" if action == "delete" else "updated",
            }
            self._notify_memory(read_revision, change)
        return receipt

    @staticmethod
    def _validate(command: dict) -> dict:
        if not isinstance(command, dict) or set(command) - {
            "schema_version",
            "operation_id",
            "kind",
            "action",
            "payload",
            "expected_version",
        }:
            raise ValueError("invalid_input")
        if (
            type(command.get("schema_version", 1)) is not int
            or command.get("schema_version", 1) != 1
        ):
            raise ValueError("invalid_input")
        if (
            not isinstance(command.get("operation_id"), str)
            or not command["operation_id"]
        ):
            raise ValueError("invalid_input")
        if command.get("kind") not in ("semantic", "episodic"):
            raise ValueError("invalid_input")
        action = command.get("action")
        if action not in ("save", "update", "delete"):
            raise ValueError("invalid_input")
        payload = command.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("invalid_input")
        fields = (
            {"subject", "fact"}
            if command["kind"] == "semantic"
            else {"summary", "occurred_at", "occurred_until"}
        )
        allowed = fields if action == "save" else fields | {"id"}
        if action == "delete":
            allowed = {"id"}
        if set(payload) - allowed:
            raise ValueError("invalid_input")
        if action == "save" and not fields <= payload.keys():
            raise ValueError("invalid_input")
        if action != "save":
            if not isinstance(payload.get("id"), str) or not payload["id"]:
                raise ValueError("invalid_input")
            if (
                type(command.get("expected_version")) is not int
                or command["expected_version"] < 1
            ):
                raise ValueError("invalid_input")
        parsed = dict(payload)
        for field, value in payload.items():
            if field in ("occurred_at", "occurred_until"):
                if field == "occurred_until" and value is None:
                    continue
                if not isinstance(value, str):
                    raise ValueError("invalid_input")
                date = datetime.fromisoformat(value)
                if date.utcoffset() is None:
                    raise ValueError("invalid_input")
                parsed[field] = date
            elif not isinstance(value, str) or not value.strip():
                raise ValueError("invalid_input")
        if parsed.get("occurred_until") is not None and "occurred_at" in parsed:
            if parsed["occurred_until"] < parsed["occurred_at"]:
                raise ValueError("invalid_input")
        return parsed
