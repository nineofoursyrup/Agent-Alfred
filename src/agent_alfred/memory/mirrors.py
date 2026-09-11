"""Current-only managed Markdown projections, owned by the command executor.

The database is authoritative. File IO runs outside its lock under the existing
mutation/Run admission. External editing must stop during managed writes;
identity checks and atomic replacement are not an external-writer CAS.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from contextlib import contextmanager
from pathlib import PurePath

from agent_alfred.memory.types import origin_json
from agent_alfred.resource_rollback import (
    ResumableRollback,
    RollbackSlot,
    dominant_error,
    raise_if_rollback_pending,
    reraise_failure,
)

TARGETS = {"facts": "memory/facts.md", "episodes": "memory/episodes.md"}


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(value).hexdigest()


class MirrorUnavailable(Exception):
    """A safe code only; neither a file body nor a path error is user output."""


class MarkdownMirrors:
    def __init__(self, owner, state):
        self._owner = owner
        self._db = owner._db
        self._state = state
        self._pending = RollbackSlot()
        self._reconciled = False
        # Construction performs SQLite work only; no independently owned root.
        with self._db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for target in TARGETS.values():
                conn.execute(
                    "INSERT OR IGNORE INTO memory_mirrors(target_id) VALUES (?)",
                    (target,),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO forget_targets VALUES (?,0,0)", (target,)
                )
            conn.commit()

    def close(self):
        return self._pending.retry_propagating()

    def reconciliation_targets(self, conn, *, memories=(), isolated=None):
        return tuple(TARGETS.values())

    def invalidate(self, conn, *, memories=(), isolated=None):
        # Reconciliation is repeatable. This signature contains only identities,
        # never bodies; committing it and returning obligations are one effect.
        signature = _digest(
            _json(
                [
                    sorted(tuple(item) for item in memories),
                    sorted(isolated or ()),
                ]
            ).encode()
        )
        inserted = conn.execute(
            "INSERT OR IGNORE INTO memory_mirror_invalidations VALUES (?)",
            (signature,),
        ).rowcount
        if not inserted:
            return ()
        conn.execute(
            "UPDATE memory_mirrors SET required_generation=required_generation+1"
        )
        return tuple(TARGETS.values())

    def _target(self, name):
        if not isinstance(name, str):
            raise MirrorUnavailable("not_found")
        if name in TARGETS:
            return TARGETS[name]
        if name in TARGETS.values():
            return name
        raise MirrorUnavailable("not_found")

    @contextmanager
    def _directory(self, *, create=False):
        cleanup = ResumableRollback()
        self._pending.begin(cleanup)
        try:
            acquire = (
                self._state.ensure_directory if create else self._state.open_directory
            )
            directory = acquire(PurePath("memory"), _rollback=cleanup)
            yield directory
        except BaseException as error:
            nested = RollbackSlot()
            nested.capture_failure(error)
            if not nested.owns(cleanup):
                # A retained temporary-file owner needs its parent capability.
                # Ordered cleanup stops here until that child has settled.
                cleanup.own(nested, nested.retry_propagating)
            cleanup.raise_failure(error)
        else:
            cleanup.close()
            self._pending.complete(cleanup)

    def _observe(self, directory, target):
        cleanup = ResumableRollback()
        self._pending.begin(cleanup)
        try:
            try:
                file = directory.open_regular(
                    PurePath(PurePath(target).name),
                    access="read",
                    create=False,
                    _rollback=cleanup,
                )
            except FileNotFoundError:
                root = os.fstat(self._state.fd)
                return {"missing": True, "root": [root.st_dev, root.st_ino]}, None
            before = file.stat()
            chunks = []
            while chunk := os.read(file.fd, 65536):
                chunks.append(chunk)
            file.verify_identity()
            after = file.stat()

            def identity(s):
                return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)

            if identity(before) != identity(after):
                raise MirrorUnavailable("file_changed")
            body = b"".join(chunks)
            root = os.fstat(self._state.fd)
            return {
                "identity": identity(after),
                "sha256": _digest(body),
                "root": [root.st_dev, root.st_ino],
            }, body
        except BaseException as error:
            self._pending.capture_failure(error)
            cleanup.raise_failure(error)
        finally:
            failure = sys.exception()
            try:
                cleanup.close()
            except BaseException as secondary:
                primary = dominant_error(failure, secondary)
                reraise_failure(
                    primary, earlier=(secondary if primary is failure else failure)
                )
            self._pending.complete(cleanup)

    def _row(self, target):
        with self._db.reading() as conn:
            cursor = conn.execute(
                "SELECT * FROM memory_mirrors WHERE target_id=?", (target,)
            )
            row = dict(zip((c[0] for c in cursor.description), cursor.fetchone()))
            row["cleanup_generation"] = conn.execute(
                "SELECT revision FROM forget_targets WHERE target_id=?", (target,)
            ).fetchone()[0]
            row["fenced"] = bool(
                conn.execute(
                    "SELECT 1 FROM forget_projection_fences WHERE target_id=? "
                    "UNION ALL SELECT 1 FROM forget_projection_unknown LIMIT 1",
                    (target,),
                ).fetchone()
            )
        return row

    def _update(self, target, **values):
        with self._db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE memory_mirrors SET "
                + ",".join(f"{key}=?" for key in values)
                + " WHERE target_id=?",
                (*values.values(), target),
            )
            conn.commit()

    def _proof(self, row, observed):
        data = {
            "target": row["target_id"],
            "generation": row["required_generation"],
            "cleanup_generation": row["cleanup_generation"],
            "observed": observed,
        }
        proof, key = self._owner._key.fingerprint(_json(data).encode())
        return {**data, "proof": proof, "key_id": key}

    def _status(self, target, *, preview=False):
        row = self._row(target)
        observed, body = None, None
        error = row["error"]
        try:
            with self._directory() as directory:
                observed, body = self._observe(directory, target)
        except Exception as failure:
            raise_if_rollback_pending(failure)
            error = "file_unavailable"
        expected = json.loads(row["fingerprint"]) if row["fingerprint"] else None
        conflict = observed is not None and (
            (expected is not None and _json(observed) != _json(expected))
            or (expected is None and not observed.get("missing"))
        )
        intent = json.loads(row["intent"]) if row["intent"] else None
        if (
            intent
            and observed
            and observed.get("identity")
            and list(observed["identity"][:2]) == intent.get("temporary_identity")
            and observed.get("sha256") == intent.get("sha256")
        ):
            conflict = False  # our publication is awaiting verification
        current = self._row(target)
        ready = (
            self._reconciled
            and observed is not None
            and body is not None
            and not conflict
            and not error
            and not current["fenced"]
            and not current["intent"]
            and not current["error"]
            and _json(observed) == current["fingerprint"]
            and row["required_generation"]
            == current["required_generation"]
            == row["verified_generation"]
            and row["covered_cleanup_generation"] == current["cleanup_generation"]
            and self._owner.forgetting.managed_target_readable(target)
        )
        result = {
            "target": target,
            "path": str(self._state.path / target),
            "ready": ready,
            "dirty": current["required_generation"] != row["verified_generation"],
            "conflict": bool(conflict or row["conflict"]),
            "error": error,
            "required_generation": current["required_generation"],
            "generated_generation": row["generated_generation"],
            "verified_generation": row["verified_generation"],
            "generated_at": row["generated_at"],
            "verified_at": row["verified_at"],
            "external_editing": "Stop external editing during managed writes.",
        }
        if observed is not None:
            result["confirmation"] = self._proof(current, observed)
        if preview and ready:
            result["text"] = body.decode("utf-8")
        return result

    def status(self, name):
        try:
            return self._status(self._target(name))
        except Exception as error:
            raise_if_rollback_pending(error)
            return {"ready": False, "error": "mirror_unavailable"}

    def preview(self, name):
        try:
            return self._status(self._target(name), preview=True)
        except Exception as error:
            raise_if_rollback_pending(error)
            return {"ready": False, "error": "mirror_unavailable"}

    def _render(self, target):
        kind, table = (
            ("semantic", "facts")
            if target == TARGETS["facts"]
            else ("episodic", "episodes")
        )
        with self._db.reading() as conn:
            stores = self._owner._stores(conn)
            store = stores[0 if kind == "semantic" else 1]
            # Enumerate the complete Store, not its default first page. Rowid is
            # the stable internal tie-breaker; opaque memory IDs imply no order.
            order = (
                "subject COLLATE BINARY,rowid"
                if kind == "semantic"
                else ("occurred_at DESC,rowid")
            )
            ids = conn.execute(f"SELECT id FROM {table} ORDER BY {order}").fetchall()
            records = [store.get(item[0]) for item in ids]
            identities = [
                (kind, str(r.id), r.record_version) for r in records if r is not None
            ]
            safety = self._owner.forgetting.evaluate_automatic_records(
                identities, connection=conn
            )
            if "error" in safety:
                raise MirrorUnavailable("source_unavailable")
            allowed = set(safety["allowed"])
            lines = ["# Facts" if kind == "semantic" else "# Episodes", ""]
            subject = None
            for record in records:
                if (
                    record is None
                    or (kind, str(record.id), record.record_version) not in allowed
                ):
                    continue
                if kind == "semantic":
                    if subject != record.subject:
                        subject = record.subject
                        lines.extend([f"## {subject}", ""])
                    lines.extend([record.fact, ""])
                else:
                    lines.extend(
                        [f"## {record.occurred_at.isoformat()}", "", record.summary, ""]
                    )
                sources = conn.execute(
                    "SELECT source_group_id FROM memory_sources WHERE kind=? "
                    "AND memory_id=? AND record_version=? ORDER BY source_group_id",
                    (kind, str(record.id), record.record_version),
                ).fetchall()
                provenance = conn.execute(
                    "SELECT state FROM memory_provenance WHERE kind=? AND "
                    "memory_id=? AND record_version=?",
                    (kind, str(record.id), record.record_version),
                ).fetchone()
                if kind == "episodic" and record.occurred_until is not None:
                    lines.extend([f"Until: {record.occurred_until.isoformat()}", ""])
                lines.extend(
                    [
                        f"Origin: {_json(origin_json(record.origin))} · "
                        f"last change: {_json(origin_json(record.last_change_origin))}",
                        f"ID: {record.id} · version: {record.record_version} · "
                        f"protected: {str(record.human_protected).lower()}",
                        f"Created: {record.created_at.isoformat()} · "
                        f"modified: {record.modified_at.isoformat()}",
                        f"Provenance: {provenance[0] if provenance else 'unknown'} · "
                        f"sources: {', '.join(s[0] for s in sources) or 'none'}",
                        "",
                    ]
                )
            return "\n".join(lines).encode("utf-8")

    def verify(self, target, generation):
        target = self._target(target)
        row = self._row(target)
        if (
            row["fenced"]
            or row["required_generation"] != row["verified_generation"]
            or row["cleanup_generation"] != generation
            or row["covered_cleanup_generation"] != generation
            or row["error"]
            or row["conflict"]
            or row["intent"]
        ):
            return False
        try:
            with self._directory() as directory:
                observed, body = self._observe(directory, target)
        except FileNotFoundError:
            return False
        return (
            body is not None
            and _json(observed) == row["fingerprint"]
            and self._row(target)["required_generation"] == row["required_generation"]
        )

    def _cleanup_intent(self, directory, target, intent):
        if not intent:
            return
        temporary = intent.get("temporary")
        identity = intent.get("temporary_identity")
        if temporary and identity:
            # Only the exact inode journalled before body write is owned. A
            # successor is unrelated and is never removed by a wildcard.
            directory._unlink_regular_identity(PurePath(temporary), tuple(identity))

    def rebuild(self, target, generation, *, confirmation=None, action_id=None):
        target = self._target(target)
        row = self._row(target)
        if row["fenced"] or generation != row["cleanup_generation"]:
            raise MirrorUnavailable("projection_fenced")
        with self._directory(create=True) as directory:
            intent = json.loads(row["intent"]) if row["intent"] else None
            self._cleanup_intent(directory, target, intent)
            observed, _ = self._observe(directory, target)
            expected = json.loads(row["fingerprint"]) if row["fingerprint"] else None
            owned = (
                _json(observed) == _json(expected)
                if expected
                else observed.get("missing")
            )
            # An interrupted published replace can be recognized only by its
            # journalled inode, never by a matching body hash alone.
            if expected and observed.get("root") != expected.get("root"):
                owned = observed.get("missing") or (
                    observed.get("sha256") == expected.get("sha256")
                )
            if intent and intent.get("rollback_observation") == observed:
                owned = True  # absent target observed inside owned rollback
            if intent and intent.get("temporary_identity") and observed.get("identity"):
                owned = owned or (
                    list(observed["identity"][:2]) == intent["temporary_identity"]
                    and observed.get("sha256") == intent.get("sha256")
                )
            if confirmation is not None:
                if not hmac.compare_digest(
                    _json(self._proof(row, observed)), _json(confirmation)
                ):
                    raise MirrorUnavailable("stale_confirmation")
                owned = True
            if not owned:
                self._update(target, conflict=1, error="external_conflict")
                raise MirrorUnavailable("external_conflict")
            body = self._render(target)
            if self._row(target)["required_generation"] != row["required_generation"]:
                raise MirrorUnavailable("generation_changed")
            intent = {
                "generation": row["required_generation"],
                "sha256": _digest(body),
                "action_id": action_id,
            }
            self._update(target, intent=_json(intent), error="verification_pending")

            def prepared(name, identity):
                intent.update(temporary=name, temporary_identity=list(identity))
                self._update(target, intent=_json(intent))

            try:
                directory.replace_bytes(
                    PurePath(PurePath(target).name), body, prepared=prepared
                )
            except BaseException as failure:
                # Observe a completed local rollback before relinquishing the
                # lease. A later arbitrary missing file is not this owned fact.
                self._pending.capture_failure(failure)
                try:
                    raise_if_rollback_pending(failure)
                    rollback_observed, _ = self._observe(directory, target)
                    if rollback_observed.get("missing"):
                        intent["rollback_observation"] = rollback_observed
                        self._update(target, intent=_json(intent))
                except BaseException as secondary:
                    primary = dominant_error(failure, secondary)
                    reraise_failure(
                        primary, earlier=(secondary if primary is failure else failure)
                    )
                raise
            self._update(
                target,
                generated_generation=row["required_generation"],
                generated_at=self._owner._clock().isoformat(),
            )
            actual, written = self._observe(directory, target)
            if (
                written != body
                or self._row(target)["required_generation"]
                != row["required_generation"]
            ):
                raise MirrorUnavailable("verification_failed")
            with self._db.transaction() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE memory_mirrors SET fingerprint=?,verified_generation=?,"
                    "covered_cleanup_generation=?,verified_at=?,conflict=0,"
                    "error=NULL,intent=NULL WHERE target_id=? "
                    "AND required_generation=?",
                    (
                        _json(actual),
                        row["required_generation"],
                        generation,
                        self._owner._clock().isoformat(),
                        target,
                        row["required_generation"],
                    ),
                )
                if intent.get("action_id"):
                    receipt = {
                        "status": "regenerated",
                        "target": target,
                        "operation_id": intent["action_id"],
                        "generation": row["required_generation"],
                    }
                    conn.execute(
                        "UPDATE memory_mirror_actions SET receipt=? "
                        "WHERE operation_id=? "
                        "AND json_extract(receipt,'$.status')='pending'",
                        (_json(receipt), intent["action_id"]),
                    )
                conn.commit()

    def _finish_action(self, operation_id, receipt):
        """Settle once; file recovery cannot rewrite an action's terminal fact."""
        with self._db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE memory_mirror_actions SET receipt=? WHERE operation_id=? "
                "AND json_extract(receipt,'$.status')='pending'",
                (_json(receipt), operation_id),
            )
            conn.execute(
                "UPDATE memory_mirrors SET intent=json_remove(intent,'$.action_id') "
                "WHERE json_extract(intent,'$.action_id')=?",
                (operation_id,),
            )
            conn.commit()

    def _settle_pending_actions(self):
        # Called only after the previous executor frame ended. Unfinished
        # confirmations require a new explicit action, never old authorization.
        with self._db.reading() as conn:
            pending = conn.execute(
                "SELECT operation_id FROM memory_mirror_actions "
                "WHERE json_extract(receipt,'$.status')='pending'"
            ).fetchall()
        for (operation_id,) in pending:
            self._finish_action(
                operation_id, {"status": "interrupted", "operation_id": operation_id}
            )

    def refresh_admitted(self):
        """Called only inside the shared command executor's existing lease."""
        self._settle_pending_actions()
        if not self._reconciled:
            self._owner.forgetting._prepare_projection_recovery()
            with self._db.reading() as conn:
                old = conn.execute(
                    "SELECT operation_id FROM forget_operations"
                ).fetchall()
            for (operation,) in old:
                if self._owner.forgetting._restore_projection(operation) is not None:
                    return
            self._reconciled = True
        for target in TARGETS.values():
            try:
                row = self._row(target)
                if not self.verify(target, row["cleanup_generation"]):
                    self.rebuild(target, row["cleanup_generation"])
            except Exception as error:
                self._pending.capture_failure(error)
                raise_if_rollback_pending(error)
                try:
                    self._update(
                        target,
                        error=(
                            str(error)
                            if isinstance(error, MirrorUnavailable)
                            else "mirror_sync_failed"
                        ),
                    )
                except Exception as failure:
                    raise_if_rollback_pending(failure)
                    # The pre-existing durable generation/intent remains dirty.
        # The real coordinator alone publishes deletion cleanup completion.
        with self._db.reading() as conn:
            operations = conn.execute(
                "SELECT DISTINCT operation_id FROM forget_cleanup "
                "WHERE state!='complete'"
            ).fetchall()
        for (operation,) in operations:
            self._owner.forgetting._retry_cleanup_admitted(operation)

    def retry(self, name, context, *, operation_id=None):
        try:
            target = self._target(name)
        except MirrorUnavailable:
            return {"error": {"code": "not_found"}}

        if operation_id is not None:
            if not isinstance(operation_id, str) or not operation_id:
                return {"error": {"code": "invalid_input"}}
            return self._file_action(
                target,
                operation_id,
                _digest(_json(["retry", target]).encode()),
                context,
                lambda: self.rebuild(
                    target,
                    self._row(target)["cleanup_generation"],
                    action_id=operation_id,
                ),
            )

        def run():
            try:
                self.rebuild(target, self._row(target)["cleanup_generation"])
            except Exception as error:
                raise_if_rollback_pending(error)
                return {
                    "error": {
                        "code": str(error)
                        if isinstance(error, MirrorUnavailable)
                        else "mirror_sync_failed"
                    }
                }
            return {"status": "regenerated", "target": target}

        return self._owner._run_mutation(run, context)

    def confirm(self, name, observation, operation_id, context):
        try:
            target = self._target(name)
            if not isinstance(operation_id, str) or not operation_id:
                return {"error": {"code": "invalid_input"}}
            if not isinstance(observation, dict):
                return {"error": {"code": "invalid_input"}}
            fingerprint = _digest(_json([target, observation]).encode())
        except MirrorUnavailable, ValueError, TypeError:
            return {"error": {"code": "invalid_input"}}

        return self._file_action(
            target,
            operation_id,
            fingerprint,
            context,
            lambda: self.rebuild(
                target,
                self._row(target)["cleanup_generation"],
                confirmation=observation,
                action_id=operation_id,
            ),
        )

    def _file_action(self, target, operation_id, fingerprint, context, execute):
        owns_result = False

        def run():
            nonlocal owns_result
            old = self.get_operation(operation_id)
            if old is not None:
                if old["fingerprint"] != fingerprint:
                    return {"error": {"code": "operation_mismatch"}}
                owns_result = True
                if old["receipt"].get("status") == "pending":
                    self._finish_action(
                        operation_id,
                        {"status": "interrupted", "operation_id": operation_id},
                    )
                return self.get_operation(operation_id)["receipt"]
            owns_result = True
            pending = {
                "status": "pending",
                "target": target,
                "operation_id": operation_id,
            }
            try:
                with self._db.transaction() as conn:
                    conn.execute(
                        "INSERT INTO memory_mirror_actions VALUES (?,?,?)",
                        (operation_id, fingerprint, _json(pending)),
                    )
                    conn.commit()
                execute()
            except BaseException as error:
                receipt = (
                    {"status": "interrupted", "operation_id": operation_id}
                    if not isinstance(error, Exception)
                    else {
                        "error": {
                            "code": str(error)
                            if isinstance(error, MirrorUnavailable)
                            else "mirror_sync_failed"
                        }
                    }
                )
                try:
                    self._finish_action(operation_id, receipt)
                except BaseException as secondary:
                    if not isinstance(error, Exception) or not isinstance(
                        secondary, Exception
                    ):
                        primary = dominant_error(error, secondary)
                        reraise_failure(
                            primary, earlier=(secondary if primary is error else error)
                        )
                    raise_if_rollback_pending(secondary)
                    # If terminal storage failed, the durable pending row owns
                    # recovery. Do not fabricate a terminal response.
                raise_if_rollback_pending(error)
                if not isinstance(error, Exception):
                    raise
                current = self.get_operation(operation_id)
                return current["receipt"] if current is not None else receipt
            return self.get_operation(operation_id)["receipt"]

        result = self._owner._run_mutation(run, context)
        if owns_result:
            current = self.get_operation(operation_id)
            if current is not None and current["fingerprint"] == fingerprint:
                return current["receipt"]
        return result

    def get_operation(self, operation_id):
        with self._db.reading() as conn:
            row = conn.execute(
                "SELECT fingerprint,receipt FROM memory_mirror_actions "
                "WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        return {"fingerprint": row[0], "receipt": json.loads(row[1])} if row else None
