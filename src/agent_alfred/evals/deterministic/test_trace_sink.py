"""Slice-1 re-review: a real, persistence-critical production TraceSink.

ADR-0003/0004/0015: redaction at the FanOut entrance, the flush barrier, and
two-phase publish are already in place; these tests pin the missing piece --
the default production wiring must write a real Run bundle and the barrier
must be fail-closed when nothing durable is behind it (ADR-0017..0019).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.events import (
    AttemptCommitted,
    AttemptStarted,
    BarrierFlushResult,
    BlockDelta,
    BlockStarted,
    BlockStopped,
    CapturingSink,
    EventEnvelope,
    FanOutSink,
    FlushResult,
    RunStarted,
    SequencedEvent,
    StepFinished,
    StepStarted,
    UnsequencedEvent,
)
from agent_alfred.messages import TextBlock, message_plain_text
from agent_alfred.model import (
    AttemptRecord,
    ClientSnapshot,
    ModelResponse,
    ModelResult,
    ScriptedModel,
    ScriptedModelFactory,
    Usage,
)
from agent_alfred.runtime.host import RuntimeHost, SubmitRequest
from agent_alfred.settings import Settings
from agent_alfred.trace import RunBundleTraceSink
from agent_alfred.wiring import build_default_host


def _scripted_factory(script: list) -> ScriptedModelFactory:
    return ScriptedModelFactory(ScriptedModel(script))


def _run_one(host: RuntimeHost, message: str = "hello"):
    submitted = host.submit(SubmitRequest(message=message))
    assert submitted.kind == "accepted"
    result = host.wait(submitted.run_id)
    return submitted, result


def _telemetry(conn: sqlite3.Connection, run_id: str) -> dict:
    raw = conn.execute(
        "SELECT telemetry FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    return json.loads(raw)


def _bundles(state_dir: Path) -> list[Path]:
    traces = state_dir / "traces"
    if not traces.is_dir():
        return []
    found: list[Path] = []
    for date_dir in sorted(traces.iterdir()):
        if not date_dir.is_dir():
            continue
        found.extend(path for path in sorted(date_dir.iterdir()) if path.is_dir())
    return found


def _trace_lines(bundle: Path) -> list[dict]:
    raw = (bundle / "trace.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _storage_id(run_id: str) -> str:
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:32]


# --- the default production Host writes a real, published Run bundle ---


def test_default_production_host_writes_a_bundle_with_run_finished(tmp_path) -> None:
    host = build_default_host(state_dir=tmp_path, factory=_scripted_factory(["pong"]))
    host.start()
    try:
        submitted, result = _run_one(host)
        assert result.outcome == "completed"
        assert message_plain_text(result.reply) == "pong"
    finally:
        host.close()

    bundles = _bundles(tmp_path)
    assert len(bundles) == 1
    bundle = bundles[0]
    run_id = submitted.run_id
    assert re.fullmatch(r"\d{6}Z-[0-9a-f]{32}", bundle.name), bundle.name
    assert bundle.name.endswith(_storage_id(run_id))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", bundle.parent.name)
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o700

    meta = json.loads((bundle / "meta.json").read_text(encoding="utf-8"))
    assert meta["run_id"] == run_id
    assert meta["run_storage_id"] == _storage_id(run_id)
    assert meta["run_dir_name"] == bundle.name

    lines = _trace_lines(bundle)
    assert lines, "trace.jsonl must not be empty"
    names = [line["payload_name"] for line in lines]
    assert names[0] == "run.started"
    assert "step.started" in names
    assert names[-1] == "run.finished"
    assert lines[-1]["run_id"] == run_id
    seqs = [line["seq"] for line in lines]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

    conn = sqlite3.connect(str(tmp_path / "db.sqlite3"))
    try:
        telemetry = _telemetry(conn, run_id)
        assert telemetry["trace_incomplete"] is False
        assert telemetry["attempts"], "telemetry comes from the attempt ledger"
    finally:
        conn.close()


def test_successful_flush_writes_trace_incomplete_false(tmp_path) -> None:
    host = build_default_host(state_dir=tmp_path, factory=_scripted_factory(["pong"]))
    host.start()
    try:
        submitted, result = _run_one(host)
        assert result.outcome == "completed"
        assert message_plain_text(result.reply) == "pong"
        telemetry = _telemetry(host._conn, submitted.run_id)
        assert telemetry["trace_incomplete"] is False
    finally:
        host.close()


def test_default_production_host_publishes_through_staging_without_leftovers(
    tmp_path,
) -> None:
    host = build_default_host(state_dir=tmp_path, factory=_scripted_factory(["ok"]))
    host.start()
    try:
        _run_one(host)
    finally:
        host.close()
    traces = tmp_path / "traces"
    for date_dir in traces.iterdir():
        leftovers = [p for p in date_dir.iterdir() if p.name.startswith(".staging-")]
        assert leftovers == []


def test_bundle_files_are_managed_paths(tmp_path) -> None:
    host = build_default_host(state_dir=tmp_path, factory=_scripted_factory(["ok"]))
    host.start()
    try:
        _run_one(host)
    finally:
        host.close()
    assert stat.S_IMODE((tmp_path / "traces").stat().st_mode) == 0o700
    for bundle in _bundles(tmp_path):
        assert stat.S_IMODE(bundle.stat().st_mode) == 0o700
        assert stat.S_IMODE((bundle / "meta.json").stat().st_mode) == 0o600
        assert stat.S_IMODE((bundle / "trace.jsonl").stat().st_mode) == 0o600
        assert stat.S_IMODE((bundle / "artifacts").stat().st_mode) == 0o700


def test_trace_lines_are_valid_json_with_persist_policy(tmp_path) -> None:
    host = build_default_host(state_dir=tmp_path, factory=_scripted_factory(["ok"]))
    host.start()
    try:
        _run_one(host)
    finally:
        host.close()
    bundle = _bundles(tmp_path)[0]
    lines = _trace_lines(bundle)
    assert lines
    for line in lines:
        assert isinstance(line["seq"], int)
        assert line["process_instance_id"] == host.process_instance_id
        # ADR-0013: the persistent trace carries persist events only.
        assert line["trace_policy"] == "persist"
    persist = [line for line in lines if line["trace_policy"] == "persist"]
    assert persist[0]["payload_name"] == "run.started"


# --- the barrier is fail-closed when no durability-critical sink exists ---


def test_zero_critical_sinks_make_the_barrier_incomplete() -> None:
    fanout = FanOutSink(
        [CapturingSink(name="render", flush_at_run_end=False)],
        process_instance_id="proc-zero",
    )
    incomplete, reason = fanout.flush_barrier("run-1")
    assert incomplete is True
    assert reason is not None and "no flush_at_run_end sink" in reason


# --- failure modes stay fail-closed while the reply is still delivered ---


def test_trace_sink_init_failure_fails_closed_but_keeps_serving_runs(tmp_path) -> None:
    # The traces root cannot be created: a *file* occupies the path.
    (tmp_path / "traces").write_text("not a directory", encoding="utf-8")
    host = build_default_host(state_dir=tmp_path, factory=_scripted_factory(["pong"]))
    host.start()
    try:
        submitted, result = _run_one(host)
        assert result.outcome == "completed"
        assert message_plain_text(result.reply) == "pong"
        telemetry = _telemetry(host._conn, submitted.run_id)
        assert telemetry["trace_incomplete"] is True
        assert telemetry["trace_incomplete_reason"]
        assert _bundles(tmp_path) == []
    finally:
        host.close()


class CommitBoomSink:
    def __init__(self) -> None:
        self.name = "commit-boom"
        self.flush_at_run_end = True

    def prepare(self, event: UnsequencedEvent) -> object:
        del event
        return None

    def commit(self, prepared: object, event: SequencedEvent) -> None:
        del prepared, event
        raise RuntimeError("commit exploded")

    def flush(self) -> FlushResult:
        return BarrierFlushResult(outcome="flushed", dropped_events=0)

    def close(self) -> None:
        return None


def test_commit_exception_marks_trace_incomplete_and_delivers_the_reply() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    fanout = FanOutSink([CommitBoomSink()], process_instance_id="proc-commit-boom")
    host = RuntimeHost(
        conn=conn,
        factory=_scripted_factory(["pong"]),
        settings=Settings(),
        clock=FakeClock(),
        fanout=fanout,
        process_instance_id="proc-commit-boom",
    )
    host.start()
    try:
        submitted, result = _run_one(host)
        assert result.outcome == "completed"
        assert message_plain_text(result.reply) == "pong"
        telemetry = _telemetry(conn, submitted.run_id)
        assert telemetry["trace_incomplete"] is True
        assert "commit" in telemetry["trace_incomplete_reason"]
    finally:
        host.close()


def test_a_failed_run_does_not_poison_the_next_run_bundle(tmp_path) -> None:
    host = build_default_host(
        state_dir=tmp_path, factory=_scripted_factory(["one", "two"])
    )
    traces = tmp_path / "traces"
    traces.mkdir(mode=0o700, exist_ok=True)
    traces.chmod(0o500)  # publication (mkdir under root) fails for run 1
    host.start()
    try:
        first, result = _run_one(host)
        assert result.outcome == "completed", "disk failure must not abort the run"
        broken = _telemetry(host._conn, first.run_id)
        assert broken["trace_incomplete"] is True
        assert broken["trace_incomplete_reason"]
        assert _bundles(tmp_path) == []

        traces.chmod(0o700)  # the next Run retries building its own bundle
        second, result = _run_one(host, "two")
        assert result.outcome == "completed"
        good = _telemetry(host._conn, second.run_id)
        assert good["trace_incomplete"] is False
        bundles = _bundles(tmp_path)
        assert len(bundles) == 1
        assert bundles[0].name.endswith(_storage_id(second.run_id))
        names = [line["payload_name"] for line in _trace_lines(bundles[0])]
        assert names[-1] == "run.finished"
    finally:
        traces.chmod(0o700)
        host.close()


def test_close_stops_the_drain_thread_and_is_idempotent(tmp_path) -> None:
    host = build_default_host(state_dir=tmp_path, factory=_scripted_factory(["ok"]))
    host.start()
    try:
        _run_one(host)
    finally:
        host.close()
        host.close()
    threads = [t for t in threading.enumerate() if t.name == "trace-drain"]
    assert threads == []
    assert (tmp_path / "traces").is_dir()


# --- the sink itself: staging publication and identity collision ---


def _emit_one(sink: RunBundleTraceSink, run_id: str, text: str = "hello") -> None:
    envelope = EventEnvelope(0.0, run_id, None, None, None, None)
    prepared = sink.prepare(
        UnsequencedEvent(
            envelope=envelope,
            payload=RunStarted(purpose="chat", user_message=None),
            trace_policy="persist",
            replayable=True,
        )
    )
    sink.commit(
        prepared,
        SequencedEvent(
            seq=1,
            process_instance_id="proc-trace",
            envelope=envelope,
            payload=RunStarted(purpose="chat", user_message=None),
            trace_policy="persist",
            replayable=True,
        ),
    )
    del text


def test_sink_publishes_bundle_for_the_derived_identity(tmp_path) -> None:
    wall = datetime(2026, 8, 28, 12, 34, 56, tzinfo=timezone.utc)
    sink = RunBundleTraceSink(
        root=tmp_path / "traces",
        clock=FakeClock(wall=wall),
        process_instance_id="proc-trace",
    )
    try:
        _emit_one(sink, "run-abc")
        result = sink.flush()
        assert result == BarrierFlushResult(
            outcome="flushed", dropped_events=0, detail=""
        )
    finally:
        sink.close()

    bundle = tmp_path / "traces" / "2026-08-28" / f"123456Z-{_storage_id('run-abc')}"
    assert (bundle / "meta.json").is_file()
    meta = json.loads((bundle / "meta.json").read_text(encoding="utf-8"))
    assert meta["run_id"] == "run-abc"
    lines = _trace_lines(bundle)
    assert len(lines) == 1
    assert lines[0]["payload_name"] == "run.started"
    assert lines[0]["seq"] == 1


def test_sink_circuit_breaks_on_identity_collision(tmp_path) -> None:
    wall = datetime(2026, 8, 28, 12, 34, 56, tzinfo=timezone.utc)
    sink = RunBundleTraceSink(
        root=tmp_path / "traces",
        clock=FakeClock(wall=wall),
        process_instance_id="proc-trace",
    )
    occupied = (
        tmp_path / "traces" / "2026-08-28" / f"123456Z-{_storage_id('run-abc')}"
    )
    occupied.mkdir(parents=True)
    try:
        _emit_one(sink, "run-abc")
        result = sink.flush()
        assert result.outcome == "failed"
        assert "storage_id_collision" in result.detail
        assert (occupied / "trace.jsonl").exists() is False, "never overwrite"
    finally:
        sink.close()


# --- ADR-0013: transient events never enter the persistent trace -------------


def _commit(
    sink: RunBundleTraceSink,
    payload: object,
    seq: int,
    trace_policy: str,
    run_id: str = "run-policy",
) -> None:
    envelope = EventEnvelope(0.0, run_id, None, 0, None, None)
    prepared = sink.prepare(
        UnsequencedEvent(
            envelope=envelope,
            payload=payload,
            trace_policy=trace_policy,
            replayable=trace_policy == "persist",
        )
    )
    sink.commit(
        prepared,
        SequencedEvent(
            seq=seq,
            process_instance_id="proc-policy",
            envelope=envelope,
            payload=payload,
            trace_policy=trace_policy,
            replayable=trace_policy == "persist",
        ),
    )


def test_transient_events_never_enter_the_persistent_trace(tmp_path) -> None:
    wall = datetime(2026, 8, 28, 12, 34, 56, tzinfo=timezone.utc)
    sink = RunBundleTraceSink(
        root=tmp_path / "traces",
        clock=FakeClock(wall=wall),
        process_instance_id="proc-policy",
    )
    try:
        _commit(sink, RunStarted(purpose="chat", user_message=None), 1, "persist")
        _commit(sink, BlockStarted(attempt_id="a", index=0), 2, "transient")
        _commit(sink, BlockDelta(attempt_id="a", index=0, text="hel"), 3, "transient")
        _commit(sink, BlockDelta(attempt_id="a", index=0, text="lo"), 4, "transient")
        _commit(sink, BlockStopped(attempt_id="a", index=0), 5, "transient")
        _commit(sink, StepFinished(step_index=0), 6, "persist")
        result = sink.flush()
        assert result.outcome == "flushed"
        assert result.dropped_events == 0, (
            "normal transient filtering is not an event loss"
        )
    finally:
        sink.close()

    bundle = tmp_path / "traces" / "2026-08-28" / f"123456Z-{_storage_id('run-policy')}"
    lines = _trace_lines(bundle)
    names = [line["payload_name"] for line in lines]
    # The persist events of the same Run survive, in order, without the deltas.
    assert names == ["run.started", "step.finished"]
    assert all(line["trace_policy"] == "persist" for line in lines)


def test_a_transient_only_run_publishes_no_bundle(tmp_path) -> None:
    wall = datetime(2026, 8, 28, 12, 34, 56, tzinfo=timezone.utc)
    sink = RunBundleTraceSink(
        root=tmp_path / "traces",
        clock=FakeClock(wall=wall),
        process_instance_id="proc-policy",
    )
    try:
        _commit(sink, BlockDelta(attempt_id="a", index=0, text="x"), 1, "transient")
        result = sink.flush()
        assert result.outcome == "flushed"
        assert result.dropped_events == 0
    finally:
        sink.close()
    assert _bundles(tmp_path) == []


class _StreamingScriptedFactory:
    """Factory whose client streams transient block events like the adapter."""

    def __init__(self, text: str):
        self._text = text
        self.snapshots: list[ClientSnapshot] = []

    def create(self, snapshot: ClientSnapshot):
        self.snapshots.append(snapshot)
        text = self._text

        class _Streamed:
            def respond(self, request, *, events=None, deadline=None):
                del deadline
                attempt_id = "att-streamed"
                emit = getattr(events, "emit", None)
                if emit is not None:
                    emit(AttemptStarted(attempt_id=attempt_id, streamed=True))
                    emit(
                        BlockStarted(
                            attempt_id=attempt_id, index=0, block_type="text"
                        )
                    )
                    for part in ("he", "llo"):
                        emit(
                            BlockDelta(
                                attempt_id=attempt_id, index=0, text=part
                            )
                        )
                    emit(BlockStopped(attempt_id=attempt_id, index=0))
                    emit(
                        AttemptCommitted(
                            attempt_id=attempt_id,
                            blocks=(TextBlock(text),),
                            stop_reason="end_turn",
                        )
                    )
                return ModelResult(
                    attempts=(
                        AttemptRecord(
                            attempt_id=attempt_id,
                            streamed=True,
                            outcome="committed",
                            usage=Usage(),
                        ),
                    ),
                    response=ModelResponse(
                        blocks=(TextBlock(text),),
                        stop_reason="end_turn",
                        model=request.model,
                    ),
                    final_error=None,
                )

        return _Streamed()


def test_cli_stream_does_not_persist_block_deltas_into_the_trace(tmp_path) -> None:
    """The CLI stream=true send path (gateway.cli._send) must leave every
    incremental block out of the persisted trace; the committed attempt
    snapshot carries the content instead."""
    from agent_alfred.gateway.cli import _send

    host = build_default_host(
        state_dir=tmp_path, factory=_StreamingScriptedFactory("hello")
    )
    host.start()
    try:
        session_id = host.create_session()
        out = io.StringIO()
        code = _send(host, "hi", session_id, out, stream=True)
        assert code == 0
        assert "hello" in out.getvalue()
    finally:
        host.close()

    bundles = _bundles(tmp_path)
    assert len(bundles) == 1
    names = [line["payload_name"] for line in _trace_lines(bundles[0])]
    assert "run.started" in names
    assert "attempt.committed" in names
    assert names[-1] == "run.finished"
    for transient in ("block.started", "block.delta", "block.stopped"):
        assert transient not in names
    assert all(
        line["trace_policy"] == "persist" for line in _trace_lines(bundles[0])
    )


# --- ADR-0019: the write side keeps every published record a contiguous
# --- prefix; one queue item owns one byte offset across its retry lifecycle --


class _ScriptedWrite:
    """os.write stand-in scripting every write to the JSONL trace fd.

    The first write whose payload starts with ``{"seq"`` identifies the
    trace fd (meta.json writes pass through), after which each call to that
    fd -- full line, partial-write continuation, or retried line -- follows
    the script in order.
    """

    def __init__(self, real, behaviors: tuple):
        self._real = real
        self._behaviors = list(behaviors)
        self._lock = threading.Lock()
        self.calls = 0
        self._trace_fd: int | None = None

    def __call__(self, fd, data):
        if self._trace_fd is None and bytes(data[:6]) == b'{"seq"':
            self._trace_fd = fd
        if fd == self._trace_fd:
            with self._lock:
                index = self.calls
                self.calls += 1
            if index < len(self._behaviors):
                behavior = self._behaviors[index]
                if isinstance(behavior, int):
                    return self._real(fd, data[:behavior])
                if isinstance(behavior, BaseException):
                    raise behavior
        return self._real(fd, data)


def _utc_sink(tmp_path: Path) -> RunBundleTraceSink:
    return RunBundleTraceSink(
        root=tmp_path / "traces",
        clock=FakeClock(wall=datetime(2026, 8, 28, 12, 34, 56, tzinfo=timezone.utc)),
        process_instance_id="proc-write",
    )


@pytest.mark.parametrize(
    "retryable",
    [BlockingIOError(11, "eagain"), InterruptedError()],
)
def test_partial_write_then_retryable_error_writes_one_clean_line(
    tmp_path, monkeypatch, retryable
) -> None:
    real = os.write
    monkeypatch.setattr(
        os, "write", _ScriptedWrite(real, (12, retryable, "rest"))
    )
    sink = _utc_sink(tmp_path)
    try:
        _commit(sink, RunStarted(purpose="chat", user_message=None), 1, "persist")
        _commit(sink, StepFinished(step_index=0), 2, "persist")
        result = sink.flush()
        assert result.outcome == "flushed"
        assert result.dropped_events == 0
    finally:
        sink.close()

    bundle = tmp_path / "traces" / "2026-08-28" / f"123456Z-{_storage_id('run-policy')}"
    raw = (bundle / "trace.jsonl").read_text(encoding="utf-8")
    assert raw.count('{"seq"') == 2, "a written prefix is never resubmitted"
    lines = raw.splitlines()
    assert len(lines) == 2
    for line in lines:
        parsed = json.loads(line)  # no prefix + full-line corruption
        assert parsed["trace_policy"] == "persist"


def test_after_a_write_break_no_lines_follow_the_truncated_tail(
    tmp_path, monkeypatch
) -> None:
    real = os.write
    monkeypatch.setattr(
        os,
        "write",
        _ScriptedWrite(real, (12, OSError(5, "input/output error"))),
    )
    sink = _utc_sink(tmp_path)
    try:
        _commit(sink, RunStarted(purpose="chat", user_message=None), 1, "persist")
        # Later events of the broken Run must never be appended behind the
        # truncated tail: an internal bad line followed by good lines would
        # turn a damaged audit file into one that reads as complete.
        _commit(sink, StepFinished(step_index=0), 2, "persist")
        result = sink.flush()
        assert result.outcome == "failed"
        assert result.dropped_events >= 1
        assert "write_failed" in result.detail
    finally:
        sink.close()

    bundle = tmp_path / "traces" / "2026-08-28" / f"123456Z-{_storage_id('run-policy')}"
    raw = (bundle / "trace.jsonl").read_text(encoding="utf-8")
    assert raw.count('{"seq"') == 1, "the prefix is never resubmitted"


def test_unrecoverable_write_failure_reports_the_first_reason_once(
    tmp_path, monkeypatch
) -> None:
    """The first failure cause survives; the retry must not resubmit the
    already-written prefix (the old code re-wrote the whole line)."""
    real = os.write
    writer = _ScriptedWrite(real, (12, OSError(5, "input/output error")))
    monkeypatch.setattr(os, "write", writer)
    sink = _utc_sink(tmp_path)
    try:
        _commit(sink, RunStarted(purpose="chat", user_message=None), 1, "persist")
        result = sink.flush()
        assert result.outcome == "failed"
        assert result.dropped_events >= 1
        assert "write_failed" in result.detail
        assert "OSError" in result.detail
    finally:
        sink.close()
    # One partial write + one unrecoverable error: exactly two calls. The
    # prefix was never resubmitted.
    assert writer.calls == 2


def test_retry_budget_exhaustion_circuit_breaks_the_run(
    tmp_path, monkeypatch
) -> None:
    real = os.write
    monkeypatch.setattr(
        os, "write", _ScriptedWrite(real, (BlockingIOError(11, "eagain"),) * 10)
    )
    sink = _utc_sink(tmp_path)
    try:
        _commit(sink, RunStarted(purpose="chat", user_message=None), 1, "persist")
        result = sink.flush()
        assert result.outcome == "failed"
        assert "write_failed" in result.detail
    finally:
        sink.close()
    # The write raised from the very first call: nothing was ever published,
    # and nothing may be appended afterwards either.
    bundle = tmp_path / "traces" / "2026-08-28" / f"123456Z-{_storage_id('run-policy')}"
    assert (bundle / "trace.jsonl").read_text(encoding="utf-8") == ""


def test_a_write_failed_run_does_not_poison_the_next_run(
    tmp_path, monkeypatch
) -> None:
    real = os.write

    def failing_for_run_one(fd, data):
        view = bytes(data)
        if view.startswith(b'{"seq"') and b'"run_id": "run-one"' in view:
            raise OSError(5, "input/output error")
        return real(fd, data)

    monkeypatch.setattr(os, "write", failing_for_run_one)
    sink = _utc_sink(tmp_path)
    try:
        _commit(
            sink,
            RunStarted(purpose="chat", user_message=None),
            1,
            "persist",
            run_id="run-one",
        )
        first = sink.flush()
        assert first.outcome == "failed"
        assert "write_failed" in first.detail

        _commit(
            sink,
            RunStarted(purpose="chat", user_message=None),
            2,
            "persist",
            run_id="run-two",
        )
        second = sink.flush()
        assert second.outcome == "flushed"
        assert second.dropped_events == 0
    finally:
        sink.close()

    bundles = {
        bundle.name: bundle for bundle in _bundles(tmp_path)
    }
    second_lines = _trace_lines(
        bundles[f"123456Z-{_storage_id('run-two')}"]
    )
    assert [line["payload_name"] for line in second_lines] == ["run.started"]
    first_raw = (
        bundles[f"123456Z-{_storage_id('run-one')}"] / "trace.jsonl"
    ).read_text(encoding="utf-8")
    assert first_raw == "", "the broken Run never publishes a partial line"


# --- ADR-0015: commit never waits on the drain thread's disk I/O -------------


def test_commit_returns_while_the_drain_is_blocked_on_disk_io(
    tmp_path, monkeypatch
) -> None:
    real = os.write
    gate = threading.Event()
    first_line_blocked = threading.Event()

    def slow_disk(fd, data):
        if bytes(data[:6]) == b'{"seq"' and not gate.is_set():
            first_line_blocked.set()
            gate.wait(5.0)
        return real(fd, data)

    monkeypatch.setattr(os, "write", slow_disk)
    sink = _utc_sink(tmp_path)
    capture = CapturingSink(name="capture", flush_at_run_end=False)
    fanout = FanOutSink([sink, capture], process_instance_id="proc-block")
    try:
        _commit(sink, RunStarted(purpose="chat", user_message=None), 1, "persist")
        assert first_line_blocked.wait(2.0), "the drain must reach the write"
        envelope = EventEnvelope(0.0, "run-policy", None, 0, None, None)
        worst = 0.0
        for i in range(20):
            started = time.monotonic()
            fanout.emit(StepStarted(step_index=i), envelope)
            worst = max(worst, time.monotonic() - started)
        assert worst < 0.5, (
            f"commit blocked on the drain's disk I/O for {worst:.3f}s"
        )
        assert len(capture.events) == 20, (
            "other sinks publish while the drain is stalled on disk"
        )
        gate.set()
        result = sink.flush()
        assert result.outcome == "flushed"
        assert result.dropped_events == 0
    finally:
        gate.set()
        sink.close()

    bundle = tmp_path / "traces" / "2026-08-28" / f"123456Z-{_storage_id('run-policy')}"
    names = [line["payload_name"] for line in _trace_lines(bundle)]
    assert names[0] == "run.started"
    assert names.count("step.started") == 20
    assert all(line["trace_policy"] == "persist" for line in _trace_lines(bundle))


# --- the Run's final barrier releases its fd and active bundle ---------------


def _open_fd_count() -> int:
    for candidate in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(candidate))
        except OSError:
            continue
    pytest.skip("no /proc/self/fd or /dev/fd on this platform")


def test_active_bundles_and_fds_do_not_grow_with_run_count(tmp_path) -> None:
    host = build_default_host(
        state_dir=tmp_path, factory=_scripted_factory(["ok"] * 8)
    )
    host.start()
    try:
        sink = next(
            s for s in host._fanout.sinks if isinstance(s, RunBundleTraceSink)
        )
        baseline = _open_fd_count()
        for i in range(8):
            submitted = host.submit(SubmitRequest(message=f"m{i}"))
            assert submitted.kind == "accepted"
            host.wait(submitted.run_id)
            # The run's final barrier released its fd and its active bundle.
            assert sink._bundles == {}, f"run {i} left an active bundle"
            assert _open_fd_count() <= baseline + 2
    finally:
        host.close()


def test_late_commit_after_the_barrier_is_rejected_without_touching_the_trace(
    tmp_path,
) -> None:
    from agent_alfred.trace import LateCommitRejected

    sink = _utc_sink(tmp_path)
    try:
        _commit(sink, RunStarted(purpose="chat", user_message=None), 1, "persist")
        result = sink.flush()
        assert result.outcome == "flushed"
        bundle = (
            tmp_path / "traces" / "2026-08-28" / f"123456Z-{_storage_id('run-policy')}"
        )
        before = (bundle / "trace.jsonl").read_bytes()

        with pytest.raises(LateCommitRejected):
            _commit(sink, StepFinished(step_index=0), 2, "persist")
        assert (bundle / "trace.jsonl").read_bytes() == before, (
            "a late commit must not reopen or extend a published bundle"
        )
        # A second barrier after termination stays honest and quiet.
        again = sink.flush()
        assert again.outcome == "flushed"
    finally:
        sink.close()


def test_sink_close_is_idempotent_and_survives_an_active_run(tmp_path) -> None:
    sink = _utc_sink(tmp_path)
    _commit(sink, RunStarted(purpose="chat", user_message=None), 1, "persist")
    sink.close()
    sink.close()  # no exception, no double-close of an fd

    second = _utc_sink(tmp_path)
    try:
        _commit(second, RunStarted(purpose="chat", user_message=None), 1, "persist")
    finally:
        second.close()  # closing mid-run (barrier never reached) must not raise
        second.close()
