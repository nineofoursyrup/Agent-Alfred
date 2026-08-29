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

import json
import os
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

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
from agent_alfred.trace import (
    REASON_FSYNC_FAILED,
    REASON_ID_COLLISION,
    REASON_WRITE_FAILED,
    RunBundleTraceSink,
    _storage_id,
)

WALL = datetime(2026, 8, 28, 12, 34, 56, tzinfo=timezone.utc)
_STAGING_NAME = ".staging-0123456789abcdef0123456789abcdef"


def _sink(tmp_path: Path) -> RunBundleTraceSink:
    return RunBundleTraceSink(
        root=tmp_path / "traces",
        clock=FakeClock(wall=WALL),
        process_instance_id="proc-bundle",
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
    try:
        for index in range(6):
            run_id = f"run-leak-{index}"
            _commit(sink, RunStarted(purpose="chat", user_message=None), 1, run_id)
            result = sink.flush(run_id=run_id)
            assert result.outcome == "failed"
            assert sink._bundles == {}, "every bundle is retired at its barrier"
        assert _open_fd_count() <= baseline + 2, (
            f"fd count grew from {baseline} to {_open_fd_count()}"
        )
    finally:
        sink.close()


def test_flush_close_and_drain_unwind_never_double_close(tmp_path, monkeypatch) -> None:
    """The three paths that can release a bundle's fd -- a healthy barrier, a
    broken bundle's barrier, and the drain's unwind after close -- must each
    release it exactly once."""
    tracker = _CloseTracker()
    real_open, real_close = os.open, os.close
    breaking = {"on": False}
    real_write = os.write

    def tracked_open(path, flags, mode=0o777, **kwargs):
        fd = real_open(path, flags, mode, **kwargs)
        tracker.note_open(fd)
        return fd

    def tracked_close(fd):
        tracker.note_close(fd)
        return real_close(fd)

    def gated_trace_write(fd, data):
        if breaking["on"] and bytes(data[:6]) == b'{"seq"':
            raise OSError(5, "input/output error")
        return real_write(fd, data)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "close", tracked_close)
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
        fd = sink._bundles["run-bundle"].trace_fd
        assert fd is not None
        tracker.note_open(fd)

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
    date_dir = tmp_path / "traces" / "2026-08-28"
    date_dir.mkdir(parents=True)
    staging = date_dir / name
    staging.mkdir()
    (staging / "meta.json").write_text('{"run_id": "x"}', encoding="utf-8")
    return staging, date_dir / "123456Z-0123456789abcdef0123456789abcdef"


def test_the_platform_primitive_refuses_an_empty_directory_target(tmp_path) -> None:
    staging, target = _staging_and_target(tmp_path)
    target.mkdir()

    with pytest.raises(FileExistsError):
        trace_module._rename_no_replace(staging, target)

    assert target.is_dir() and not any(target.iterdir()), (
        "an existing empty directory is never replaced"
    )
    assert (staging / "meta.json").is_file()


def test_the_platform_primitive_refuses_a_non_empty_directory_target(tmp_path) -> None:
    staging, target = _staging_and_target(tmp_path)
    target.mkdir()
    (target / "keep.txt").write_text("concurrent contents", encoding="utf-8")
    (target / "artifacts").mkdir()

    with pytest.raises(FileExistsError):
        trace_module._rename_no_replace(staging, target)

    assert (target / "keep.txt").read_text(encoding="utf-8") == "concurrent contents"
    assert sorted(path.name for path in target.iterdir()) == [
        "artifacts",
        "keep.txt",
    ], "a concurrent bundle is never merged into or overwritten"


def test_the_primitive_publishes_atomically_and_keeps_the_bundle_shape(
    tmp_path,
) -> None:
    staging, target = _staging_and_target(tmp_path)
    (staging / "trace.jsonl").write_text("", encoding="utf-8")
    (staging / "artifacts").mkdir()

    trace_module._rename_no_replace(staging, target)

    assert sorted(path.name for path in target.iterdir()) == [
        "artifacts",
        "meta.json",
        "trace.jsonl",
    ]
    assert (target / "meta.json").read_text(encoding="utf-8") == '{"run_id": "x"}'
    assert not staging.exists(), "the staging name is consumed by the rename"


