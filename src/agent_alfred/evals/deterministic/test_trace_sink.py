"""Slice-1 re-review: a real, persistence-critical production TraceSink.

ADR-0003/0004/0015: redaction at the FanOut entrance, the flush barrier, and
two-phase publish are already in place; these tests pin the missing piece --
the default production wiring must write a real Run bundle and the barrier
must be fail-closed when nothing durable is behind it (ADR-0017..0019).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.events import (
    BarrierFlushResult,
    CapturingSink,
    EventEnvelope,
    FanOutSink,
    FlushResult,
    RunStarted,
    SequencedEvent,
    UnsequencedEvent,
)
from agent_alfred.messages import message_plain_text
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
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
        assert line["trace_policy"] in ("persist", "transient")
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
