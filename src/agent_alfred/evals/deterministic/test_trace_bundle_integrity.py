"""Issue #13 review: Run bundle resource ownership and publication.

Two ADR-0017 promises were not executable:

1. A bundle that had been circuit-broken was retired *without* its
   ``trace_fd`` ever being taken and closed, so every injected write failure
   leaked one file descriptor for the life of the process.
2. Publication checked ``target.exists()`` and then called ``os.rename()``,
   which on this platform replaces an existing (even empty) directory. The
   "no-replace atomic rename" the ADR asks for was simulated by a TOCTOU
   check, not enforced by a primitive.

The tests below fail against the pre-fix code for the reasons named in their
docstrings.
"""

from __future__ import annotations

import dis
import errno
import json
import os
import shutil
import sqlite3
import stat
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path, PurePath
from types import CodeType

import pytest

from agent_alfred import managed_state as managed_module
from agent_alfred import trace as trace_module
from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic._monitoring_test_helpers import (
    claimed_monitoring_tool,
    interrupt_instruction_once,
)
from agent_alfred.evals.deterministic._trace_test_helpers import (
    _captured_thread_exception,
    _open_fd_count,
)
from agent_alfred.events import (
    BarrierFlushResult,
    EventEnvelope,
    RunStarted,
    SequencedEvent,
    StepFinished,
    UnsequencedEvent,
)
from agent_alfred.managed_state import (
    ManagedPathSecurityError,
    ManagedStateDirectory,
)
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.resource_rollback import ResumableRollback
from agent_alfred.trace import (
    REASON_FSYNC_FAILED,
    REASON_ID_COLLISION,
    REASON_WRITE_FAILED,
    RunBundleTraceSink,
    _RunBundle,
    _storage_id,
)
from agent_alfred.wiring import build_default_host

WALL = datetime(2026, 8, 28, 12, 34, 56, tzinfo=timezone.utc)
_STAGING_NAME = ".staging-0123456789abcdef0123456789abcdef"


def test_bundle_publishes_its_retirement_owner_before_closing_resources() -> None:
    """An interrupted owner store leaves every capability reachable."""
    closed: list[str] = []

    class Resource:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            closed.append(self.name)

    resources = [Resource("trace"), Resource("artifacts"), Resource("run")]
    bundle = _RunBundle(
        "resource-owner-store",
        trace_file=resources[0],  # type: ignore[arg-type]
        artifacts_dir=resources[1],  # type: ignore[arg-type]
        run_dir=resources[2],  # type: ignore[arg-type]
    )
    code = _RunBundle.retry_resources.__code__
    instructions = tuple(dis.get_instructions(code))
    target = next(
        instruction.offset
        for instruction in instructions
        if instruction.opname in {"LOAD_ATTR", "LOAD_METHOD"}
        and instruction.argval == "retry"
    )
    control = SystemExit("retirement owner stored before close")

    with interrupt_instruction_once(code, target, control) as armed:
        with pytest.raises(SystemExit) as caught:
            bundle.retry_resources()

    assert armed == [False]
    assert caught.value is control
    assert bundle.trace_file is resources[0]
    assert bundle.artifacts_dir is resources[1]
    assert bundle.run_dir is resources[2]
    assert closed == []
    assert bundle.retry_resources() == (True, None)
    assert closed == ["trace", "artifacts", "run"]




def _trace_drain_start_boundary(*, after_effect: bool) -> int:
    instructions = tuple(dis.get_instructions(RunBundleTraceSink.__init__))
    start_load = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "LOAD_ATTR" and instruction.argval == "start"
    )
    call = next(
        index
        for index, instruction in enumerate(instructions[start_load:], start_load)
        if instruction.opname in {"CALL", "CALL_KW"}
    )
    return instructions[call + int(after_effect)].offset


def _trace_transfer_boundary(
    *, resource: str, after_effect: bool
) -> tuple[CodeType, int, int]:
    from agent_alfred.resource_rollback import ConstructionOwner

    code = ConstructionOwner.publish.__code__
    instructions = tuple(dis.get_instructions(code))
    transfer = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname in {"LOAD_ATTR", "LOAD_METHOD"}
        and instruction.argval == "transfer"
    )
    call = next(
        index
        for index, instruction in enumerate(instructions[transfer:], transfer)
        if instruction.opname in {"CALL", "CALL_KW"}
    )
    return code, instructions[call + int(after_effect)].offset, {
        "drain": 1,
        "root": 2,
    }[resource]


def _sink(tmp_path: Path) -> RunBundleTraceSink:
    return RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(tmp_path / "traces"),
        clock=FakeClock(wall=WALL),
        process_instance_id="proc-bundle",
    )


@pytest.mark.parametrize(
    ("seam", "occurrence"),
    (
        ("date", 1),
        ("staging", 1),
        ("meta", 1),
        ("initial_trace", 2),
        ("artifacts", 2),
        ("append_trace", 3),
    ),
)
def test_trace_publish_owns_each_managed_result_before_its_return(
    tmp_path, monkeypatch, seam: str, occurrence: int
) -> None:
    """Every managed capability has a caller owner across RETURN and STORE."""
    directory_type = managed_module.ManagedDirectoryLease
    file_type = managed_module.ManagedFileLease
    if seam == "date":
        code = managed_module.ManagedTraceRoot.ensure_directory.__code__
    elif seam in {"staging", "artifacts"}:
        code = directory_type.create_directory.__code__
    else:
        code = directory_type.open_regular.__code__
    target = next(
        instruction.offset
        for instruction in dis.get_instructions(code)
        if instruction.opname == "RETURN_VALUE"
    )

    # Record aggregates only so the intentionally red version can be released
    # after its leak assertion. Production cleanup still has to close them
    # before that assertion; this list is not an owner on the exercised path.
    built = []

    def record_directory(*args, **kwargs):
        lease = directory_type(*args, **kwargs)
        built.append(lease)
        return lease

    def record_file(*args, **kwargs):
        lease = file_type(*args, **kwargs)
        built.append(lease)
        return lease

    sink = _sink(tmp_path)
    monkeypatch.setattr(
        managed_module, "ManagedDirectoryLease", record_directory
    )
    monkeypatch.setattr(managed_module, "ManagedFileLease", record_file)
    baseline = _open_fd_count()
    control = SystemExit(f"{seam} returned before trace publication stored it")
    remaining = occurrence

    try:
        with claimed_monitoring_tool(
            f"trace-publish-{seam}-return", local_codes=(code,)
        ) as tool_id:

            def interrupt(actual_code: CodeType, actual_offset: int) -> None:
                nonlocal remaining
                if actual_code is not code or actual_offset != target:
                    return
                remaining -= 1
                if remaining == 0:
                    raise control

            sys.monitoring.register_callback(
                tool_id, sys.monitoring.events.INSTRUCTION, interrupt
            )
            sys.monitoring.set_local_events(
                tool_id, code, sys.monitoring.events.INSTRUCTION
            )
            with pytest.raises(SystemExit) as caught:
                sink._publish(_RunBundle(f"run-{seam}"))

        assert remaining == 0
        assert caught.value is control
        assert _open_fd_count() == baseline
    finally:
        for lease in reversed(built):
            lease.close()
        sink.close()


