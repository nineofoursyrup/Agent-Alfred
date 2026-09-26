"""In-process query handles, one worker, and reachable cleanup ownership."""

from __future__ import annotations

import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any, Callable

from agent_alfred.clock import SystemClock, format_instant
from agent_alfred.database_console.budget import (
    CANCEL_BUDGET_S,
    EXECUTE_BUDGET_S,
    HANDLE_CAPACITY,
    HANDLE_TTL_S,
    RESULT_JSON_LIMIT,
    SQL_TEXT_LIMIT,
    TERMINAL_TTL_S,
)
from agent_alfred.database_console.catalog import (
    limits,
    manifest,
)
from agent_alfred.database_console.encode import dump
from agent_alfred.database_console.errors import ConsoleError
from agent_alfred.database_console.project import _protect, _source_rows
from agent_alfred.database_console.schema import compatible_schema
from agent_alfred.database_console.sqlite_limits import (
    hard_heap_limit_supported,
    memory_temp_store,
    required_functions_available,
)
from agent_alfred.redact import Redactor

WORKER = Path(__file__).with_name("worker.py")
DIAGNOSTIC_TARGET = "database-console"


@dataclass
class SendLease:
    """One HTTP response body the handler is responsible for releasing."""

    query_id: str
    body: bytes
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    sock: object | None = None


@dataclass
class QueryRecord:
    query_id: str
    created: float
    expires: float
    instance_id: str
    used: bool = False
    status: str = "unused"
    cleanup: str = "released"
    terminal_at: float | None = None
    process: subprocess.Popen | None = None
    owner: object | None = None
    response_owner: int | None = None
    result: dict[str, Any] | None = None
    result_delivered: bool = False
    error: str | None = None
    cancel_before_execute: bool = False
    completed_before_cancel: bool = False
    memory_revision: str | None = None
    protection_version: str | None = None
    barriers: dict[str, str] = field(default_factory=dict)
    stop: threading.Event = field(default_factory=threading.Event)