def test_the_primitive_rejects_a_cross_directory_rename(tmp_path) -> None:
    staging = tmp_path / "a" / _STAGING_NAME
    staging.mkdir(parents=True)
    target = tmp_path / "b" / "123456Z-0123456789abcdef0123456789abcdef"
    with pytest.raises(ValueError, match="same directory"):
        trace_module._rename_no_replace(staging, target)


def test_publish_never_replaces_a_target_created_after_the_check(
    tmp_path, monkeypatch
) -> None:
    """The TOCTOU window: another publisher lands between the existence check
    and the rename. Only an atomic no-replace rename can refuse it."""
    real_rename = trace_module._rename_no_replace
    created = threading.Event()

    def racing_rename(staging, target):
        target.mkdir(mode=0o700)
        (target / "concurrent.txt").write_text("other publisher", encoding="utf-8")
        created.set()
        return real_rename(staging, target)

    monkeypatch.setattr(trace_module, "_rename_no_replace", racing_rename)
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
    monkeypatch.setattr(trace_module, "_platform_rename", lambda platform: None)
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
    assert trace_module._remove_staging(good) is True
    assert not good.exists()

    # Not a staging name at all: refused and left untouched.
    foreign = date_dir / "123456Z-0123456789abcdef0123456789abcdef"
    foreign.mkdir()
    (foreign / "meta.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="not a managed staging directory"):
        trace_module._remove_staging(foreign)
    assert (foreign / "meta.json").is_file()

    # A staging name pointing elsewhere: refused, never followed.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep me", encoding="utf-8")
    linked = date_dir / ".staging-fedcba9876543210fedcba9876543210"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="not a managed staging directory"):
        trace_module._remove_staging(linked)
    assert (outside / "precious.txt").read_text(encoding="utf-8") == "keep me"
    assert linked.is_symlink(), "the refused path is not removed either"

    # A staging directory holding something the publisher never writes: the
    # shape is unrecognizable, so it is left to the reclaimer.
    odd = date_dir / ".staging-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    odd.mkdir()
    (odd / "surprise").write_text("?", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected"):
        trace_module._remove_staging(odd)
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
        trace_module._remove_staging(staging)

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
        trace_module._remove_staging(staging)

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
        trace_module._remove_staging(staging)

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
        trace_module._remove_staging(staging)

    assert staging.is_dir(), "a refused shape deletes nothing"
    assert stat.S_ISFIFO((staging / "trace.jsonl").lstat().st_mode)


def test_reclamation_still_removes_a_complete_managed_staging(tmp_path) -> None:
    """The type check must not over-tighten into a ban: the full shape the
    publisher actually leaves behind is still reclaimed."""
    staging = _bare_staging(tmp_path, ".staging-44444444444444444444444444444444")
    (staging / "meta.json").write_text('{"run_id": "x"}', encoding="utf-8")
    (staging / "trace.jsonl").write_text("", encoding="utf-8")
    (staging / "artifacts").mkdir()

    assert trace_module._remove_staging(staging) is True
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

        assert trace_module._remove_staging(staging) is True, prefix
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
    from agent_alfred.model import ScriptedModel, ScriptedModelFactory
    from agent_alfred.runtime.host import SubmitRequest
    from agent_alfred.wiring import build_default_host

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


def test_broken_bundle_fd_is_released_for_a_real_host_run(
    tmp_path, monkeypatch
) -> None:
    """The leak as a user would hit it: one real Run whose trace write fails,
    driven through the production Host."""
    from agent_alfred.model import ScriptedModel, ScriptedModelFactory
    from agent_alfred.runtime.host import SubmitRequest
    from agent_alfred.wiring import build_default_host

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