@pytest.mark.parametrize("failure_point", ["stat", "open", "fchmod"])
@pytest.mark.parametrize("denied_errno", [errno.EACCES, errno.EPERM])
def test_trace_publish_preserves_unverified_creation_staging_when_denied(
    tmp_path, monkeypatch, failure_point: str, denied_errno: int
) -> None:
    real_stat = managed_module.os.stat
    real_mkdir = managed_module.os.mkdir
    real_open = managed_module.os.open
    real_fchmod = managed_module.os.fchmod
    denied = False
    creation_name: str | None = None
    creation_identity: tuple[int, int] | None = None

    def observe_staging_mkdir(name, mode=0o777, *, dir_fd=None):
        nonlocal creation_identity, creation_name
        result = real_mkdir(name, mode, dir_fd=dir_fd)
        if creation_name is None and str(name).startswith(".staging-"):
            creation_name = os.fspath(name)
            info = real_stat(name, dir_fd=dir_fd, follow_symlinks=False)
            creation_identity = (info.st_dev, info.st_ino)
        return result

    def deny_staging_stat(name, *, dir_fd=None, follow_symlinks=True):
        nonlocal denied
        if failure_point == "stat" and name == creation_name and not denied:
            denied = True
            raise PermissionError(denied_errno, "injected stat denial", name)
        return real_stat(name, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    def deny_staging_open(name, flags, mode=0o777, *, dir_fd=None):
        nonlocal denied
        if failure_point == "open" and name == creation_name and not denied:
            denied = True
            raise PermissionError(denied_errno, "injected open denial", name)
        return real_open(name, flags, mode, dir_fd=dir_fd)

    def deny_staging_fchmod(fd, mode):
        nonlocal denied
        info = os.fstat(fd)
        if (
            failure_point == "fchmod"
            and (info.st_dev, info.st_ino) == creation_identity
            and not denied
        ):
            denied = True
            raise PermissionError(denied_errno, "injected fchmod denial")
        return real_fchmod(fd, mode)

    monkeypatch.setattr(managed_module.os, "mkdir", observe_staging_mkdir)
    monkeypatch.setattr(managed_module.os, "stat", deny_staging_stat)
    monkeypatch.setattr(managed_module.os, "open", deny_staging_open)
    monkeypatch.setattr(managed_module.os, "fchmod", deny_staging_fchmod)
    sink = _sink(tmp_path)
    bundle = _RunBundle("identity-read-denied")
    try:
        sink._publish(bundle)
        leftovers = list((tmp_path / "traces").rglob(".staging-*"))
        assert denied is True
        assert bundle.broken == trace_module.REASON_PUBLISH_FAILED
        assert len(leftovers) == 1
        assert leftovers[0].name == creation_name
    finally:
        sink.close()


@pytest.mark.parametrize("failure_point", ("date_fsync", "trace_reopen"))
def test_publish_failure_after_rename_closes_untransferred_capabilities(
    tmp_path, monkeypatch, failure_point: str
) -> None:
    """A successful rename transfers no lease until the bundle owns it.

    The post-rename durability/reopen window must therefore close every local
    capability on failure, while circuit-breaking only this Run.
    """
    sink = _sink(tmp_path)
    baseline = _open_fd_count()
    renamed = False
    failed = False
    real_rename = trace_module.ManagedDirectoryLease.rename_directory_no_replace
    real_fsync = trace_module.ManagedDirectoryLease.fsync
    real_open = trace_module.ManagedDirectoryLease.open_regular

    def rename(directory, source, target):
        nonlocal renamed
        result = real_rename(directory, source, target)
        renamed = True
        return result

    def fsync(directory):
        nonlocal failed
        if failure_point == "date_fsync" and renamed and not failed:
            failed = True
            raise OSError(5, "post-rename directory fsync failed")
        return real_fsync(directory)

    def open_regular(directory, relative, **kwargs):
        nonlocal failed
        if (
            failure_point == "trace_reopen"
            and renamed
            and relative == PurePath("trace.jsonl")
            and kwargs.get("access") == "append"
            and not failed
        ):
            failed = True
            raise OSError(5, "post-rename trace reopen failed")
        return real_open(directory, relative, **kwargs)

    monkeypatch.setattr(
        trace_module.ManagedDirectoryLease,
        "rename_directory_no_replace",
        rename,
    )
    monkeypatch.setattr(trace_module.ManagedDirectoryLease, "fsync", fsync)
    monkeypatch.setattr(
        trace_module.ManagedDirectoryLease, "open_regular", open_regular
    )
    broken = _RunBundle("broken-after-rename")
    healthy = _RunBundle("healthy-after-rename")
    try:
        sink._publish(broken)
        assert broken.broken == trace_module.REASON_PUBLISH_FAILED
        assert broken.run_dir is None
        assert broken.artifacts_dir is None
        assert broken.trace_file is None
        assert _open_fd_count() == baseline

        sink._publish(healthy)
        assert healthy.broken is None
        assert healthy.run_dir is not None
        assert healthy.artifacts_dir is not None
        assert healthy.trace_file is not None
    finally:
        for resource in (
            healthy.trace_file,
            healthy.artifacts_dir,
            healthy.run_dir,
        ):
            if resource is not None:
                resource.close()
        sink.close()


def test_trace_publish_retains_incomplete_cleanup_until_a_barrier_retries() -> None:
    """A broken bundle keeps the rollback whose close returned ``False``."""

    class RefusingDateLease:
        def __init__(self) -> None:
            self.close_attempts = 0
            self.closed = False

        def create_directory(self, *args, **kwargs):
            del args, kwargs
            raise FileExistsError("injected staging collision")

        def close(self) -> bool:
            self.close_attempts += 1
            if self.close_attempts == 1:
                return False
            self.closed = True
            return True

    class Root:
        def __init__(self, date_lease: RefusingDateLease) -> None:
            self.date_lease = date_lease

        def ensure_directory(self, *args, _rollback, **kwargs):
            del args, kwargs
            _rollback.own(self.date_lease)
            return self.date_lease

        def close(self) -> None:
            return None

    date_lease = RefusingDateLease()
    sink = RunBundleTraceSink(
        root=Root(date_lease),
        clock=FakeClock(wall=WALL),
        process_instance_id="proc-retained-publish-cleanup",
    )
    bundle = _RunBundle("retained-publish-cleanup")
    sink._bundles[bundle.run_id] = bundle
    try:
        sink._publish(bundle)
        assert date_lease.close_attempts == 1
        assert date_lease.closed is False

        barrier = trace_module._WriteBarrier((bundle,))
        sink._process_barrier(barrier)

        assert date_lease.close_attempts == 2
        assert date_lease.closed is True
        assert bundle.run_id not in sink._bundles
        assert barrier.done.is_set()
    finally:
        sink.close()


def test_drain_crash_answers_the_barrier_it_already_removed() -> None:
    """A drain owns a barrier before removing it from the shared queue."""
    sink = object.__new__(RunBundleTraceSink)
    barrier = trace_module._WriteBarrier(())
    sink._wake = threading.Condition()
    sink._queue = trace_module.deque([barrier])
    sink._stopping = False
    sink._bundles = {}
    control = SystemExit("barrier processing interrupted")

    def interrupt_barrier(_barrier) -> None:
        raise control

    sink._process_barrier = interrupt_barrier

    with pytest.raises(SystemExit) as caught:
        sink._drain_loop()

    assert caught.value is control
    assert barrier.done.is_set(), "the removed barrier kept no reachable answerer"
    assert barrier.failed == trace_module.REASON_SINK_FAILED


def test_drain_unwind_keeps_bundle_resources_behind_newer_cleanup() -> None:
    """Retirement pauses before resources and keeps every reference reachable."""
    trace: list[str] = []
    attempts = 0

    class Resource:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            trace.append(self.name)

    cleanup = ResumableRollback()

    def close_construction() -> bool:
        nonlocal attempts
        attempts += 1
        trace.append("construction")
        return attempts > 1

    cleanup.own(object(), close_construction)
    bundle = _RunBundle(
        "ordered-unwind",
        run_dir=Resource("run"),  # type: ignore[arg-type]
        artifacts_dir=Resource("artifacts"),  # type: ignore[arg-type]
        trace_file=Resource("trace"),  # type: ignore[arg-type]
    )
    bundle.retain_cleanup(cleanup)
    sink = object.__new__(RunBundleTraceSink)
    sink._wake = threading.Condition()
    sink._bundles = {bundle.run_id: bundle}

    sink._retry_remaining_cleanup()

    assert trace == ["construction"]
    assert bundle.trace_file is not None
    assert bundle.artifacts_dir is not None
    assert bundle.run_dir is not None
    assert sink._bundles == {bundle.run_id: bundle}

    assert sink._retry_remaining_cleanup() is True
    assert trace == [
        "construction",
        "construction",
        "trace",
        "artifacts",
        "run",
    ]
    assert bundle.trace_file is None
    assert bundle.artifacts_dir is None
    assert bundle.run_dir is None
    assert sink._bundles == {}


@pytest.mark.parametrize("block_cleanup", [False, True])
def test_trace_close_retains_incomplete_staging_cleanup(
    tmp_path, monkeypatch, block_cleanup: bool,
) -> None:
    """A failed staging close stays retryable through the public sink."""
    baseline = _open_fd_count()
    publish_attempted = threading.Event()
    cleanup_attempted = threading.Event()
    release_cleanup = threading.Event()
    if not block_cleanup:
        release_cleanup.set()
    retained_leases = []
    real_file_close = managed_module.ManagedFileLease.close

    def refuse_publish(*_args, **_kwargs):
        publish_attempted.set()
        raise PermissionError(errno.EACCES, "injected publication refusal")

    def close_cleanup_file(lease):
        if lease._role == "bundle staging entry" and lease.path.name == "meta.json":
            cleanup_attempted.set()
            if lease not in retained_leases:
                retained_leases.append(lease)
            if not release_cleanup.is_set():
                raise PermissionError(errno.EACCES, "cleanup remains unavailable")
        return real_file_close(lease)

    monkeypatch.setattr(
        managed_module.ManagedDirectoryLease,
        "rename_directory_no_replace",
        refuse_publish,
    )
    monkeypatch.setattr(managed_module.ManagedFileLease, "close", close_cleanup_file)
    sink = _sink(tmp_path)
    try:
        _commit(sink, RunStarted(purpose="chat", user_message=None), 1)
        result = sink.flush("run-bundle")
        assert publish_attempted.is_set(), "publication refusal was not reached"
        assert cleanup_attempted.is_set(), "staging cleanup was not reached"
        assert result.outcome == "failed"
        assert sink.close(timeout=5.0) is (not block_cleanup)
        release_cleanup.set()
        assert sink.close(timeout=5.0) is True
        assert _open_fd_count() == baseline
    finally:
        release_cleanup.set()
        sink.close(timeout=5.0)
        # Only rescue a deliberately red version after its ownership assertion.
        for lease in retained_leases:
            real_file_close(lease)


def test_staging_cleanup_control_cannot_skip_publication_rollback() -> None:
    """Even a staging-removal control exit must close construction leases."""
    control = SystemExit("staging cleanup interrupted")
    closed: list[str] = []

    class Staging:
        def open_regular(self, *args, **kwargs):
            del args, kwargs
            raise RuntimeError("publish failed after staging creation")

        def remove_managed_staging(self):
            raise control

        def close(self):
            closed.append("staging")

    class DateLease:
        def __init__(self, staging):
            self.staging = staging

        def create_directory(self, *args, _rollback, **kwargs):
            del args, kwargs
            _rollback.own(self.staging)
            return self.staging

        def close(self):
            closed.append("date")

    class Root:
        def __init__(self, date):
            self.date = date

        def ensure_directory(self, *args, _rollback, **kwargs):
            del args, kwargs
            _rollback.own(self.date)
            return self.date

    staging = Staging()
    date = DateLease(staging)
    sink = object.__new__(RunBundleTraceSink)
    sink._root_lease = Root(date)
    sink._clock = FakeClock(wall=WALL)
    sink._process_instance_id = "proc-staging-cleanup"
    bundle = _RunBundle("staging-cleanup-control")

    with pytest.raises(SystemExit) as caught:
        sink._publish(bundle)

    assert caught.value is control
    assert closed == ["staging", "date"]
    assert bundle.cleanup_rollback is None


@pytest.mark.parametrize("failure_point", ("date_fsync", "trace_reopen"))
def test_public_trace_pipeline_survives_post_rename_failure_and_uncertain_close(
    tmp_path, monkeypatch, failure_point: str
) -> None:
    baseline = _open_fd_count()
    sink = _sink(tmp_path)
    renamed = False
    injected_publish_failure = False
    injected_close_failure = False
    staging_fd: int | None = None
    real_rename = trace_module.ManagedDirectoryLease.rename_directory_no_replace
    real_fsync = trace_module.ManagedDirectoryLease.fsync
    real_open = trace_module.ManagedDirectoryLease.open_regular
    real_close = os.close

    def rename(directory, source, target):
        nonlocal renamed, staging_fd
        result = real_rename(directory, source, target)
        renamed = True
        staging_fd = source.fd
        return result

    def fsync(directory):
        nonlocal injected_publish_failure
        if failure_point == "date_fsync" and renamed and not injected_publish_failure:
            injected_publish_failure = True
            raise OSError(5, "post-rename directory fsync failed")
        return real_fsync(directory)

    def open_regular(directory, relative, **kwargs):
        nonlocal injected_publish_failure
        if (
            failure_point == "trace_reopen"
            and renamed
            and relative == PurePath("trace.jsonl")
            and kwargs.get("access") == "append"
            and not injected_publish_failure
        ):
            injected_publish_failure = True
            raise OSError(5, "post-rename trace reopen failed")
        return real_open(directory, relative, **kwargs)

    def close_then_raise(fd):
        nonlocal injected_close_failure
        if fd == staging_fd and not injected_close_failure:
            injected_close_failure = True
            real_close(fd)
            raise OSError(5, "staging close result was uncertain")
        return real_close(fd)

    monkeypatch.setattr(
        trace_module.ManagedDirectoryLease,
        "rename_directory_no_replace",
        rename,
    )
    monkeypatch.setattr(trace_module.ManagedDirectoryLease, "fsync", fsync)
    monkeypatch.setattr(
        trace_module.ManagedDirectoryLease, "open_regular", open_regular
    )
    monkeypatch.setattr(managed_module.os, "close", close_then_raise)
    with _captured_thread_exception() as thread_error:
        _commit(sink, RunStarted(user_message=None), 1, "failed-public-run")
        first = sink.flush("failed-public-run")
        assert first.outcome == "failed"
        assert "failed-public-run" not in sink._bundles

        _commit(sink, RunStarted(user_message=None), 2, "healthy-public-run")
        second = sink.flush("healthy-public-run")
        assert second.outcome == "flushed"
        assert "healthy-public-run" not in sink._bundles
        assert sink.close() is True
    assert thread_error == {}
    assert _open_fd_count() == baseline


def test_trace_drain_close_failure_finishes_all_resources_without_fd_reuse(
    tmp_path, monkeypatch
) -> None:
    baseline = _open_fd_count()
    sink = _sink(tmp_path)
    trace_fd: int | None = None
    failed = False
    real_open = trace_module.ManagedDirectoryLease.open_regular
    real_close = os.close

    def capture_trace(directory, relative, **kwargs):
        nonlocal trace_fd
        lease = real_open(directory, relative, **kwargs)
        if (
            trace_fd is None
            and relative == PurePath("trace.jsonl")
            and kwargs.get("access") == "append"
        ):
            trace_fd = lease.fd
        return lease

    def close_then_raise(fd):
        nonlocal failed
        if fd == trace_fd and not failed:
            failed = True
            real_close(fd)
            raise OSError(5, "trace close result was uncertain")
        return real_close(fd)

    monkeypatch.setattr(
        trace_module.ManagedDirectoryLease, "open_regular", capture_trace
    )
    monkeypatch.setattr(managed_module.os, "close", close_then_raise)
    _commit(sink, RunStarted(user_message=None), 1, "close-failed-run")
    first = sink.flush("close-failed-run")
    assert first.outcome == "failed"
    assert trace_fd is not None
    reused_fds = []
    while trace_fd not in reused_fds:
        reused_fds.append(os.open("/dev/null", os.O_RDONLY))
        assert len(reused_fds) < 64

    _commit(sink, RunStarted(user_message=None), 2, "next-run")
    assert sink.flush("next-run").outcome == "flushed"
    os.fstat(trace_fd)
    assert sink.close() is True
    for reused in reused_fds:
        real_close(reused)
    assert _open_fd_count() == baseline


def test_trace_sink_refuses_symlink_root_before_starting_drain(tmp_path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    target.chmod(0o755)
    root = tmp_path / "traces"
    root.symlink_to(target, target_is_directory=True)
    before_threads = {thread.ident for thread in threading.enumerate()}
    with pytest.raises(ManagedPathSecurityError) as caught:
        RunBundleTraceSink(
            root=ManagedStateDirectory.acquire_trace_root(root),
            clock=FakeClock(wall=WALL),
            process_instance_id="proc-symlink-root",
        )
    assert caught.value.reason == "symlink"
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert {thread.ident for thread in threading.enumerate()} == before_threads


@pytest.mark.parametrize("after_effect", (False, True), ids=("before", "after"))
def test_trace_sink_constructor_rolls_back_an_interrupted_drain_start(
    tmp_path,
    monkeypatch,
    after_effect: bool,
) -> None:
    """A constructor exit never strands its unpublished non-daemon drain."""
    failure = KeyboardInterrupt(f"trace drain {after_effect=}")
    entered = threading.Event()
    exited = threading.Event()
    sinks: list[RunBundleTraceSink] = []
    threads: list[threading.Thread] = []
    real_thread = threading.Thread
    real_drain = RunBundleTraceSink._drain_loop

    class CapturedThread(real_thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            threads.append(self)

    def observed_drain(sink: RunBundleTraceSink) -> None:
        sinks.append(sink)
        entered.set()
        try:
            real_drain(sink)
        finally:
            exited.set()

    monkeypatch.setattr(trace_module.threading, "Thread", CapturedThread)
    monkeypatch.setattr(RunBundleTraceSink, "_drain_loop", observed_drain)
    root = ManagedStateDirectory.acquire_trace_root(tmp_path / "traces")
    target = _trace_drain_start_boundary(after_effect=after_effect)

    try:
        with interrupt_instruction_once(
            RunBundleTraceSink.__init__.__code__, target, failure
        ) as armed:
            with pytest.raises(KeyboardInterrupt) as raised:
                RunBundleTraceSink(
                    root=root,
                    clock=FakeClock(wall=WALL),
                    process_instance_id="proc-interrupted-trace-start",
                )

        assert armed == [False]
        assert raised.value is failure
        assert len(threads) == 1
        if after_effect:
            assert entered.wait(2.0), "real drain target never entered"
            assert exited.wait(0), "constructor rollback left trace-drain running"
            threads[0].join(timeout=0)
        else:
            assert threads[0].ident is None
            assert entered.is_set() is False
    finally:
        for sink in sinks:
            with sink._wake:
                sink._stopping = True
                sink._wake.notify_all()
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=2.0)
        root.close()


@pytest.mark.parametrize(
    ("resource", "after_effect"),
    (
        ("drain", False),
        ("drain", True),
        ("root", False),
        ("root", True),
    ),
    ids=(
        "drain-transfer-before",
        "drain-transfer-after",
        "root-transfer-before",
        "root-transfer-after",
    ),
)
@pytest.mark.parametrize("external_rollback", (False, True), ids=("local", "external"))
def test_trace_sink_constructor_rolls_back_an_interrupted_transfer_tail(
    monkeypatch,
    resource: str,
    after_effect: bool,
    external_rollback: bool,
) -> None:
    """Every pre-return transfer boundary still has an unpublished owner."""
    failure = KeyboardInterrupt(f"trace {resource} transfer {after_effect=}")
    entered = threading.Event()
    exited = threading.Event()
    sinks: list[RunBundleTraceSink] = []
    threads: list[threading.Thread] = []
    real_thread = threading.Thread
    real_drain = RunBundleTraceSink._drain_loop
    rollback = ResumableRollback() if external_rollback else None

    class RecordingRoot:
        def __init__(self) -> None:
            self.close_attempts = 0

        def close(self) -> None:
            self.close_attempts += 1
            if self.close_attempts != 1:
                raise AssertionError("trace root ownership settled twice")

    class CapturedThread(real_thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            threads.append(self)

    def observed_drain(sink: RunBundleTraceSink) -> None:
        sinks.append(sink)
        entered.set()
        try:
            real_drain(sink)
        finally:
            exited.set()

    monkeypatch.setattr(trace_module.threading, "Thread", CapturedThread)
    monkeypatch.setattr(RunBundleTraceSink, "_drain_loop", observed_drain)
    root = RecordingRoot()
    code, target, occurrence = _trace_transfer_boundary(
        resource=resource, after_effect=after_effect
    )

    try:
        with interrupt_instruction_once(
            code, target, failure, occurrence=occurrence
        ) as armed:
            with pytest.raises(KeyboardInterrupt) as raised:
                RunBundleTraceSink(
                    root=root,
                    clock=FakeClock(wall=WALL),
                    process_instance_id="proc-interrupted-trace-transfer",
                    _rollback=rollback,
                )

        assert armed == [False]
        assert raised.value is failure
        assert len(threads) == 1
        assert entered.wait(2.0), "real drain target never entered"
        assert exited.wait(0), "transfer-tail exit left trace-drain running"
        threads[0].join(timeout=0)
        if rollback is not None:
            assert rollback.retry() is True
        assert root.close_attempts == 1
    finally:
        for sink in sinks:
            with sink._wake:
                sink._stopping = True
                sink._wake.notify_all()
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=2.0)


@pytest.mark.parametrize("external_rollback", (False, True), ids=("local", "external"))
def test_trace_transfer_failure_keeps_a_refused_root_owned_for_retry(
    monkeypatch,
    external_rollback: bool,
) -> None:
    """Cleanup refusal cannot replace the first exit or orphan the root."""
    failure = KeyboardInterrupt("trace root transfer completed before exit")
    entered = threading.Event()
    exited = threading.Event()
    sinks: list[RunBundleTraceSink] = []
    threads: list[threading.Thread] = []
    real_thread = threading.Thread
    real_drain = RunBundleTraceSink._drain_loop
    rollback = ResumableRollback() if external_rollback else None

    class RefusingRoot:
        def __init__(self) -> None:
            self.close_attempts = 0
            self.closed = False

        def close(self) -> None:
            self.close_attempts += 1
            if self.close_attempts == 1:
                raise RuntimeError("injected trace-root close refusal")
            self.closed = True

    class CapturedThread(real_thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            threads.append(self)

    def observed_drain(sink: RunBundleTraceSink) -> None:
        sinks.append(sink)
        entered.set()
        try:
            real_drain(sink)
        finally:
            exited.set()

    monkeypatch.setattr(trace_module.threading, "Thread", CapturedThread)
    monkeypatch.setattr(RunBundleTraceSink, "_drain_loop", observed_drain)
    root = RefusingRoot()
    code, target, occurrence = _trace_transfer_boundary(
        resource="root", after_effect=True
    )

    try:
        with interrupt_instruction_once(
            code, target, failure, occurrence=occurrence
        ) as armed:
            with pytest.raises(KeyboardInterrupt) as raised:
                RunBundleTraceSink(
                    root=root,
                    clock=FakeClock(wall=WALL),
                    process_instance_id="proc-refused-trace-root",
                    _rollback=rollback,
                )

        assert armed == [False]
        assert raised.value is failure
        assert entered.wait(2.0), "real drain target never entered"
        assert exited.wait(0), "root refusal left trace-drain running"
        if rollback is not None:
            assert root.closed is False
            assert rollback.retry() is True
        assert root.closed is True
        assert root.close_attempts == 2
    finally:
        for sink in sinks:
            with sink._wake:
                sink._stopping = True
                sink._wake.notify_all()
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=2.0)
        if not root.closed:
            root.close()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX managed paths")
@pytest.mark.parametrize("kind", ("file", "fifo", "symlink"))
def test_trace_publish_refuses_managed_child_symlink_without_touching_target(
    tmp_path, kind: str
) -> None:
    root = tmp_path / "state"
    target = tmp_path / "outside"
    target.mkdir()
    target.chmod(0o755)
    traces = root / "traces"
    traces.mkdir(parents=True)
    child = traces / WALL.strftime("%Y-%m-%d")
    if kind == "file":
        child.write_bytes(b"not a trace date directory")
    elif kind == "fifo":
        os.mkfifo(child)
    else:
        child.symlink_to(target, target_is_directory=True)
    before = target.stat()
    sink = RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(traces),
        clock=FakeClock(wall=WALL),
        process_instance_id="proc-child-symlink",
    )
    try:
        _commit(sink, RunStarted(user_message=None), 1)
        result = sink.flush("run-bundle")
        assert result.outcome == "failed"
    finally:
        sink.close()
    after = target.stat()
    assert list(target.iterdir()) == []
    assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (
        before.st_ino,
        before.st_mode,
        before.st_mtime_ns,
    )


def _commit(
    sink: RunBundleTraceSink,
    payload: object,
    seq: int,
    run_id: str = "run-bundle",
) -> None:
    envelope = EventEnvelope(0.0, run_id, None, 0, None, None)
    prepared = sink.prepare(
        UnsequencedEvent(
            event_id=f"event-{seq}",
            envelope=envelope,
            payload=payload,
            trace_policy="persist",
            replayable=True,
        )
    )
    sink.commit(
        prepared,
        SequencedEvent(
            seq=seq,
            process_instance_id="proc-bundle",
            event_id=f"event-{seq}",
            envelope=envelope,
            payload=payload,
            trace_policy="persist",
            replayable=True,
        ),
    )


def _observe_next_publish(sink: RunBundleTraceSink) -> threading.Event:
    """Signal after the drain's real publication call has returned."""
    published = threading.Event()
    real_publish = sink._publish

    def observe(bundle) -> None:
        try:
            real_publish(bundle)
        finally:
            published.set()

    sink._publish = observe
    return published


def _failing_trace_write(real_write):
    """An ``os.write`` stand-in that breaks only the JSONL trace fd."""

    def write(fd, data):
        if bytes(data[:6]) == b'{"seq"':
            raise OSError(5, "input/output error")
        return real_write(fd, data)

    return write


class _CloseTracker:
    """Flags a close of an fd that no observed open made live.

    A second close of a live fd number is the bug this looks for; a reused fd
    number (closed, reopened, closed again) is legitimate, so the tracker
    follows open/close pairs instead of bare numbers.
    """

    def __init__(self) -> None:
        self.double_closes: list[int] = []
        self.closes: list[int] = []
        self._live: set[int] = set()
        self._lock = threading.Lock()

    def note_open(self, fd: int) -> None:
        with self._lock:
            self._live.add(fd)

    def note_close(self, fd: int) -> None:
        with self._lock:
            self.closes.append(fd)
            if fd not in self._live:
                self.double_closes.append(fd)
            self._live.discard(fd)


# --- problem 2: a broken bundle still owns an fd, and must close it -------


def test_a_broken_bundle_releases_its_fd_at_the_barrier(tmp_path, monkeypatch) -> None:
    """The broken branch retired the bundle as ``(bundle, None)``, so the
    drain neither claimed nor closed the fd it had opened for that Run."""
    monkeypatch.setattr(os, "write", _failing_trace_write(os.write))
    sink = _sink(tmp_path)
    published = _observe_next_publish(sink)
    try:
        _commit(sink, RunStarted(purpose="chat", user_message=None), 1)
        assert published.wait(2.0), "bundle publication did not finish"
        bundle = sink._bundles["run-bundle"]
        fd = bundle.trace_fd
        assert fd is not None, "the published bundle owns an fd"

        result = sink.flush(run_id="run-bundle")

        assert result.outcome == "failed"
        assert bundle.trace_fd is None, "the bundle must stop referencing the fd"
        with pytest.raises(OSError) as excinfo:
            os.fstat(fd)
        assert excinfo.value.errno == 9, f"the fd must be closed, got {excinfo.value}"
    finally:
        sink.close()


def test_repeated_injected_failures_do_not_grow_the_fd_count(
    tmp_path, monkeypatch
) -> None:
    """Six broken Runs used to leave six fds open for the process' life."""
    monkeypatch.setattr(os, "write", _failing_trace_write(os.write))
    baseline = _open_fd_count()
    sink = _sink(tmp_path)
    steady = _open_fd_count()
    try:
        for index in range(6):
            run_id = f"run-leak-{index}"
            _commit(sink, RunStarted(purpose="chat", user_message=None), 1, run_id)
            result = sink.flush(run_id=run_id)
            assert result.outcome == "failed"
            assert sink._bundles == {}, "every bundle is retired at its barrier"
        assert _open_fd_count() == steady
    finally:
        sink.close()
    assert _open_fd_count() == baseline


def test_flush_close_and_drain_unwind_never_double_close(tmp_path, monkeypatch) -> None:
    """The three paths that can release a bundle's fd -- a healthy barrier, a
    broken bundle's barrier, and the drain's unwind after close -- must each
    release it exactly once."""
    tracker = _CloseTracker()
    real_open, real_close, real_dup = os.open, os.close, os.dup
    breaking = {"on": False}
    real_write = os.write

    def tracked_open(path, flags, mode=0o777, **kwargs):
        fd = real_open(path, flags, mode, **kwargs)
        tracker.note_open(fd)
        return fd

    def tracked_close(fd):
        tracker.note_close(fd)
        return real_close(fd)

    def tracked_dup(fd):
        duplicated = real_dup(fd)
        tracker.note_open(duplicated)
        return duplicated

    def gated_trace_write(fd, data):
        if breaking["on"] and bytes(data[:6]) == b'{"seq"':
            raise OSError(5, "input/output error")
        return real_write(fd, data)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "close", tracked_close)
    monkeypatch.setattr(os, "dup", tracked_dup)
    monkeypatch.setattr(os, "write", gated_trace_write)

    sink = _sink(tmp_path)
    try:
        # Path 1: a healthy bundle retired by its own flush barrier.
        _commit(sink, RunStarted(purpose="chat", user_message=None), 1, "run-ok")
        assert sink.flush(run_id="run-ok").outcome == "flushed"

        # Path 2: a broken bundle retired by its own flush barrier.
        breaking["on"] = True
        _commit(sink, RunStarted(purpose="chat", user_message=None), 2, "run-bad")
        assert sink.flush(run_id="run-bad").outcome == "failed"

        # Path 3: a bundle still live when the sink closes, released by the
        # drain's unwind rather than by any barrier.
        _commit(sink, RunStarted(purpose="chat", user_message=None), 3, "run-late")
    finally:
        sink.close()
        sink._drain.join(5.0)

    assert tracker.double_closes == [], (
        f"an fd was closed twice: {tracker.double_closes}"
    )


