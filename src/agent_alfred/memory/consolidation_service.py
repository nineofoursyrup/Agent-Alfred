"""Persistent consolidation batches on caller-owned RecordingStore transactions."""

from __future__ import annotations

import hmac
import json
import sqlite3
from dataclasses import replace
from uuid import uuid4

from agent_alfred.memory.consolidation import (
    ConsolidationCandidateReadError,
    ConsolidationCandidateReads,
    ConsolidationLimits,
    ConsolidationPlanError,
    ConsolidationSource,
    ConsolidationSourceEvidenceError,
    ConsolidationSourceTooLarge,
    CreateSemanticIntention,
    PreparedConsolidation,
    SelectedConsolidationSources,
    UpdateSemanticIntention,
    parse_consolidation_plan,
    prepare_consolidation_request,
    select_consolidation_sources,
)
from agent_alfred.memory.types import (
    ConsolidationApprovalProof,
    ConsolidationOrigin,
    DuplicateConflict,
    FactQuery,
    MemoryId,
    NotFound,
    ProtectedMemoryError,
    UpdateApplied,
    VersionConflict,
    origin_json,
)
from agent_alfred.messages import TextBlock, text_message
from agent_alfred.model import ModelRef, ModelRequest
from agent_alfred.resource_rollback import raise_if_rollback_pending
from agent_alfred.schema import parse_instant


def _json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _error(code: str, **metadata):
    return {"error": {"code": code, **metadata}}


_ACTION_BODY_FIELDS = ("plan", "episode_summary", "candidate_text")


def _without_action_bodies(value):
    if not isinstance(value, dict):
        return value
    return {
        key: item for key, item in value.items() if key not in _ACTION_BODY_FIELDS
    }


def _log_text(content: str) -> str:
    try:
        blocks = json.loads(content)
    except ValueError:
        return ""
    if not isinstance(blocks, list):
        return ""
    parts = []
    for block in blocks:
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and type(block.get("text")) is str
        ):
            parts.append(block["text"])
    return "".join(parts)


