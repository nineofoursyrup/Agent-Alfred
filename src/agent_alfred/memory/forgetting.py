"""Durable source restrictions, shared by every automatic history consumer.

Registration accepts trusted structured business evidence, never model/form claims.
Transaction helpers never commit: the shared command service owns the boundary.
"""

from __future__ import annotations

import hmac
import json
import sqlite3
import threading
from functools import wraps

from agent_alfred.memory.forget_graph import (
    add_scopes,
    bump,
    carry_memory_provenance,
    ensure_group,
    insert_scope,
    operation_state,
    propagate,
    record_memory_sources,
    record_observations,
    refresh_pauses,
    revision,
    update_members,
)
from agent_alfred.resource_rollback import (
    dominant_error,
    raise_if_rollback_pending,
    reraise_failure,
)
from agent_alfred.runtime.recording import RecordingUnavailable

PURPOSES = frozenset(
    ("working_window", "gate_history", "tool_summary", "consolidation_source")
)


def safe_read(method):
    @wraps(method)
    def read(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except (sqlite3.Error, RecordingUnavailable, ValueError) as error:
            raise_if_rollback_pending(error)
            return {"error": {"code": "storage_read_failed"}}

    return read


class ForgettingService:
    """Public metadata ports; production consumers retain their own body stores."""

    def __init__(self, owner):
        self._owner = owner
        self._db = owner._db

    def invalidate(self, conn, operation_id, *, memories=(), isolated=None):
        """Participant work and durable file intentions share the caller transaction."""
        if isolated is None:
            isolated = tuple(
                g
                for (g,) in conn.execute(
                    (
                        "SELECT group_id FROM forget_limits WHERE operation_id=? AND "
                        "mode='isolated'"
                    ),
                    (operation_id,),
                )
            )
        conn.executemany(
            "UPDATE tool_ledger SET summary=NULL WHERE run_id=?",
            [(g,) for g in isolated],
        )
        targets = set()
        for participant in self._owner._projection_participants:
            targets.update(
                participant.invalidate(conn, memories=memories, isolated=isolated)
            )
        self._invalidate_targets(conn, operation_id, targets)

    def _invalidate_targets(self, conn, operation_id, targets):
        for target in sorted(targets):
            generation = conn.execute(
                """INSERT INTO forget_targets VALUES (?,1,0)
                ON CONFLICT(target_id) DO UPDATE SET revision=revision+1
                RETURNING revision""",
                (target,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO forget_cleanup VALUES (?,?,?,'pending',NULL)
                ON CONFLICT(operation_id,target_id) DO UPDATE SET
                revision=excluded.revision,state='pending',error=NULL""",
                (operation_id, target, generation),
            )
            # One output generation covers every older deletion of that output.
            conn.execute(
                (
                    "UPDATE forget_cleanup SET "
                    "revision=?,state='pending',error=NULL WHERE target_id=?"
                ),
                (generation, target),
            )

    def _projection_fenced(self, conn, operation_id):
        return (
            conn.execute(
                "SELECT 1 FROM forget_projection_fences WHERE operation_id=? "
                "UNION SELECT 1 FROM forget_projection_unknown WHERE operation_id=?",
                (operation_id, operation_id),
            ).fetchone()
            is not None
        )

    def _prepare_projection_recovery(self, operation_id=None, attempted=None):
        # Keep the deferred graph in the caller until every inner propagation
        # edge has completed, including entry into an exception helper.
        held_failure = [None]
        try:
            return self._prepare_projection_recovery_attempt(
                operation_id, attempted, held_failure
            )
        except BaseException as error:
            failure = held_failure[0]
            if failure is None or error is failure:
                raise
            primary = dominant_error(failure, error)
            reraise_failure(primary, earlier=failure if primary is error else error)

    def _prepare_projection_recovery_attempt(
        self, operation_id, attempted, held_failure
    ):
        """Commit body-free safety obligations before fallible projection work.

        These obligations belong to an already committed deletion, not to its
        immutable receipt or to the later, atomic projection effects transaction.
        """
        scope_failure = None
        failed_operation = None
        try:
            with self._db.transaction() as conn:
                conn.execute("BEGIN IMMEDIATE")
                operations = conn.execute(
                    "SELECT operation_id,kind,memory_id FROM forget_operations "
                    "WHERE ? IS NULL "
                    "OR operation_id=?",
                    (operation_id, operation_id),
                ).fetchall()
                known_targets = {
                    t for (t,) in conn.execute("SELECT target_id FROM forget_targets")
                }
                changed = False
                plans = {}
                incomplete = set()
                for operation, kind, memory_id in operations:
                    if operation_id is not None and not self._projection_fenced(
                        conn, operation
                    ):
                        pending = conn.execute(
                            "SELECT 1 FROM forget_projection_recovery WHERE "
                            "operation_id=? AND evidence_id IS NULL",
                            (operation,),
                        ).fetchone()
                        if pending is None:
                            continue
                    isolated = tuple(
                        g
                        for (g,) in conn.execute(
                            "SELECT group_id FROM forget_limits WHERE operation_id=? "
                            "AND mode='isolated'",
                            (operation,),
                        )
                    )
                    targets = set()
                    for participant in self._owner._projection_participants:
                        if scope_failure is not None:
                            # Do not acquire more resources after declaration failure.
                            incomplete.add(operation)
                            continue
                        try:
                            scope = getattr(participant, "reconciliation_targets", None)
                            # Only a complete trusted declaration narrows the fence.
                            if scope is None:
                                incomplete.add(operation)
                            targets.update(
                                ()
                                if scope is None
                                else scope(
                                    conn,
                                    memories=((kind, memory_id),),
                                    isolated=isolated,
                                )
                            )
                        except BaseException as error:
                            incomplete.add(operation)
                            if scope_failure is None or (
                                isinstance(scope_failure, Exception)
                                and not isinstance(error, Exception)
                            ):
                                scope_failure, failed_operation = error, operation
                                held_failure[0] = scope_failure
                                if attempted is not None:
                                    attempted[0] = failed_operation
                    plans[operation] = targets
                    known_targets.update(targets)
                # A failed/missing declaration covers the entire round, including
                # shared outputs first discovered by later operations.
                for operation, targets in plans.items():
                    if operation in incomplete:
                        targets.update(known_targets)
                        changed |= bool(
                            conn.execute(
                                "INSERT OR IGNORE INTO forget_projection_unknown "
                                "VALUES (?)",
                                (operation,),
                            ).rowcount
                        )
                    newly_fenced = []
                    for target in targets:
                        if conn.execute(
                            "INSERT OR IGNORE INTO forget_projection_fences "
                            "VALUES (?,?)",
                            (operation, target),
                        ).rowcount:
                            newly_fenced.append(target)
                    if newly_fenced:
                        self._invalidate_targets(conn, operation, newly_fenced)
                        changed = True
                if changed:
                    bump(conn)
                    read_revision = self.prepare_commit(conn)
                conn.commit()
            if changed:
                self._owner._notify_memory(read_revision)
        except BaseException as error:
            primary = dominant_error(scope_failure, error)
            reraise_failure(
                primary, earlier=scope_failure if primary is error else error
            )
        if scope_failure is not None:
            reraise_failure(scope_failure)

    def _restore_projection(self, operation_id):
        try:
            self._prepare_projection_recovery(operation_id)
            with self._db.transaction() as conn:
                conn.execute("BEGIN IMMEDIATE")
                recovery = conn.execute(
                    "SELECT kind,memory_id FROM forget_operations "
                    "LEFT JOIN forget_projection_recovery USING(operation_id) "
                    "WHERE operation_id=? AND (evidence_id IS NULL AND "
                    "forget_projection_recovery.operation_id IS NOT NULL OR EXISTS "
                    "(SELECT 1 FROM forget_projection_fences f WHERE "
                    "f.operation_id=forget_operations.operation_id) OR EXISTS "
                    "(SELECT 1 FROM forget_projection_unknown u WHERE "
                    "u.operation_id=forget_operations.operation_id))",
                    (operation_id,),
                ).fetchone()
                if recovery is not None:
                    if not self._owner._projection_inventory_evidence_id:
                        return {"error": {"code": "projection_inventory_required"}}
                    self.invalidate(conn, operation_id, memories=(tuple(recovery),))
                    conn.execute(
                        "UPDATE forget_projection_recovery SET "
                        "evidence_id=?,error=NULL,failed_at=NULL "
                        "WHERE operation_id=?",
                        (self._owner._projection_inventory_evidence_id, operation_id),
                    )
                    conn.execute(
                        "DELETE FROM forget_projection_fences WHERE operation_id=?",
                        (operation_id,),
                    )
                    conn.execute(
                        "DELETE FROM forget_projection_unknown WHERE operation_id=?",
                        (operation_id,),
                    )
                    bump(conn)
                    self.prepare_commit(conn)
                read_revision = conn.execute(
                    "SELECT revision FROM memory_revision WHERE singleton=1"
                ).fetchone()[0]
                conn.commit()
            if recovery is not None:
                self._owner._notify_memory(read_revision)
        except Exception as error:
            # Record this execution failure before exposing unfinished resource
            # recovery; outer input validation must not reinterpret its phase.
            return self._projection_failure(operation_id, error)
        return None

    def _projection_failure(self, operation_id, error):
        try:
            result = self._record_projection_failure(operation_id)
        except BaseException as secondary:
            primary = dominant_error(error, secondary)
            reraise_failure(
                primary, earlier=error if primary is secondary else secondary
            )
        raise_if_rollback_pending(error)
        return result

    def _record_projection_failure(self, operation_id):
        """Record only after the failed projection transaction has rolled back."""
        result = {"error": {"code": "storage_write_failed", "failure_recorded": False}}
        if operation_id is None or self._db.transaction_in_progress:
            return result
        try:
            with self._db.transaction() as conn:
                conn.execute("BEGIN IMMEDIATE")
                changed = conn.execute(
                    "INSERT INTO forget_projection_recovery "
                    "SELECT operation_id,NULL,'projection_invalidation_failed',? "
                    "FROM forget_operations WHERE operation_id=? "
                    "ON CONFLICT(operation_id) DO UPDATE SET evidence_id=NULL,"
                    "error=excluded.error,failed_at=excluded.failed_at",
                    (self._owner._clock().isoformat(), operation_id),
                ).rowcount
                if not changed:
                    return result
                bump(conn)
                read_revision = self.prepare_commit(conn)
                conn.commit()
            result["error"]["failure_recorded"] = True
            self._owner._notify_memory(read_revision)
        except (sqlite3.Error, RecordingUnavailable) as error:
            raise_if_rollback_pending(error)
            # The database may itself be unwritable/poisoned. Do not fabricate
            # the independent failure transaction or change the original receipt.
            pass
        return result

    def retry_cleanup(self, operation_id, context):
        """Resume unfinished items; file verification precedes any repeat effect.

        The mutation lease spans IO, the SQLite lock does not. A process-control
        interruption leaves a durable running item for the same recovery path.
        """

        return self._owner._run_mutation(
            lambda: self._retry_cleanup_admitted(operation_id), context
        )

    def _retry_cleanup_admitted(self, operation_id):
        """Internal continuation; caller already owns mutation admission."""
        restored = self._restore_projection(operation_id)
        if restored is not None:
            return restored
        with self._db.reading() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM forget_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                is None
            ):
                return {"error": {"code": "not_found"}}
            items = conn.execute(
                (
                    "SELECT target_id,revision FROM forget_cleanup WHERE "
                    "operation_id=? AND state!='complete' AND NOT EXISTS "
                    "(SELECT 1 FROM forget_projection_fences f WHERE "
                    "f.target_id=forget_cleanup.target_id) AND NOT EXISTS "
                    "(SELECT 1 FROM forget_projection_unknown) ORDER BY target_id"
                ),
                (operation_id,),
            ).fetchall()
        for target, generation in items:
            with self._db.transaction() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    (
                        "UPDATE forget_cleanup SET state='running',err"
                        "or=NULL WHERE "
                        "target_id=? AND revision=? AND state!='complete'"
                    ),
                    (target, generation),
                )
                bump(conn)
                read_revision = conn.execute(
                    "SELECT revision FROM memory_revision WHERE singleton=1"
                ).fetchone()[0]
                record_observations(conn, self._owner._clock().isoformat())
                conn.commit()
            self._owner._notify_memory(read_revision)
            try:
                port = self._owner._cleanup_port
                if port is None:
                    raise ValueError("cleanup_port_unavailable")
                if not port.verify(target, generation):
                    port.rebuild(target, generation)
                if not port.verify(target, generation):
                    raise ValueError("cleanup_not_verified")
            except Exception as failure:
                try:
                    self._record_file_cleanup(
                        target, generation, "cleanup_unconfirmed"
                    )
                except BaseException as secondary:
                    if dominant_error(failure, secondary) is failure:
                        raise_if_rollback_pending(failure)
                    raise
                raise_if_rollback_pending(failure)
            else:
                self._record_file_cleanup(target, generation, None)
        return self.get_forgetting(operation_id)

    def _record_file_cleanup(self, target, generation, error):
        """Publish a verified result while the originating failure stays in scope."""
        with self._db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                (
                    "UPDATE forget_cleanup SET state=?,error=? WHE"
                    "RE target_id=? "
                    "AND revision=?"
                ),
                (
                    "failed" if error else "complete",
                    error,
                    target,
                    generation,
                ),
            )
            if error is None:
                conn.execute(
                    (
                        "UPDATE forget_targets SET verified_revision=? "
                        "WHERE "
                        "target_id=? AND revision=?"
                    ),
                    (generation, target, generation),
                )
            bump(conn)
            read_revision = conn.execute(
                "SELECT revision FROM memory_revision WHERE singleton=1"
            ).fetchone()[0]
            record_observations(conn, self._owner._clock().isoformat())
            conn.commit()
        self._owner._notify_memory(read_revision)

    def reconcile_projections(self, context):
        """Register a newly discovered managed projection before publishing it."""

        attempted = [None]

        def write(conn):
            pending = conn.execute(
                "SELECT 1 FROM forget_projection_recovery "
                "WHERE evidence_id IS NULL "
                "UNION SELECT 1 FROM forget_projection_fences "
                "UNION SELECT 1 FROM forget_projection_unknown LIMIT 1"
            ).fetchone()
            if pending and not self._owner._projection_inventory_evidence_id:
                return {"error": {"code": "projection_inventory_required"}}
            for operation, kind, memory_id in conn.execute(
                "SELECT operation_id,kind,memory_id FROM forget_operations"
            ).fetchall():
                attempted[0] = operation
                self.invalidate(conn, operation, memories=((kind, memory_id),))
                conn.execute(
                    "UPDATE forget_projection_recovery SET "
                        "evidence_id=?,error=NULL,failed_at=NULL "
                    "WHERE operation_id=? AND evidence_id IS NULL",
                    (self._owner._projection_inventory_evidence_id, operation),
                )
            conn.execute("DELETE FROM forget_projection_fences")
            conn.execute("DELETE FROM forget_projection_unknown")
            bump(conn)
            return {"status": "reconciled"}

        return self._write(
            write,
            context,
            on_failure=lambda error: self._projection_failure(attempted[0], error),
            prepare=lambda: self._prepare_projection_recovery(attempted=attempted),
        )

    def managed_target_readable(self, target_id):
        with self._db.reading() as conn:
            row = conn.execute(
                (
                    "SELECT revision,verified_revision FROM forget_targets WHERE "
                    "target_id=?"
                ),
                (target_id,),
            ).fetchone()
            fenced = conn.execute(
                "SELECT 1 FROM forget_projection_fences WHERE target_id=? "
                "UNION SELECT 1 FROM forget_projection_unknown",
                (target_id,),
            ).fetchone()
            return row is not None and row[0] == row[1] and fenced is None

    def _participate(self, conn, fn):
        before = set(
            conn.execute(
                "SELECT operation_id,group_id FROM forget_limits WHERE mode='isolated'"
            )
        )
        value = fn(conn)
        if "error" in value:
            return value
        for (op,) in conn.execute(
            "SELECT operation_id FROM forget_operations"
        ).fetchall():
            unknown = [
                g
                for (g,) in conn.execute(
                    "SELECT group_id FROM history_groups WHERE evidence='unknown' "
                    "AND group_id NOT IN (SELECT group_id FROM forget_limits "
                    "WHERE operation_id=?)",
                    (op,),
                )
            ]
            add_scopes(conn, op, unknown)
        after = set(
            conn.execute(
                "SELECT operation_id,group_id FROM forget_limits WHERE mode='isolated'"
            )
        )
        for operation in {op for op, _ in after - before}:
            groups = tuple(g for op, g in after - before if op == operation)
            self.invalidate(conn, operation, isolated=groups)
        return value

    def prepare_commit(self, transaction):
        """Borrowing owners call once after their last effect, before commit.

        This records the final state of the transaction, not intermediate nested
        participant states; the caller still owns commit and notification.
        """
        self._db.validate_borrowed_transaction(transaction)
        record_observations(transaction, self._owner._clock().isoformat())
        return transaction.execute(
            "SELECT revision FROM memory_revision WHERE singleton=1"
        ).fetchone()[0]

    def notify_committed(self, revision):
        """Notify readers after a borrowing owner has committed its transaction."""
        self._owner._notify_memory(revision)

    def _write(
        self, fn, context, *, transaction=None, on_failure=None, prepare=None
    ):
        if transaction is not None:
            # Internal port: caller owns admission, connection lock, commit,
            # rollback and post-commit notification. Exceptions must reach it.
            if self._owner._admission is not None:
                own_executor = self._owner._mutation_thread == threading.get_ident()
                inherited = self._owner._admission.validate_memory_permission(
                    context.permission, context.run_id
                )
                if not own_executor and not inherited:
                    return {"error": {"code": "busy"}}
            self._db.validate_borrowed_transaction(transaction)
            return self._participate(transaction, fn)

        def execute_transaction():
            if self._db.transaction_in_progress:
                return {"error": {"code": "transaction_required"}}
            if prepare is not None:
                prepare()
            with self._db.transaction() as conn:
                conn.execute("BEGIN IMMEDIATE")
                old_revision = revision(conn)
                value = self._participate(conn, fn)
                if "error" in value:
                    return value
                self.prepare_commit(conn)
                changed = old_revision != revision(conn)
                read_revision = conn.execute(
                    "SELECT revision FROM memory_revision WHERE singleton=1"
                ).fetchone()[0]
                conn.commit()
            if changed:
                self._owner._notify_memory(read_revision)
            return value

        def execute():
            try:
                return execute_transaction()
            except Exception as error:
                if on_failure is not None:
                    return on_failure(error)
                raise_if_rollback_pending(error)
                raise

        return self._owner._run_mutation(execute, context)

    def register_group(
        self,
        group_id,
        *,
        kind,
        container_id,
        evidence="unknown",
        evidence_id=None,
        occurred_at=None,
        context,
        transaction=None,
    ):
        if (
            not isinstance(group_id, str)
            or not group_id
            or kind not in ("run", "batch", "legacy")
            or evidence not in ("unknown", "complete")
            or (evidence == "complete" and not evidence_id)
        ):
            return {"error": {"code": "invalid_input"}}

        def write(conn):
            old = conn.execute(
                "SELECT kind,container_id FROM history_groups WHERE group_id=?",
                (group_id,),
            ).fetchone()
            if (
                old is not None
                and old[0] != "unresolved"
                and old != (kind, container_id)
            ):
                return {"error": {"code": "invalid_input"}}
            conn.execute(
                """INSERT INTO history_groups VALUES (?,?,?,?,?)
                ON CONFLICT(group_id) DO UPDATE SET kind=excluded.kind,
                container_id=excluded.container_id,evidence=excluded.evidence,
                evidence_id=excluded.evidence_id""",
                (group_id, kind, container_id, evidence, evidence_id),
            )
            if old is not None and old[0] == "unresolved":
                # A regrouped session/batch can also contain unresolved members.
                # Its membership is unchanged, but its confirmation evidence is not.
                for scope, encoded, prior_evidence in conn.execute(
                    "SELECT scope_id,members,evidence_id FROM forget_scopes "
                    "WHERE state='pending' AND kind!='unresolved'"
                ).fetchall():
                    members = json.loads(encoded)
                    if group_id in members:
                        update_members(conn, scope, members, prior_evidence)
            if occurred_at is not None:
                from agent_alfred.schema import parse_instant

                parse_instant(occurred_at)
                conn.execute(
                    "INSERT INTO history_group_times VALUES (?,?) "
                    "ON CONFLICT(group_id) DO UPDATE SET "
                    "occurred_at=excluded.occurred_at",
                    (group_id, occurred_at),
                )
            if evidence == "unknown":
                for (operation,) in conn.execute(
                    "SELECT operation_id FROM forget_operations"
                ).fetchall():
                    isolated = conn.execute(
                        (
                            "SELECT 1 FROM forget_limits WHERE operation_id=? AND "
                            "group_id=? AND mode='isolated'"
                        ),
                        (operation, group_id),
                    ).fetchone()
                    if not isolated:
                        add_scopes(conn, operation, [group_id])
            else:
                for scope_id, members in conn.execute(
                    "SELECT scope_id,members FROM forget_scopes WHERE state='pending'"
                ).fetchall():
                    members = json.loads(members)
                    if group_id in members:
                        members.remove(group_id)
                        update_members(conn, scope_id, members, evidence_id)
                refresh_pauses(conn)
            bump(conn)
            return {"group_id": group_id}

        return self._write(write, context, transaction=transaction)

    def register_sources(
        self,
        kind,
        memory_id,
        version,
        *,
        groups,
        evidence_id,
        context,
        transaction=None,
    ):
        if (
            kind not in ("semantic", "episodic")
            or not memory_id
            or type(version) is not int
            or version < 1
            or not groups
            or not evidence_id
        ):
            return {"error": {"code": "invalid_input"}}

        def write(conn):
            record_memory_sources(conn, kind, memory_id, version, groups)
            conn.execute(
                "INSERT OR IGNORE INTO memory_source_evidence VALUES (?,?,?,?,?)",
                (
                    kind,
                    memory_id,
                    version,
                    evidence_id,
                    json.dumps(sorted(set(groups))),
                ),
            )
            # Adding positive edges does not certify that every historical edge
            # has been found. Only identified members leave pending scopes;
            # other unknown obligations remain pending.
            bump(conn)
            return {"status": "registered"}

        return self._write(write, context, transaction=transaction)

    def record_input_preparation(self, run_id, explanation, *, failed=False, context):
        """Capacity explanation only: this does not register a model Attempt."""
        kind = "failure" if failed else "preparation"

        def write(conn):
            conn.execute(
                "INSERT INTO run_input_explanations "
                "(run_id,identity,kind,explanation) VALUES (?,?,?,?) "
                "ON CONFLICT(run_id,identity) DO UPDATE SET "
                "explanation=excluded.explanation",
                (run_id, kind, kind, json.dumps(explanation, ensure_ascii=False)),
            )
            return {"status": "recorded"}

        return self._write(write, context)

    def register_read(
        self,
        consumer,
        *,
        sources=(),
        memories=(),
        attempt_id,
        purpose,
        context,
        transaction=None,
        input_explanation=None,
        provisional=False,
    ):
        if (
            not consumer
            or not attempt_id
            or purpose not in ("gate", "answer", "consolidation", "skill_selector")
            or (provisional and input_explanation is None)
        ):
            return {"error": {"code": "invalid_input"}}

        sources, memories = tuple(sources), tuple(memories)

        def write(conn):
            ensure_group(conn, consumer)
            for source in (() if provisional else sources):
                ensure_group(conn, source)
                conn.execute(
                    "INSERT OR IGNORE INTO history_reads VALUES (?,?,?,?)",
                    (source, consumer, attempt_id, purpose),
                )
            for kind, memory_id, version in (() if provisional else memories):
                conn.execute(
                    "INSERT OR IGNORE INTO memory_uses VALUES (?,?,?,?,?,?)",
                    (kind, memory_id, version, consumer, attempt_id, purpose),
                )
                if not provisional:
                    carry_memory_provenance(
                        conn,
                        consumer,
                        attempt_id,
                        purpose,
                        ((kind, memory_id, version),),
                    )
                    conn.execute(
                        (
                            "INSERT INTO forget_limits\n                    SELECT "
                            "operation_id,?,'isolated' FROM forget_operations "
                            "WHERE kind=? "
                            "AND memory_id=?\n                    ON "
                            "CONFLICT(operation_id,group_id) DO UPDATE "
                            "SET mode='isolated'"
                        ),
                        (consumer, kind, memory_id),
                    )
            if input_explanation is not None:
                explanation = dict(input_explanation)
                if provisional:
                    explanation["dispatch_state"] = "unconfirmed"
                    # A reservation is recoverable provenance, not an actual-use
                    # edge. Deletion closure consumes only confirmed graph rows.
                    explanation["pending_sources"] = sources
                    explanation["pending_memories"] = memories
                conn.execute(
                    "INSERT INTO run_input_explanations "
                    "(run_id,identity,kind,explanation) VALUES (?,?,'attempt',?)",
                    (consumer, attempt_id, json.dumps(explanation,
                                                    ensure_ascii=False)),
                )
            if not provisional:
                propagate(conn)
            bump(conn)
            return {"consumer": consumer}

        return self._write(write, context, transaction=transaction)

    def _resolve_input(self, conn, consumer, attempt_id, sent):
        row = conn.execute(
            "SELECT explanation FROM run_input_explanations "
            "WHERE run_id=? AND identity=? AND kind='attempt'",
            (consumer, attempt_id),
        ).fetchone()
        if row is None:
            return {"status": "absent"}
        explanation = json.loads(row[0])
        state = explanation.get("dispatch_state")
        resolved = "sent" if sent else "not_sent"
        if state != "unconfirmed":
            if state not in (None, resolved):
                raise ValueError("conflicting_input_receipt")
            return {"status": resolved, "explanation": explanation}
        if sent:
            for source in explanation.get("pending_sources", ()):
                ensure_group(conn, source)
                conn.execute("INSERT OR IGNORE INTO history_reads VALUES (?,?,?,?)",
                             (source, consumer, attempt_id, explanation["purpose"]))
            for kind, memory_id, version in explanation.get("pending_memories", ()):
                conn.execute("INSERT OR IGNORE INTO memory_uses VALUES (?,?,?,?,?,?)",
                             (kind, memory_id, version, consumer, attempt_id,
                              explanation["purpose"]))
            carry_memory_provenance(
                conn,
                consumer,
                attempt_id,
                explanation["purpose"],
                tuple(explanation.get("pending_memories") or ()),
            )
            conn.execute(
                "INSERT INTO forget_limits "
                "SELECT o.operation_id,?,'isolated' FROM forget_operations o "
                "JOIN memory_uses u ON u.kind=o.kind AND u.memory_id=o.memory_id "
                "WHERE u.consumer=? AND u.attempt_id=? "
                "ON CONFLICT(operation_id,group_id) DO UPDATE SET mode='isolated'",
                (consumer, consumer, attempt_id),
            )
            propagate(conn)
        else:
            # These keys belong solely to the unsent Attempt. Earlier real reads
            # and any independent isolation restrictions retain their ownership.
            conn.execute("DELETE FROM history_reads WHERE consumer=? AND attempt_id=?",
                         (consumer, attempt_id))
            conn.execute("DELETE FROM memory_uses WHERE consumer=? AND attempt_id=?",
                         (consumer, attempt_id))
        explanation["dispatch_state"] = resolved
        explanation.pop("pending_sources", None)
        explanation.pop("pending_memories", None)
        conn.execute(
            "UPDATE run_input_explanations SET explanation=? "
            "WHERE run_id=? AND identity=?",
            (json.dumps(explanation, ensure_ascii=False), consumer, attempt_id),
        )
        bump(conn)
        return {"status": resolved, "explanation": explanation}

    def resolve_input_registration(self, consumer, attempt_id, *, sent, context):
        """Resolve a provisional registration from the host's transport receipt."""
        return self._write(
            lambda conn: self._resolve_input(conn, consumer, attempt_id, sent), context
        )

    def recover_input_registrations(self):
        """Reconcile only durable positive receipts; a crash gap stays unknown."""
        if self._db.transaction_in_progress:
            raise RuntimeError("caller_transaction_active")
        with self._db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT i.run_id,i.identity,r.telemetry FROM run_input_explanations i "
                "JOIN runs r ON r.run_id=i.run_id WHERE i.kind='attempt' "
                "AND json_extract(i.explanation,'$.dispatch_state')='unconfirmed'"
            ).fetchall()
            changed = False
            for consumer, identity, encoded in rows:
                telemetry = json.loads(encoded) if encoded else {}
                actual = {a["attempt_id"] for a in telemetry.get("attempts", [])}
                unsent = telemetry.get("memory", {}).get("input_not_sent_attempts", [])
                if identity not in actual and identity not in unsent:
                    continue
                self._participate(
                    conn,
                    lambda conn: self._resolve_input(
                        conn, consumer, identity, identity in actual
                    ),
                )
                changed = True
            revision = self.prepare_commit(conn) if changed else None
            conn.commit()
        if revision is not None:
            self.notify_committed(revision)

    @safe_read
    def get_forgetting(self, operation_id):
        with self._db.reading() as conn:
            row = conn.execute(
                (
                    "SELECT kind,memory_id,completeness FROM forget_operations "
                    "WHERE operation_id=?"
                ),
                (operation_id,),
            ).fetchone()
            if row is None:
                return None
            limits = conn.execute(
                (
                    "SELECT group_id,mode FROM forget_limits WHERE operation_id=? "
                    "ORDER BY group_id"
                ),
                (operation_id,),
            ).fetchall()
            pending = conn.execute(
                (
                    "SELECT count(*) FROM forget_scopes WHERE operation_id=? AND "
                    "state='pending'"
                ),
                (operation_id,),
            ).fetchone()[0]
            cleanup = [
                {"target_id": t, "revision": v, "state": st, "error": e}
                for t, v, st, e in conn.execute(
                    (
                        "SELECT target_id,revision,state,error FROM forget_cleanup "
                        "WHERE operation_id=? ORDER BY target_id"
                    ),
                    (operation_id,),
                )
            ]
            state = operation_state(conn, operation_id)
            observations = [
                {"progress_revision": r, "observed_at": at, "state": st}
                for r, at, st in conn.execute(
                    "SELECT progress_revision,observed_at,state "
                    "FROM forget_observations WHERE operation_id=? "
                    "ORDER BY progress_revision",
                    (operation_id,),
                )
            ]
            recovery = conn.execute(
                "SELECT evidence_id,error,failed_at FROM forget_projection_recovery "
                "WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            return {
                "operation_id": operation_id,
                "kind": row[0],
                "memory_id": row[1],
                "completeness": row[2],
                "state": state,
                "unresolved_scopes": pending,
                "projection_recovery_error": recovery[1] if recovery else None,
                "projection_recovery_failed_at": recovery[2] if recovery else None,
                "projection_recovery_pending": (recovery is not None
                and recovery[0] is None) or self._projection_fenced(conn, operation_id),
                "observations": observations,
                "cleanup": cleanup,
                "progress_revision": revision(conn),
                "limits": [{"group_id": g, "mode": m} for g, m in limits],
            }

    def offer_scope(
        self, operation_id, *, kind, container_id, groups, evidence_id, context
    ):
        """Trusted evidence adapter regroups existing obligations for display.

        Confirmation never accepts members. This metadata port can only move
        already-paused members, and invalidates every changed old snapshot.
        """
        if (
            kind not in ("session", "batch")
            or not container_id
            or not groups
            or not evidence_id
        ):
            return {"error": {"code": "invalid_input"}}

        def write(conn):
            pending = conn.execute(
                (
                    "SELECT scope_id,members FROM forget_scopes WHERE "
                    "operation_id=? AND state='pending'"
                ),
                (operation_id,),
            ).fetchall()
            selected = set(groups)
            available = {g for _, members in pending for g in json.loads(members)}
            if not selected <= available:
                return {"error": {"code": "invalid_input"}}
            for scope, encoded in pending:
                members = json.loads(encoded)
                rest = [g for g in members if g not in selected]
                if rest != members:
                    update_members(conn, scope, rest, evidence_id)
            scope_id = insert_scope(
                conn, operation_id, kind, container_id, selected, evidence_id
            )
            refresh_pauses(conn)
            bump(conn)
            return {"status": "offered", "scope_id": scope_id}

        return self._write(write, context)

    @safe_read
    def list_scopes(self, operation_id):
        with self._db.reading() as conn:
            return [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT v.snapshot FROM forget_scopes s "
                    "JOIN forget_scope_versions v USING(scope_id,revision) "
                    "WHERE s.operation_id=? ORDER BY s.container_id,s.scope_id",
                    (operation_id,),
                )
            ]

    @safe_read
    def get_scope(self, scope_id, scope_revision):
        with self._db.reading() as conn:
            row = conn.execute(
                "SELECT snapshot FROM forget_scope_versions "
                "WHERE scope_id=? AND revision=?",
                (scope_id, scope_revision),
            ).fetchone()
            return None if row is None else json.loads(row[0])

    @safe_read
    def get_action(self, action_id):
        with self._db.reading() as conn:
            row = conn.execute(
                "SELECT receipt FROM forget_actions WHERE action_id=?", (action_id,)
            ).fetchone()
            return None if row is None else json.loads(row[0])

    def resolve_scope(self, action, context):
        from agent_alfred.memory.commands import _json
        from agent_alfred.memory.types import ManualOrigin, origin_json

        try:
            action = json.loads(_json(action))
            if not isinstance(context.origin, ManualOrigin) or set(action) != {
                "operation_id",
                "action_id",
                "scopes",
            }:
                raise ValueError
            if (
                not all(
                    isinstance(action[k], str) and action[k]
                    for k in ("operation_id", "action_id")
                )
                or not isinstance(action["scopes"], list)
                or not action["scopes"]
            ):
                raise ValueError
            scopes = action["scopes"]
            for scope in scopes:
                if (
                    set(scope) != {"scope_id", "expected_revision"}
                    or not isinstance(scope["scope_id"], str)
                    or type(scope["expected_revision"]) is not int
                ):
                    raise ValueError
            if len({s["scope_id"] for s in scopes}) != len(scopes):
                raise ValueError
            canonical = _json(
                {
                    "v": 1,
                    "action": action,
                    "origin": origin_json(context.origin),
                    "source": context.source,
                }
            ).encode()
        except (ValueError, TypeError, KeyError) as error:
            raise_if_rollback_pending(error)
            return {"error": {"code": "invalid_input"}}

        def write(conn):
            fingerprint, key_id = self._owner._key.fingerprint(canonical)
            old = conn.execute(
                (
                    "SELECT fingerprint,key_id,receipt FROM forget_actions WHERE "
                    "action_id=?"
                ),
                (action["action_id"],),
            ).fetchone()
            if old:
                if old[1] != key_id:
                    return {"error": {"code": "operation_unverifiable"}}
                if not hmac.compare_digest(old[0], fingerprint):
                    return {"error": {"code": "operation_mismatch"}}
                return json.loads(old[2])
            frozen = []
            for scope in scopes:
                row = conn.execute(
                    (
                        "SELECT operation_id,revision,state,members FROM forget_scopes "
                        "WHERE scope_id=?"
                    ),
                    (scope["scope_id"],),
                ).fetchone()
                if row is None or row[0] != action["operation_id"]:
                    return {"error": {"code": "invalid_input"}}
                if row[1] != scope["expected_revision"] or row[2] != "pending":
                    return {
                        "error": {
                            "code": "scope_stale",
                            "scope_id": scope["scope_id"],
                            "current_revision": row[1],
                        }
                    }
                unresolved = conn.execute(
                    (
                        "SELECT 1 FROM history_groups WHERE kind='unresolved' AND "
                        "group_id IN (SELECT value FROM json_each(?)) LIMIT 1"
                    ),
                    (row[3],),
                ).fetchone()
                if unresolved:
                    return {"error": {"code": "invalid_input"}}
                frozen.extend(json.loads(row[3]))
            at = self._owner._clock().isoformat()
            for scope in scopes:
                conn.execute(
                    (
                        "UPDATE forget_scopes SET "
                        "state='confirmed',revision=revision+1,confirmed_at=? WHERE "
                        "scope_id=?"
                    ),
                    (at, scope["scope_id"]),
                )
            conn.executemany(
                (
                    "INSERT INTO forget_limits VALUES (?,?,'isolated') ON "
                    "CONFLICT(operation_id,group_id) DO UPDATE SET mode='isolated'"
                ),
                [(action["operation_id"], g) for g in frozen],
            )
            refresh_pauses(conn)
            bump(conn)
            receipt = {
                "action_id": action["action_id"],
                "operation_id": action["operation_id"],
                "status": "confirmed",
                "scopes": scopes,
                "members": sorted(set(frozen)),
                "committed_at": at,
            }
            conn.execute(
                "INSERT INTO forget_actions VALUES (?,?,?,?)",
                (action["action_id"], fingerprint, key_id, _json(receipt)),
            )
            return receipt

        return self._write(write, context)

    def evaluate_history(
        self, groups, *, purpose, transaction=None, connection=None
    ):
        if purpose not in PURPOSES:
            return {"error": {"code": "invalid_input"}}

        def read(conn):
            allowed, denied = [], []
            for group in dict.fromkeys(groups):
                restricted = conn.execute(
                    "SELECT 1 FROM forget_limits WHERE group_id=? LIMIT 1", (group,)
                ).fetchone()
                evidence = conn.execute(
                    "SELECT evidence FROM history_groups WHERE group_id=?", (group,)
                ).fetchone()
                unresolved_input = conn.execute(
                    "SELECT 1 FROM run_input_explanations WHERE run_id=? "
                    "AND kind='attempt' AND "
                    "json_extract(explanation,'$.dispatch_state')='unconfirmed' "
                    "LIMIT 1", (group,),
                ).fetchone()
                (
                    allowed
                    if (
                        not restricted and not unresolved_input
                        and evidence == ("complete",)
                    )
                    else denied
                ).append(group)
            token = {
                "allowed": allowed,
                "denied": denied,
                "revision": revision(conn),
                "purpose": purpose,
            }
            token["proof"], token["key_id"] = self._owner._key.fingerprint(
                json.dumps(token, sort_keys=True).encode()
            )
            return token

        try:
            if transaction is not None:
                self._db.validate_borrowed_transaction(transaction)
                return read(transaction)
            if connection is not None:
                self._db.validate_owner_connection(connection)
                return read(connection)
            with self._db.reading() as conn:
                return read(conn)
        except (sqlite3.Error, RecordingUnavailable, ValueError) as error:
            raise_if_rollback_pending(error)
            return {"error": {"code": "storage_read_failed"}}

    def evaluate_automatic_records(
        self, memories, *, transaction=None, connection=None
    ):
        """Deny automatic reuse of records whose known sources are restricted."""

        def read(conn):
            allowed, denied = [], []
            for kind, memory_id, version in memories:
                restricted = conn.execute(
                    "SELECT 1 FROM memory_sources s "
                    "JOIN forget_limits l ON l.group_id=s.source_group_id "
                    "WHERE s.kind=? AND s.memory_id=? AND s.record_version=? "
                    "LIMIT 1",
                    (kind, memory_id, version),
                ).fetchone()
                identity = (kind, memory_id, version)
                (denied if restricted else allowed).append(identity)
            return {"allowed": allowed, "denied": denied}

        try:
            if transaction is not None:
                self._db.validate_borrowed_transaction(transaction)
                return read(transaction)
            if connection is not None:
                self._db.validate_owner_connection(connection)
                return read(connection)
            with self._db.reading() as conn:
                return read(conn)
        except (sqlite3.Error, RecordingUnavailable, ValueError) as error:
            raise_if_rollback_pending(error)
            return {"error": {"code": "storage_read_failed"}}

    def _token_valid(self, conn, token):
        try:
            data = dict(token)
            proof, key_id = data.pop("proof"), data.pop("key_id")
            expected, current_key = self._owner._key.fingerprint(
                json.dumps(data, sort_keys=True).encode()
            )
            return (
                key_id == current_key
                and hmac.compare_digest(proof, expected)
                and data.get("revision") == revision(conn)
                and "error" not in data
            )
        except (KeyError, TypeError, ValueError) as error:
            raise_if_rollback_pending(error)
            return False

    def projection_valid(self, token):
        try:
            with self._db.reading() as conn:
                return self._token_valid(conn, token)
        except (sqlite3.Error, RecordingUnavailable) as error:
            raise_if_rollback_pending(error)
            return False

    def write_projection(
        self, token, *, targets=(), write, context, transaction=None
    ):
        """Atomic #18 seam: the callback uses this connection, never commits it."""

        def apply(conn):
            if not self._token_valid(conn, token):
                return {"error": {"code": "projection_stale"}}
            if token["denied"]:
                return {"error": {"code": "sources_unavailable"}}
            stores = self._owner._stores(conn)
            for kind, memory_id, expected_version in targets:
                if kind not in ("semantic", "episodic"):
                    return {"error": {"code": "invalid_input"}}
                current = stores[0 if kind == "semantic" else 1].get(memory_id)
                if current is None or current.record_version != expected_version:
                    return {"error": {"code": "batch_invalidated"}}
            result = write(conn)
            if "error" not in result:
                for operation, kind, memory_id in conn.execute(
                    "SELECT operation_id,kind,memory_id FROM forget_operations"
                ).fetchall():
                    self.invalidate(conn, operation, memories=((kind, memory_id),))
                bump(conn)
            return result

        return self._write(apply, context, transaction=transaction)

    def consume_history(self, token, consume, context):
        """Validate at use under the same admission lease that orders deletion.

        The trusted consumer receives only permitted identities. IO runs outside
        the DB lock. Register actual Attempt evidence separately; an interrupted
        unregistered consumer must remain unknown, never certify completeness.
        """

        def use():
            if not self.projection_valid(token):
                return {"error": {"code": "projection_stale"}}
            consume(tuple(token["allowed"]))
            return {"status": "consumed"}

        return self._owner._run_mutation(use, context)