def test_a_crashing_drain_releases_the_broken_bundle_fd_once(tmp_path) -> None:
    """The drain is the sole owner of every fd for its whole life, including
    its crash unwind. The expected crash is captured rather than left as an
    unhandled thread exception."""
    sink = _sink(tmp_path)
    reached = threading.Event()
    release = threading.Event()
    real_write_item = sink._write_item
    tracker = _CloseTracker()
    real_open, real_close = os.open, os.close

    def crashing_write_item(item):
        real_write_item(item)  # publishes the bundle and opens its fd
        reached.set()
        assert release.wait(5.0), "the test must release the drain"
        raise AssertionError("injected drain crash")

    sink._write_item = crashing_write_item
    try:
        _commit(sink, RunStarted(purpose="chat", user_message=None), 1)
        assert reached.wait(2.0), "the drain must reach the write"
        bundle = sink._bundles["run-bundle"]
        fd = bundle.trace_fd
        assert fd is not None
        for opened in range(256):
            try:
                os.fstat(opened)
            except OSError:
                continue
            tracker.note_open(opened)

        def tracked_open(path, flags, mode=0o777, **kwargs):
            got = real_open(path, flags, mode, **kwargs)
            tracker.note_open(got)
            return got

        def tracked_close(closing):
            tracker.note_close(closing)
            return real_close(closing)

        os.open, os.close = tracked_open, tracked_close
        try:
            with _captured_thread_exception() as caught:
                release.set()
                sink._drain.join(5.0)
                assert caught.get("exc_type") is AssertionError, (
                    f"the crash stays observable: {caught}"
                )
                assert str(caught.get("exc_value")) == "injected drain crash"
        finally:
            os.open, os.close = real_open, real_close
    finally:
        release.set()
        sink.close()

    assert tracker.closes.count(fd) == 1, f"closed {tracker.closes.count(fd)} times"
    assert tracker.double_closes == []