class ConsolidationService:
    """Batch lifecycle for Issue 18. Does not dispatch a model or write mirrors."""

    def __init__(self, owner, *, limits: ConsolidationLimits | None = None):
        self._owner = owner
        self._db = owner._db
        self._limits = limits or ConsolidationLimits()
        from agent_alfred.memory.consolidation_scheduling import ConsolidationScheduling
        self.scheduling = ConsolidationScheduling(self)

    def session_status(self, session_id: str) -> dict:
        if type(session_id) is not str or not session_id:
            return _error("invalid_input")
        try:
            with self._db.reading() as conn:
                loaded = self._load_sources(conn, session_id)
                if "error" in loaded:
                    return loaded
                block = conn.execute(
                    "SELECT run_id, characters, \"limit\" FROM "
                    "memory_consolidation_blocks WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                batch = conn.execute(
                    "SELECT batch_id, revision, status, error_code FROM "
                    "memory_consolidation_batches WHERE session_id=? "
                    "AND status IN ("
                    "'queued','running','awaiting_approval','failed'"
                    ") ORDER BY revision DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
                return {
                    "session_id": session_id,
                    "unprocessed_count": len(loaded["sources"]),
                    "threshold": self._limits.source_threshold,
                    "block": (
                        None
                        if block is None
                        else {
                            "run_id": block[0],
                            "reason": "source_too_large",
                            "characters": block[1],
                            "limit": block[2],
                        }
                    ),
                    "batch": (
                        None
                        if batch is None
                        else {
                            "batch_id": batch[0],
                            "revision": batch[1],
                            "status": batch[2],
                            "error_code": batch[3],
                        }
                    ),
                }
        except (sqlite3.Error, ValueError):
            return _error("storage_read_failed")

    def list_queue(self, *, session_id=None, limit=50, offset=0):
        with self._db.reading() as conn:
            sessions = conn.execute(
                "SELECT session_id FROM sessions WHERE ? IS NULL OR session_id=? "
                "ORDER BY rowid DESC LIMIT ? OFFSET ?",
                (session_id, session_id, limit + 1, offset),
            ).fetchall()
            batches = conn.execute(
                "SELECT batch_id FROM memory_consolidation_batches WHERE ? IS NULL "
                "OR session_id=? ORDER BY rowid DESC LIMIT ? OFFSET ?",
                (session_id, session_id, limit + 1, offset),
            ).fetchall()
            summaries = [
                _without_action_bodies(self._batch_view(conn, row[0]))
                for row in batches[:limit]
            ]
        return {
            "sessions": [self.session_status(row[0]) for row in sessions[:limit]],
            "batches": summaries,
            "sessions_has_more": len(sessions) > limit,
            "batches_has_more": len(batches) > limit,
        }

    def get_action_result(self, operation_id):
        """An operation's own body-free fact, never a successor batch view."""
        with self._db.reading() as conn:
            row = conn.execute(
                "SELECT receipt FROM memory_consolidation_actions WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is None:
                return None
            result = _without_action_bodies(json.loads(row[0]))
            if (
                result.get("action") == "begin_generation"
                or result.get("status") == "generation_required"
            ):
                projected = self._project_action_result(conn, result)
                if result.get("generation_run_id"):
                    projected = {**projected,
                                 "generation_run_id": result["generation_run_id"]}
                return projected
            return result

    def get_batch(self, batch_id: str) -> dict | None:
        if type(batch_id) is not str or not batch_id:
            return None
        with self._db.reading() as conn:
            return self._batch_view(conn, batch_id)

    def get_action(self, operation_id: str) -> dict | None:
        if type(operation_id) is not str or not operation_id:
            return None
        with self._db.reading() as conn:
            row = conn.execute(
                "SELECT receipt FROM memory_consolidation_actions "
                "WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is None:
                return None
            return self._project_action_result(conn, json.loads(row[0]))

    def generation_input_is_current(self, batch_id, revision, run_id):
        """Check the frozen input at dispatch, under the existing Run lease.

        Source safety is checked by input registration. Content versions are
        independent: editing a record need not quarantine its source group.
        Protection alone is deliberately rechecked at approval, not rejected.
        """
        with self._db.reading() as conn:
            batch = conn.execute(
                "SELECT status,generation_run_id FROM memory_consolidation_batches "
                "WHERE batch_id=? AND revision=?",
                (batch_id, revision),
            ).fetchone()
            if batch != ("running", run_id):
                return False
            facts, _ = self._owner._stores(conn)
            for memory_id, version in conn.execute(
                "SELECT memory_id,record_version FROM memory_consolidation_reads "
                "WHERE batch_id=? AND revision=? AND in_request=1",
                (batch_id, revision),
            ):
                record = facts.get(MemoryId(memory_id))
                if record is None or record.record_version != version:
                    return False
        return True

    def finalize_run(self, conn, run_id):
        """Join a terminal Run's stranded batch to the recording transaction.

        A begin/finish return may be interrupted after its transaction commits.
        Only still-running batches owned by this Run are retired; a committed
        result or awaiting approval is independent of recording success.
        """
        rows = conn.execute(
            "SELECT batch_id,revision FROM memory_consolidation_batches "
            "WHERE generation_run_id=? AND status='running'",
            (run_id,),
        ).fetchall()
        for batch_id, revision in rows:
            self._fail_generation(conn, batch_id, revision, "generation_interrupted")
        if rows:
            self._owner.forgetting.prepare_commit(conn)

    def notify_finalized(self):
        self._owner.forgetting.notify_committed(self._owner.memory_revision)

    def recover_abandoned(self) -> None:
        """Retire running batches whose generation cannot resume in this process."""
        with self._db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT batch_id, revision FROM memory_consolidation_batches "
                "WHERE status='running'"
            ).fetchall()
            for batch_id, revision in rows:
                self._fail_generation(
                    conn, batch_id, revision, "storage_write_failed"
                )
            retired = self._clear_obsolete_bodies(conn)
            notified = None
            if rows or retired:
                notified = self._owner.forgetting.prepare_commit(conn)
            conn.commit()
        if notified is not None:
            self._owner.forgetting.notify_committed(notified)

    def submit(
        self,
        session_id: str,
        model_output,
        *,
        context,
        operation_id: str,
        model,
    ) -> dict:
        payload = {
            "action": "submit",
            "session_id": session_id,
            "operation_id": operation_id,
        }
        return self._act(operation_id, payload, context, lambda conn: self._submit(
            conn, session_id, model_output, context=context, model=model
        ))

    def begin_generation(
        self,
        session_id: str,
        *,
        context,
        operation_id: str,
        model,
        generation_run_id: str,
        retry_batch_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict:
        payload = {
            "action": "begin_generation",
            "session_id": session_id,
            "operation_id": operation_id,
            "generation_run_id": generation_run_id,
            "retry_batch_id": retry_batch_id,
            "expected_revision": expected_revision,
        }
        return self._act(
            operation_id,
            payload,
            context,
            lambda conn: self._begin_generation(
                conn,
                session_id,
                context=context,
                model=model,
                generation_run_id=generation_run_id,
                retry_batch_id=retry_batch_id,
                expected_revision=expected_revision,
            ),
        )

    def finish_generation(
        self,
        batch_id: str,
        expected_revision: int,
        model_output,
        *,
        context,
        operation_id: str,
    ) -> dict:
        payload = {
            "action": "finish_generation",
            "batch_id": batch_id,
            "revision": expected_revision,
            "operation_id": operation_id,
        }
        return self._act(
            operation_id,
            payload,
            context,
            lambda conn: self._finish_generation(
                conn, batch_id, expected_revision, model_output, context
            ),
        )

    def fail_generation(
        self,
        batch_id: str,
        expected_revision: int,
        *,
        context,
        operation_id: str,
        error_code: str = "storage_write_failed",
    ) -> dict:
        payload = {
            "action": "fail_generation",
            "batch_id": batch_id,
            "revision": expected_revision,
            "operation_id": operation_id,
            "error_code": error_code,
        }
        return self._act(
            operation_id,
            payload,
            context,
            lambda conn: self._fail_generation(
                conn, batch_id, expected_revision, error_code
            ),
        )

    def approve(
        self, batch_id: str, expected_revision: int, *, context, operation_id: str
    ) -> dict:
        payload = {
            "action": "approve",
            "batch_id": batch_id,
            "revision": expected_revision,
            "operation_id": operation_id,
        }
        return self._act(
            operation_id,
            payload,
            context,
            lambda conn: self._approve(
                conn, batch_id, expected_revision, context, operation_id
            ),
        )

    def reject(
        self, batch_id: str, expected_revision: int, *, context, operation_id: str
    ) -> dict:
        payload = {
            "action": "reject",
            "batch_id": batch_id,
            "revision": expected_revision,
            "operation_id": operation_id,
        }
        return self._act(
            operation_id,
            payload,
            context,
            lambda conn: self._reject(conn, batch_id, expected_revision),
        )

    def retry(
        self,
        batch_id: str,
        expected_revision: int,
        *,
        context,
        operation_id: str,
        model=None,
        model_output=None,
    ) -> dict:
        payload = {
            "action": "retry",
            "batch_id": batch_id,
            "revision": expected_revision,
            "operation_id": operation_id,
            "has_output": model_output is not None,
        }
        return self._act(
            operation_id,
            payload,
            context,
            lambda conn: self._retry(
                conn,
                batch_id,
                expected_revision,
                context=context,
                model=model,
                model_output=model_output,
            ),
        )

    def skip_oversized(
        self, session_id: str, run_id: str, *, context, operation_id: str
    ) -> dict:
        payload = {
            "action": "skip_oversized",
            "session_id": session_id,
            "run_id": run_id,
            "operation_id": operation_id,
        }
        return self._act(
            operation_id,
            payload,
            context,
            lambda conn: self._skip(conn, session_id, run_id),
        )

    def reconciliation_targets(self, connection, *, memories, isolated):
        return ()

    def invalidate(self, connection, *, memories=(), isolated=(), exclude=()):
        self._clear_obsolete_bodies(connection)
        memory_ids = {(kind, memory_id) for kind, memory_id in memories}
        groups = set(isolated)
        skipped = set(exclude)
        rows = connection.execute(
            "SELECT batch_id, revision FROM memory_consolidation_batches "
            "WHERE status IN ("
            "'queued','running','awaiting_approval','failed'"
            ")"
        ).fetchall()
        for batch_id, revision in rows:
            if batch_id in skipped:
                continue
            sources = {
                row[0]
                for row in connection.execute(
                    "SELECT run_id FROM memory_consolidation_sources "
                    "WHERE batch_id=? AND revision=?",
                    (batch_id, revision),
                )
            }
            deps = list(
                connection.execute(
                    "SELECT kind, memory_id FROM memory_consolidation_reads "
                    "WHERE batch_id=? AND revision=? AND in_request=1",
                    (batch_id, revision),
                )
            )
            if sources & groups or any(item in memory_ids for item in deps):
                self._invalidate(connection, batch_id, revision)
        return ()

    def _act(self, operation_id, payload, context, produce):
        if type(operation_id) is not str or not operation_id:
            return _error("invalid_input")
        canonical = _json(
            {
                "version": 1,
                "payload": payload,
                "origin": origin_json(context.origin),
                "source": context.source,
            }
        ).encode()
        fingerprint, key_id = self._owner._key.fingerprint(canonical)

        def operation():
            with self._db.transaction() as conn:
                conn.execute("BEGIN IMMEDIATE")
                old = conn.execute(
                    "SELECT fingerprint,key_id,receipt FROM "
                    "memory_consolidation_actions WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if old is not None:
                    if old[1] != key_id:
                        return _error("operation_unverifiable")
                    if not hmac.compare_digest(old[0], fingerprint):
                        return _error("operation_mismatch")
                    return self._project_action_result(conn, json.loads(old[2]))
                result = produce(conn)
                if "error" in result and "status" not in result:
                    return result
                conn.execute(
                    "INSERT INTO memory_consolidation_actions VALUES (?,?,?,?)",
                    (
                        operation_id,
                        fingerprint,
                        key_id,
                        _json(self._durable_action_receipt(payload, result)),
                    ),
                )
                revision = self._owner.forgetting.prepare_commit(conn)
                conn.commit()
            self._owner.forgetting.notify_committed(revision)
            return result

        return self._owner._run_mutation(operation, context)

    def _durable_action_receipt(self, payload, result):
        if not isinstance(result, dict):
            return {"action": payload.get("action")}
        batch_id = result.get("batch_id") or payload.get("batch_id")
        revision = result.get("revision") or payload.get("revision")
        if type(batch_id) is str and batch_id:
            durable = {"action": payload.get("action"), "batch_id": batch_id}
            if type(revision) is int:
                durable["revision"] = revision
            status = result.get("status")
            if status in (
                "failed", "succeeded", "rejected", "invalidated", "generation_required"
            ):
                durable["status"] = status
            if status == "failed" and isinstance(result.get("error"), dict):
                durable["error"] = result["error"]
            for key in ("generation_run_id", "generation_result"):
                if key in result:
                    durable[key] = result[key]
            if (
                payload.get("action") == "begin_generation"
                and payload.get("generation_run_id")
            ):
                durable["generation_run_id"] = payload["generation_run_id"]
            return durable
        return _without_action_bodies(result)

    def bind_retry_run(self, conn, operation_id, batch_id, revision, run_id):
        """Join intent and Run before logical handoff, never after submit returns.

        The admission owner supplies its transaction and reserved Run identity.
        A failed transaction leaves neither row; a committed row is interpreted
        through the existing Run admission/recovery evidence, never resubmitted.
        """
        from agent_alfred.memory.types import ManualOrigin

        payload = {
            "action": "retry",
            "batch_id": batch_id,
            "revision": revision,
            "operation_id": operation_id,
            "has_output": False,
        }
        fingerprint, key_id = self._owner._key.fingerprint(
            _json(
                {
                    "version": 1,
                    "payload": payload,
                    "origin": origin_json(ManualOrigin("cli")),
                    "source": "cli",
                }
            ).encode()
        )
        row = conn.execute(
            "SELECT fingerprint,key_id,receipt FROM memory_consolidation_actions "
            "WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if (
            row is None
            or row[1] != key_id
            or not hmac.compare_digest(row[0], fingerprint)
        ):
            raise ValueError("retry_identity_unverifiable")
        durable = json.loads(row[2])
        if durable.get("status") != "generation_required" or durable.get(
            "generation_run_id"
        ):
            raise ValueError("retry_already_bound")
        current = conn.execute(
            "SELECT revision,status FROM memory_consolidation_batches WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        if current != (revision, "failed"):
            raise ValueError("retry_candidate_changed")
        durable["generation_run_id"] = run_id
        conn.execute(
            "UPDATE memory_consolidation_actions SET receipt=? WHERE operation_id=?",
            (_json(durable), operation_id),
        )

    def _project_generation_intent(self, conn, durable):
        run_id = durable.get("generation_run_id")
        batch_id = durable["batch_id"]
        view = self._batch_view(conn, batch_id)
        if run_id is None:
            # No accepted Run transaction exists for this intent. Recheck the
            # exact requested revision before allowing admission to try again.
            if view is not None and view["revision"] == durable["revision"]:
                if view["status"] == "failed":
                    return {**durable, "error": {"code": "generation_required"}}
                if view["status"] == "invalidated":
                    return _error("batch_invalidated")
            return _error("invalid_batch_state")
        if "generation_result" in durable:
            return durable["generation_result"]
        if view is not None and view["generation_run_id"] == run_id:
            if view["status"] == "succeeded" and view.get("receipt"):
                return view["receipt"]
            return _without_action_bodies(view)
        begun = conn.execute(
            "SELECT receipt FROM memory_consolidation_actions WHERE operation_id=?",
            ("begin:" + run_id,),
        ).fetchone()
        if begun is not None:
            result = json.loads(begun[0])
            if result.get("status") in ("below_threshold", "source_too_large"):
                return {
                    **result,
                    "batch_id": batch_id,
                    "revision": None,
                    "generation_run_id": run_id,
                }
        row = conn.execute(
            "SELECT phase,outcome,admission_state FROM runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        # No generation revision exists yet. In particular, the preceding
        # batch's error/Run is not evidence about this admitted operation.
        return {
            "batch_id": batch_id,
            "revision": None,
            "generation_run_id": run_id,
            "status": "failed" if row and row[0] == "finished" else "running",
            "error_code": (
                "generation_interrupted"
                if row and row[1] == "interrupted"
                else "generation_failed"
                if row and row[1] == "failed"
                else "generation_unconfirmed"
            ),
            "run_phase": row[0] if row else None,
            "run_outcome": row[1] if row else None,
            "admission_state": row[2] if row else None,
        }

    def _archive_generation_result(self, conn, batch_id, next_run_id):
        """Preserve only safe facts before the batch moves to a different Run."""
        view = self._batch_view(conn, batch_id)
        if (
            view is None
            or view["generation_run_id"] is None
            or view["generation_run_id"] == next_run_id
        ):
            return
        rows = conn.execute(
            "SELECT operation_id,receipt FROM memory_consolidation_actions"
        ).fetchall()
        for operation_id, receipt in rows:
            durable = json.loads(receipt)
            # Older begin receipts did not persist the Run. The still-current
            # revision provides authoritative ownership before it is replaced.
            if (
                durable.get("action") == "begin_generation"
                and durable.get("batch_id") == batch_id
                and durable.get("revision") == view["revision"]
                and not durable.get("generation_run_id")
            ):
                durable["generation_run_id"] = view["generation_run_id"]
            if (
                durable.get("generation_run_id") == view["generation_run_id"]
                and durable.get("batch_id") == batch_id
                and "generation_result" not in durable
            ):
                durable["generation_result"] = _without_action_bodies(view)
                conn.execute(
                    "UPDATE memory_consolidation_actions SET receipt=? "
                    "WHERE operation_id=?",
                    (_json(durable), operation_id),
                )

    def _project_legacy_generation(self, conn, durable):
        """Read old receipts using revision evidence, never a successor's view."""
        view = self._batch_view(conn, durable["batch_id"])
        if view is not None and view["revision"] == durable.get("revision"):
            if view["status"] == "succeeded" and view.get("receipt"):
                return view["receipt"]
            return _without_action_bodies(view)
        receipts = conn.execute(
            "SELECT receipt FROM memory_consolidation_actions"
        ).fetchall()
        historical = None
        for (receipt,) in receipts:
            other = json.loads(receipt)
            archived = other.get("generation_result")
            if (
                isinstance(archived, dict)
                and archived.get("batch_id") == durable["batch_id"]
                and archived.get("revision") == durable.get("revision")
            ):
                return _without_action_bodies(archived)
            if (
                other.get("batch_id") == durable["batch_id"]
                and other.get("revision") == durable.get("revision")
                and other.get("action") in ("finish_generation", "fail_generation")
                and other.get("status") in (
                    "failed", "succeeded", "rejected", "invalidated"
                )
            ):
                historical = _without_action_bodies(other)
        if historical is not None:
            return historical
        if durable.get("status") in ("failed", "succeeded", "rejected", "invalidated"):
            return durable
        # An old receipt without surviving terminal evidence must not borrow
        # another revision's status, products or Run identity.
        return {**durable, "error": {"code": "generation_unconfirmed"}}

    def _project_action_result(self, conn, durable):
        durable = _without_action_bodies(durable)
        if not isinstance(durable, dict):
            return durable
        batch_id = durable.get("batch_id")
        if type(batch_id) is not str or not batch_id:
            return durable
        if durable.get("action") == "begin_generation":
            if not durable.get("generation_run_id"):
                return self._project_legacy_generation(conn, durable)
            return self._project_generation_intent(conn, durable)
        if durable.get("status") == "generation_required":
            return self._project_generation_intent(conn, durable)
        if durable.get("status") == "failed":
            return {
                "status": "failed",
                "batch_id": batch_id,
                "revision": durable.get("revision"),
                "error": durable.get("error")
                or {"code": "storage_write_failed"},
            }
        if durable.get("status") == "rejected":
            return {
                "status": "rejected",
                "batch_id": batch_id,
                "revision": durable.get("revision"),
            }
        view = self._batch_view(conn, batch_id)
        if view is None:
            return {
                "status": "invalidated",
                "batch_id": batch_id,
                "revision": durable.get("revision"),
                "error": {"code": "batch_invalidated"},
            }
        if (
            type(durable.get("revision")) is int
            and view["revision"] != durable["revision"]
        ):
            if durable.get("status") in (
                "failed",
                "succeeded",
                "rejected",
                "invalidated",
            ):
                return _without_action_bodies(view)
        if view["status"] == "succeeded" and view.get("receipt"):
            return view["receipt"]
        return view

    def _submit(self, conn, session_id, model_output, *, context, model):
        if type(session_id) is not str or not session_id:
            return _error("invalid_input")
        open_batch = conn.execute(
            "SELECT batch_id, status FROM memory_consolidation_batches "
            "WHERE session_id=? AND status IN ("
            "'queued','running','awaiting_approval','failed'"
            ")",
            (session_id,),
        ).fetchone()
        if open_batch is not None:
            return _error(
                "batch_unresolved", batch_id=open_batch[0], status=open_batch[1]
            )
        prepared = self._prepare(conn, session_id, model)
        if "error" in prepared:
            return prepared
        if prepared.get("status") in ("below_threshold", "source_too_large"):
            return prepared
        return self._record_plan(
            conn,
            prepared["prepared"],
            model_output,
            context=context,
            generation_run_id=None,
        )

    def _begin_generation(
        self,
        conn,
        session_id,
        *,
        context,
        model,
        generation_run_id,
        retry_batch_id=None,
        expected_revision=None,
    ):
        if type(session_id) is not str or not session_id:
            return _error("invalid_input")
        if type(generation_run_id) is not str or not generation_run_id:
            return _error("invalid_input")
        retrying = retry_batch_id is not None
        if retrying and (
            type(retry_batch_id) is not str
            or type(expected_revision) is not int
            or expected_revision < 1
        ):
            return _error("invalid_input")
        open_batch = conn.execute(
            "SELECT batch_id, status, revision FROM memory_consolidation_batches "
            "WHERE session_id=? AND status IN ("
            "'queued','running','awaiting_approval','failed'"
            ")",
            (session_id,),
        ).fetchone()
        if retrying:
            if (
                open_batch is None
                or open_batch[0] != retry_batch_id
                or open_batch[2] != expected_revision
            ):
                return _error("not_found")
            if open_batch[1] != "failed":
                return _error("invalid_batch_state", status=open_batch[1])
        elif open_batch is not None:
            return _error(
                "batch_unresolved", batch_id=open_batch[0], status=open_batch[1]
            )
        prepared = self._prepare(conn, session_id, model)
        if "error" in prepared:
            return self._record_prepare_failure(
                conn,
                session_id,
                generation_run_id,
                prepared["error"].get("code") or "storage_read_failed",
                retry_batch_id=retry_batch_id,
                expected_revision=expected_revision,
                sources=prepared.get("sources", ()),
            )
        if prepared.get("status") in ("below_threshold", "source_too_large"):
            return prepared
        bound = prepared["prepared"]
        now = self._now()
        if retrying:
            batch_id = retry_batch_id
            revision = expected_revision + 1
        else:
            batch_id = uuid4().hex
            revision = 1
        self._insert_batch(
            conn,
            batch_id=batch_id,
            prepared=bound,
            revision=revision,
            status="running",
            error_code=None,
            now=now,
            plan=None,
            generation_run_id=generation_run_id,
            request_json=self._freeze_request(bound.request),
        )
        grouped = self._owner.forgetting.register_group(
            generation_run_id,
            kind="run",
            container_id=session_id,
            evidence="complete",
            evidence_id="consolidation-generation:" + generation_run_id,
            context=context,
            transaction=conn,
        )
        if "error" in grouped:
            return grouped
        return {
            "status": "running",
            "batch_id": batch_id,
            "revision": revision,
            "source_run_ids": [source.run_id for source in bound.sources],
            "candidate_refs": [
                {
                    "kind": "semantic",
                    "id": record.id,
                    "version": record.record_version,
                }
                for record in bound.candidates
            ],
            "request": self._freeze_request(bound.request),
        }

    def _record_prepare_failure(
        self,
        conn,
        session_id,
        generation_run_id,
        error_code,
        *,
        retry_batch_id=None,
        expected_revision=None,
        sources=(),
    ):
        now = self._now()
        if retry_batch_id is not None:
            batch_id = retry_batch_id
            self._archive_generation_result(conn, batch_id, generation_run_id)
            self._clear_bodies(conn, batch_id, expected_revision)
            revision = expected_revision + 1
            conn.execute(
                "UPDATE memory_consolidation_batches SET status='failed', "
                "error_code=?, updated_at=?, finished_at=?, "
                "generation_run_id=?, revision=? "
                "WHERE batch_id=? AND revision=?",
                (
                    error_code,
                    now,
                    now,
                    generation_run_id,
                    revision,
                    batch_id,
                    expected_revision,
                ),
            )
        else:
            batch_id = uuid4().hex
            revision = 1
            conn.execute(
                "INSERT INTO memory_consolidation_batches ("
                "batch_id, session_id, revision, status, created_at, updated_at, "
                "finished_at, error_code, generation_run_id, receipt"
                ") VALUES (?,?,?,'failed',?,?,?,?,?,NULL)",
                (
                    batch_id,
                    session_id,
                    revision,
                    now,
                    now,
                    now,
                    error_code,
                    generation_run_id,
                ),
            )
        conn.executemany(
            "INSERT INTO memory_consolidation_sources "
            "(batch_id,revision,run_id,session_id,ordinal,accepted_at,finished_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [(batch_id, revision, source.run_id, source.session_id, ordinal,
              source.accepted_at.isoformat(), source.finished_at.isoformat())
             for ordinal, source in enumerate(sources)],
        )
        return {
            "status": "failed",
            "batch_id": batch_id,
            "revision": revision,
            "error": {"code": error_code},
        }

    def _finish_generation(
        self, conn, batch_id, expected_revision, model_output, context
    ):
        row = conn.execute(
            "SELECT session_id, status FROM memory_consolidation_batches "
            "WHERE batch_id=? AND revision=?",
            (batch_id, expected_revision),
        ).fetchone()
        if row is None:
            return _error("not_found")
        if row[1] == "invalidated":
            return _error("batch_invalidated")
        if row[1] != "running":
            return _error("invalid_batch_state", status=row[1])
        restored = self._restore_prepared(conn, batch_id, expected_revision)
        if "prepared" not in restored:
            return restored
        return self._record_plan(
            conn,
            restored["prepared"],
            model_output,
            context=context,
            generation_run_id=conn.execute(
                "SELECT generation_run_id FROM memory_consolidation_batches "
                "WHERE batch_id=?",
                (batch_id,),
            ).fetchone()[0],
            batch_id=batch_id,
            revision=expected_revision,
        )

    def _fail_generation(self, conn, batch_id, expected_revision, error_code):
        row = conn.execute(
            "SELECT status FROM memory_consolidation_batches "
            "WHERE batch_id=? AND revision=?",
            (batch_id, expected_revision),
        ).fetchone()
        if row is None:
            return _error("not_found")
        if row[0] not in ("queued", "running"):
            return self._batch_view(conn, batch_id) or _error(
                "invalid_batch_state", status=row[0]
            )
        return self._persist_failed_apply(
            conn,
            batch_id,
            expected_revision,
            {"code": error_code},
            None,
            None,
        )

    def _freeze_request(self, request: ModelRequest) -> str:
        return _json(
            {
                "model": {
                    "endpoint_id": request.model.endpoint_id,
                    "model_id": request.model.model_id,
                },
                "system": [block.text for block in (request.system or ())],
                "messages": [
                    {
                        "role": message.role,
                        "text": "".join(
                            block.text
                            for block in message.blocks
                            if isinstance(block, TextBlock)
                        ),
                    }
                    for message in request.messages
                ],
                "tool_choice": request.tool_choice,
            }
        )

    def thaw_request(self, payload, *, on_attempt_preflight=None) -> ModelRequest:
        data = json.loads(payload) if type(payload) is str else payload
        return ModelRequest(
            model=ModelRef(data["model"]["endpoint_id"], data["model"]["model_id"]),
            system=tuple(TextBlock(text) for text in data.get("system") or ()),
            messages=tuple(
                text_message(item["role"], item["text"])
                for item in data.get("messages") or ()
            ),
            tools=(),
            tool_choice=data.get("tool_choice") or "none",
            on_attempt_preflight=on_attempt_preflight,
        )

    def _record_source_block(self, conn, selected):
        now = self._now()
        conn.execute(
            "INSERT INTO memory_consolidation_blocks "
            "(session_id, run_id, reason, characters, \"limit\", created_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "run_id=excluded.run_id, characters=excluded.characters, "
            "\"limit\"=excluded.\"limit\", created_at=excluded.created_at",
            (
                selected.session_id,
                selected.run_id,
                "source_too_large",
                selected.characters,
                selected.limit,
                now,
            ),
        )

    def _prepare(self, conn, session_id, model):
        loaded = self._load_sources(conn, session_id)
        if "error" in loaded:
            return loaded
        sources = loaded["sources"]
        selected = select_consolidation_sources(
            session_id, sources, model=model, limits=self._limits
        )
        if isinstance(selected, ConsolidationSourceTooLarge):
            self._record_source_block(conn, selected)
            return {
                "status": "source_too_large",
                "session_id": selected.session_id,
                "run_id": selected.run_id,
                "characters": selected.characters,
                "limit": selected.limit,
            }
        if isinstance(selected, ConsolidationSourceEvidenceError):
            return _error(selected.code, run_id=selected.run_id)
        if not isinstance(selected, SelectedConsolidationSources):
            return {
                "status": "below_threshold",
                "session_id": session_id,
                "unprocessed_count": selected.unprocessed_count,
                "threshold": selected.threshold,
            }
        reads = self._candidate_reads(conn, selected)
        if isinstance(reads, ConsolidationCandidateReadError):
            return {**_error(reads.code), "sources": selected.sources}
        bound = prepare_consolidation_request(selected, reads)
        if not isinstance(bound, PreparedConsolidation):
            return _error(bound.code)
        return {"prepared": bound}

    def _record_plan(
        self,
        conn,
        prepared: PreparedConsolidation,
        model_output,
        *,
        context,
        generation_run_id,
        batch_id=None,
        revision=1,
    ):
        now = self._now()
        batch_id = batch_id or uuid4().hex
        try:
            plan = parse_consolidation_plan(model_output, prepared)
        except ConsolidationPlanError as error:
            self._insert_batch(
                conn,
                batch_id=batch_id,
                prepared=prepared,
                revision=revision,
                status="failed",
                error_code=error.code,
                now=now,
                plan=None,
                generation_run_id=generation_run_id,
            )
            return {
                "status": "failed",
                "batch_id": batch_id,
                "revision": revision,
                "error": {"code": error.code},
            }
        needs_approval = self._needs_approval(conn, plan)
        self._insert_batch(
            conn,
            batch_id=batch_id,
            prepared=prepared,
            revision=revision,
            status="awaiting_approval" if needs_approval else "failed",
            error_code=None,
            now=now,
            plan=plan,
            generation_run_id=generation_run_id,
        )
        if needs_approval:
            return self._batch_view(conn, batch_id)
        return self._apply_after_candidate(
            conn, batch_id, revision, prepared, plan, context, proof=None
        )

    def _needs_approval(self, conn, plan) -> bool:
        semantic, _ = self._owner._stores(conn)
        for item in plan.semantic:
            if not isinstance(item, UpdateSemanticIntention):
                continue
            current = semantic.get(item.id)
            if current is not None and current.human_protected:
                return True
        return False

    def _apply_after_candidate(
        self, conn, batch_id, revision, prepared, plan, context, *, proof
    ):
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        return self._safe_apply(
            conn, batch_id, revision, prepared, plan, context, proof=proof
        )

    def _safe_apply(
        self,
        conn,
        batch_id,
        revision,
        prepared,
        plan,
        context,
        *,
        proof,
        approval_operation_id=None,
    ):
        try:
            applied = self._commit_plan(
                conn, batch_id, revision, prepared, plan, context, proof=proof
            )
        except sqlite3.Error as error:
            raise_if_rollback_pending(error)
            if conn.in_transaction:
                conn.rollback()
            conn.execute("BEGIN IMMEDIATE")
            return self._persist_failed_apply(
                conn,
                batch_id,
                revision,
                {"code": "storage_write_failed"},
                proof,
                approval_operation_id,
            )
        if "error" in applied and applied.get("status") != "invalidated":
            if conn.in_transaction:
                conn.rollback()
            conn.execute("BEGIN IMMEDIATE")
            return self._persist_failed_apply(
                conn,
                batch_id,
                revision,
                applied["error"],
                proof,
                approval_operation_id,
            )
        return applied

    def _persist_failed_apply(
        self, conn, batch_id, revision, error, proof, approval_operation_id
    ):
        now = self._now()
        conn.execute(
            "UPDATE memory_consolidation_batches SET status='failed', "
            "error_code=?, updated_at=? WHERE batch_id=? AND revision=? "
            "AND status IN ("
            "'queued','running','awaiting_approval','failed')",
            (error["code"], now, batch_id, revision),
        )
        if proof is not None and type(approval_operation_id) is str:
            conn.execute(
                "INSERT OR REPLACE INTO memory_consolidation_approvals "
                "VALUES (?,?,?,?)",
                (batch_id, revision, now, approval_operation_id),
            )
        return {
            "status": "failed",
            "batch_id": batch_id,
            "revision": revision,
            "error": error,
        }

    def _commit_plan(
        self, conn, batch_id, revision, prepared, plan, context, *, proof
    ):
        token = self._history_token(conn, prepared)
        if "error" in token:
            return token
        if token.get("denied"):
            return self._invalidated_result(conn, batch_id, revision)
        targets = tuple(
            ("semantic", record.id, record.record_version)
            for record in prepared.candidates
        )

        def write(inner):
            return self._apply_writes(
                inner,
                batch_id,
                revision,
                prepared,
                plan,
                context,
                proof=proof,
            )

        result = self._write_with_refresh(
            conn, token, targets, write, context, prepared
        )
        if result.get("error", {}).get("code") == "sources_unavailable":
            return self._invalidated_result(conn, batch_id, revision)
        if result.get("error", {}).get("code") == "batch_invalidated":
            return self._invalidated_result(conn, batch_id, revision)
        if result.get("status") == "awaiting_approval":
            conn.execute(
                "UPDATE memory_consolidation_batches SET status="
                "'awaiting_approval', error_code=NULL, updated_at=? "
                "WHERE batch_id=? AND revision=?",
                (self._now(), batch_id, revision),
            )
            return self._batch_view(conn, batch_id)
        return result

    def _invalidated_result(self, conn, batch_id, revision):
        self._invalidate(conn, batch_id, revision)
        return {
            "status": "invalidated",
            "batch_id": batch_id,
            "revision": revision,
            "error": {"code": "batch_invalidated"},
        }

    def _write_with_refresh(self, conn, token, targets, write, context, prepared):
        forgetting = self._owner.forgetting
        result = forgetting.write_projection(
            token, targets=targets, write=write, context=context, transaction=conn
        )
        if result.get("error", {}).get("code") != "projection_stale":
            return result
        refreshed = self._history_token(conn, prepared)
        if "error" in refreshed:
            return refreshed
        if refreshed.get("denied"):
            return _error("sources_unavailable")
        return forgetting.write_projection(
            refreshed,
            targets=targets,
            write=write,
            context=context,
            transaction=conn,
        )

    def _history_token(self, conn, prepared: PreparedConsolidation):
        groups = tuple(source.run_id for source in prepared.sources)
        token = self._owner.forgetting.evaluate_history(
            groups, purpose="consolidation_source", transaction=conn
        )
        if "error" in token:
            return token
        return token

    def _apply_writes(
        self, conn, batch_id, revision, prepared, plan, context, *, proof
    ):
        semantic, episodic = self._owner._stores(conn)
        origin = ConsolidationOrigin(batch_id)
        approved = proof is not None
        if not approved and self._needs_approval(conn, plan):
            return {
                "status": "awaiting_approval",
                "batch_id": batch_id,
                "revision": revision,
            }
        for record in prepared.candidates:
            current = semantic.get(record.id)
            if current is None or current.record_version != record.record_version:
                return _error("batch_invalidated")
            if current.human_protected != record.human_protected and not approved:
                if any(
                    isinstance(item, UpdateSemanticIntention) and item.id == record.id
                    for item in plan.semantic
                ):
                    return {
                        "status": "awaiting_approval",
                        "batch_id": batch_id,
                        "revision": revision,
                    }
        products = []
        changed_memories = []
        for item in plan.semantic:
            if isinstance(item, CreateSemanticIntention):
                saved = semantic.save_with_result(
                    item.subject,
                    item.fact,
                    origin,
                    human_protected=approved,
                )
                products.append(("semantic", saved.id, saved.version))
            elif isinstance(item, UpdateSemanticIntention):
                try:
                    if proof is None:
                        outcome = semantic.update(
                            item.id,
                            expected_version=item.expected_version,
                            origin=origin,
                            subject=item.subject,
                            fact=item.fact,
                            human_protected=False,
                        )
                    else:
                        outcome = semantic.apply_approved_consolidation(
                            item.id,
                            expected_version=item.expected_version,
                            origin=origin,
                            proof=proof,
                            subject=item.subject,
                            fact=item.fact,
                        )
                except ProtectedMemoryError:
                    return _error("batch_conflict")
                if isinstance(
                    outcome, (VersionConflict, DuplicateConflict, NotFound)
                ):
                    return _error("batch_conflict")
                if not isinstance(outcome, UpdateApplied):
                    return _error("storage_write_failed")
                products.append(("semantic", outcome.id, outcome.version))
                if outcome.changed:
                    changed_memories.append(("semantic", item.id))
        saved_episode = episodic.save_with_result(
            plan.episode_summary,
            plan.occurred_at,
            plan.occurred_until,
            origin,
            human_protected=approved,
        )
        products.append(("episodic", saved_episode.id, saved_episode.version))
        if changed_memories:
            self.invalidate(
                conn,
                memories=tuple(changed_memories),
                isolated=(),
                exclude=(batch_id,),
            )
        generation = conn.execute(
            "SELECT generation_run_id FROM memory_consolidation_batches "
            "WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        groups = tuple(source.run_id for source in prepared.sources)
        if generation is not None and generation[0]:
            groups = (generation[0], *groups)
        registered = self._owner.forgetting.register_group(
            batch_id,
            kind="batch",
            container_id=prepared.session_id,
            evidence="complete",
            evidence_id="consolidation-batch:" + batch_id,
            context=context,
            transaction=conn,
        )
        if "error" in registered:
            return registered
        for kind, memory_id, version in products:
            sourced = self._owner.forgetting.register_sources(
                kind,
                memory_id,
                version,
                groups=groups,
                evidence_id="consolidation-batch:" + batch_id,
                context=context,
                transaction=conn,
            )
            if "error" in sourced:
                return sourced
        now = self._now()
        for source in prepared.sources:
            conn.execute(
                "INSERT INTO memory_consolidation_source_results "
                "(run_id, session_id, outcome, batch_id, recorded_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(run_id) DO NOTHING",
                (source.run_id, prepared.session_id, "succeeded", batch_id, now),
            )
        receipt = {
            "status": "succeeded",
            "batch_id": batch_id,
            "revision": revision,
            "products": [
                {"kind": kind, "memory_id": memory_id, "record_version": version}
                for kind, memory_id, version in products
            ],
        }
        conn.execute(
            "UPDATE memory_consolidation_batches SET status='succeeded', "
            "error_code=NULL, finished_at=?, updated_at=?, receipt=? "
            "WHERE batch_id=? AND revision=?",
            (now, now, _json(receipt), batch_id, revision),
        )
        conn.execute(
            "DELETE FROM memory_consolidation_blocks WHERE session_id=?",
            (prepared.session_id,),
        )
        self._clear_bodies(conn, batch_id, revision)
        self.scheduling.refresh_session(conn, prepared.session_id)
        return receipt

    def _approve(self, conn, batch_id, expected_revision, context, operation_id):
        row = conn.execute(
            "SELECT session_id, status FROM memory_consolidation_batches "
            "WHERE batch_id=? AND revision=?",
            (batch_id, expected_revision),
        ).fetchone()
        if row is None:
            return _error("not_found")
        if row[1] == "invalidated":
            return _error("batch_invalidated")
        if row[1] != "awaiting_approval":
            return _error("invalid_batch_state", status=row[1])
        prepared = self._restore_prepared(conn, batch_id, expected_revision)
        if "prepared" not in prepared:
            return prepared
        plan = self._restore_plan(
            conn, batch_id, expected_revision, prepared["prepared"]
        )
        if "plan" not in plan:
            return plan
        conn.execute(
            "INSERT OR REPLACE INTO memory_consolidation_approvals "
            "VALUES (?,?,?,?)",
            (batch_id, expected_revision, self._now(), operation_id),
        )
        proof = ConsolidationApprovalProof(batch_id, expected_revision)
        return self._safe_apply(
            conn,
            batch_id,
            expected_revision,
            prepared["prepared"],
            plan["plan"],
            context,
            proof=proof,
            approval_operation_id=operation_id,
        )

    def _reject(self, conn, batch_id, expected_revision):
        row = conn.execute(
            "SELECT session_id, status FROM memory_consolidation_batches "
            "WHERE batch_id=? AND revision=?",
            (batch_id, expected_revision),
        ).fetchone()
        if row is None:
            return _error("not_found")
        if row[1] == "invalidated":
            return _error("batch_invalidated")
        if row[1] != "awaiting_approval":
            return _error("invalid_batch_state", status=row[1])
        now = self._now()
        sources = conn.execute(
            "SELECT run_id, session_id FROM memory_consolidation_sources "
            "WHERE batch_id=? AND revision=?",
            (batch_id, expected_revision),
        ).fetchall()
        for run_id, session_id in sources:
            conn.execute(
                "INSERT INTO memory_consolidation_source_results "
                "(run_id, session_id, outcome, batch_id, recorded_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(run_id) DO NOTHING",
                (run_id, session_id, "user_rejected", batch_id, now),
            )
        conn.execute(
            "UPDATE memory_consolidation_batches SET status='rejected', "
            "finished_at=?, updated_at=?, receipt=? WHERE batch_id=? AND revision=?",
            (
                now,
                now,
                _json(
                    {
                        "status": "rejected",
                        "batch_id": batch_id,
                        "revision": expected_revision,
                    }
                ),
                batch_id,
                expected_revision,
            ),
        )
        self._clear_bodies(conn, batch_id, expected_revision)
        self.scheduling.refresh_session(conn, row[0])
        return {
            "status": "rejected",
            "batch_id": batch_id,
            "revision": expected_revision,
        }

    def _retry(
        self, conn, batch_id, expected_revision, *, context, model, model_output
    ):
        row = conn.execute(
            "SELECT session_id, status, receipt FROM memory_consolidation_batches "
            "WHERE batch_id=? AND revision=?",
            (batch_id, expected_revision),
        ).fetchone()
        if row is None:
            return _error("not_found")
        session_id, status, receipt = row
        if status == "succeeded" and receipt:
            return json.loads(receipt)
        if status == "invalidated":
            return _error("batch_invalidated")
        if status != "failed":
            return _error("invalid_batch_state", status=status)
        plan_row = conn.execute(
            "SELECT plan_json FROM memory_consolidation_plans "
            "WHERE batch_id=? AND revision=?",
            (batch_id, expected_revision),
        ).fetchone()
        if plan_row is not None and plan_row[0]:
            restored = self._restore_prepared(conn, batch_id, expected_revision)
            if "prepared" not in restored:
                return restored
            plan = self._restore_plan(
                conn, batch_id, expected_revision, restored["prepared"]
            )
            if "plan" not in plan:
                return plan
            approved = conn.execute(
                "SELECT 1 FROM memory_consolidation_approvals "
                "WHERE batch_id=? AND revision=?",
                (batch_id, expected_revision),
            ).fetchone()
            proof = (
                ConsolidationApprovalProof(batch_id, expected_revision)
                if approved is not None
                else None
            )
            return self._safe_apply(
                conn,
                batch_id,
                expected_revision,
                restored["prepared"],
                plan["plan"],
                context,
                proof=proof,
            )
        if model_output is None or model is None:
            return {
                "status": "generation_required",
                "batch_id": batch_id,
                "revision": expected_revision,
                "error": {"code": "generation_required"},
            }
        prepared = self._prepare(conn, session_id, model)
        if "error" in prepared or prepared.get("status") in (
            "below_threshold",
            "source_too_large",
        ):
            return prepared if "error" in prepared or "status" in prepared else prepared
        if "prepared" not in prepared:
            return prepared
        return self._record_plan(
            conn,
            prepared["prepared"],
            model_output,
            context=context,
            generation_run_id=None,
            batch_id=batch_id,
            revision=expected_revision + 1,
        )

    def _skip(self, conn, session_id, run_id):
        loaded = self._load_sources(conn, session_id)
        if "error" in loaded:
            return loaded
        sources = loaded["sources"]
        if not sources or sources[0].run_id != run_id:
            return _error("source_mismatch")
        block = conn.execute(
            "SELECT run_id FROM memory_consolidation_blocks WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if block is None or block[0] != run_id:
            return _error("source_mismatch")
        from agent_alfred.model import ModelRef

        selected = select_consolidation_sources(
            session_id,
            sources,
            model=ModelRef("consolidation", "primary"),
            limits=self._limits,
        )
        if (
            not isinstance(selected, ConsolidationSourceTooLarge)
            or selected.run_id != run_id
        ):
            return _error("source_mismatch")
        now = self._now()
        conn.execute(
            "INSERT INTO memory_consolidation_source_results "
            "(run_id, session_id, outcome, batch_id, recorded_at) "
            "VALUES (?,?,?,?,?)",
            (run_id, session_id, "user_skipped", None, now),
        )
        conn.execute(
            "DELETE FROM memory_consolidation_blocks WHERE session_id=?",
            (session_id,),
        )
        remaining = self._load_sources(conn, session_id)
        if "error" in remaining:
            return remaining
        count = len(remaining["sources"])
        self.scheduling.refresh_session(conn, session_id)
        return {
            "status": "skipped",
            "session_id": session_id,
            "run_id": run_id,
            "unprocessed_count": count,
            "threshold": self._limits.source_threshold,
        }

    def _load_sources(self, conn, session_id) -> dict:
        try:
            rows = conn.execute(
                """SELECT runs.run_id, runs.session_id, runs.accepted_at,
                          runs.finished_at, user_log.content, assistant_log.content
                   FROM runs
                   JOIN agent_log AS user_log
                     ON user_log.run_id = runs.run_id AND user_log.role = 'user'
                   JOIN agent_log AS assistant_log
                     ON assistant_log.run_id = runs.run_id
                    AND assistant_log.role = 'assistant'
                   LEFT JOIN memory_consolidation_source_results AS done
                     ON done.run_id = runs.run_id
                   WHERE runs.session_id = ?
                     AND runs.purpose = 'chat'
                     AND runs.phase = 'finished'
                     AND runs.outcome = 'completed'
                     AND runs.admission_state = 'admitted'
                     AND done.run_id IS NULL
                   ORDER BY user_log.id ASC""",
                (session_id,),
            ).fetchall()
        except sqlite3.Error:
            return _error("storage_read_failed")
        candidates = []
        for run_id, session, accepted, finished, user_raw, assistant_raw in rows:
            user_text = _log_text(user_raw)
            assistant_text = _log_text(assistant_raw)
            if not user_text.strip() or not assistant_text.strip():
                continue
            try:
                accepted_at = parse_instant(accepted)
                finished_at = parse_instant(finished)
            except (TypeError, ValueError):
                return _error("invalid_timestamp", run_id=run_id)
            candidates.append(
                ConsolidationSource(
                    session_id=session,
                    run_id=run_id,
                    user_text=user_text,
                    assistant_text=assistant_text,
                    accepted_at=accepted_at,
                    finished_at=finished_at,
                )
            )
        if not candidates:
            return {"sources": ()}
        token = self._owner.forgetting.evaluate_history(
            tuple(item.run_id for item in candidates),
            purpose="consolidation_source",
            connection=conn,
        )
        if "error" in token:
            return token
        allowed = set(token.get("allowed", ()))
        return {
            "sources": tuple(
                item for item in candidates if item.run_id in allowed
            )
        }

    def _candidate_reads(self, conn, selected: SelectedConsolidationSources):
        semantic, _ = self._owner._stores(conn)
        try:
            hits = semantic.search(
                FactQuery(
                    text=selected.search_query,
                    limit=self._limits.candidate_count_limit,
                )
            )
            recent = semantic.list_recent(limit=self._limits.candidate_count_limit)
        except (sqlite3.Error, ValueError):
            return ConsolidationCandidateReadError("storage_read_failed")
        records = [hit.record for hit in hits] + list(recent.records)
        token = self._owner.forgetting.evaluate_automatic_records(
            tuple(("semantic", record.id, record.record_version) for record in records),
            connection=conn,
        )
        if "error" in token:
            return ConsolidationCandidateReadError("storage_read_failed")
        denied = set(token["denied"])
        return ConsolidationCandidateReads(
            search_hits=tuple(
                hit.record
                for hit in hits
                if ("semantic", hit.record.id, hit.record.record_version) not in denied
            ),
            recent=tuple(
                record
                for record in recent.records
                if ("semantic", record.id, record.record_version) not in denied
            ),
        )

    def _insert_batch(
        self,
        conn,
        *,
        batch_id,
        prepared: PreparedConsolidation,
        revision,
        status,
        error_code,
        now,
        plan,
        generation_run_id,
        request_json=None,
    ):
        existing = conn.execute(
            "SELECT created_at, revision FROM memory_consolidation_batches "
            "WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO memory_consolidation_batches ("
                "batch_id, session_id, revision, status, created_at, updated_at, "
                "finished_at, error_code, generation_run_id, receipt"
                ") VALUES (?,?,?,?,?,?,NULL,?,?,NULL)",
                (
                    batch_id,
                    prepared.session_id,
                    revision,
                    status,
                    now,
                    now,
                    error_code,
                    generation_run_id,
                ),
            )
        else:
            self._archive_generation_result(conn, batch_id, generation_run_id)
            if existing[1] != revision:
                self._clear_bodies(conn, batch_id, existing[1])
            conn.execute(
                "UPDATE memory_consolidation_batches SET revision=?, status=?, "
                "updated_at=?, finished_at=NULL, error_code=?, generation_run_id=?, "
                "receipt=NULL WHERE batch_id=?",
                (
                    revision,
                    status,
                    now,
                    error_code,
                    generation_run_id,
                    batch_id,
                ),
            )
        if existing is None or existing[1] != revision:
            conn.executemany(
                "INSERT INTO memory_consolidation_sources "
                "(batch_id, revision, run_id, session_id, ordinal, accepted_at, "
                "finished_at) VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        batch_id,
                        revision,
                        source.run_id,
                        source.session_id,
                        index,
                        source.accepted_at.isoformat(),
                        source.finished_at.isoformat(),
                    )
                    for index, source in enumerate(prepared.sources)
                ],
            )
            selected_ids = {record.id for record in prepared.candidates}
            conn.executemany(
                "INSERT INTO memory_consolidation_reads "
                "(batch_id, revision, kind, memory_id, record_version, "
                "human_protected, in_request) VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        batch_id,
                        revision,
                        "semantic",
                        record.id,
                        record.record_version,
                        int(record.human_protected),
                        int(record.id in selected_ids),
                    )
                    for record in prepared.candidates
                ],
            )
        plan_json = None if plan is None else _json(self._plan_payload(plan))
        if request_json is None:
            previous = conn.execute(
                "SELECT request_json FROM memory_consolidation_plans "
                "WHERE batch_id=? AND revision=?",
                (batch_id, revision),
            ).fetchone()
            if previous is not None:
                request_json = previous[0]
        conn.execute(
            "INSERT OR REPLACE INTO memory_consolidation_plans ("
            "batch_id, revision, plan_json, episode_summary, occurred_at, "
            "occurred_until, candidate_text, request_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                batch_id,
                revision,
                plan_json,
                None if plan is None else plan.episode_summary,
                None if plan is None else plan.occurred_at.isoformat(),
                None
                if plan is None or plan.occurred_until is None
                else plan.occurred_until.isoformat(),
                prepared.candidate_text,
                request_json,
            ),
        )

    def _plan_payload(self, plan):
        items = []
        for item in plan.semantic:
            if isinstance(item, CreateSemanticIntention):
                items.append(
                    {"action": "create", "subject": item.subject, "fact": item.fact}
                )
            elif isinstance(item, UpdateSemanticIntention):
                items.append(
                    {
                        "action": "update",
                        "id": item.id,
                        "subject": item.subject,
                        "fact": item.fact,
                        "expected_version": item.expected_version,
                    }
                )
            else:
                items.append(
                    {
                        "action": "keep",
                        "id": item.id,
                        "expected_version": item.expected_version,
                    }
                )
        return {
            "semantic": items,
            "episode_summary": plan.episode_summary,
            "occurred_at": plan.occurred_at.isoformat(),
            "occurred_until": None
            if plan.occurred_until is None
            else plan.occurred_until.isoformat(),
        }

    def _restore_prepared(self, conn, batch_id, revision):
        row = conn.execute(
            "SELECT session_id, generation_run_id FROM memory_consolidation_batches "
            "WHERE batch_id=? AND revision=?",
            (batch_id, revision),
        ).fetchone()
        if row is None:
            return _error("not_found")
        session_id = row[0]
        source_rows = conn.execute(
            "SELECT run_id, accepted_at, finished_at FROM "
            "memory_consolidation_sources WHERE batch_id=? AND revision=? "
            "ORDER BY ordinal",
            (batch_id, revision),
        ).fetchall()
        loaded = self._load_sources(conn, session_id)
        if "error" in loaded:
            return loaded
        by_id = {source.run_id: source for source in loaded["sources"]}
        # Processed sources are absent from eligible load; recover text from log.
        sources = []
        for run_id, accepted, finished in source_rows:
            if run_id in by_id:
                sources.append(by_id[run_id])
                continue
            restored = self._source_from_log(
                conn, session_id, run_id, accepted, finished
            )
            if restored is None:
                return self._invalidated_result(conn, batch_id, revision)
            sources.append(restored)
        from agent_alfred.model import ModelRef

        model = ModelRef("consolidation", "primary")
        start = min(source.accepted_at for source in sources)
        end = max(source.finished_at for source in sources)
        selected = SelectedConsolidationSources(
            session_id=session_id,
            sources=tuple(sources),
            search_query=" ".join(
                part
                for source in sources
                for part in (source.user_text, source.assistant_text)
            ),
            occurred_at=start,
            occurred_until=None if start == end else end,
            model=model,
            limits=self._limits,
        )
        read_rows = conn.execute(
            "SELECT memory_id, record_version, human_protected FROM "
            "memory_consolidation_reads WHERE batch_id=? AND revision=? "
            "AND in_request=1",
            (batch_id, revision),
        ).fetchall()
        semantic, _ = self._owner._stores(conn)
        records = []
        for memory_id, frozen_version, frozen_protected in read_rows:
            record = semantic.get(MemoryId(memory_id))
            if record is None or record.record_version != frozen_version:
                return self._invalidated_result(conn, batch_id, revision)
            if bool(record.human_protected) != bool(frozen_protected):
                record = replace(record, human_protected=bool(frozen_protected))
            records.append(record)
        bound = prepare_consolidation_request(
            selected,
            ConsolidationCandidateReads(search_hits=tuple(records)),
        )
        if not isinstance(bound, PreparedConsolidation):
            return self._invalidated_result(conn, batch_id, revision)
        if tuple(item.id for item in bound.candidates) != tuple(
            item.id for item in records
        ):
            return self._invalidated_result(conn, batch_id, revision)
        return {"prepared": bound}

    def _source_from_log(self, conn, session_id, run_id, accepted, finished):
        row = conn.execute(
            """SELECT user_log.content, assistant_log.content
               FROM agent_log AS user_log
               JOIN agent_log AS assistant_log
                 ON assistant_log.run_id = user_log.run_id
                AND assistant_log.role = 'assistant'
               WHERE user_log.run_id=? AND user_log.role='user'""",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            accepted_at = parse_instant(accepted)
            finished_at = parse_instant(finished)
        except (TypeError, ValueError):
            return None
        user_text, assistant_text = _log_text(row[0]), _log_text(row[1])
        if not user_text or not assistant_text:
            return None
        return ConsolidationSource(
            session_id=session_id,
            run_id=run_id,
            user_text=user_text,
            assistant_text=assistant_text,
            accepted_at=accepted_at,
            finished_at=finished_at,
        )

    def _restore_plan(self, conn, batch_id, revision, prepared):
        row = conn.execute(
            "SELECT plan_json FROM memory_consolidation_plans "
            "WHERE batch_id=? AND revision=?",
            (batch_id, revision),
        ).fetchone()
        if row is None or not row[0]:
            return _error("generation_required")
        payload = json.loads(row[0])
        raw = _json(
            {
                "semantic": [
                    {
                        key: value
                        for key, value in item.items()
                        if key != "expected_version"
                    }
                    for item in payload["semantic"]
                ],
                "episode_summary": payload["episode_summary"],
            }
        )
        try:
            plan = parse_consolidation_plan(raw, prepared)
        except ConsolidationPlanError as error:
            return _error(error.code)
        stored_versions = {
            item.get("id"): item.get("expected_version")
            for item in payload.get("semantic", ())
            if isinstance(item, dict) and "id" in item
        }
        for item in plan.semantic:
            expected = getattr(item, "expected_version", None)
            frozen = stored_versions.get(getattr(item, "id", None))
            if expected is not None and frozen is not None and frozen != expected:
                return self._invalidated_result(conn, batch_id, revision)
        return {"plan": plan}

    def _batch_view(self, conn, batch_id) -> dict | None:
        row = conn.execute(
            "SELECT batch_id, session_id, revision, status, created_at, "
            "updated_at, finished_at, error_code, generation_run_id, receipt "
            "FROM memory_consolidation_batches WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        if row is None:
            return None
        sources = [
            item[0]
            for item in conn.execute(
                "SELECT run_id FROM memory_consolidation_sources "
                "WHERE batch_id=? AND revision=? ORDER BY ordinal",
                (row[0], row[2]),
            )
        ]
        plan_row = conn.execute(
            "SELECT plan_json, episode_summary, request_json "
            "FROM memory_consolidation_plans "
            "WHERE batch_id=? AND revision=?",
            (row[0], row[2]),
        ).fetchone()
        view = {
            "batch_id": row[0],
            "session_id": row[1],
            "revision": row[2],
            "status": row[3],
            "created_at": row[4],
            "updated_at": row[5],
            "finished_at": row[6],
            "error_code": row[7],
            "generation_run_id": row[8],
            "source_run_ids": sources,
            "receipt": None if row[9] is None else json.loads(row[9]),
        }
        span = conn.execute(
            "SELECT min(accepted_at),max(finished_at) "
            "FROM memory_consolidation_sources "
            "WHERE batch_id=? AND revision=?",
            (row[0], row[2]),
        ).fetchone()
        view["source_range"] = {"start": span[0], "end": span[1]}
        run = conn.execute(
            "SELECT started_at,finished_at,outcome,telemetry FROM runs WHERE run_id=?",
            (row[8],),
        ).fetchone()
        view["run"] = (
            None
            if run is None
            else {
                "run_id": row[8],
                "started_at": run[0],
                "finished_at": run[1],
                "outcome": run[2],
            }
        )
        view["model"] = (
            json.loads(plan_row[2]).get("model")
            if plan_row is not None and plan_row[2] else None
        )
        view["model_usage"] = None
        if run and run[3]:
            telemetry = json.loads(run[3])
            view["model_usage"] = [
                {
                    "attempt_id": item.get("attempt_id"),
                    "model": item.get("model"),
                    "outcome": item.get("outcome"),
                    "usage": {
                        key: value
                        for key, value in item.get("usage", {}).items()
                        if key != "raw"
                    },
                }
                for item in telemetry.get("attempts", [])
            ]
        if view["model"] is None and view["model_usage"]:
            view["model"] = view["model_usage"][0]["model"]
        view["actions"] = (
            ["approve", "reject"]
            if row[3] == "awaiting_approval"
            else ["retry"]
            if row[3] == "failed"
            else []
        )
        safe = self._owner.forgetting.evaluate_history(
            tuple(sources), purpose="consolidation_source", connection=conn
        )
        refs = tuple(
            conn.execute(
                "SELECT kind,memory_id,record_version FROM "
                "memory_consolidation_reads WHERE batch_id=? "
                "AND revision=? AND in_request=1",
                (row[0], row[2]),
            )
        )
        valid = not safe.get("error") and not safe.get("denied")
        records = self._owner._stores(conn)
        for kind, memory_id, version in refs:
            record = records[0 if kind == "semantic" else 1].get(MemoryId(memory_id))
            valid = valid and record is not None and record.record_version == version
        allowed = self._owner.forgetting.evaluate_automatic_records(
            refs, connection=conn
        )
        valid = valid and not allowed.get("error") and not allowed.get("denied")
        view["candidate_available"] = bool(
            valid and plan_row and plan_row[0]
            and row[3] in ("awaiting_approval", "failed")
        )
        if view["candidate_available"]:
            view["plan"] = json.loads(plan_row[0])
            view["episode_summary"] = plan_row[1]
        return view

    def _invalidate(self, conn, batch_id, revision):
        now = self._now()
        conn.execute(
            "UPDATE memory_consolidation_batches SET status='invalidated', "
            "updated_at=?, finished_at=?, error_code='batch_invalidated' "
            "WHERE batch_id=? AND revision=? AND status IN ("
            "'queued','running','awaiting_approval','failed')",
            (now, now, batch_id, revision),
        )
        self._clear_bodies(conn, batch_id, revision)

    def _clear_obsolete_bodies(self, conn):
        """Retire persisted historical copies, including pre-upgrade batches."""
        rows = conn.execute(
            "SELECT DISTINCT b.batch_id,b.revision,b.status "
            "FROM memory_consolidation_batches b "
            "JOIN memory_consolidation_plans p ON p.batch_id=b.batch_id "
            "WHERE (p.revision<b.revision "
            "OR b.status IN ('succeeded','rejected','invalidated')) "
            "AND (p.plan_json IS NOT NULL OR p.episode_summary IS NOT NULL "
            "OR p.candidate_text IS NOT NULL OR p.request_json IS NOT NULL "
            "OR p.occurred_at IS NOT NULL OR p.occurred_until IS NOT NULL)"
        ).fetchall()
        for batch_id, revision, status in rows:
            last = (
                revision if status in ("succeeded", "rejected", "invalidated")
                else revision - 1
            )
            self._clear_bodies(conn, batch_id, last)
        return bool(rows)

    def _clear_bodies(self, conn, batch_id, revision):
        conn.execute(
            "UPDATE memory_consolidation_plans SET plan_json=NULL, "
            "episode_summary=NULL, candidate_text=NULL, occurred_at=NULL, "
            "occurred_until=NULL, request_json=NULL WHERE batch_id=? AND revision<=?",
            (batch_id, revision),
        )
        rows = conn.execute(
            "SELECT operation_id, receipt FROM memory_consolidation_actions"
        ).fetchall()
        for operation_id, receipt in rows:
            try:
                payload = json.loads(receipt)
            except ValueError:
                continue
            if not isinstance(payload, dict) or payload.get("batch_id") != batch_id:
                continue
            durable = self._durable_action_receipt(
                {"action": payload.get("action")}, payload
            )
            if durable != payload:
                conn.execute(
                    "UPDATE memory_consolidation_actions SET receipt=? "
                    "WHERE operation_id=?",
                    (_json(durable), operation_id),
                )

    def _now(self) -> str:
        return self._owner._clock().isoformat()
