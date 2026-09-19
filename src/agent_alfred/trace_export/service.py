"""Host-owned single export, including worker, send and cleanup lifetimes."""

import errno
import hmac
import json
import os
import secrets
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePath

from agent_alfred.managed_state import (
    ManagedPathSecurityError,
    ManagedStateDirectory,
    ManagedStateLease,
)
from agent_alfred.resource_rollback import ResumableRollback, thread_exit_confirmed
from agent_alfred.trace_export.archive import OutputFile, build
from agent_alfred.trace_export.errors import DETAILS, ExportError
from agent_alfred.trace_export.leases import BundleLeases
from agent_alfred.trace_export.source import CHUNK, Source

TARGET = "trace-export:managed"


@dataclass
class Task:
    task_id: str
    run_id: str
    mode: str
    version: tuple
    accepted: float
    state: str = "generating"
    reason: str | None = None
    token: str | None = None
    ready_at: float = 0
    send_at: float = 0
    resources: ResumableRollback = field(default_factory=ResumableRollback)
    source: object = None
    thread: object = None
    directory: object = None
    output: object = None
    report: dict = field(default_factory=dict)
    done: threading.Event = field(default_factory=threading.Event)
    cleanup: str = "pending"
    removed: bool = False
    wake: threading.Event = field(default_factory=threading.Event)
    generating: bool = False
    sending: bool = False
    sender: object = None
    sent_all: bool = False


class RecoveredDirectory:
    """Retain verified ownership even when unlink/rmdir only partly succeeds."""

    def __init__(self, root, directory, resources):
        self.root, self.directory, self.resources = root, directory, resources
        self.removed = False

    def close(self):
        if not self.removed:
            for member in ("archive.zip", "owner.json"):
                self.directory.unlink_regular(PurePath(member), missing_ok=True)
            removed = self.root.remove_directory_if_owned(self.directory)
            if not removed and os.fstat(self.directory.fd).st_nlink:
                raise OSError("owned directory not removed")
            self.removed = True
        if not self.resources.retry():
            raise OSError("recovery handles pending")