def test_the_original_failure_reason_and_dropped_count_reach_the_barrier(
    tmp_path, monkeypatch
) -> None:
    """A broken bundle skips only the fsync it cannot trust -- not the
    reason, not the drop count, not the fd."""
    real_write = os.write
    calls = {"n": 0}

    def scripted_write(fd, data):
        if bytes(data[:6]) == b'{"seq"':
            calls["n"] += 1
            if calls["n"] == 1:
                return real_write(fd, data[:12])  # a truncated tail
            raise OSError(5, "input/output error")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", scripted_write)
    sink = _sink(tmp_path)
    try:
        _commit(sink, RunStarted(purpose="chat", user_message=None), 1)
        _commit(sink, StepFinished(step_index=0), 2)
        result = sink.flush(run_id="run-bundle")
        assert result.outcome == "failed"
        assert result.dropped_events >= 1, "the drop is reported, not swallowed"
        assert REASON_WRITE_FAILED in result.detail, result.detail
        assert "OSError" in result.detail, result.detail
    finally:
        sink.close()


def test_a_barrier_fsync_failure_still_closes_the_fd(tmp_path, monkeypatch) -> None:
    """The un-broken path is unchanged: an fsync failure is reported and the
    fd is still released by the barrier that owns it."""
    real_fsync = os.fsync
    failed = {"on": False}

    def failing_fsync(fd):
        failed["on"] = True
        raise OSError(28, "no space left on device")

    sink = _sink(tmp_path)
    published = _observe_next_publish(sink)
    try:
        _commit(sink, RunStarted(purpose="chat", user_message=None), 1)
        assert published.wait(2.0), "bundle publication did not finish"
        fd = sink._bundles["run-bundle"].trace_fd
        monkeypatch.setattr(os, "fsync", failing_fsync)
        result = sink.flush(run_id="run-bundle")
        monkeypatch.setattr(os, "fsync", real_fsync)
        assert result.outcome == "failed"
        assert REASON_FSYNC_FAILED in result.detail, result.detail
        assert failed["on"] is True
        with pytest.raises(OSError) as excinfo:
            os.fstat(fd)
        assert excinfo.value.errno == 9, "the barrier still closes its fd"
    finally:
        sink.close()


# --- problem 4: publication must be an atomic no-replace rename ------------


def _staging_and_target(tmp_path: Path, name: str = _STAGING_NAME):
    root = ManagedStateDirectory.acquire(tmp_path / "traces")
    date = root.ensure_directory(PurePath("2026-08-28"))
    staging = date.create_directory(
        PurePath(name), role="bundle staging directory"
    )
    meta = staging.open_regular(
        PurePath("meta.json"),
        access="exclusive_write",
        create=True,
        role="bundle metadata",
    )
    meta.write_all(b'{"run_id": "x"}')
    meta.close()
    target = PurePath("123456Z-0123456789abcdef0123456789abcdef")
    return root, date, staging, target


def test_the_platform_primitive_refuses_an_empty_directory_target(tmp_path) -> None:
    root, date, staging, target = _staging_and_target(tmp_path)
    occupied = date.create_directory(target, role="occupied bundle")
    try:
        with pytest.raises(FileExistsError):
            date.rename_directory_no_replace(staging, target)
        target_path = tmp_path / "traces" / "2026-08-28" / str(target)
        assert target_path.is_dir() and not any(target_path.iterdir())
        assert (staging.path / "meta.json").is_file()
    finally:
        occupied.close()
        staging.close()
        date.close()
        root.close()


