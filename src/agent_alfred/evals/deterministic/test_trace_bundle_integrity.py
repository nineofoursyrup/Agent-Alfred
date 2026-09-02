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

import errno
import json
import os
import shutil
import sqlite3
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePath

import pytest

from agent_alfred import managed_state as managed_module
from agent_alfred import trace as trace_module
from agent_alfred.clock import FakeClock
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
from agent_alfred.managed_state import ManagedPathSecurityError, ManagedStateDirectory
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
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


def _sink(tmp_path: Path) -> RunBundleTraceSink:
    return RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(tmp_path / "traces"),
        clock=FakeClock(wall=WALL),
        process_instance_id="proc-bundle",
    )


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
        _close_bundle_resources = trace_module._close_bundle_resources
        _close_bundle_resources(
            [(healthy.trace_file, healthy.artifacts_dir, healthy.run_dir)]
        )
        sink.close()


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


def _wait_until(predicate, timeout: float = 2.0) -> None:
    """Commits are asynchronous; wait for the drain to reach a known point."""
    deadline = _monotonic() + timeout
    while _monotonic() < deadline:
        if predicate():
            return
        _sleep(0.005)
    raise AssertionError("condition not met before timeout")


def _published(sink: RunBundleTraceSink, run_id: str) -> bool:
    bundle = sink._bundles.get(run_id)
    return bundle is not None and bundle.trace_fd is not None


def _monotonic() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


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
    try:
        _commit(sink, RunStarted(purpose="chat", user_message=None), 1)
        _wait_until(lambda: _published(sink, "run-bundle"))
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
    try:
        _commit(sink, RunStarted(purpose="chat", user_message=None), 1)
        _wait_until(lambda: _published(sink, "run-bundle"))
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
        published = (
            [path.name for path in date_dir.iterdir()] if date_dir.is_dir() else []
        )
        assert published == [], f"nothing is published: {published}"
    finally:
        sink.close()


def test_a_failed_publish_removes_only_its_own_managed_staging(tmp_path) -> None:
    """ADR-0017: staging reclamation validates the exact managed shape; it is
    never a broad recursive delete of a path it did not create."""
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
    # shape is unrecognizable, so it is left to the reclaimer.
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


def test_reclamation_still_removes_a_complete_managed_staging(tmp_path) -> None:
    """The type check must not over-tighten into a ban: the full shape the
    publisher actually leaves behind is still reclaimed."""
    staging = _bare_staging(tmp_path, ".staging-44444444444444444444444444444444")
    (staging / "meta.json").write_text('{"run_id": "x"}', encoding="utf-8")
    (staging / "trace.jsonl").write_text("", encoding="utf-8")
    (staging / "artifacts").mkdir()

    assert _remove_staging(staging) is True
    assert not staging.exists()


def test_reclamation_removes_a_partial_staging_left_by_a_crash(tmp_path) -> None:
    """A publish can die between any two entries, so an incomplete staging is
    a normal leftover rather than an unrecognizable one.

    Every prefix of the publisher's write order must still be reclaimed.
    Requiring all three entries would strand real crash leftovers forever --
    permanent debris of exactly the kind ADR-0017 introduced staging to avoid
    -- so the rule is "the entries that exist have the right type", never
    "the whole set is present".
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
        assert not staging.exists(), prefix


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
        nonlocal swapped, replacement_identity
        name = os.fspath(path)
        selected = (
            created_role == "staging" and name.startswith(".staging-")
        ) or (created_role == "artifacts" and name == "artifacts")
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
        host.close()

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
    replacement_stat: os.stat_result | None = None

    def fail_first_created_stat(path, *, dir_fd=None, follow_symlinks=True):
        nonlocal injected, replacement_stat
        name = os.fspath(path)
        selected = (
            created_role == "staging" and name.startswith(".staging-")
        ) or (created_role == "artifacts" and name == "artifacts")
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
        host.close()
    if stat_failure == "process_control":
        assert thread_error.get("exc_value") is control
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

    staging = [
        path
        for date_dir in (tmp_path / "traces").iterdir()
        for path in date_dir.iterdir()
        if path.name.startswith(".staging-")
    ]
    assert injected
    assert staging, "an identity-unknown staging transaction is retained"
    if replace_before_failure:
        assert replacement_stat is not None
        candidates = (
            staging
            if created_role == "staging"
            else [path for root in staging for path in root.rglob("artifacts")]
        )
        replacement_path = next(
            (
                path
                for path in candidates
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

    def create(directory, relative, *, role):
        if replacement_point == "artifacts" and role == "bundle artifacts directory":
            pause(directory.path)
        result = real_create(directory, relative, role=role)
        if replacement_point == "staging" and role == "bundle staging directory":
            pause(result.path)
        return result

    def open_regular(directory, relative, *, access, create, role="managed file"):
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
