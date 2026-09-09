"""Real trace and SSE projections of registry execution."""

import json

from agent_alfred.clock import FakeClock
from agent_alfred.events import (
    EventEnvelope,
    FanOutSink,
    ToolFinished,
    UnsequencedEvent,
)
from agent_alfred.gateway.web.broker import SSEBroker
from agent_alfred.managed_state import ManagedStateDirectory
from agent_alfred.runtime.snapshot import RuntimeSnapshot
from agent_alfred.trace import RunBundleTraceSink


def payload(content):
    return ToolFinished(
        "c",
        "read",
        "ok",
        None,
        "short",
        content,
        True,
        len(content.encode()),
        "digest",
        0,
    )


def test_sse_omits_audit_content():
    broker = SSEBroker(
        process_instance_id="p", snapshot=RuntimeSnapshot("p", 0, "idle", None, None)
    )
    try:
        event = UnsequencedEvent(
            "e",
            EventEnvelope(0, "r", None, 0, None, None),
            payload("audit-only"),
            "persist",
            True,
        )
        frames = broker.prepare(event).with_checkpoint(1, "p")
        wire = b"".join(frames.wire_frames())
        assert b"audit-only" not in wire
        assert b"audit_content" not in wire
        assert b"short" in wire
    finally:
        broker.close()


def test_trace_moves_large_tool_audit_to_private_artifact(tmp_path):
    trace = RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(tmp_path / "traces"),
        clock=FakeClock(),
        process_instance_id="p",
    )
    fanout = FanOutSink([trace], process_instance_id="p")
    content = "你" * 100000
    try:
        fanout.emit(payload(content), EventEnvelope(0, "r", None, 0, None, None))
        assert fanout.flush_barrier("r")[0] is False
        path = next((tmp_path / "traces").rglob("trace.jsonl"))
        data = json.loads(path.read_text())["payload"]
        reference = data["audit_content"]
        assert isinstance(reference, dict)
        artifact = path.parent / reference["artifact"]
        assert artifact.read_text() == content
        assert artifact.stat().st_mode & 0o777 == 0o600
    finally:
        trace.close()


def test_real_fanout_accepts_tool_events_and_redacts_every_projection(tmp_path):
    from agent_alfred.events import CapturingSink
    from agent_alfred.messages import TextBlock, ToolCallBlock
    from agent_alfred.redact import Redactor
    from agent_alfred.tools import Tool, ToolContext, ToolRegistry, ToolSuccess

    redactor = Redactor(("synthetic-secret",))
    trace = RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(tmp_path / "trace"),
        clock=FakeClock(),
        process_instance_id="p",
    )
    capture = CapturingSink()
    fanout = FanOutSink([trace, capture], process_instance_id="p", redactor=redactor)
    registry = ToolRegistry(
        (
            Tool(
                "read",
                "Read",
                {"type": "object"},
                lambda a, c: ToolSuccess(
                    (TextBlock("synthetic-secret " + "你" * 100000),)
                ),
                "local_read",
            ),
        ),
        clock=FakeClock(),
        redactor=redactor,
    )
    try:
        registry.execute(
            ToolCallBlock("call", "read", {}),
            ToolContext("run", 0, "call", "cli", float("inf")),
            events=fanout,
        )
        assert not fanout.flush_barrier("run")[0]
        assert any(isinstance(e.payload, ToolFinished) for e in capture.events)
        for path in (tmp_path / "trace").rglob("*"):
            if path.is_file():
                assert b"synthetic-secret" not in path.read_bytes()
    finally:
        fanout.close()