def test_the_platform_primitive_refuses_a_non_empty_directory_target(tmp_path) -> None:
    root, date, staging, target = _staging_and_target(tmp_path)
    target_path = tmp_path / "traces" / "2026-08-28" / str(target)
    target_path.mkdir()
    (target_path / "keep.txt").write_text("concurrent contents", encoding="utf-8")
    (target_path / "artifacts").mkdir()
    try:
        with pytest.raises(FileExistsError):
            date.rename_directory_no_replace(staging, target)
        assert (target_path / "keep.txt").read_text(encoding="utf-8") == (
            "concurrent contents"
        )
        assert sorted(path.name for path in target_path.iterdir()) == [
            "artifacts",
            "keep.txt",
        ]
    finally:
        staging.close()
        date.close()
        root.close()


def test_the_primitive_publishes_atomically_and_keeps_the_bundle_shape(
    tmp_path,
) -> None:
    root, date, staging, target = _staging_and_target(tmp_path)
    staging_path = staging.path
    (staging_path / "trace.jsonl").write_text("", encoding="utf-8")
    (staging_path / "artifacts").mkdir()
    date.rename_directory_no_replace(staging, target)
    target_path = tmp_path / "traces" / "2026-08-28" / str(target)
    assert sorted(path.name for path in target_path.iterdir()) == [
        "artifacts",
        "meta.json",
        "trace.jsonl",
    ]
    assert (target_path / "meta.json").read_text(encoding="utf-8") == (
        '{"run_id": "x"}'
    )
    assert not staging_path.exists(), "the staging name is consumed by the rename"
    staging.close()
    date.close()
    root.close()


def test_publish_rechecks_the_moved_inode_before_rebinding_or_continuing(
    tmp_path, monkeypatch
) -> None:
    """A no-replace rename protects the target, not the checked source name.

    Replace the staging name only after every Python-side ownership check and
    before the kernel rename.  The replacement may be moved by the syscall,
    but it must never be adopted as this bundle or touched by later publish or
    rollback work.
    """
    real_publish = managed_module.ManagedDirectoryLease.rename_directory_no_replace
    real_no_replace = managed_module._rename_no_replace
    real_open = managed_module.ManagedDirectoryLease.open_regular
    real_unlink = managed_module.ManagedDirectoryLease.unlink_regular
    real_remove = managed_module.ManagedDirectoryLease.remove_directory_if_owned
    entered = threading.Event()
    release = threading.Event()
    replacement_installed = threading.Event()
    captured_source: list[managed_module.ManagedDirectoryLease] = []
    calls_after_replacement: list[str] = []

    def capture_publish(directory, source, target):
        captured_source.append(source)
        return real_publish(directory, source, target)

    def pause_at_rename(
        source,
        target,
        *,
        source_parent_fd,
        target_parent_fd,
        role,
        path,
    ):
        if role == "bundle publication":
            entered.set()
            assert release.wait(3.0)
        return real_no_replace(
            source,
            target,
            source_parent_fd=source_parent_fd,
            target_parent_fd=target_parent_fd,
            role=role,
            path=path,
        )

    def record_open(directory, relative, **kwargs):
        if replacement_installed.is_set():
            calls_after_replacement.append(f"open:{relative}")
        return real_open(directory, relative, **kwargs)

    def record_unlink(directory, relative, *, missing_ok):
        if replacement_installed.is_set():
            calls_after_replacement.append(f"unlink:{relative}")
        return real_unlink(directory, relative, missing_ok=missing_ok)

    def record_remove(directory, child):
        if replacement_installed.is_set():
            calls_after_replacement.append(f"rmdir:{child.path.name}")
        return real_remove(directory, child)

    monkeypatch.setattr(
        managed_module.ManagedDirectoryLease,
        "rename_directory_no_replace",
        capture_publish,
    )
    monkeypatch.setattr(managed_module, "_rename_no_replace", pause_at_rename)
    monkeypatch.setattr(
        managed_module.ManagedDirectoryLease, "open_regular", record_open
    )
    monkeypatch.setattr(
        managed_module.ManagedDirectoryLease, "unlink_regular", record_unlink
    )
    monkeypatch.setattr(
        managed_module.ManagedDirectoryLease,
        "remove_directory_if_owned",
        record_remove,
    )

    sink = _sink(tmp_path)
    staging_path = (
        tmp_path
        / "traces"
        / "2026-08-28"
        / f".staging-{_storage_id('source-race')}"
    )
    displaced = staging_path.with_name(f"{staging_path.name}.owned")
    target = staging_path.with_name(
        f"123456Z-{_storage_id('source-race')}"
    )
    replacement_trace = b"replacement sentinel\n"
    try:
        _commit(
            sink,
            RunStarted(purpose="chat", user_message=None),
            1,
            "source-race",
        )
        assert entered.wait(3.0)
        staging_path.rename(displaced)
        staging_path.mkdir(mode=0o700)
        replacement_meta = staging_path / "meta.json"
        replacement_meta.write_text('{"run_id":"replacement"}', encoding="utf-8")
        replacement_meta.chmod(0o600)
        replacement_file = staging_path / "trace.jsonl"
        replacement_file.write_bytes(replacement_trace)
        replacement_file.chmod(0o600)
        (staging_path / "artifacts").mkdir(mode=0o700)
        replacement = staging_path.stat()
        replacement_installed.set()
        release.set()

        result = sink.flush(run_id="source-race")

        assert result.outcome == "failed"
        assert "ManagedPathSecurityError" in result.detail
        assert len(captured_source) == 1
        source = captured_source[0]
        assert source.path == staging_path
        assert source._name == staging_path.name
        assert calls_after_replacement == []
        current = target.stat()
        assert (current.st_dev, current.st_ino) == (
            replacement.st_dev,
            replacement.st_ino,
        )
        assert (target / "trace.jsonl").read_bytes() == replacement_trace
        assert sorted(path.name for path in displaced.iterdir()) == [
            "artifacts",
            "meta.json",
            "trace.jsonl",
        ]
    finally:
        release.set()
        sink.close()


def test_remove_directory_if_owned_removes_a_verified_empty_child(tmp_path) -> None:
    parent = ManagedStateDirectory.acquire(tmp_path)
    child = parent.create_directory(PurePath("child"), role="test child")
    try:
        assert parent.remove_directory_if_owned(child) is True
        assert not (tmp_path / "child").exists()
    finally:
        child.close()
        parent.close()


def test_pre_publish_failure_removes_its_recognized_staging(
    tmp_path, monkeypatch
) -> None:
    """The publisher removes its own valid staging before reporting failure."""

    def fail_publication(directory, source, target):
        del directory, source, target
        raise OSError(errno.EIO, "injected publication failure")

    monkeypatch.setattr(
        managed_module.ManagedDirectoryLease,
        "rename_directory_no_replace",
        fail_publication,
    )
    sink = _sink(tmp_path)
    try:
        _commit(
            sink,
            RunStarted(purpose="chat", user_message=None),
            1,
            "abandoned-staging",
        )
        result = sink.flush(run_id="abandoned-staging")
        staging = (
            tmp_path
            / "traces"
            / "2026-08-28"
            / f".staging-{_storage_id('abandoned-staging')}"
        )

        assert result.outcome == "failed"
        assert not staging.exists()
    finally:
        sink.close()


def test_the_primitive_rejects_a_cross_directory_rename(tmp_path) -> None:
    a = ManagedStateDirectory.acquire(tmp_path / "a")
    b = ManagedStateDirectory.acquire(tmp_path / "b")
    staging = a.create_directory(
        PurePath(_STAGING_NAME), role="bundle staging directory"
    )
    try:
        with pytest.raises(ValueError, match="direct child"):
            b.rename_directory_no_replace(
                staging,
                PurePath("123456Z-0123456789abcdef0123456789abcdef"),
            )
    finally:
        staging.close()
        b.close()
        a.close()


def test_publish_never_replaces_a_target_created_after_the_check(
    tmp_path, monkeypatch
) -> None:
    """The TOCTOU window: another publisher lands between the existence check
    and the rename. Only an atomic no-replace rename can refuse it."""
    real_rename = trace_module.ManagedDirectoryLease.rename_directory_no_replace
    created = threading.Event()

    def racing_rename(directory, staging, target):
        target_path = directory.path / str(target)
        target_path.mkdir(mode=0o700)
        (target_path / "concurrent.txt").write_text(
            "other publisher", encoding="utf-8"
        )
        created.set()
        return real_rename(directory, staging, target)

    monkeypatch.setattr(
        trace_module.ManagedDirectoryLease,
        "rename_directory_no_replace",
        racing_rename,
    )
    sink = _sink(tmp_path)
    try:
        _commit(sink, RunStarted(purpose="chat", user_message=None), 1)
        result = sink.flush(run_id="run-bundle")

        assert created.wait(2.0), "the racing publisher must run"
        assert result.outcome == "failed"
        assert REASON_ID_COLLISION in result.detail, result.detail
        bundle = (
            tmp_path
            / "traces"
            / "2026-08-28"
            / f"123456Z-{_storage_id('run-bundle')}"
        )
        assert (bundle / "concurrent.txt").read_text(encoding="utf-8") == (
            "other publisher"
        ), "the target that won the race is left exactly as its creator left it"
    finally:
        sink.close()


def test_publish_fails_closed_when_the_primitive_is_unsupported(
    tmp_path, monkeypatch
) -> None:
    """A platform with no no-replace primitive must refuse to publish, never
    fall back to an overwriting ``os.rename``."""
    def unsupported(directory, staging, target):
        raise ManagedPathSecurityError(
            reason="unsupported_nofollow",
            role="bundle publication",
            path=directory.path / str(target),
        )

    monkeypatch.setattr(
        trace_module.ManagedDirectoryLease,
        "rename_directory_no_replace",
        unsupported,
    )
    renames: list[tuple[object, object]] = []
    real_rename = os.rename

    def spy_rename(src, dst, **kwargs):
        renames.append((src, dst))
        return real_rename(src, dst, **kwargs)

    monkeypatch.setattr(os, "rename", spy_rename)
    sink = _sink(tmp_path)
    try:
        _commit(sink, RunStarted(purpose="chat", user_message=None), 1)
        result = sink.flush(run_id="run-bundle")

        assert result.outcome == "failed"
        assert "no_replace_unsupported" in result.detail, result.detail
        assert renames == [], "no overwriting rename is ever attempted"
        date_dir = tmp_path / "traces" / "2026-08-28"
        names = (
            [path.name for path in date_dir.iterdir()] if date_dir.is_dir() else []
        )
        assert names == []
    finally:
        sink.close()