class DatabaseConsole:
    def __init__(
        self,
        *,
        instance_id: str,
        state,
        redactor: Redactor,
        memory_revision: Callable[[], int],
        clock=None,
        broker=None,
        interrupt: threading.Event | None = None,
    ):
        self.instance_id = instance_id
        self._state = state
        self._redactor = redactor
        self._memory_revision = memory_revision
        self._clock = clock or SystemClock()
        self._broker = broker
        self.interrupt = interrupt or threading.Event()
        self._lock = threading.Lock()
        self._records: dict[str, QueryRecord] = {}
        self._active: str | None = None
        self._cleanup_failed = False
        self._closing = False
        self._admission = True
        self._send_buffers: dict[str, bytes] = {}
        self._sends: dict[int, SendLease] = {}
        self._cv = threading.Condition(self._lock)
        self.send_barriers: dict[str, str] = {}

    def protection_version(self) -> str:
        return self._redactor.protection_version

    def current_memory_revision(self) -> str:
        path = self._state.path / "db.sqlite3"
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)
        try:
            conn.execute("PRAGMA query_only=ON")
            row = conn.execute(
                "SELECT revision FROM memory_revision WHERE singleton=1"
            ).fetchone()
        except sqlite3.Error:
            raise ConsoleError("database_unavailable") from None
        finally:
            conn.close()
        if row is None:
            return str(self._memory_revision())
        if type(row[0]) is not int or row[0] < 0:
            raise ConsoleError("database_unavailable")
        return str(row[0])

    def pause_admission(self) -> None:
        with self._lock:
            self._admission = False

    def resume_admission_if_clean(self) -> None:
        available, _, _ = self._capability()
        with self._lock:
            if available and not self._cleanup_failed and not self._closing:
                self._admission = True

    def released(self) -> bool:
        with self._lock:
            return self._released_locked()

    def _released_locked(self) -> bool:
        if self._active is not None:
            return False
        if self._sends or self._send_buffers:
            return False
        for record in self._records.values():
            if record.response_owner is not None:
                return False
            if record.process is not None and record.process.poll() is None:
                return False
            if record.cleanup == "failed":
                return False
            if (
                record.status not in {"unused", "expired"}
                and record.cleanup != "released"
            ):
                return False
        return True

    def begin_send(self, query_id: str, body: bytes) -> SendLease:
        lease = SendLease(query_id=query_id, body=body)
        current_memory = self.current_memory_revision()
        current_protection = self.protection_version()
        with self._lock:
            record = self._records.get(query_id)
            if record is not None:
                stale = (
                    record.memory_revision not in (None, current_memory)
                    or record.protection_version not in (None, current_protection)
                    or record.instance_id != self.instance_id
                )
                if stale and record.status == "completed":
                    record.status = "invalidated"
                    record.error = "data_invalidated"
                if (
                    record.status in {"invalidated", "cancelled", "failed"}
                    or record.error in {"data_invalidated", "query_cancelled"}
                    or stale
                ):
                    lease.cancel.set()
            self._sends[id(lease)] = lease
            self._send_buffers[query_id] = body
        self._send_gate(lease)
        return lease

    def finish_send(self, lease: SendLease, *, delivered: bool = False) -> None:
        with self._cv:
            lease.body = b""
            self._sends.pop(id(lease), None)
            remaining = any(
                item.query_id == lease.query_id for item in self._sends.values()
            )
            if not remaining:
                self._send_buffers.pop(lease.query_id, None)
            record = self._records.get(lease.query_id)
            if record is not None and record.status == "completed":
                record.result_delivered = delivered
            lease.done.set()
            self._cv.notify_all()

    def _send_gate(self, lease: SendLease) -> None:
        ready = self.send_barriers.get("ready")
        hold = self.send_barriers.get("hold")
        if ready:
            signal = os.open(ready, os.O_WRONLY)
            os.close(signal)
        if hold and not lease.cancel.is_set():
            waiter = os.open(hold, os.O_RDONLY)
            os.close(waiter)

    def _abort_sends(self) -> None:
        with self._lock:
            leases = list(self._sends.values())
        for lease in leases:
            lease.cancel.set()
            sock = lease.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        hold = self.send_barriers.get("hold")
        if hold:
            try:
                os.close(os.open(hold, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass

    def catalog(self) -> tuple[int, dict[str, Any]]:
        protection = self.protection_version()
        available, reason, schema = self._capability()
        revision = (
            self.current_memory_revision()
            if available else str(self._memory_revision())
        )
        if protection != self.protection_version():
            # Never label strings protected under an older version as current.
            available, reason = False, "database_unavailable"
            schema = {"compatible": False, "migrations": []}
        body = {
            "instance_id": self.instance_id,
            "available": available,
            "reason": reason,
            "schema": schema,
            "memory_revision": revision,
            "protection_version": protection,
            "objects": manifest(),
            "limits": limits(),
        }
        return 200, body

    def issue(self, body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        if body:
            raise ConsoleError("invalid_request")
        if not self._admission_open():
            raise ConsoleError(
                "cleanup_failed" if self._cleanup_failed else "database_unavailable"
            )
        available, reason, _schema = self._capability()
        if not available:
            raise ConsoleError(reason or "database_unavailable")
        now = self._clock.monotonic()
        with self._lock:
            self._prune(now)
            live = [
                record
                for record in self._records.values()
                if record.status == "unused" or record.terminal_at is not None
            ]
            if len(live) >= HANDLE_CAPACITY:
                raise ConsoleError("resource_limit")
            query_id = secrets.token_urlsafe(18)
            record = QueryRecord(
                query_id=query_id,
                created=now,
                expires=now + HANDLE_TTL_S,
                instance_id=self.instance_id,
            )
            self._records[query_id] = record
        return 200, {
            "query_id": query_id,
            "instance_id": self.instance_id,
            "expires_in_s": int(HANDLE_TTL_S),
        }

    def execute(
        self, query_id: str, body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        if set(body) - {"instance_id", "memory_revision", "protection_version", "sql"}:
            raise ConsoleError("invalid_request")
        sql = body.get("sql")
        if type(sql) is not str:
            raise ConsoleError("invalid_request")
        try:
            if len(sql.encode("utf-8")) > SQL_TEXT_LIMIT:
                raise ConsoleError("invalid_request")
        except UnicodeError:
            raise ConsoleError("invalid_request") from None
        if body.get("instance_id") != self.instance_id:
            raise ConsoleError("instance_changed")
        current_memory = self.current_memory_revision()
        current_protection = self.protection_version()
        if (
            str(body.get("memory_revision")) != current_memory
            or str(body.get("protection_version")) != current_protection
        ):
            raise ConsoleError("data_invalidated")
        available, reason, _schema = self._capability()
        if not available:
            raise ConsoleError(reason or "database_unavailable")
        now = self._clock.monotonic()
        with self._lock:
            self._prune(now)
            record = self._require(query_id, now)
            if record.cancel_before_execute:
                record.status = "cancelled"
                record.terminal_at = now
                raise ConsoleError("query_cancelled", query_id=query_id)
            if record.used:
                raise ConsoleError("handle_used", query_id=query_id)
            if self._active is not None:
                raise ConsoleError("database_busy")
            if not self._admission:
                raise ConsoleError(
                    "cleanup_failed" if self._cleanup_failed else "database_unavailable"
                )
            record.used = True
            record.response_owner = threading.get_ident()
            record.status = "running"
            record.cleanup = "pending"
            record.memory_revision = current_memory
            record.protection_version = current_protection
            record.stop.clear()
            self._active = query_id
        deadline = now + EXECUTE_BUDGET_S
        try:
            payload = self._run_worker(
                record, sql, deadline, current_memory, current_protection
            )
        except ConsoleError as exc:
            alive = record.process is not None and record.process.poll() is None
            if record.cleanup != "failed" and not alive:
                self._finish(record, exc.code)
            raise ConsoleError(
                "cleanup_failed" if record.cleanup == "failed" else exc.code,
                query_id=query_id,
            ) from None
        if (
            self.current_memory_revision() != current_memory
            or self.protection_version() != current_protection
        ):
            self._finish(record, "data_invalidated")
            raise ConsoleError("data_invalidated", query_id=query_id)
        payload = {
            **payload,
            "query_id": query_id,
            "instance_id": self.instance_id,
            "memory_revision": current_memory,
            "protection_version": current_protection,
        }
        if len(dump(payload)) > RESULT_JSON_LIMIT:
            self._finish(record, "result_too_large")
            raise ConsoleError("result_too_large", query_id=query_id)
        if self._clock.monotonic() >= deadline:
            self._finish(record, "query_timeout")
            raise ConsoleError("query_timeout", query_id=query_id)
        self._finish(record, None, payload)
        return 200, payload

    def cancel(
        self, query_id: str, body: dict[str, Any] | None
    ) -> tuple[int, dict[str, Any]]:
        if body:
            raise ConsoleError("invalid_request")
        now = self._clock.monotonic()
        with self._lock:
            self._prune(now)
            record = self._require(query_id, now)
            if record.status in {"completed"}:
                record.completed_before_cancel = True
                return 200, {
                    "query_id": query_id,
                    "status": "completed",
                    "cleanup": record.cleanup,
                }
            if record.status == "unused":
                record.cancel_before_execute = True
                record.status = "cancelled"
                record.used = True
                record.terminal_at = now
                return 200, {
                    "query_id": query_id,
                    "status": "cancelled",
                    "cleanup": "released",
                }
            record.stop.set()
            record.status = "stopping"
            record.cleanup = "pending"
            process = record.process
        deadline = self._clock.monotonic() + CANCEL_BUDGET_S
        self._kill(record, process)
        with self._cv:
            while record.cleanup == "pending":
                remaining = deadline - self._clock.monotonic()
                if remaining <= 0:
                    record.cleanup = "failed"
                    record.status = "failed"
                    record.error = "cleanup_failed"
                    self._cleanup_failed = True
                    self._admission = False
                    break
                self._cv.wait(remaining)
        return 200, {
            "query_id": query_id,
            "status": record.status,
            "cleanup": record.cleanup,
        }

    def status(self, query_id: str) -> tuple[int, dict[str, Any]]:
        now = self._clock.monotonic()
        with self._lock:
            self._prune(now)
            record = self._records.get(query_id)
            if record is None:
                raise ConsoleError("not_found")
            if record.status == "unused" and now >= record.expires:
                record.status = "expired"
            if record.status == "expired":
                raise ConsoleError("handle_expired", query_id=query_id)
            body = {
                "query_id": query_id,
                "instance_id": self.instance_id,
                "status": record.status,
                "cleanup": record.cleanup,
            }
            if record.completed_before_cancel:
                body["completed_before_cancel"] = True
            if record.status == "completed":
                body["result_delivered"] = record.result_delivered
            return 200, body

    def invalidate(self, reason: str = "data_invalidated") -> None:
        now = self._clock.monotonic()
        with self._lock:
            active = self._active
            records = list(self._records.values())
            pending = set(self._send_buffers)
            pending.update(lease.query_id for lease in self._sends.values())
        self._abort_sends()
        for record in records:
            if record.query_id in pending and record.status == "completed":
                with self._lock:
                    record.status = "invalidated"
                    record.error = reason
                    record.terminal_at = now
                    record.cleanup = "released"
            active_status = record.status in {"running", "prepared", "stopping"}
            if active_status or record.query_id == active or record.process is not None:
                self._kill(record, record.process, reason=reason)
        self._publish_protection()

    def invalidate_and_wait(self) -> None:
        self.invalidate()
        deadline = self._clock.monotonic() + CANCEL_BUDGET_S
        with self._cv:
            while self._clock.monotonic() < deadline:
                if self._released_locked():
                    self._cleanup_failed = False
                    return
                remaining = deadline - self._clock.monotonic()
                self._cv.wait(timeout=max(0.0, remaining))
            if not self._released_locked():
                self._cleanup_failed = True
                self._admission = False
                for record in self._records.values():
                    if record.response_owner is not None or record.process is not None:
                        record.cleanup = "failed"
                        record.status = "failed"
                        record.error = "cleanup_failed"

    def shutdown(self) -> bool:
        with self._lock:
            self._closing = True
            self._admission = False
        self.invalidate_and_wait()
        return self.released()

    def note_writer(self) -> None:
        self.interrupt.set()
        with self._lock:
            active = self._active
            record = self._records.get(active) if active else None
            process = record.process if record else None
        if record is not None:
            self._kill(record, process, reason="database_busy")
        self.interrupt.clear()

    def _admission_open(self) -> bool:
        with self._lock:
            return self._admission and not self._cleanup_failed

    def _require(self, query_id: str, now: float) -> QueryRecord:
        record = self._records.get(query_id)
        if record is None:
            raise ConsoleError("not_found")
        if record.instance_id != self.instance_id:
            raise ConsoleError("instance_changed", query_id=query_id)
        if record.status == "unused" and now >= record.expires:
            record.status = "expired"
            if record.terminal_at is None:
                record.terminal_at = now
        if record.status == "expired":
            raise ConsoleError("handle_expired", query_id=query_id)
        return record

    def _prune(self, now: float) -> None:
        drop = []
        for query_id, record in self._records.items():
            if record.status == "unused" and now >= record.expires:
                record.status = "expired"
                if record.terminal_at is None:
                    record.terminal_at = now
            if (
                record.terminal_at is not None
                and now - record.terminal_at >= TERMINAL_TTL_S
            ):
                drop.append(query_id)
        for query_id in drop:
            record = self._records[query_id]
            if (
                query_id != self._active
                and record.process is None
                and record.owner is None
                and record.response_owner is None
                and record.cleanup == "released"
                and query_id not in self._send_buffers
                and not any(s.query_id == query_id for s in self._sends.values())
            ):
                self._records.pop(query_id, None)
                self._send_buffers.pop(query_id, None)

    def _capability(self) -> tuple[bool, str | None, dict[str, Any]]:
        schema: dict[str, Any] = {"compatible": False, "migrations": []}
        if self._cleanup_failed:
            return False, "cleanup_failed", schema
        if not WORKER.is_file() or not hard_heap_limit_supported():
            return False, "database_unavailable", schema
        try:
            path = self._state.path / "db.sqlite3"
            if not path.is_file():
                return False, "database_unavailable", schema
            conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)
            try:
                conn.execute("PRAGMA query_only=ON")
                conn.execute("BEGIN")
                protection = self.protection_version()
                migrations = []
                for version, applied in _source_rows(
                    conn,
                    "SELECT version, applied_at "
                    "FROM schema_migrations ORDER BY version",
                ):
                    if (
                        type(version) is not int or version < 1
                        or type(applied) is not str
                    ):
                        raise ConsoleError("data_invalid")
                    migrations.append({
                        "version": version,
                        "applied_at": _protect(self._redactor, applied, identity=False),
                    })
                if protection != self.protection_version():
                    raise ConsoleError("data_invalidated")
                memory_temp_store(conn)
                compatible = compatible_schema(conn) and required_functions_available(
                    conn
                )
            finally:
                conn.close()
        except OSError, sqlite3.Error, ConsoleError, UnicodeError:
            return False, "database_unavailable", schema
        schema["migrations"] = migrations
        schema["compatible"] = compatible
        if not schema["compatible"]:
            return False, "database_unavailable", schema
        return True, None, schema

    def _db_identity(self) -> tuple[str, tuple[int, int]]:
        path = self._state.path / "db.sqlite3"
        lease = self._state.open_regular(
            PurePath("db.sqlite3"), access="read", create=False, role="SQLite database"
        )
        try:
            lease.verify_identity()
            stat = os.stat(path)
            return str(path), (stat.st_dev, stat.st_ino)
        finally:
            lease.close()

    def _run_worker(
        self,
        record: QueryRecord,
        sql: str,
        deadline: float,
        memory_revision: str,
        protection_version: str,
    ) -> dict[str, Any]:
        remaining = deadline - self._clock.monotonic()
        if remaining <= 0:
            raise ConsoleError("query_timeout")
        if record.stop.is_set():
            raise ConsoleError(record.error or "query_cancelled")
        path, identity = self._db_identity()
        if record.stop.is_set():
            raise ConsoleError(record.error or "query_cancelled")
        request = {
            "sql": sql,
            "db_path": path,
            "identity": list(identity),
            "secrets": list(self._redactor.secrets),
            "min_length": self._redactor.min_length,
            "remaining_s": remaining,
            "read_at": format_instant(self._clock.wall_utc()),
            "query_id": record.query_id,
            "instance_id": self.instance_id,
            "memory_revision": memory_revision,
            "protection_version": protection_version,
            "barriers": record.barriers,
        }
        raw = json.dumps(
            request, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
        process = subprocess.Popen.__new__(subprocess.Popen)
        with self._lock:
            record.owner = process
        try:
            subprocess.Popen.__init__(
                process,
                [sys.executable, str(WORKER)],
                env={},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except BaseException as exc:
            if getattr(process, "pid", None) is not None:
                with self._lock:
                    record.process = process
                self._kill(record, process, reason="database_unavailable")
            else:
                with self._lock:
                    record.owner = None
            if isinstance(exc, (OSError, KeyboardInterrupt)):
                raise ConsoleError("database_unavailable") from None
            raise
        with self._lock:
            record.process = process
            record.owner = process
            record.cleanup = "pending"
        if record.stop.is_set():
            reason = record.error or "query_cancelled"
            self._kill(record, process, reason=reason)
            raise ConsoleError(reason)
        try:
            output, _err = process.communicate(
                raw, timeout=max(0.001, deadline - self._clock.monotonic())
            )
        except subprocess.TimeoutExpired:
            self._kill(record, process, reason="query_timeout")
            raise ConsoleError("query_timeout") from None
        except OSError, ValueError:
            # A concurrent stop can close a pipe after communicate selected its
            # fd. Preserve the recorded cancellation, not an incidental EBADF.
            with self._lock:
                stopped = record.error if record.stop.is_set() else None
            if stopped not in {
                "query_cancelled", "query_timeout", "data_invalidated", "cleanup_failed"
            }:
                raise
            self._reap(record, process, stopped)
            raise ConsoleError(stopped) from None
        with self._lock:
            stopped = record.error
            status = record.status
        if stopped in {
            "database_busy",
            "query_cancelled",
            "data_invalidated",
            "query_timeout",
        }:
            self._reap(record, process, stopped)
            raise ConsoleError(stopped)
        if status in {"cancelled", "timed_out", "invalidated"}:
            raise ConsoleError(
                {
                    "cancelled": "query_cancelled",
                    "timed_out": "query_timeout",
                    "invalidated": "data_invalidated",
                }[status]
            )
        if process.returncode != 0:
            self._reap(record, process, "failed")
            raise ConsoleError("sql_error")
        try:
            payload = json.loads(output)
        except ValueError:
            self._reap(record, process, "failed")
            raise ConsoleError("data_invalid") from None
        if not isinstance(payload, dict):
            self._reap(record, process, "failed")
            raise ConsoleError("data_invalid")
        if "error" in payload:
            code = payload["error"]
            self._reap(
                record, process, code if code != "query_cancelled" else "cancelled"
            )
            raise ConsoleError(
                code
                if code
                in {
                    "invalid_request",
                    "sql_rejected",
                    "sql_error",
                    "database_busy",
                    "handle_expired",
                    "handle_used",
                    "instance_changed",
                    "database_unavailable",
                    "input_too_large",
                    "result_too_large",
                    "resource_limit",
                    "query_timeout",
                    "query_cancelled",
                    "data_invalidated",
                    "data_invalid",
                    "protected_identity",
                    "cleanup_failed",
                }
                else "sql_error"
            )
        self._reap(record, process, "completed")
        return payload

    def _kill(
        self,
        record: QueryRecord,
        process: subprocess.Popen | None,
        reason: str = "query_cancelled",
    ) -> None:
        record.stop.set()
        if process is None:
            with self._lock:
                record.stop.set()
                if record.response_owner is None and record.owner is None:
                    record.status = {
                        "query_cancelled": "cancelled",
                        "query_timeout": "timed_out",
                        "data_invalidated": "invalidated",
                    }.get(reason, "failed")
                    record.cleanup = "released"
                    record.error = reason
                    if self._active == record.query_id:
                        self._active = None
                    self._cv.notify_all()
                    return
                if record.cleanup != "failed":
                    record.status = "stopping"
                    record.cleanup = "pending"
                    record.error = reason
            return
        with self._lock:
            if record.error is None:
                record.error = reason
                if reason != "completed":
                    record.status = "stopping"
        deadline = self._clock.monotonic() + CANCEL_BUDGET_S
        try:
            if process.poll() is None:
                os.killpg(os.getpgid(process.pid), 9)
        except OSError:
            self._fail_cleanup(record, process)
            return
        try:
            process.wait(timeout=max(0.0, deadline - self._clock.monotonic()))
        except subprocess.TimeoutExpired:
            self._fail_cleanup(record, process)
            return
        self._reap(record, process, reason)

    def _fail_cleanup(self, record: QueryRecord, process: subprocess.Popen) -> None:
        with self._lock:
            # A concurrent owner may have completed the real release while this
            # stop/pipe operation was in flight. Its late error owns no state.
            if record.process is not process and record.owner is not process:
                return
            self._cleanup_failed = True
            self._admission = False
            record.cleanup = "failed"
            record.status = "failed"
            record.error = "cleanup_failed"
            self._cv.notify_all()

    def _reap(
        self, record: QueryRecord, process: subprocess.Popen, status: str
    ) -> None:
        try:
            if process.poll() is None:
                raise OSError("worker is still running")
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()
        except OSError:
            self._fail_cleanup(record, process)
            return
        with self._lock:
            record.process = None
            record.owner = None
            record.cleanup = (
                "pending" if record.response_owner is not None else "released"
            )
            if status == "completed" and record.stop.is_set():
                status = record.error or "query_cancelled"
            record.status = {
                "query_timeout": "timed_out",
                "query_cancelled": "cancelled",
                "data_invalidated": "invalidated",
                "completed": "completed",
            }.get(status, "failed")
            record.terminal_at = self._clock.monotonic()
            if self._active == record.query_id and record.response_owner is None:
                self._active = None
            record.error = None if status == "completed" else status
            self._cleanup_failed = any(
                item.cleanup == "failed" for item in self._records.values()
            )
            self._cv.notify_all()
        if not self._cleanup_failed and not self._closing and not self._admission:
            self.resume_admission_if_clean()

    def _finish(
        self,
        record: QueryRecord,
        error: str | None,
        result: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            if record.cleanup == "failed":
                self._cv.notify_all()
                return
            alive = record.process is not None and record.process.poll() is None
            if alive:
                self._cleanup_failed = True
                self._admission = False
                record.cleanup = "failed"
                record.status = "failed"
                record.error = "cleanup_failed"
                self._cv.notify_all()
                return
            if error is None and record.stop.is_set():
                error = record.error or "data_invalidated"
                result = None
            record.result = None
            record.result_delivered = False
            record.error = error
            if error:
                record.status = {
                    "query_timeout": "timed_out",
                    "query_cancelled": "cancelled",
                    "data_invalidated": "invalidated",
                }.get(error, "failed")
            else:
                record.status = "completed"
            record.cleanup = (
                "pending" if record.response_owner is not None else "released"
            )
            record.terminal_at = self._clock.monotonic()
            if self._active == record.query_id and record.response_owner is None:
                self._active = None
            if result is not None:
                self._send_buffers[record.query_id] = json.dumps(
                    result, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                ).encode()
            self._cv.notify_all()

    def release_response(self, query_id: str) -> None:
        """Called only after the owning HTTP handler dropped all body references."""
        with self._cv:
            record = self._records.get(query_id)
            if record is None or record.response_owner != threading.get_ident():
                return
            record.response_owner = None
            self._send_buffers.pop(query_id, None)
            if record.process is None and record.owner is None:
                record.cleanup = "released"
                if self._active == query_id:
                    self._active = None
            self._cleanup_failed = any(
                item.cleanup == "failed" for item in self._records.values()
            )
            self._cv.notify_all()
        if not self._cleanup_failed and not self._closing and not self._admission:
            self.resume_admission_if_clean()

    def drop_send_buffer(self, query_id: str) -> None:
        with self._cv:
            self._send_buffers.pop(query_id, None)
            self._cv.notify_all()

    def _publish_protection(self) -> None:
        if self._broker is None or not hasattr(
            self._broker, "publish_protection_patch"
        ):
            return
        self._broker.publish_protection_patch(
            {
                "schema_version": 1,
                "process_instance_id": self.instance_id,
                "protection_version": self.protection_version(),
            }
        )


class DiagnosticProjection:
    def invalidate(self, connection, *, memories, isolated):
        del connection, memories, isolated
        return (DIAGNOSTIC_TARGET,)


class ConsoleCleanupPort:
    def __init__(self, console: DatabaseConsole, inner):
        self._console = console
        self._inner = inner

    def verify(self, target_id: str, generation: int) -> bool:
        if target_id == DIAGNOSTIC_TARGET:
            return self._console.released()
        return self._inner.verify(target_id, generation)

    def rebuild(self, target_id: str, generation: int) -> None:
        if target_id == DIAGNOSTIC_TARGET:
            self._console.invalidate_and_wait()
            return
        self._inner.rebuild(target_id, generation)
