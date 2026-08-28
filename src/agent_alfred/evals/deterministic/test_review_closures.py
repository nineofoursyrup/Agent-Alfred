"""Review closures for issue #13: fail on HEAD 61410df, then stay green.

Seams: RuntimeHost.submit, FanOutSink, the public ModelClient chain,
OpenAICompatibleAdapter (wire only), and the shared Redactor.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
import stat
import threading
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.events import (
    BarrierFlushResult,
    CapturingSink,
    EventEnvelope,
    FanOutSink,
    FlushResult,
    Notice,
    SequencedEvent,
    UnsequencedEvent,
)
from agent_alfred.messages import TextBlock, message_plain_text, text_message
from agent_alfred.model import (
    ModelRef,
    ModelRequest,
    ScriptedModel,
    ScriptedModelFactory,
)
from agent_alfred.openai_compatible import OpenAICompatibleAdapter
from agent_alfred.runtime.config import MutableAssignmentProvider
from agent_alfred.runtime.host import RuntimeHost, SubmitRequest
from agent_alfred.settings import Settings
from agent_alfred.stream_fallback import StreamFallback
from agent_alfred.wiring import OpenCodeGoFactory, open_database

SECRET = "supersecret-key-value"
ROTATED = "rotated-secret-key-value"
_MODEL = ModelRef(endpoint_id="opencode-go", model_id="deepseek-v4-flash")


def _request(text: str = "hi") -> ModelRequest:
    return ModelRequest(
        model=_MODEL,
        system=(TextBlock("You are Alfred."),),
        messages=(text_message("user", text),),
    )


class _FailOn:
    def __init__(self, inner: sqlite3.Connection, when):
        self._inner = inner
        self._when = when

    def execute(self, sql, parameters=()):
        if self._when(sql, parameters):
            raise sqlite3.OperationalError("injected write failure")
        return self._inner.execute(sql, parameters)

    def commit(self):
        return self._inner.commit()

    def rollback(self):
        return self._inner.rollback()

    def close(self):
        return self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)


class BoomCaptureProvider:
    def __init__(self, exc: BaseException):
        self.exc = exc
        self.calls = 0

    def capture(self, *, stream: bool = False) -> object:
        del stream
        self.calls += 1
        raise self.exc


class BoomPrepareSink:
    def __init__(self, *, name: str, flush_at_run_end: bool):
        self.name = name
        self.flush_at_run_end = flush_at_run_end
        self.prepare_calls = 0
        self.commit_calls = 0

    def prepare(self, event: UnsequencedEvent) -> object:
        del event
        self.prepare_calls += 1
        raise RuntimeError("cannot prepare")

    def commit(self, prepared: object, event: SequencedEvent) -> None:
        del prepared, event
        self.commit_calls += 1

    def flush(self) -> FlushResult:
        if self.flush_at_run_end:
            return BarrierFlushResult(outcome="flushed", dropped_events=0)
        from agent_alfred.events import BestEffortFlushResult

        return BestEffortFlushResult(outcome="best_effort")

    def close(self) -> None:
        return None


class FailingFlushSink:
    def __init__(self, *, name: str = "fail-flush"):
        self.name = name
        self.flush_at_run_end = True

    def prepare(self, event: UnsequencedEvent) -> object:
        del event
        return None

    def commit(self, prepared: object, event: SequencedEvent) -> None:
        del prepared, event

    def flush(self) -> FlushResult:
        return BarrierFlushResult(outcome="failed", dropped_events=3)

    def close(self) -> None:
        return None


class FakeCompletions:
    """Minimal OpenAI-shaped chat.completions surface for Adapter tests."""

    def __init__(self, script: list[object]):
        self.script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("FakeCompletions has no remaining responses")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeOpenAI:
    def __init__(self, script: list[object]):
        completions = FakeCompletions(script)
        self.completions = completions
        self.chat = SimpleNamespace(completions=completions)
        self.calls = completions.calls


def _ns(**kwargs):
    return SimpleNamespace(**kwargs)


def _nonstream_completion(text: str, *, finish: str = "stop"):
    return _ns(
        choices=[_ns(message=_ns(content=text), finish_reason=finish)],
        usage=_ns(
            prompt_tokens=3,
            completion_tokens=2,
            model_dump=lambda: {"prompt_tokens": 3},
        ),
    )


def _stream_chunks(parts: list[str], *, finish: str | None):
    chunks = []
    for part in parts:
        chunks.append(
            _ns(
                choices=[_ns(delta=_ns(content=part), finish_reason=None)],
                usage=None,
            )
        )
    chunks.append(
        _ns(
            choices=[_ns(delta=_ns(content=None), finish_reason=finish)],
            usage=_ns(
                prompt_tokens=2,
                completion_tokens=len(parts),
                model_dump=lambda: {},
            ),
        )
    )
    return chunks


class AuthError(Exception):
    def __init__(self, status_code: int, message: str = "unauthorized"):
        super().__init__(message)
        self.status_code = status_code


class InterruptedStream:
    def __iter__(self):
        yield _ns(
            choices=[_ns(delta=_ns(content="partial"), finish_reason=None)],
            usage=None,
        )
        raise ConnectionError("stream disconnected")


@dataclass(frozen=True)
class UnknownSensitivePayload:
    name: str = "unknown.payload"
    trace_policy: str = "persist"
    token: str = "not-a-loaded-secret"


def _client(
    fake: FakeOpenAI,
    *,
    stream: bool = True,
    stream_fallback: bool = True,
    clock: FakeClock | None = None,
    streaming_adapter: OpenAICompatibleAdapter | None = None,
) -> StreamFallback:
    clock = clock or FakeClock()
    streaming = streaming_adapter or OpenAICompatibleAdapter(
        client=fake, model=_MODEL, stream=True
    )
    nonstream = OpenAICompatibleAdapter(client=fake, model=_MODEL, stream=False)
    return StreamFallback(
        streaming,
        clock=clock,
        stream=stream,
        stream_fallback=stream_fallback,
        nonstream=nonstream,
    )


def _host(
    *,
    script: list | None = None,
    extra_sinks: list | None = None,
    secrets: tuple[str, ...] = (),
    snapshot_provider=None,
    factory=None,
    publish_work=None,
    conn: sqlite3.Connection | None = None,
    settings: Settings | None = None,
    fanout_redactor=None,
):
    if conn is None:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        schema.migrate(conn)
    capture = CapturingSink(name="capture", flush_at_run_end=True)
    sinks = [capture, *(extra_sinks or ())]
    fanout = FanOutSink(
        sinks, process_instance_id="proc-review", redactor=fanout_redactor
    )
    host = RuntimeHost(
        conn=conn,
        factory=factory or ScriptedModelFactory(ScriptedModel(script or ["pong"])),
        settings=settings or Settings(),
        clock=FakeClock(),
        fanout=fanout,
        process_instance_id="proc-review",
        publish_work=publish_work,
        secrets=secrets,
        snapshot_provider=snapshot_provider,
    )
    return host, conn, capture


def _payload_names(capture: CapturingSink) -> list[str]:
    return [event.payload.name for event in capture.events]


def _dumped_events(capture: CapturingSink) -> str:
    return json.dumps(
        [repr(event.payload) for event in capture.events],
        ensure_ascii=False,
    )


# --- 1. rotated key must enter the shared redactor before preview ---


def test_rotated_key_is_redacted_on_the_first_run_preview() -> None:
    provider = MutableAssignmentProvider(
        endpoint_id="opencode-go",
        model_id="deepseek-v4-flash",
        wire_style="openai",
        api_key=SECRET,
        settings=Settings(),
    )
    host, conn, _ = _host(
        script=["ok"],
        secrets=(SECRET,),
        snapshot_provider=provider,
    )
    host.start()
    try:
        provider.rotate_key(ROTATED)
        submitted = host.submit(
            SubmitRequest(message=f"my key is {ROTATED} please")
        )
        assert submitted.kind == "accepted"
        host.wait(submitted.run_id)
        preview = conn.execute("SELECT prompt_preview FROM runs").fetchone()[0]
        blob = " ".join(
            str(cell)
            for row in conn.execute("SELECT * FROM runs").fetchall()
            for cell in row
        )
        assert ROTATED not in preview
        assert "***" in preview
        assert ROTATED not in blob
    finally:
        host.close()


# --- 2. capture() failure must roll admission back completely ---


def test_snapshot_capture_failure_rolls_back_admission() -> None:
    published: list[object] = []
    provider = BoomCaptureProvider(
        RuntimeError(f"cannot capture {SECRET}")
    )
    host, conn, _ = _host(
        script=["unused"],
        snapshot_provider=provider,
        publish_work=published.append,
        secrets=(SECRET,),
    )
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        assert submitted.kind == "admission_failed"
        assert submitted.run_id is None
        assert provider.calls == 1
        assert published == []
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone() == (0,)
        assert host.snapshot().coordinator_state == "idle"
        assert host.snapshot().active_run is None
        assert SECRET not in repr(submitted)
    finally:
        host.close()


# --- 3. handoff + interrupted finalize both fail => fail-closed ---


def test_handoff_and_interrupt_double_failure_fails_closed(tmp_path) -> None:
    path = tmp_path / "double-fail.sqlite3"
    raw = sqlite3.connect(str(path), check_same_thread=False)
    schema.migrate(raw)
    fail_interrupt = True

    def when(sql: str, _parameters) -> bool:
        if not fail_interrupt:
            return False
        stripped = sql.lstrip().upper()
        return stripped.startswith("UPDATE RUNS") and "FINISHED_AT" in stripped

    wrapped = _FailOn(raw, when)

    def boom(_item) -> None:
        raise RuntimeError("queue full")

    host, conn, _ = _host(script=["unused"], publish_work=boom, conn=wrapped)
    host.start()
    try:
        first = host.submit(SubmitRequest(message="one"))
        assert first.kind == "admission_failed"
        leftover = conn.execute(
            "SELECT run_id, phase, outcome FROM runs"
        ).fetchall()
        assert leftover
        assert all(row[1] == "accepted" and row[2] is None for row in leftover)
        second = host.submit(SubmitRequest(message="two"))
        assert second.kind == "recording_unavailable"
        third = host.submit(SubmitRequest(message="three"))
        assert third.kind == "recording_unavailable"
        accepted = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE phase = 'accepted'"
        ).fetchone()[0]
        assert accepted == 1
        assert host.snapshot().coordinator_state != "idle"
    finally:
        host.close()

    fail_interrupt = False
    recovered, conn2, _ = _host(script=["pong"], conn=raw)
    recovered.start()
    try:
        stored = conn2.execute("SELECT phase, outcome FROM runs").fetchall()
        assert stored == [("finished", "interrupted")]
        nxt = recovered.submit(SubmitRequest(message="after-recovery"))
        assert nxt.kind == "accepted"
        recovered.wait(nxt.run_id)
    finally:
        recovered.close()
        raw.close()


# --- 4. missing API key: zero network, zero Attempt ---


def test_missing_api_key_is_zero_network_zero_attempts() -> None:
    provider = MutableAssignmentProvider(
        endpoint_id="opencode-go",
        model_id="deepseek-v4-flash",
        wire_style="openai",
        api_key=None,
        settings=Settings(),
    )
    factory = OpenCodeGoFactory(clock=FakeClock())
    published: list[object] = []
    host, conn, _ = _host(
        snapshot_provider=provider,
        factory=factory,
        publish_work=published.append,
    )
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        assert submitted.kind == "admission_failed"
        assert submitted.run_id is None
        assert published == []
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)
        assert host.snapshot().coordinator_state == "idle"
        for (raw,) in conn.execute("SELECT telemetry FROM runs").fetchall():
            if not raw:
                continue
            for attempt in json.loads(raw).get("attempts", ()):
                assert attempt.get("attempt_id") != "unconfigured"
    finally:
        host.close()


# --- 5–7. Attempt/Block trace, fallback gating, Adapter is wire-only ---


def test_successful_stream_emits_attempt_and_block_trace() -> None:
    fake = FakeOpenAI([_stream_chunks(["Hel", "lo"], finish="stop")])
    client = _client(fake)
    capture = CapturingSink(name="capture")
    fanout = FanOutSink([capture], process_instance_id="proc-stream")
    result = client.respond(_request(), events=fanout, deadline=10)
    assert result.response is not None
    text = "".join(
        block.text for block in result.response.blocks if hasattr(block, "text")
    )
    assert text == "Hello"
    names = _payload_names(capture)
    assert "attempt.started" in names
    assert "block.started" in names
    assert "block.delta" in names
    assert "block.stopped" in names
    assert "attempt.committed" in names
    started = [
        event.payload
        for event in capture.events
        if event.payload.name == "attempt.started"
    ]
    committed = [
        event.payload
        for event in capture.events
        if event.payload.name == "attempt.committed"
    ]
    assert started[0].attempt_id == committed[0].attempt_id
    assert started[0].attempt_id == result.attempts[0].attempt_id
    assert capture.events[names.index("block.delta")].trace_policy == "transient"
    assert capture.events[names.index("attempt.committed")].trace_policy == "persist"


def test_incomplete_stream_falls_back_and_keeps_partial_out_of_the_reply() -> None:
    fake = FakeOpenAI(
        [
            _stream_chunks(["partial-secret-text"], finish=None),
            _nonstream_completion("recovered"),
        ]
    )
    client = _client(fake)
    capture = CapturingSink(name="capture")
    fanout = FanOutSink([capture], process_instance_id="proc-fb")
    result = client.respond(_request(), events=fanout, deadline=20)
    assert result.response is not None
    text = "".join(
        block.text for block in result.response.blocks if hasattr(block, "text")
    )
    assert text == "recovered"
    assert "partial-secret-text" not in text
    names = _payload_names(capture)
    assert names.count("attempt.started") == 2
    assert "attempt.aborted" in names
    assert "attempt.committed" in names
    aborted = next(
        event.payload
        for event in capture.events
        if event.payload.name == "attempt.aborted"
    )
    assert aborted.partial is True
    assert result.attempts[0].outcome == "aborted"
    assert result.attempts[1].outcome == "committed"
    assert len(fake.calls) == 2
    assert fake.calls[0].get("stream") is True
    assert fake.calls[1].get("stream") in (None, False)


def test_final_stream_failure_emits_aborted_trace_without_a_reply() -> None:
    fake = FakeOpenAI([_stream_chunks(["nope"], finish=None)])
    client = _client(fake, stream_fallback=False)
    capture = CapturingSink(name="capture")
    fanout = FanOutSink([capture], process_instance_id="proc-fail")
    result = client.respond(_request(), events=fanout, deadline=20)
    assert result.response is None
    assert result.final_error is not None
    names = _payload_names(capture)
    assert "attempt.started" in names
    assert "attempt.aborted" in names
    assert "attempt.committed" not in names
    assert len(fake.calls) == 1


def test_events_none_still_records_real_attempts() -> None:
    fake = FakeOpenAI([_nonstream_completion("ok")])
    adapter = OpenAICompatibleAdapter(client=fake, model=_MODEL, stream=False)
    result = adapter.respond(_request(), events=None, deadline=None)
    assert result.attempts
    assert result.attempts[0].attempt_id != "unconfigured"
    assert result.response is not None


def test_auth_and_structural_errors_do_not_trigger_stream_fallback() -> None:
    for status in (401, 403):
        fake = FakeOpenAI(
            [AuthError(status), _nonstream_completion("should-not")]
        )
        client = _client(fake)
        result = client.respond(_request(), events=None, deadline=20)
        assert len(fake.calls) == 1
        assert result.response is None
        assert result.final_error is not None
        assert result.final_error.status_code == status
        assert result.final_error.code != "incomplete_stream"


def test_overall_deadline_marks_the_attempt_and_does_not_fallback() -> None:
    fake = FakeOpenAI(
        [
            _stream_chunks(["x"], finish=None),
            _nonstream_completion("should-not"),
        ]
    )
    clock = FakeClock(monotonic_value=0)

    class AdvancingAdapter(OpenAICompatibleAdapter):
        def respond(self, request, *, events=None, deadline=None):
            result = super().respond(
                request, events=events, deadline=deadline
            )
            clock.monotonic_value = 100
            return result

    client = _client(
        fake,
        clock=clock,
        streaming_adapter=AdvancingAdapter(
            client=fake, model=_MODEL, stream=True
        ),
    )
    result = client.respond(_request(), events=None, deadline=10)
    assert len(fake.calls) == 1
    assert result.response is None
    assert result.final_error is not None
    assert result.final_error.retryable is False


def test_adapter_fixture_has_no_clock_or_timeout_policy() -> None:
    init = inspect.signature(OpenAICompatibleAdapter.__init__)
    respond = inspect.signature(OpenAICompatibleAdapter.respond)
    assert "clock" not in init.parameters
    assert "stream" not in respond.parameters
    protocol = inspect.signature(
        __import__("agent_alfred.model", fromlist=["ModelClient"]).ModelClient.respond
    )
    assert "stream" not in protocol.parameters
    fake = FakeOpenAI([_nonstream_completion("wire-ok")])
    adapter = OpenAICompatibleAdapter(client=fake, model=_MODEL)
    result = adapter.respond(_request())
    assert result.response is not None
    assert fake.calls[0].get("timeout") in (None, inspect.Parameter.empty)
    assert "timeout" not in fake.calls[0] or fake.calls[0]["timeout"] is None


def test_public_model_client_chain_enforces_attempt_deadline_on_transport() -> None:
    fake = FakeOpenAI([_nonstream_completion("wire-ok")])
    client = _client(fake, stream=False, clock=FakeClock(monotonic_value=2))

    result = client.respond(_request(), deadline=7)

    assert result.response is not None
    assert fake.calls == [
        {
            "model": "deepseek-v4-flash",
            "messages": [
                {"role": "system", "content": "You are Alfred."},
                {"role": "user", "content": "hi"},
            ],
            "max_tokens": None,
            "timeout": 5,
        }
    ]


def test_nonstream_parse_failure_is_an_aborted_real_attempt() -> None:
    fake = FakeOpenAI([_ns(choices=[], usage=None)])
    capture = CapturingSink(name="capture")
    events = FanOutSink([capture], process_instance_id="proc-parse")
    adapter = OpenAICompatibleAdapter(client=fake, model=_MODEL)

    result = adapter.respond(_request(), events=events)

    assert result.response is None
    assert result.final_error is not None
    assert result.final_error.code == "invalid_response"
    assert [event.payload.name for event in capture.events] == [
        "attempt.started",
        "attempt.aborted",
    ]
    assert len(result.attempts) == 1
    assert result.attempts[0].outcome == "aborted"


def test_stream_iteration_disconnect_triggers_nonstream_fallback() -> None:
    fake = FakeOpenAI(
        [InterruptedStream(), _nonstream_completion("recovered")]
    )
    client = _client(fake, stream=True, stream_fallback=True)

    result = client.respond(_request())

    assert result.response is not None
    assert result.response.blocks == (TextBlock("recovered"),)
    assert [attempt.outcome for attempt in result.attempts] == [
        "aborted",
        "committed",
    ]
    assert len(fake.calls) == 2


def test_unknown_payload_and_sensitive_notice_field_fail_closed() -> None:
    capture = CapturingSink(name="capture")
    fanout = FanOutSink(
        [capture],
        process_instance_id="proc-redaction",
        redactor=__import__(
            "agent_alfred.redact", fromlist=["Redactor"]
        ).Redactor(()),
    )

    fanout.emit(UnknownSensitivePayload())
    fanout.emit(Notice(detail=(("token", "not-a-loaded-secret"),)))

    assert [event.payload.name for event in capture.events] == [
        "notice",
        "notice",
    ]
    assert capture.events[0].payload.code == "redaction_failed"
    assert capture.events[1].payload.detail == (("token", "***"),)
    assert "not-a-loaded-secret" not in _dumped_events(capture)


def test_open_database_forces_managed_path_permissions(tmp_path) -> None:
    state_dir = tmp_path / "state"

    conn = open_database(state_dir)
    conn.close()

    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((state_dir / "db.sqlite3").stat().st_mode) == 0o600


def test_connection_local_transport_notices_are_not_domain_events() -> None:
    for code in ("replay_gap", "deltas_dropped"):
        with pytest.raises(ValueError, match="domain notice"):
            Notice(code=code)  # type: ignore[arg-type]


def test_usage_decode_failure_is_an_aborted_real_attempt() -> None:
    def explode():
        raise ValueError("malformed usage")

    response = _nonstream_completion("paid response")
    response.usage.model_dump = explode
    fake = FakeOpenAI([response])
    capture = CapturingSink(name="capture")
    events = FanOutSink([capture], process_instance_id="proc-usage")

    result = OpenAICompatibleAdapter(
        client=fake, model=_MODEL
    ).respond(_request(), events=events)

    assert result.response is None
    assert result.final_error is not None
    assert result.final_error.code == "invalid_response"
    assert [event.payload.name for event in capture.events] == [
        "attempt.started",
        "attempt.aborted",
    ]
    assert len(result.attempts) == 1


def test_public_chain_measures_real_attempt_duration_outside_adapter() -> None:
    clock = FakeClock(monotonic_value=2)

    class AdvancingStream:
        def __iter__(self):
            clock.monotonic_value += 0.125
            yield from _stream_chunks(["done"], finish="stop")

    fake = FakeOpenAI([AdvancingStream()])
    capture = CapturingSink(name="capture")
    events = FanOutSink([capture], process_instance_id="proc-duration")

    result = _client(fake, clock=clock).respond(
        _request(), events=events, deadline=10
    )

    assert result.response is not None
    committed = next(
        event.payload
        for event in capture.events
        if event.payload.name == "attempt.committed"
    )
    assert committed.duration_ms == 125


def test_retry_policy_retries_retryable_status_on_the_same_deadline() -> None:
    from agent_alfred.retry import RetryPolicy

    class FakeSleeper:
        def __init__(self):
            self.calls: list[float] = []

        def sleep(self, seconds: float) -> None:
            self.calls.append(seconds)
            clock.monotonic_value += seconds

    clock = FakeClock(monotonic_value=0)
    sleeper = FakeSleeper()
    fake = FakeOpenAI([AuthError(429, "rate limited"), _nonstream_completion("ok")])
    fallback = _client(fake, stream=False, clock=clock)
    client = RetryPolicy(
        fallback,
        clock=clock,
        sleeper=sleeper,
        max_retries=1,
        retry_delay_s=0.25,
    )

    result = client.respond(_request(), deadline=10)

    assert result.response is not None
    assert result.response.blocks == (TextBlock("ok"),)
    assert [attempt.outcome for attempt in result.attempts] == [
        "aborted",
        "committed",
    ]
    assert sleeper.calls == [0.25]
    assert fake.calls[0]["timeout"] == 10
    assert fake.calls[1]["timeout"] == 9.75


def test_retry_sleep_crossing_deadline_preserves_paid_attempt() -> None:
    from agent_alfred.retry import RetryPolicy

    class OversleepingSleeper:
        def sleep(self, seconds: float) -> None:
            del seconds
            clock.monotonic_value = 11

    clock = FakeClock(monotonic_value=0)
    fake = FakeOpenAI([AuthError(429, "rate limited")])
    client = RetryPolicy(
        _client(fake, stream=False, clock=clock),
        clock=clock,
        sleeper=OversleepingSleeper(),
        max_retries=1,
        retry_delay_s=0.25,
    )

    result = client.respond(_request(), deadline=10)

    assert result.response is None
    assert result.final_error is not None
    assert result.final_error.retryable is False
    assert len(result.attempts) == 1
    assert len(fake.calls) == 1


def test_concurrent_disable_prevents_commit_after_prepare() -> None:
    class InterleavingSink:
        name = "interleaving"
        flush_at_run_end = False

        def __init__(self):
            self.first_entered = threading.Event()
            self.release_first = threading.Event()
            self.prepare_calls = 0
            self.commit_calls = 0
            self._lock = threading.Lock()

        def prepare(self, event):
            del event
            with self._lock:
                self.prepare_calls += 1
                call = self.prepare_calls
            if call == 1:
                self.first_entered.set()
                assert self.release_first.wait(timeout=1)
                return object()
            raise RuntimeError("disable this sink")

        def commit(self, prepared, event):
            del prepared, event
            self.commit_calls += 1

        def flush(self):
            from agent_alfred.events import BestEffortFlushResult

            return BestEffortFlushResult(outcome="best_effort")

        def close(self):
            return None

    bad = InterleavingSink()
    capture = CapturingSink(name="capture")
    fanout = FanOutSink([bad, capture], process_instance_id="proc-race")
    envelope = EventEnvelope(
        ts=0,
        run_id="run-race",
        session_id=None,
        step_index=None,
        attempt_id=None,
        node_id=None,
    )
    first = threading.Thread(
        target=fanout.emit,
        args=(Notice(code="model_support_flipped"), envelope),
    )
    first.start()
    assert bad.first_entered.wait(timeout=1)

    fanout.emit(Notice(code="model_support_flipped"), envelope)
    bad.release_first.set()
    first.join(timeout=1)

    assert not first.is_alive()
    assert bad.commit_calls == 0
    incomplete, reason = fanout.flush_barrier("run-race")
    assert incomplete is True
    assert reason is not None and "dropped persist" in reason


# --- 8. StepStarted.system is redacted for every sink ---


def test_step_started_system_is_redacted_for_every_sink() -> None:
    capture = CapturingSink(name="capture", flush_at_run_end=True)
    extra = CapturingSink(name="extra", flush_at_run_end=False)
    from agent_alfred.redact import Redactor

    redactor = Redactor((SECRET,))
    fanout = FanOutSink(
        [capture, extra], process_instance_id="proc-sys", redactor=redactor
    )
    host = RuntimeHost(
        conn=sqlite3.connect(":memory:", check_same_thread=False),
        factory=ScriptedModelFactory(ScriptedModel(["pong"])),
        settings=Settings(persona=f"Never leak {SECRET}."),
        clock=FakeClock(),
        fanout=fanout,
        process_instance_id="proc-sys",
        secrets=(SECRET,),
    )
    schema.migrate(host._conn)
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hi"))
        host.wait(submitted.run_id)
        for sink in (capture, extra):
            dumped = _dumped_events(sink)
            assert SECRET not in dumped
            systems = [
                event.payload.system
                for event in sink.events
                if event.payload.name == "step.started"
            ]
            assert systems and systems[0] is not None
            joined = " ".join(block.text for block in systems[0])
            assert SECRET not in joined
            assert "***" in joined
    finally:
        host.close()


# --- 9–11. sink failure isolation and the persist barrier ---


def test_persist_loss_marks_trace_incomplete_even_if_flush_succeeds() -> None:
    boom = BoomPrepareSink(name="trace", flush_at_run_end=True)
    host, conn, capture = _host(script=["pong"], extra_sinks=[boom])
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hi"))
        result = host.wait(submitted.run_id)
        assert result.outcome == "completed"
        assert message_plain_text(result.reply) == "pong"
        payload = json.loads(conn.execute("SELECT telemetry FROM runs").fetchone()[0])
        assert payload["trace_incomplete"] is True
        assert payload["trace_incomplete_reason"]
        assert boom.prepare_calls == 1
        assert "run.finished" in _payload_names(capture)
    finally:
        host.close()


def test_sink_disable_is_scoped_to_the_failing_run() -> None:
    boom = BoomPrepareSink(name="boom", flush_at_run_end=False)
    host, _conn, _capture = _host(script=["one", "two"], extra_sinks=[boom])
    host.start()
    try:
        first = host.submit(SubmitRequest(message="a"))
        host.wait(first.run_id)
        assert boom.prepare_calls == 1
        second = host.submit(SubmitRequest(message="b"))
        host.wait(second.run_id)
        assert boom.prepare_calls >= 2
    finally:
        host.close()


def test_critical_flush_failure_delivers_reply_and_trace_incomplete_notice() -> None:
    fail = FailingFlushSink()
    host, conn, capture = _host(script=["pong"], extra_sinks=[fail])
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hi"))
        result = host.wait(submitted.run_id)
        assert result.outcome == "completed"
        assert message_plain_text(result.reply) == "pong"
        payload = json.loads(conn.execute("SELECT telemetry FROM runs").fetchone()[0])
        assert payload["trace_incomplete"] is True
        notices = [
            event.payload
            for event in capture.events
            if event.payload.name == "notice"
            and getattr(event.payload, "code", None) == "trace_incomplete"
        ]
        assert notices
        dumped = _dumped_events(capture)
        assert "trace_incomplete" in dumped
    finally:
        host.close()