def test_staging_reclamation_removes_managed_shape_and_refuses_foreign_shape(
    tmp_path,
) -> None:
    """ADR-0017 deletes only names and child types the publisher can create."""
    date_dir = tmp_path / "traces" / "2026-08-28"
    date_dir.mkdir(parents=True)

    good = date_dir / _STAGING_NAME
    good.mkdir()
    (good / "meta.json").write_text("{}", encoding="utf-8")
    (good / "trace.jsonl").write_text("", encoding="utf-8")
    (good / "artifacts").mkdir()
    assert _remove_staging(good) is True
    assert not good.exists()

    # Not a staging name at all: refused and left untouched.
    foreign = date_dir / "123456Z-0123456789abcdef0123456789abcdef"
    foreign.mkdir()
    (foreign / "meta.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="not a managed staging directory"):
        _remove_staging(foreign)
    assert (foreign / "meta.json").is_file()

    # A staging name pointing elsewhere: refused, never followed.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep me", encoding="utf-8")
    linked = date_dir / ".staging-fedcba9876543210fedcba9876543210"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ManagedPathSecurityError) as caught:
        _remove_staging(linked)
    assert caught.value.reason == "symlink"
    assert (outside / "precious.txt").read_text(encoding="utf-8") == "keep me"
    assert linked.is_symlink(), "the refused path is not removed either"

    # A staging directory holding something the publisher never writes: the
    # shape is unrecognizable, so it is retained for operator inspection.
    odd = date_dir / ".staging-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    odd.mkdir()
    (odd / "surprise").write_text("?", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected"):
        _remove_staging(odd)
    assert (odd / "surprise").is_file()


def _bare_staging(tmp_path: Path, name: str) -> Path:
    """A correctly named, empty staging directory that the caller then shapes.

    Reclamation has to accept an *incomplete* set of entries -- a publish can
    crash between creating them -- so each test below builds only the shape
    it is arguing about.
    """
    date_dir = tmp_path / "traces" / "2026-08-28"
    date_dir.mkdir(parents=True, exist_ok=True)
    staging = date_dir / name
    staging.mkdir()
    return staging


def _remove_staging(staging: Path) -> bool:
    staging.chmod(0o700)
    for entry in staging.iterdir():
        info = entry.lstat()
        if entry.name in {"meta.json", "trace.jsonl"} and stat.S_ISREG(
            info.st_mode
        ):
            entry.chmod(0o600)
        elif entry.name == "artifacts" and stat.S_ISDIR(info.st_mode):
            entry.chmod(0o700)
    parent = ManagedStateDirectory.acquire(staging.parent)
    lease = None
    try:
        lease = parent.ensure_directory(PurePath(staging.name))
        return lease.remove_managed_staging()
    finally:
        if lease is not None:
            lease.close()
        parent.close()


def test_reclamation_refuses_a_meta_json_that_is_a_directory(tmp_path) -> None:
    """ADR-0017 asks for the internal structure to be within the allowed
    shape, not merely for the entry names to be known. The publisher always
    writes ``meta.json`` as a regular file, so a *directory* of that name is
    not a shape it produces -- and a recursive delete there would take
    whatever that directory holds down with it."""
    staging = _bare_staging(tmp_path, ".staging-11111111111111111111111111111111")
    (staging / "meta.json").mkdir()
    (staging / "meta.json" / "inner").write_text("precious", encoding="utf-8")

    with pytest.raises(ValueError, match="not a regular file"):
        _remove_staging(staging)

    assert staging.is_dir(), "a refused shape deletes nothing"
    assert (staging / "meta.json" / "inner").read_text(encoding="utf-8") == "precious"


def test_reclamation_refuses_a_trace_jsonl_that_is_a_directory(tmp_path) -> None:
    """Same argument as ``meta.json``: ``trace.jsonl`` is the file the drain
    holds the fd to, always a regular file. A directory of that name means
    this tree was not shaped by the publisher."""
    staging = _bare_staging(tmp_path, ".staging-22222222222222222222222222222222")
    (staging / "meta.json").write_text("{}", encoding="utf-8")
    (staging / "trace.jsonl").mkdir()
    (staging / "trace.jsonl" / "inner").write_text("precious", encoding="utf-8")

    with pytest.raises(ValueError, match="not a regular file"):
        _remove_staging(staging)

    assert staging.is_dir(), "a refused shape deletes nothing"
    assert (staging / "trace.jsonl" / "inner").read_text(encoding="utf-8") == (
        "precious"
    )


def test_reclamation_refuses_an_artifacts_that_is_a_regular_file(tmp_path) -> None:
    """``artifacts`` is the one entry the publisher creates as a directory. A
    regular file of that name is the mirror image of the case above and is
    exactly as unrecognizable."""
    staging = _bare_staging(tmp_path, ".staging-33333333333333333333333333333333")
    (staging / "meta.json").write_text("{}", encoding="utf-8")
    (staging / "artifacts").write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="not a directory"):
        _remove_staging(staging)

    assert (staging / "artifacts").read_text(encoding="utf-8") == "not a directory"
    assert (staging / "meta.json").is_file()


@pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="POSIX only: no FIFO to create here"
)
def test_reclamation_refuses_a_special_file_in_place_of_an_entry(tmp_path) -> None:
    """The check has to be on the file *type*, not on "is it a symlink".

    A FIFO is neither a regular file nor a directory, so it is not a shape
    the publisher writes. Nothing in this tree is a link, so a symlink check
    alone cannot see it. Creating and ``lstat``-ing a FIFO never opens it, so
    the test cannot block even though reading one would.
    """
    staging = _bare_staging(tmp_path, ".staging-55555555555555555555555555555555")
    os.mkfifo(staging / "trace.jsonl")

    with pytest.raises(ValueError, match="not a regular file"):
        _remove_staging(staging)

    assert staging.is_dir(), "a refused shape deletes nothing"
    assert stat.S_ISFIFO((staging / "trace.jsonl").lstat().st_mode)


def test_reclamation_removes_a_complete_managed_staging_under_the_process_lock(
    tmp_path,
) -> None:
    """A recognized stale shape is reclaimed before the new runtime starts."""
    staging = _bare_staging(tmp_path, ".staging-44444444444444444444444444444444")
    (staging / "meta.json").write_text('{"run_id": "x"}', encoding="utf-8")
    (staging / "trace.jsonl").write_text("", encoding="utf-8")
    (staging / "artifacts").mkdir()

    assert _remove_staging(staging) is True
    assert not staging.exists()


def test_reclamation_removes_every_valid_partial_staging_after_a_crash(
    tmp_path,
) -> None:
    """A publish can die between any two entries, so an incomplete staging is
    a normal leftover rather than an unrecognizable one.

    Every prefix of the publisher's write order remains a valid candidate. The
    rule is "the entries that exist have the right type", never "the whole set
    is present".
    """
    # The publisher's order in trace.py: meta.json, then trace.jsonl, then
    # the artifacts directory.
    write_order = ["meta.json", "trace.jsonl", "artifacts"]
    for length in range(len(write_order) + 1):
        prefix = write_order[:length]
        staging = _bare_staging(tmp_path, f".staging-{length:032x}")
        for name in prefix:
            if name == "artifacts":
                (staging / name).mkdir()
            else:
                (staging / name).write_text("{}", encoding="utf-8")

        assert _remove_staging(staging) is True, prefix
        assert not staging.exists()


def test_a_successful_publish_still_leaves_no_staging_behind(tmp_path) -> None:
    sink = _sink(tmp_path)
    try:
        _commit(sink, RunStarted(purpose="chat", user_message=None), 1)
        assert sink.flush(run_id="run-bundle") == BarrierFlushResult(
            outcome="flushed", dropped_events=0, detail=""
        )
    finally:
        sink.close()

    date_dir = tmp_path / "traces" / "2026-08-28"
    bundle = date_dir / f"123456Z-{_storage_id('run-bundle')}"
    assert sorted(path.name for path in date_dir.iterdir()) == [bundle.name]
    meta = json.loads((bundle / "meta.json").read_text(encoding="utf-8"))
    assert meta["run_id"] == "run-bundle"
    assert (bundle / "trace.jsonl").is_file()
    assert (bundle / "artifacts").is_dir()


def test_a_real_run_through_the_host_publishes_without_staging_leftovers(
    tmp_path,
) -> None:
    """End-to-end: the production wiring still publishes through staging and
    the new failure-path cleanup never fires on the happy path."""
    from agent_alfred.runtime.host import SubmitRequest

    host = build_default_host(
        state_dir=tmp_path, factory=ScriptedModelFactory(ScriptedModel(["pong"]))
    )
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        assert submitted.kind == "accepted"
        host.wait(submitted.run_id)
    finally:
        host.close()

    names = [
        path.name
        for date_dir in sorted((tmp_path / "traces").iterdir())
        for path in sorted(date_dir.iterdir())
    ]
    assert not any(name.startswith(".staging-") for name in names), names
    assert len([name for name in names if not name.startswith(".")]) == 1, names


@pytest.mark.parametrize("created_role", ("staging", "artifacts"))
def test_real_host_trace_create_refuses_directory_replaced_before_open(
    tmp_path, monkeypatch, created_role: str
) -> None:
    """The create-exclusive token follows the created inode into first open."""
    from agent_alfred.runtime.host import SubmitRequest

    baseline = _open_fd_count()
    real_open = managed_module.os.open
    swapped = False
    replacement_identity: tuple[int, int] | None = None

    def replace_created_directory(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal replacement_identity, swapped
        name = os.fspath(path)
        selected = (
            created_role == "staging" and name.startswith(".staging-")
        ) or (
            created_role == "artifacts" and name == "artifacts"
        )
        if selected and dir_fd is not None and not swapped:
            swapped = True
            displaced = f"{name}.displaced"
            os.rename(name, displaced, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.mkdir(name, 0o700, dir_fd=dir_fd)
            replacement_fd = real_open(
                name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                dir_fd=dir_fd,
            )
            try:
                sentinel_fd = real_open(
                    "sentinel",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=replacement_fd,
                )
                os.close(sentinel_fd)
                info = os.fstat(replacement_fd)
                replacement_identity = (info.st_dev, info.st_ino)
            finally:
                os.close(replacement_fd)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(managed_module.os, "open", replace_created_directory)
    host = build_default_host(
        state_dir=tmp_path,
        factory=ScriptedModelFactory(ScriptedModel(["first", "second"])),
    )
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="first"))
        assert submitted.kind == "accepted"
        assert host.wait(submitted.run_id).outcome == "completed"
    finally:
        first_close = host.close()

    sentinels = list((tmp_path / "traces").rglob("sentinel"))
    assert swapped
    assert len(sentinels) == 1
    replacement = sentinels[0].parent
    info = replacement.stat()
    assert (info.st_dev, info.st_ino) == replacement_identity
    assert list(replacement.iterdir()) == [sentinels[0]]

    staging = replacement if created_role == "staging" else replacement.parent
    assert staging.name.startswith(".staging-")
    shutil.rmtree(staging)
    if first_close is False:
        assert host.close() is True
    successor = build_default_host(
        state_dir=tmp_path,
        factory=ScriptedModelFactory(ScriptedModel(["successor"])),
    )
    successor.start()
    try:
        submitted = successor.submit(SubmitRequest(message="successor"))
        assert submitted.kind == "accepted"
        assert successor.wait(submitted.run_id).outcome == "completed"
    finally:
        successor.close()
    assert _open_fd_count() == baseline