class TraceExports:
    def __init__(self, host, state, root):
        self.host = host
        self.root = Path(root)
        self.state_path = (
            state.path if isinstance(state, ManagedStateLease) else Path(state)
        )
        self._borrowed_state = state if isinstance(state, ManagedStateLease) else None
        self.instance = host._process_instance_id
        self.clock = host._clock
        self.redactor = host._redactor
        self.leases = BundleLeases()
        self._lock = threading.RLock()
        self._task = None
        self._closing = False
        self._root = None
        self._startup = ResumableRollback()
        self._recovery = ResumableRollback()
        self._startup_failed = False
        # Publish a close-capable owner before acquiring any resources.
        host.trace_exports = self
        self._initialize()

    def _initialize(self):
        try:
            if self._root is not None:
                self._recover()
                self._startup_failed = False
                return
            if not self._startup.retry():
                raise OSError("startup cleanup pending")
            state_lease = self._borrowed_state or ManagedStateDirectory.acquire(
                self.state_path, _rollback=self._startup
            )
            self._root = state_lease.ensure_directory(
                PurePath("trace-exports"), _rollback=self._startup
            )
            self._recover()
            self._startup_failed = False
        except OSError, ManagedPathSecurityError:
            self._startup_failed = True

    def _recover(self):
        if not self._recovery.retry():
            raise OSError("recovery pending")
        # Only exact feature names with the fixed ownership marker are eligible.
        for name in os.listdir(self._root.fd):
            import re

            if not re.fullmatch(r"export-[0-9a-f]{32}", name):
                continue
            owner = ResumableRollback()
            try:
                directory = self._root.open_directory(PurePath(name), _rollback=owner)
                names = set(os.listdir(directory.fd))
                if (
                    not names <= {"owner.json", "archive.zip"}
                    or "owner.json" not in names
                ):
                    continue
                marker = directory.open_regular(
                    PurePath("owner.json"), access="read", create=False, _rollback=owner
                )
                if (
                    os.pread(marker.fd, 1024, 0)
                    != b'{"feature":"trace-export","schema":1}\n'
                ):
                    continue
                marker.close()
                self._recovery.own(RecoveredDirectory(self._root, directory, owner))
                owner = ResumableRollback()
                if not self._recovery.retry():
                    raise OSError("recovery pending")
            finally:
                if not owner.retry():
                    self._recovery.own(owner)
                    raise OSError("cleanup failed")

    @contextmanager
    def boundary(self):
        # Memory commit and secret registration share these exact boundaries.
        with self.host._store.reading() as conn, self.redactor.protection_guard():
            revision = conn.execute(
                "SELECT revision FROM memory_revision WHERE singleton=1"
            ).fetchone()[0]
            yield (self.instance, revision, self.redactor.protection_version, 1, 1)

    def _facts(self, run_id):
        snapshot = self.host.snapshot()
        active = snapshot.active_run
        with self.host._store.reading() as conn:
            row = conn.execute(
                "SELECT phase,outcome,telemetry IS NOT NULL, "
                "telemetry -> '$.trace_incomplete' FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ExportError("unknown_run")
            if conn.execute(
                "SELECT 1 FROM trace_prunes WHERE run_id=?", (run_id,)
            ).fetchone():
                raise ExportError("trace_pruned")
        same = active is not None and active.run_id == run_id
        recording = (
            active.recording_state if same else "recorded" if row[2] else "unknown"
        )
        if same and recording == "pending":
            raise ExportError("not_stopped")
        if row[0] != "finished" and not (same and recording == "failed"):
            raise ExportError("not_stopped")
        # Writer-side retirement must be affirmative for any current-process bundle.
        trace_incomplete = json.loads(row[3]) if row[3] else None
        return {
            "persistent_phase": row[0],
            "persistent_outcome": row[1],
            "recording_state": recording,
            "trace_incomplete": trace_incomplete,
            "current_outcome": active.outcome if same else None,
        }

    def start(self, run_id, mode="share", instance_id=None):
        if (
            not isinstance(run_id, str)
            or not run_id
            or mode not in {"share", "diagnostic"}
        ):
            raise ExportError("invalid_request")
        if instance_id is not None and instance_id != self.instance:
            raise ExportError("invalidated")
        facts = self._facts(run_id)
        with self.boundary() as version, self._lock:
            if self._closing:
                raise ExportError("shutting_down")
            if self._startup_failed:
                self._initialize()
                if self._startup_failed:
                    raise ExportError("cleanup_failed")
            if self._task and self._task.cleanup != "released":
                raise ExportError("busy")
            task = Task(
                secrets.token_hex(16), run_id, mode, version, self.clock.monotonic()
            )
            # The registered task owns acquisition even if its return is interrupted.
            self._task = task
            try:
                self.leases.acquire(run_id, task.task_id)
                task.thread = threading.Thread(
                    target=self._generate,
                    args=(task, facts),
                    name="trace-export",
                    daemon=True,
                )
                task.generating = True
                task.thread.start()
            except BaseException:
                from agent_alfred.resource_rollback import thread_start_effect_happened

                task.reason = "io_failed"
                task.wake.set()
                if task.thread is None or not thread_start_effect_happened(task.thread):
                    task.generating = False
                    task.thread = None
                self._cleanup(task)
                raise
            return self._view(task)

    def _check_locked(self, task, version):
        if task.reason:
            raise ExportError(task.reason)
        if task.version != version:
            task.reason = "invalidated"
            raise ExportError("invalidated")
        now = self.clock.monotonic()
        code = None
        if task.generating and now - task.accepted >= 60:
            code = "generation_timeout"
        elif task.sending and now - task.send_at >= 120:
            code = "download_timeout"
        elif task.state == "ready" and now - task.ready_at >= 300:
            code = "expired"
        if code:
            task.reason = code
            raise ExportError(code)

    def check(self, task):
        with self.boundary() as version, self._lock:
            self._check_locked(task, version)

    def _generate(self, task, facts):
        try:
            task.source = Source(
                self.root, task.run_id, task.resources, lambda: self.check(task)
            )
            if task.source.meta["process_instance_id"] == self.instance:
                writers = [s for s in self.host._fanout.sinks if s.name == "trace"]
                if not writers or not all(
                    getattr(s, "run_stopped", lambda _: False)(task.run_id)
                    for s in writers
                ):
                    raise ExportError("not_stopped")
            task.directory = self._root.create_directory(
                PurePath("export-" + task.task_id),
                role="trace export",
                _rollback=task.resources,
            )
            task.directory.create_bytes(
                PurePath("owner.json"),
                b'{"feature":"trace-export","schema":1}\n',
                _rollback=task.resources,
            )
            task.output = task.directory.open_regular(
                PurePath("archive.zip"),
                access="read_write",
                create=True,
                _rollback=task.resources,
            )
            # The stream borrows the managed descriptor; task owns its close.
            with OutputFile(task.output.fd, "wb", closefd=False) as output:
                report = build(
                    task.source,
                    output,
                    task.mode,
                    self.redactor,
                    self.state_path,
                    facts,
                    lambda: self.check(task),
                )
                output.flush()
                os.fsync(output.fileno())
            with self.boundary() as version, self._lock:
                self._check_locked(task, version)
                try:
                    task.source.verify_identity()
                except ManagedPathSecurityError:
                    raise ExportError("source_changed") from None
                task.report = report
                task.state = "ready"
                task.ready_at = self.clock.monotonic()
                task.token = secrets.token_urlsafe(32)
                task.generating = False
        except ExportError as exc:
            task.reason = task.reason or exc.code
        except ManagedPathSecurityError:
            task.reason = task.reason or "unsafe_source"
        except ValueError, KeyError, TypeError, UnicodeError:
            task.reason = task.reason or "corrupt_trace"
        except OSError as exc:
            task.reason = task.reason or (
                "disk_full" if exc.errno == errno.ENOSPC else "io_failed"
            )
        except BaseException:
            task.reason = task.reason or "io_failed"
        finally:
            with self._lock:
                task.generating = False
            if task.reason:
                self._cleanup(task)
            task.done.set()
        # Keep a bounded owner alive for unattended ready expiry and missed notices.
        while task.cleanup != "released" and not task.reason:
            task.wake.wait(0.2)
            task.wake.clear()
            try:
                self.check(task)
            except ExportError as exc:
                task.reason = task.reason or exc.code
                self._cleanup(task)

    def _cleanup(self, task):
        with self._lock:
            if task.cleanup == "released":
                return
            task.token = None
            task.state = "cleaning"
            if task.generating:
                # A finally block may itself be interrupted. Reap a completed
                # native thread, including start-before-_started publication.
                if task.thread is threading.current_thread() or (
                    task.thread is not None
                    and not thread_exit_confirmed(task.thread, 0)
                ):
                    return
                task.generating = False
            if task.sender is not None and task.sender.gi_running:
                return
            try:
                if task.sender is not None:
                    # The only suspension is after all writes and the final
                    # version check. Preserve that monotonic fact across close.
                    if task.sender.gi_suspended:
                        task.sent_all = True
                    task.sender.close()
                    task.sender = None
                if task.sending and not task.sent_all:
                    task.reason = task.reason or "io_failed"
                task.sending = False
                if task.output is not None:
                    task.output.close()
                    task.output = None
                if task.directory is not None:
                    if not task.removed:
                        for name in ("archive.zip", "owner.json"):
                            task.directory.unlink_regular(
                                PurePath(name), missing_ok=True
                            )
                        removed = self._root.remove_directory_if_owned(task.directory)
                        if not removed and os.fstat(task.directory.fd).st_nlink:
                            raise OSError("owned directory not removed")
                        task.removed = True
                    task.directory.close()
                    task.directory = None
                if not task.resources.retry():
                    raise OSError("cleanup incomplete")
                task.source = None
                self.leases.release(task.run_id, task.task_id)
                task.cleanup = "released"
                task.wake.set()
                task.state = {
                    "cancelled": "cancelled",
                    "invalidated": "invalidated",
                    "expired": "expired",
                    None: "transferred",
                }.get(task.reason, "failed")
            except BaseException:
                task.cleanup = "failed"

    def _view(self, task):
        return {
            "task_id": task.task_id,
            "instance_id": self.instance,
            "state": task.state,
            "reason": task.reason,
            "detail": DETAILS.get(task.reason, ""),
            "cleanup": task.cleanup,
            "download_token": task.token if task.state == "ready" else None,
            **task.report,
        }

    def _find(self, task_id):
        if self._task is None or self._task.task_id != task_id:
            raise ExportError("not_found")
        return self._task

    def status(self, task_id):
        with self._lock:
            task = self._find(task_id)
        if task.cleanup != "released":
            try:
                self.check(task)
            except ExportError as exc:
                task.reason = task.reason or exc.code
                self._cleanup(task)
        with self._lock:
            return self._view(task)

    def wait(self, task_id, timeout=5):
        task = self._find(task_id)
        task.done.wait(timeout)
        return self.status(task_id)

    def cancel(self, task_id):
        with self._lock:
            task = self._find(task_id)
            if task.cleanup == "released":
                return self._view(task)
            task.reason = task.reason or "cancelled"
            task.wake.set()
        self._cleanup(task)
        return self.status(task_id)

    def invalidate(self):
        with self._lock:
            task = self._task
            if task is None or task.cleanup == "released":
                return
            task.reason = task.reason or "invalidated"
            task.wake.set()
        self._cleanup(task)

    def download(self, task_id, token, write, headers=None, wait_writable=None):
        task = self._find(task_id)
        sender = self._send(task, write, headers, wait_writable)
        try:
            with self.boundary() as version, self._lock:
                self._check_locked(task, version)
                if (
                    task.state != "ready"
                    or task.sender is not None
                    or not isinstance(token, str)
                    or not task.token
                    or not hmac.compare_digest(token, task.token)
                ):
                    raise ExportError("credential_invalid")
                # This invocation owns retirement before consumption can start.
                task.sender = sender
                task.token = None
                task.state = "sending"
                task.sending = True
                task.send_at = self.clock.monotonic()
            next(sender, None)
            self.check(task)
        except ExportError as exc:
            if task.sender is sender:
                task.reason = task.reason or exc.code
            raise
        except ManagedPathSecurityError:
            if task.sender is sender:
                task.reason = task.reason or "source_changed"
            raise ExportError(task.reason or "source_changed") from None
        except OSError, TimeoutError:
            if task.sender is sender:
                task.reason = task.reason or "disconnected"
            raise ExportError(task.reason or "disconnected") from None
        except BaseException:
            if task.sender is sender:
                task.reason = task.reason or "io_failed"
            raise
        finally:
            with self._lock:
                if task.sender is sender:
                    self._cleanup(task)

    def _send(self, task, write, headers, wait_writable):
        """Synchronous IO frame; its sole yield certifies completed sending.

        While reading, writing or waiting this generator is running. Cleanup
        can observe a closed/created/suspended frame even if the caller's
        finally was interrupted, and close it before releasing its resources.
        """
        self.check(task)
        task.source.verify()
        size = task.output.stat().st_size
        if headers:
            with self.boundary() as version, self._lock:
                self._check_locked(task, version)
                headers(size)
        offset = 0
        while offset < size:
            self.check(task)
            task.output.verify_identity()
            raw = os.pread(task.output.fd, min(CHUNK, size - offset), offset)
            if not raw:
                raise ExportError("io_failed")
            try:
                with self.boundary() as version, self._lock:
                    self._check_locked(task, version)
                    count = write(raw)
                    if count is None:
                        count = len(raw)
                    if not 0 < count <= len(raw):
                        raise OSError("invalid send count")
            except BlockingIOError:
                if wait_writable is None:
                    raise
                raw = None
                wait_writable()
                continue
            offset += count
        self.check(task)
        yield

    def released(self):
        with self._lock:
            return not self._startup_failed and (
                self._task is None or self._task.cleanup == "released"
            )

    def close(self, timeout=5):
        self._closing = True
        self.invalidate()
        task = self._task
        if task and task.thread and task.thread is not threading.current_thread():
            task.thread.join(max(0, timeout))
        if task:
            self._cleanup(task)
        if self._startup_failed:
            self._initialize()
        return self.released() and self._recovery.retry() and self._startup.retry()


class ExportProjection:
    def invalidate(self, connection, *, memories, isolated):
        return (TARGET,)


class ExportCleanupPort:
    def __init__(self, exports, inner):
        self.exports, self.inner = exports, inner

    def verify(self, target_id, generation):
        return (
            self.exports.released()
            if target_id == TARGET
            else self.inner.verify(target_id, generation)
        )

    def rebuild(self, target_id, generation):
        if target_id == TARGET:
            self.exports.invalidate()
            if not self.exports.released():
                raise OSError("trace export cleanup pending")
        else:
            self.inner.rebuild(target_id, generation)
