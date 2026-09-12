"""File actions retain durable intent until publication and ledger are verified."""

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import PurePath
from uuid import NAMESPACE_URL, uuid5

from agent_alfred.clock import format_instant
from agent_alfred.messages import TextBlock
from agent_alfred.resource_rollback import ResumableRollback, RollbackSlot
from agent_alfred.tools import Tool, ToolFailure, ToolSuccess
from agent_alfred.tools.calendar import object_schema, string_schema


def digest(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def operation_id(context):
    return uuid5(
        NAMESPACE_URL, json.dumps([context.run_id, context.step_index, context.call_id])
    ).hex


class FileTools:
    def __init__(self, store, state, clock):
        self._store = store
        self._state = state
        self._clock = clock
        self._cleanup = ResumableRollback()
        self._nested_cleanup = RollbackSlot()
        self._deadline = float("inf")
        self._publication_op = None

    @property
    def state_path(self):
        return None if self._state is None else self._state.path

    @property
    def recording_store(self):
        return self._store

    def target_pending(self, target):
        with self._store.reading() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM file_operations WHERE target=? "
                    "AND state!='complete'",
                    (target,),
                ).fetchone()
                is not None
            )

    def target_created(self, target):
        with self._store.reading() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM file_operations WHERE target=?", (target,)
                ).fetchone()
                is not None
            )

    @contextmanager
    def tool_deadline(self, context):
        previous = self._deadline
        self._deadline = min(previous, context.deadline)
        try:
            context.checkpoint()
            yield
        finally:
            self._deadline = previous

    def set_run_deadline(self, deadline):
        self._deadline = deadline

    def checkpoint(self):
        if self._clock.monotonic() >= self._deadline:
            raise TimeoutError("File IO deadline exhausted")

    def close(self):
        return (
            self._nested_cleanup.retry_propagating()
            and self._cleanup.retry_propagating()
        )

    @contextmanager
    def directory(self, relative):
        # The service retains the owner even if an IO/close return is interrupted.
        directory = self._state.ensure_directory(
            PurePath(relative), _rollback=self._cleanup
        )
        try:
            yield directory
        except BaseException as exc:
            self._nested_cleanup.capture_failure(exc)
            raise
        finally:
            self.close()

    def read_file(self, target):
        self.checkpoint()
        relative = PurePath(target)
        with self.directory(relative.parent) as directory:
            try:
                file = directory.open_regular(
                    PurePath(relative.name),
                    access="read",
                    create=False,
                    _rollback=self._cleanup,
                )
            except FileNotFoundError:
                return None
            chunks = []
            while chunk := os.read(file.fd, 65536):
                self.checkpoint()
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8")

    def publish(self, target, content, *, replace_existing=False):
        self.checkpoint()
        relative = PurePath(target)
        with self.directory(relative.parent) as directory:
            # Existing targets are first preserved under an operation-specific
            # name in recover(); publication itself always protects late arrivals.
            self.record_publication_attempt()
            directory.create_bytes(
                PurePath(relative.name),
                content.encode("utf-8"),
                _rollback=self._cleanup,
                prepared=self.record_publication_identity,
                checkpoint=self.checkpoint,
            )

    def record_publication_attempt(self):
        self.checkpoint()
        if self._publication_op is None:
            raise RuntimeError("publication has no durable operation")
        with self._store.transaction() as conn:
            conn.execute(
                "UPDATE file_operations SET publication_attempted=1 "
                "WHERE operation_id=?", (self._publication_op,),
            )
            conn.commit()
        self.checkpoint()

    def record_publication_identity(self, identity):
        self.checkpoint()
        if self._publication_op is None:
            raise RuntimeError("publication has no durable operation")
        with self._store.transaction() as conn:
            conn.execute(
                "UPDATE file_operations SET candidate_identity=? WHERE operation_id=?",
                (json.dumps(identity), self._publication_op),
            )
            conn.commit()
        self.checkpoint()

    def has_publication_identity(self, op):
        with self._store.reading() as conn:
            return conn.execute(
                "SELECT candidate_identity IS NOT NULL "
                "FROM file_operations WHERE operation_id=?",
                (op,),
            ).fetchone()[0]

    def publication_was_attempted(self, op):
        with self._store.reading() as conn:
            return conn.execute(
                "SELECT publication_attempted FROM file_operations "
                "WHERE operation_id=?", (op,),
            ).fetchone()[0]

    def replay(self, op, tool, target, content, expected):
        old = self.get_operation(op)
        if old is None:
            return None
        fingerprint = digest(json.dumps([tool, target, content, expected]))
        with self._store.reading() as conn:
            previous = conn.execute(
                "SELECT request_digest FROM file_operations WHERE operation_id=?", (op,)
            ).fetchone()[0]
        if previous != fingerprint:
            return ToolFailure(
                "execution_error", (TextBlock("Operation parameter mismatch."),)
            )
        return self.recover(op)

    def publication_matches(self, op, target):
        with self._store.reading() as conn:
            identity = conn.execute(
                "SELECT candidate_identity FROM file_operations WHERE operation_id=?",
                (op,),
            ).fetchone()[0]
        if identity is None:
            return False
        relative = PurePath(target)
        with self.directory(relative.parent) as directory:
            file = directory.open_regular(
                PurePath(relative.name),
                access="read",
                create=False,
                _rollback=self._cleanup,
            )
            info = os.fstat(file.fd)
            return json.loads(identity) == [info.st_dev, info.st_ino]

    def preserve(self, target, backup):
        relative = PurePath(target)
        with self.directory(relative.parent) as directory:
            directory.preserve_regular(
                PurePath(relative.name), PurePath(PurePath(backup).name)
            )

    def pending_persona(self):
        with self._store.reading() as conn:
            return conn.execute(
                "SELECT original_content FROM file_operations "
                "WHERE target='persona/persona.md' AND state!='complete'"
            ).fetchone()

    def declarations(self):
        return (
            Tool(
                "draft_message",
                "Create a local Markdown draft; never send it.",
                object_schema(
                    {key: string_schema() for key in ("recipient", "subject", "body")},
                    ("body",),
                ),
                self.draft,
                "local_write",
            ),
        )

    def draft(self, args, context):
        if self._state is None:
            return ToolFailure(
                "unavailable", (TextBlock("State directory unavailable."),)
            )
        parts = []
        if "subject" in args:
            parts.append("# " + args["subject"])
        if "recipient" in args:
            parts.append("收件对象：" + args["recipient"])
        parts.append(args["body"])
        op = operation_id(context)
        return self.write(
            op,
            "draft_message",
            f"outbox/{op}.md",
            "\n\n".join(parts) + "\n",
            None,
            context,
        )

    def resume_pending(self, run_id=None):
        with self._store.reading() as conn:
            pending = conn.execute(
                "SELECT operation_id FROM file_operations WHERE state='prepared'"
            ).fetchall()
        for (op,) in pending:
            self.recover(op, run_id)

    def unverified_targets(self):
        with self._store.reading() as conn:
            return tuple(
                row[0]
                for row in conn.execute(
                    "SELECT target FROM file_operations WHERE state!='complete'"
                )
            )

    def get_operation(self, op):
        with self._store.reading() as conn:
            row = conn.execute(
                "SELECT state,receipt,target FROM file_operations WHERE operation_id=?",
                (op,),
            ).fetchone()
        if row is None:
            return None
        if row[1] is not None:
            return json.loads(row[1])
        return {
            "operation_id": op,
            "state": row[0],
            "path": str(self._state.path / row[2]),
        }

    def write(self, op, tool, target, content, expected, context):
        context.checkpoint()
        previous = self._deadline
        self._deadline = min(previous, context.deadline)
        try:
            return self._write(op, tool, target, content, expected, context)
        except BaseException as failure:
            # A commit can take effect and still raise at its return boundary.
            # Retain cleanup before observing durable state or normalizing it.
            self._nested_cleanup.capture_failure(failure)
            if not isinstance(failure, Exception):
                raise
            try:
                details = self.get_operation(op)
            except BaseException as observation:
                self._nested_cleanup.capture_failure(observation)
                if not isinstance(observation, Exception):
                    raise
                return self._pending_details(op, "unverified", None)
            if details is None:
                # Only an authoritative absence permits ordinary model retry:
                # there is no old durable intent for resume_pending to execute.
                return ToolFailure(
                    "execution_error",
                    (TextBlock("Preparation was not persisted; not executed."),),
                )
            return self._pending_details(op, details["state"], details)
        finally:
            self._deadline = previous

    def _write(self, op, tool, target, content, expected, context):
        if self._store.transaction_in_progress:
            return ToolFailure(
                "execution_error", (TextBlock("Caller transaction is active."),)
            )
        request_digest = digest(json.dumps([tool, target, content, expected]))
        old = self.get_operation(op)
        if old is not None:
            with self._store.reading() as conn:
                previous = conn.execute(
                    "SELECT request_digest FROM file_operations WHERE operation_id=?",
                    (op,),
                ).fetchone()[0]
            if previous != request_digest:
                return ToolFailure(
                    "execution_error", (TextBlock("Operation parameter mismatch."),)
                )
            return self.recover(op)
        with self._store.reading() as conn:
            pending = conn.execute(
                "SELECT operation_id FROM file_operations "
                "WHERE target=? AND state!='complete'",
                (target,),
            ).fetchone()
        if pending:
            return ToolFailure(
                "execution_error", (TextBlock("Target awaiting recovery."),)
            )
        current = self.read_file(target)
        if (None if current is None else digest(current)) != expected:
            return ToolFailure(
                "execution_error", (TextBlock("Target version conflict."),)
            )
        self.checkpoint()
        with self._store.transaction() as conn:
            conn.execute(
                "INSERT INTO file_operations VALUES "
                "(?,?,?,?,?,'prepared',?,?,?,?,NULL,?,?,NULL,0)",
                (
                    op,
                    tool,
                    target,
                    content,
                    expected,
                    context.call_id,
                    context.run_id,
                    context.session_id,
                    format_instant(self._clock.wall_utc()),
                    request_digest,
                    current,
                ),
            )
            if context.metering is not None:
                context.metering.associate_operation(context, conn, op)
            conn.commit()
        return self.recover(op)

    def recover(self, op, run_id=None):
        if self._store.transaction_in_progress:
            return ToolFailure(
                "execution_error", (TextBlock("Caller transaction is active."),)
            )
        previous = self._publication_op
        self._publication_op = op
        try:
            result = self._recover(op)
            with self._store.transaction() as conn:
                conn.execute(
                    "INSERT INTO tool_operation_verifications "
                    "VALUES (?,?,?,'file_operations',?) "
                    "ON CONFLICT(operation_id) DO UPDATE SET state=excluded.state, "
                    "verified_at=excluded.verified_at, "
                    "related_run=COALESCE(excluded.related_run,related_run)",
                    (
                        op,
                        "complete" if isinstance(result, ToolSuccess) else "unverified",
                        format_instant(self._clock.wall_utc()),
                        run_id,
                    ),
                )
                conn.commit()
            return result
        finally:
            self._publication_op = previous

    def _recover(self, op):
        if self._store.transaction_in_progress:
            return ToolFailure(
                "execution_error", (TextBlock("Caller transaction is active."),)
            )
        with self._store.reading() as conn:
            row = conn.execute(
                "SELECT tool_name,target,content,expected_digest,state,"
                "call_id,run_id,session_id,created_at,receipt "
                "FROM file_operations WHERE operation_id=?",
                (op,),
            ).fetchone()
        if row is None:
            return ToolFailure("invalid_input", (TextBlock("Unknown operation."),))
        tool, target, content, expected, state, call, run, session, created, receipt = (
            row
        )
        if state == "complete":
            return ToolSuccess((TextBlock(receipt),), operation_id=op)
        if state == "conflict":
            return self.pending(op, "conflict")
        backup = str(PurePath(target).with_name(".original-" + op + ".md"))
        try:
            current = self.read_file(target)
            if current is None and self.publication_was_attempted(op):
                # Attempt intent is committed before exclusive create: even a
                # lost identity receipt cannot turn a deletion into a retry.
                with self._store.transaction() as conn:
                    conn.execute(
                        "UPDATE file_operations SET state='conflict' "
                        "WHERE operation_id=?", (op,),
                    )
                    conn.commit()
                return self.pending(op, "conflict")
            observed = None if current is None else digest(current)
            if observed != digest(content) or (
                observed == expected and not self.has_publication_identity(op)
            ):
                if expected is not None and current is None:
                    preserved = self.read_file(backup)
                    observed = None if preserved is None else digest(preserved)
                if observed != expected:
                    with self._store.transaction() as conn:
                        conn.execute(
                            "UPDATE file_operations SET state='conflict' "
                            "WHERE operation_id=?",
                            (op,),
                        )
                        conn.commit()
                    return self.pending(op, "conflict")
                if expected is None:
                    self.publish(target, content)
                else:
                    if self.read_file(backup) is None:
                        self.preserve(target, backup)
                    # The original inode is retained even after success. A late
                    # editor can keep writing it without losing its content.
                    if digest(self.read_file(backup) or "") != expected:
                        with self._store.transaction() as conn:
                            conn.execute(
                                "UPDATE file_operations SET state='conflict' "
                                "WHERE operation_id=?",
                                (op,),
                            )
                            conn.commit()
                        return self.pending(op, "conflict")
                    self.publish(target, content, replace_existing=True)
            if (
                expected is not None
                and digest(self.read_file(backup) or "") != expected
            ):
                with self._store.transaction() as conn:
                    conn.execute(
                        "UPDATE file_operations SET state='conflict' "
                        "WHERE operation_id=?",
                        (op,),
                    )
                    conn.commit()
                return self.pending(op, "conflict")
            if not self.publication_matches(op, target):
                return self.pending(op, "prepared")
            if self.read_file(target) != content:
                return self.pending(op, "prepared")
            # Re-establish persistence on recovery even when publication survived
            # but the original directory sync did not return.
            relative = PurePath(target)
            with self.directory(relative.parent) as directory:
                file = directory.open_regular(
                    PurePath(relative.name),
                    access="read",
                    create=False,
                    _rollback=self._cleanup,
                )
                self.checkpoint()
                file.fsync()
                self.checkpoint()
                directory.fsync()
                self.checkpoint()
            receipt = json.dumps(
                {
                    "operation_id": op,
                    "state": "complete",
                    "path": str(self._state.path / target),
                    **(
                        {"activation": "重启后加载；尚未匹配或注入。"}
                        if tool == "create_skill"
                        else {}
                    ),
                },
                ensure_ascii=False,
            )
            with self._store.transaction() as conn:
                conn.execute(
                    "INSERT INTO tool_ledger(tool_name,fingerprint,effect,status,"
                    "call_id,run_id,session_id,summary,created_at) "
                    "VALUES (?,?,'local_write','succeeded',?,?,?,?,?)",
                    (
                        tool,
                        digest(content),
                        call,
                        run,
                        session,
                        "Local file publication verified",
                        created,
                    ),
                )
                conn.execute(
                    "UPDATE file_operations SET state='complete',receipt=?,"
                    "content='' WHERE operation_id=?",
                    (receipt, op),
                )
                conn.commit()
            return ToolSuccess((TextBlock(receipt),), operation_id=op)
        except Exception as exc:
            self._nested_cleanup.capture_failure(exc)
            # The prepared record is durable; an exception cannot prove that
            # publication did not happen. Stop this Run, keep the same operation.
            return self.pending(op, "prepared")

    def pending(self, op, state):
        try:
            details = self.get_operation(op)
        except BaseException as observation:
            self._nested_cleanup.capture_failure(observation)
            if not isinstance(observation, Exception):
                raise
            return self._pending_details(op, "unverified", None)
        return self._pending_details(op, state, details)

    def _pending_details(self, op, state, details):
        recovery_path = None
        if details is not None:
            recovery_path = str(
                PurePath(details["path"]).with_name(".original-" + op + ".md")
            )
        return ToolFailure(
            "execution_error",
            (
                TextBlock(
                    json.dumps(
                        {
                            "preserved_original": recovery_path,
                            "operation_id": op,
                            "state": state,
                            "recovery_command": "恢复操作 " + op,
                        },
                        ensure_ascii=False,
                    )
                ),
            ),
            stop_reason="file_result_unverified",
            operation_id=op,
        )

    def handle_command(self, message, run_id=None):
        for prefix in ("恢复操作 ", "查看操作 "):
            if message.startswith(prefix):
                op = message[len(prefix) :].strip()
                if not op or any(c not in "0123456789abcdef" for c in op):
                    return ToolFailure(
                        "invalid_input", (TextBlock("Invalid operation ID."),)
                    )
                if prefix == "恢复操作 ":
                    return self.recover(op, run_id)
                return ToolSuccess(
                    (TextBlock(json.dumps(self.get_operation(op), ensure_ascii=False)),)
                )
        return None