def test_created_directory_replaced_before_final_identity_check_is_not_deleted(
    tmp_path, monkeypatch
) -> None:
    from agent_alfred.resource_rollback import IncompleteRollback

    parent = ManagedStateDirectory.acquire(tmp_path)
    real_verify = managed_module.ManagedDirectoryLease.verify_identity
    entered = threading.Event()
    release = threading.Event()
    intercepted = False
    errors: list[BaseException] = []

    def block_final_identity_check(lease):
        nonlocal intercepted
        if lease.role == "test child" and not intercepted:
            intercepted = True
            entered.set()
            assert release.wait(3.0)
        return real_verify(lease)

    monkeypatch.setattr(
        managed_module.ManagedDirectoryLease,
        "verify_identity",
        block_final_identity_check,
    )

    def create() -> None:
        try:
            parent.create_directory(PurePath("child"), role="test child")
        except BaseException as exc:
            errors.append(exc)

    creator = threading.Thread(target=create)
    creator.start()
    assert entered.wait(3.0)
    child = tmp_path / "child"
    child.rename(tmp_path / "displaced")
    child.mkdir(mode=0o700)
    sentinel = child / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    replacement = child.stat()
    release.set()
    creator.join(3.0)
    try:
        assert not creator.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], ManagedPathSecurityError)
        assert errors[0].reason == "identity_changed"
        assert isinstance(errors[0].__cause__, IncompleteRollback)
        current = child.stat()
        assert (current.st_dev, current.st_ino, current.st_mode) == (
            replacement.st_dev,
            replacement.st_ino,
            replacement.st_mode,
        )
        assert sentinel.read_text(encoding="utf-8") == "keep"
    finally:
        parent.close()


def test_created_directory_replaced_before_identity_capture_is_not_deleted(
    tmp_path, monkeypatch
) -> None:
    """Rollback never adopts the occupant installed just after ``mkdir``.

    The creator has not observed an identity when ``mkdir`` returns.  Replacing
    that name before its next filesystem operation must therefore leave the
    replacement outside this transaction's ownership, even when descriptor
    verification subsequently fails.
    """
    parent = ManagedStateDirectory.acquire(tmp_path)
    real_mkdir = managed_module.os.mkdir
    real_fchmod = managed_module.os.fchmod
    mkdir_returning = threading.Event()
    release_mkdir = threading.Event()
    intercepted = False
    verification_failed = False
    created_name: str | None = None
    replacement_identity: tuple[int, int] | None = None
    errors: list[BaseException] = []

    def pause_before_mkdir_returns(name, mode=0o777, *, dir_fd=None):
        nonlocal created_name, intercepted
        result = real_mkdir(name, mode, dir_fd=dir_fd)
        if dir_fd == parent.fd and not intercepted:
            intercepted = True
            created_name = os.fspath(name)
            mkdir_returning.set()
            assert release_mkdir.wait(3.0)
        return result

    def fail_replacement_verification(fd, mode):
        nonlocal verification_failed
        info = os.fstat(fd)
        identity = (info.st_dev, info.st_ino)
        if identity == replacement_identity and not verification_failed:
            verification_failed = True
            raise PermissionError(errno.EACCES, "verification denied")
        return real_fchmod(fd, mode)

    monkeypatch.setattr(managed_module.os, "mkdir", pause_before_mkdir_returns)
    monkeypatch.setattr(managed_module.os, "fchmod", fail_replacement_verification)
    creator = threading.Thread(
        target=lambda: _capture_exception(
            errors,
            lambda: parent.create_directory(PurePath("child"), role="test child"),
        )
    )
    creator.start()
    try:
        assert mkdir_returning.wait(3.0)
        assert created_name is not None
        created_path = tmp_path / created_name
        created_path.rename(tmp_path / "displaced")
        created_path.mkdir(mode=0o700)
        replacement = created_path.stat()
        replacement_identity = (replacement.st_dev, replacement.st_ino)
        release_mkdir.set()
        creator.join(3.0)

        assert not creator.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], ManagedPathSecurityError)
        assert errors[0].reason == "mode_tighten_failed"
        current = created_path.stat()
        assert (current.st_dev, current.st_ino) == replacement_identity
    finally:
        release_mkdir.set()
        creator.join(3.0)
        parent.close()


def test_created_directory_does_not_publish_through_a_revalidated_source_name(
    tmp_path, monkeypatch
) -> None:
    """Creation must not rename whatever later occupies an observed source name."""
    parent = ManagedStateDirectory.acquire(tmp_path)
    real_no_replace = managed_module._rename_no_replace
    publication_attempted = False
    replacement_identity: tuple[int, int] | None = None
    owned_path: Path | None = None
    lease = None
    errors: list[BaseException] = []

    def replace_source_at_publish(
        source,
        target,
        *,
        source_parent_fd,
        target_parent_fd,
        role,
        path,
    ):
        nonlocal owned_path, publication_attempted, replacement_identity
        source_name = os.fspath(source)
        if os.fspath(target) == "child":
            publication_attempted = True
            owned_name = f"{source_name}.owned"
            owned_path = tmp_path / owned_name
            os.rename(
                source_name,
                owned_name,
                src_dir_fd=source_parent_fd,
                dst_dir_fd=source_parent_fd,
            )
            os.mkdir(source_name, 0o700, dir_fd=source_parent_fd)
            replacement = os.stat(
                source_name, dir_fd=source_parent_fd, follow_symlinks=False
            )
            replacement_identity = (replacement.st_dev, replacement.st_ino)
        return real_no_replace(
            source,
            target,
            source_parent_fd=source_parent_fd,
            target_parent_fd=target_parent_fd,
            role=role,
            path=path,
        )

    monkeypatch.setattr(managed_module, "_rename_no_replace", replace_source_at_publish)
    try:
        try:
            lease = parent.create_directory(PurePath("child"), role="test child")
        except BaseException as exc:
            errors.append(exc)

        assert errors == []
        assert publication_attempted is False
        assert replacement_identity is None
        assert lease is not None
        lease.verify_identity()
        assert lease.path == tmp_path / "child"
        assert not list(tmp_path.glob(".create-*"))
    finally:
        if lease is not None:
            lease.close()
        for candidate in tmp_path.iterdir():
            if candidate.is_dir():
                candidate.rmdir()
        parent.close()


@pytest.mark.parametrize("denied_errno", (errno.EACCES, errno.EPERM))
def test_initial_stat_denial_never_adopts_a_replacement(
    tmp_path, monkeypatch, denied_errno: int
) -> None:
    from agent_alfred.resource_rollback import IncompleteRollback

    parent = ManagedStateDirectory.acquire(tmp_path)
    real_stat = managed_module.os.stat
    replaced = False
    created_name: str | None = None
    replacement: os.stat_result | None = None

    def replace_then_deny_stat(path, *, dir_fd=None, follow_symlinks=True):
        nonlocal created_name, replaced, replacement
        name = os.fspath(path)
        if name == "child" and dir_fd is not None and not replaced:
            replaced = True
            created_name = name
            os.rename(name, "displaced", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.mkdir(name, 0o700, dir_fd=dir_fd)
            replacement = real_stat(name, dir_fd=dir_fd, follow_symlinks=False)
            raise PermissionError(denied_errno, "identity denied", path)
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(managed_module.os, "stat", replace_then_deny_stat)
    try:
        with pytest.raises(ManagedPathSecurityError) as caught:
            parent.create_directory(PurePath("child"), role="test child")
        assert caught.value.reason == "permission_denied"
        assert isinstance(caught.value.__cause__, IncompleteRollback)
        assert created_name is not None
        assert replacement is not None
        current = (tmp_path / created_name).stat()
        assert (current.st_dev, current.st_ino, current.st_mode) == (
            replacement.st_dev,
            replacement.st_ino,
            replacement.st_mode,
        )
    finally:
        parent.close()


def test_open_rechecks_initial_created_identity_before_adopting_it(
    tmp_path, monkeypatch
) -> None:
    from agent_alfred.resource_rollback import IncompleteRollback

    parent = ManagedStateDirectory.acquire(tmp_path)
    real_open = managed_module.os.open
    entered = threading.Event()
    release = threading.Event()
    created_name: str | None = None

    def pause_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal created_name
        name = os.fspath(path)
        if name == "child" and dir_fd is not None:
            created_name = name
            entered.set()
            assert release.wait(3.0)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(managed_module.os, "open", pause_open)
    errors: list[BaseException] = []
    creator = threading.Thread(
        target=lambda: _capture_exception(
            errors,
            lambda: parent.create_directory(PurePath("child"), role="test child"),
        )
    )
    creator.start()
    try:
        assert entered.wait(3.0)
        assert created_name is not None
        created = tmp_path / created_name
        created.rename(tmp_path / "displaced")
        created.mkdir(mode=0o700)
        sentinel = created / "sentinel"
        sentinel.write_text("replacement", encoding="utf-8")
        replacement = created.stat()
        release.set()
        creator.join(3.0)

        assert not creator.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], ManagedPathSecurityError)
        assert errors[0].reason == "identity_changed"
        assert isinstance(errors[0].__cause__, IncompleteRollback)
        current = created.stat()
        assert (current.st_dev, current.st_ino) == (
            replacement.st_dev,
            replacement.st_ino,
        )
        assert sentinel.read_text(encoding="utf-8") == "replacement"
    finally:
        release.set()
        creator.join(3.0)
        parent.close()


def test_created_directory_rollback_does_not_remove_post_verification_replacement(
    tmp_path, monkeypatch
) -> None:
    from agent_alfred.resource_rollback import IncompleteRollback

    parent = ManagedStateDirectory.acquire(tmp_path)
    real_rename = managed_module.os.rename
    real_verify = managed_module.ManagedDirectoryLease.verify_identity
    replaced = False
    replacement: os.stat_result | None = None

    def replace_after_verification(lease):
        nonlocal replaced, replacement
        result = real_verify(lease)
        if lease.role == "test child" and not replaced:
            replaced = True
            real_rename(
                "child",
                "displaced",
                src_dir_fd=parent.fd,
                dst_dir_fd=parent.fd,
            )
            os.mkdir("child", 0o700, dir_fd=parent.fd)
            replacement = os.stat(
                "child", dir_fd=parent.fd, follow_symlinks=False
            )
            raise RuntimeError("injected post-verification failure")
        return result

    monkeypatch.setattr(
        managed_module.ManagedDirectoryLease,
        "verify_identity",
        replace_after_verification,
    )
    try:
        with pytest.raises(RuntimeError, match="post-verification") as caught:
            parent.create_directory(PurePath("child"), role="test child")
        assert isinstance(caught.value.__cause__, IncompleteRollback)
        assert replacement is not None
        current = (tmp_path / "child").stat()
        assert (current.st_dev, current.st_ino) == (
            replacement.st_dev,
            replacement.st_ino,
        )
    finally:
        parent.close()


def test_staging_cleanup_attempts_every_opened_lease_close(
    tmp_path, monkeypatch
) -> None:
    staging = _bare_staging(tmp_path, ".staging-88888888888888888888888888888888")
    for name in ("meta.json", "trace.jsonl"):
        entry = staging / name
        entry.write_text("managed", encoding="utf-8")
        entry.chmod(0o600)
    (staging / "artifacts").mkdir(mode=0o700)
    real_file_close = managed_module.ManagedFileLease.close
    real_directory_close = managed_module.ManagedDirectoryLease.close
    closed: list[str] = []

    def close_file(lease):
        name = lease.path.name
        closed.append(name)
        real_file_close(lease)
        if name == "meta.json":
            raise RuntimeError("injected file lease close failure")

    def close_directory(lease):
        closed.append(lease.path.name)
        real_directory_close(lease)

    monkeypatch.setattr(managed_module.ManagedFileLease, "close", close_file)
    monkeypatch.setattr(
        managed_module.ManagedDirectoryLease, "close", close_directory
    )

    with pytest.raises(RuntimeError, match="injected file lease close failure"):
        _remove_staging(staging)

    assert {"meta.json", "trace.jsonl", "artifacts"} <= set(closed)
    assert ".staging-88888888888888888888888888888888" in closed


def _capture_exception(errors: list[BaseException], action) -> None:
    try:
        action()
    except BaseException as exc:
        errors.append(exc)


@pytest.mark.parametrize("created_role", ("staging", "artifacts"))
@pytest.mark.parametrize(
    "stat_failure", ("permission", "ordinary", "process_control")
)
@pytest.mark.parametrize("replace_before_failure", (False, True))
def test_real_host_trace_preserves_unknown_directory_after_identity_capture_fails(
    tmp_path,
    monkeypatch,
    created_role: str,
    stat_failure: str,
    replace_before_failure: bool,
) -> None:
    """A name is never deleted before its created identity is knowable."""
    from agent_alfred.resource_rollback import IncompleteRollback
    from agent_alfred.runtime.host import SubmitRequest

    baseline = _open_fd_count()
    real_stat = managed_module.os.stat
    control = SystemExit(71)
    failure = OSError(errno.EIO, "first created-directory stat failed")
    injected = False
    creation_names: list[str] = []
    replacement_stat: os.stat_result | None = None

    def fail_first_created_stat(path, *, dir_fd=None, follow_symlinks=True):
        nonlocal injected, replacement_stat
        name = os.fspath(path)
        matches_role = (
            created_role == "staging" and name.startswith(".staging-")
        ) or (
            created_role == "artifacts" and name == "artifacts"
        )
        if matches_role and name not in creation_names:
            creation_names.append(name)
        selected = (
            matches_role
            and len(creation_names) == 1
            and creation_names[-1] == name
        )
        if dir_fd is not None and selected and not injected:
            injected = True
            if replace_before_failure:
                displaced = f"{name}.displaced"
                os.rename(name, displaced, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                os.mkdir(name, 0o700, dir_fd=dir_fd)
                replacement_stat = real_stat(
                    name, dir_fd=dir_fd, follow_symlinks=False
                )
            if stat_failure == "permission":
                raise PermissionError(errno.EACCES, "identity capture denied")
            if stat_failure == "process_control":
                raise control
            raise failure
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(managed_module.os, "stat", fail_first_created_stat)
    host = build_default_host(
        state_dir=tmp_path,
        factory=ScriptedModelFactory(ScriptedModel(["first"])),
    )
    host.start()
    with _captured_thread_exception() as thread_error:
        submitted = host.submit(SubmitRequest(message="first"))
        assert submitted.kind == "accepted"
        assert host.wait(submitted.run_id).outcome == "completed"
        first_close = host.close()
    safely_reclaimed = created_role == "artifacts" and not replace_before_failure
    if stat_failure == "process_control":
        assert thread_error.get("exc_value") is control
        if safely_reclaimed:
            assert control.__cause__ is None
        else:
            assert isinstance(control.__cause__, IncompleteRollback)
    else:
        assert thread_error == {}

    conn = sqlite3.connect(tmp_path / "db.sqlite3")
    try:
        row = conn.execute(
            "SELECT telemetry FROM runs WHERE run_id = ?", (submitted.run_id,)
        ).fetchone()
        assert row is not None
        assert json.loads(row[0])["trace_incomplete"] is True
    finally:
        conn.close()

    creation_directories = list(
        (tmp_path / "traces").rglob(
            ".staging-*" if created_role == "staging" else "artifacts"
        )
    )
    assert injected
    if safely_reclaimed:
        # The first read failed, but the enclosing staging lease remained
        # owned and later revalidated this unchanged child before removing it.
        assert creation_directories == []
    else:
        assert creation_directories, (
            "an identity-unknown or replaced created directory is retained"
        )
    if replace_before_failure:
        assert replacement_stat is not None
        replacement_path = next(
            (
                path
                for path in creation_directories
                if path.is_dir()
                and (path.stat().st_dev, path.stat().st_ino)
                == (replacement_stat.st_dev, replacement_stat.st_ino)
            ),
            None,
        )
        assert replacement_path is not None
        current = replacement_path.stat()
        assert (current.st_dev, current.st_ino) == (
            replacement_stat.st_dev,
            replacement_stat.st_ino,
        )
        assert stat.S_IMODE(current.st_mode) == stat.S_IMODE(
            replacement_stat.st_mode
        )
        assert current.st_mtime_ns == replacement_stat.st_mtime_ns
        assert list(replacement_path.iterdir()) == []

    monkeypatch.undo()
    shutil.rmtree(tmp_path / "traces")
    if first_close is False:
        assert host.close() is True
    successor = build_default_host(
        state_dir=tmp_path,
        factory=ScriptedModelFactory(ScriptedModel(["successor"])),
    )
    successor.start()
    try:
        second = successor.submit(SubmitRequest(message="successor"))
        assert second.kind == "accepted"
        assert successor.wait(second.run_id).outcome == "completed"
    finally:
        successor.close()
    assert _open_fd_count() == baseline


def test_broken_bundle_fd_is_released_for_a_real_host_run(
    tmp_path, monkeypatch
) -> None:
    """The leak as a user would hit it: one real Run whose trace write fails,
    driven through the production Host."""
    from agent_alfred.runtime.host import SubmitRequest

    monkeypatch.setattr(os, "write", _failing_trace_write(os.write))
    rounds = 4
    host = build_default_host(
        state_dir=tmp_path,
        factory=ScriptedModelFactory(ScriptedModel(["pong"] * rounds)),
    )
    sinks = [
        item for item in host._fanout.sinks if isinstance(item, RunBundleTraceSink)
    ]
    assert sinks, "the production Host carries a trace sink"
    host.start()
    # The leak only shows between the Run's barrier and sink close -- close()
    # has always tidied up, which is why checking after it proved nothing. So
    # the count is taken across several broken Runs with no close in between.
    baseline = _open_fd_count()
    try:
        for index in range(4):
            submitted = host.submit(SubmitRequest(message=f"hello {index}"))
            assert submitted.kind == "accepted"
            result = host.wait(submitted.run_id)
            assert result.outcome == "completed", "a trace failure never breaks the Run"
        growth = _open_fd_count() - baseline
        assert growth <= 2, (
            f"four broken Runs leaked {growth} fds; "
            "each barrier must close the fd it owns"
        )
    finally:
        host.close()

    for sink in sinks:
        assert sink._bundles == {}, "close() leaves no bundle behind either"


@pytest.mark.parametrize(
    "replacement_point",
    ["staging", "meta", "trace", "artifacts", "publish", "append"],
)
def test_trace_bundle_replacement_never_reaches_external_directory(
    tmp_path, monkeypatch, replacement_point
) -> None:
    """Every publish/write step stays attached to verified directory fds."""
    reached = threading.Event()
    release = threading.Event()
    victim: list[Path] = []
    external = tmp_path / "external"
    external.mkdir(mode=0o755)
    (external / "sentinel").write_text("untouched", encoding="utf-8")
    before = external.stat()
    before_entries = sorted(path.name for path in external.iterdir())
    real_create = trace_module.ManagedDirectoryLease.create_directory
    real_open = trace_module.ManagedDirectoryLease.open_regular
    real_publish = (
        trace_module.ManagedDirectoryLease.rename_directory_no_replace
    )

    def pause(path: Path) -> None:
        victim.append(path)
        reached.set()
        assert release.wait(5.0), "replacement controller must release drain"

    def create(directory, relative, *, role, _rollback=None):
        if replacement_point == "artifacts" and role == "bundle artifacts directory":
            pause(directory.path)
        result = real_create(
            directory, relative, role=role, _rollback=_rollback
        )
        if replacement_point == "staging" and role == "bundle staging directory":
            pause(result.path)
        return result

    def open_regular(
        directory,
        relative,
        *,
        access,
        create,
        role="managed file",
        _rollback=None,
    ):
        if replacement_point == "meta" and role == "bundle metadata":
            pause(directory.path)
        if (
            replacement_point == "trace"
            and role == "bundle trace"
            and access == "exclusive_write"
        ):
            pause(directory.path)
        if (
            replacement_point == "append"
            and role == "bundle trace"
            and access == "append"
        ):
            pause(directory.path)
        return real_open(
            directory,
            relative,
            access=access,
            create=create,
            role=role,
            _rollback=_rollback,
        )

    def publish(directory, source, target):
        if replacement_point == "publish":
            pause(source.path)
        return real_publish(directory, source, target)

    monkeypatch.setattr(
        trace_module.ManagedDirectoryLease, "create_directory", create
    )
    monkeypatch.setattr(
        trace_module.ManagedDirectoryLease, "open_regular", open_regular
    )
    monkeypatch.setattr(
        trace_module.ManagedDirectoryLease,
        "rename_directory_no_replace",
        publish,
    )
    sink = _sink(tmp_path)
    try:
        _commit(sink, RunStarted(purpose="chat", user_message=None), 1)
        assert reached.wait(2.0), f"{replacement_point} seam was not reached"
        named = victim[0]
        displaced = named.with_name(f"{named.name}.displaced-{replacement_point}")
        os.rename(named, displaced)
        named.symlink_to(external, target_is_directory=True)
        release.set()
        result = sink.flush(run_id="run-bundle")

        after = external.stat()
        assert result.outcome == "failed"
        assert (after.st_dev, after.st_ino, after.st_mode, after.st_mtime_ns) == (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_mtime_ns,
        )
        assert sorted(path.name for path in external.iterdir()) == before_entries
        assert (external / "sentinel").read_text(encoding="utf-8") == "untouched"
    finally:
        release.set()
        sink.close()
