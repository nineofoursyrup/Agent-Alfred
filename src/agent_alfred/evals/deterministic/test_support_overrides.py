"""Test-only evidence verifies mechanisms, never supplier classification."""

from dataclasses import replace

import pytest

from agent_alfred.endpoints import list_endpoints, resolve_model


def override(**changes):
    from agent_alfred.support_overrides import SupportOverride

    return SupportOverride(
        **(
            dict(
                endpoint_id="opencode-go",
                model_id="deepseek-v4-flash",
                wire_style="openai",
                reason="shape_mismatch_400",
                flip_rule="shape_mismatch_400",
                attempt_id="attempt",
                status_code=400,
                body_excerpt="synthetic evidence",
                run_id="run",
                config_version="1",
                observed_at="2026-09-06T00:00:00Z",
            )
            | changes
        )
    )


def test_complete_override_is_idempotent_and_does_not_mutate_the_table():
    from agent_alfred.support_overrides import SupportOverrides

    store = SupportOverrides()
    before = list_endpoints()
    record = override()
    assert store.record(record)
    assert not store.record(replace(record, attempt_id="later"))
    result = resolve_model("opencode-go", "deepseek-v4-flash", overrides=store)
    assert (result.support, result.support_basis) == ("unsupported", "probe_evidence")
    assert store.for_run("run") == (record,)
    assert store.for_run("other") == ()
    assert list_endpoints() == before


def test_shape_a_b_a_reuses_evidence_but_other_models_and_restart_do_not():
    from agent_alfred.support_overrides import SupportOverrides

    store = SupportOverrides()
    store.record(override())
    assert (
        resolve_model(
            "opencode-go",
            "deepseek-v4-flash",
            wire_style_override="anthropic",
            overrides=store,
        ).support
        == "unknown"
    )
    assert (
        resolve_model(
            "opencode-go",
            "deepseek-v4-flash",
            wire_style_override="openai",
            overrides=store,
        ).support
        == "unsupported"
    )
    assert (
        resolve_model("opencode", "deepseek-v4-flash", overrides=store).support
        == "supported"
    )
    assert (
        resolve_model("opencode-go", "qwen3.7-max", overrides=store).support
        == "supported"
    )
    restarted = resolve_model(
        "opencode-go", "deepseek-v4-flash", overrides=SupportOverrides()
    )
    assert (restarted.support, restarted.support_basis) == (
        "supported",
        "builtin_table",
    )


def test_override_is_visible_while_recording_lease_is_held(tmp_path):
    from agent_alfred.evals.deterministic._web_runtime_test_helpers import (
        SelectiveLatch,
        build_runtime_host,
    )
    from agent_alfred.model import ModelError
    from agent_alfred.runtime.work import SubmitRequest
    from agent_alfred.support_overrides import SupportOverrides

    store = SupportOverrides()
    latch = SelectiveLatch()
    latch.arm()
    host, conn = build_runtime_host(
        [ModelError(False, 400, "test-only failure", "attempt")],
        support_overrides=store,
        support_rule=lambda snapshot, error: "shape_mismatch_400",
        before_recording_commit=latch,
    )
    host.start()
    try:
        submitted = host.submit(SubmitRequest("hi"))
        assert submitted.kind == "accepted"
        assert latch.entered.wait(2)
        evidence = host.read_run_evidence(submitted.run_id, trace_root=tmp_path)
        assert evidence["support_overrides"][0]["attempt_id"] == "attempt"
        assert evidence["support_overrides"][0]["support_basis"] == "probe_evidence"
        assert host.submit(SubmitRequest("second")).kind == "run_in_progress"
        support = resolve_model("opencode-go", "deepseek-v4-flash", overrides=store)
        assert (support.support, support.support_basis) == (
            "unsupported",
            "probe_evidence",
        )
        from agent_alfred.evals.deterministic._web_runtime_test_helpers import (
            dashboard_api,
        )

        before = conn.execute("SELECT count(*) FROM sessions").fetchone()
        refused = dashboard_api(host).create_session()
        assert (refused.status, refused.code) == (409, "mutation_in_flight")
        assert conn.execute("SELECT count(*) FROM sessions").fetchone() == before
        latch.release()
        assert host.wait(submitted.run_id, timeout=2).outcome == "failed"
    finally:
        latch.release()
        host.close()
        conn.close()


@pytest.mark.parametrize("endpoint", [row.endpoint_id for row in list_endpoints()])
@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500])
def test_production_has_zero_rules_even_for_suggestive_error_text(endpoint, status):
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic.test_endpoint_factory import snapshot
    from agent_alfred.model import ModelError, ModelRef, ModelRequest, ScriptedModel
    from agent_alfred.redact import Redactor
    from agent_alfred.support_overrides import SupportOverrides, SupportRecorder

    store = SupportOverrides()
    error = ModelError(False, status, "endpoint not found invalid request", "a")
    result = ScriptedModel([error]).respond(
        ModelRequest(ModelRef(endpoint, "m"), None, ())
    )
    SupportRecorder(store, Redactor(()), FakeClock()).observe(
        snapshot(endpoint=endpoint), "run", result, None
    )
    assert store.for_run("run") == ()


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("stage", ["gate", "answer"])
@pytest.mark.parametrize("fault", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_record_fault_settles_real_run_and_preserves_attempt_ledger(
    when, stage, fault, tmp_path
):
    from agent_alfred.evals.deterministic._web_runtime_test_helpers import (
        build_runtime_host,
    )
    from agent_alfred.model import ModelError
    from agent_alfred.runtime.work import SubmitRequest
    from agent_alfred.support_overrides import SupportOverrides

    class FailingStore(SupportOverrides):
        def record(self, record):
            if when == "after":
                super().record(record)
            raise fault("injected at recording boundary")

    store = FailingStore()
    # Precise execution injection keeps SystemExit on this test thread.
    items = []
    host, conn = build_runtime_host(
        [ModelError(False, 400, "synthetic", "a")],
        support_overrides=store,
        support_rule=lambda *_: "shape_mismatch_400",
        publish_work=items.append,
        chat_script=stage == "answer",
    )
    host.start()
    try:
        submitted = host.submit(SubmitRequest("hi"))
        if fault is SystemExit:
            with pytest.raises(SystemExit):
                host._executor.execute(items[0])
        else:
            host._executor.execute(items[0])
        result = host.wait(submitted.run_id, timeout=2)
        assert result.outcome == ("failed" if fault is RuntimeError else "interrupted")
        evidence = host.read_run_evidence(submitted.run_id, trace_root=tmp_path)
        assert len(evidence["attempts"]) == (1 if stage == "gate" else 2)
        if fault is RuntimeError:
            assert result.error == "RuntimeError"
        assert bool(evidence["support_overrides"]) == (when == "after")
        assert host.snapshot().coordinator_state == "idle"
    finally:
        host.close()
        conn.close()


def test_notice_is_initiated_once_and_only_after_complete_redacted_override():
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic.test_endpoint_factory import snapshot
    from agent_alfred.model import ModelError, ModelRef, ModelRequest, ScriptedModel
    from agent_alfred.redact import Redactor
    from agent_alfred.support_overrides import SupportOverrides, SupportRecorder

    store = SupportOverrides()
    notices = []

    class Sink:
        def emit(self, payload):
            assert store.for_run("run")
            notices.append(payload)
            raise KeyboardInterrupt("injected publication fatal")

    error = ModelError(False, 400, "synthetic-secret body", "a")
    result = ScriptedModel([error]).respond(
        ModelRequest(ModelRef("opencode-go", "m"), None, ())
    )
    recorder = SupportRecorder(
        store,
        Redactor(("synthetic-secret",)),
        FakeClock(),
        lambda *_: "shape_mismatch_400",
    )
    with pytest.raises(KeyboardInterrupt):
        recorder.observe(snapshot(), "run", result, Sink())
    recorder.observe(snapshot(), "run", result, Sink())
    assert len(notices) == 1
    assert notices[0].evidence == "*** body"
    assert store.for_run("run")[0].body_excerpt == "*** body"


def test_redaction_failure_never_publishes_override_or_raw_evidence():
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic.test_endpoint_factory import snapshot
    from agent_alfred.model import ModelError, ModelRef, ModelRequest, ScriptedModel
    from agent_alfred.redact import Redactor
    from agent_alfred.support_overrides import SupportOverrides, SupportRecorder

    class FailingRedactor(Redactor):
        def redact_jsonable(self, value):
            raise ValueError("injected redaction failure")

    class Sink:
        notices = []

        def emit(self, payload):
            self.notices.append(payload)

    store, sink = SupportOverrides(), Sink()
    result = ScriptedModel([ModelError(False, 400, "secret", "a")]).respond(
        ModelRequest(ModelRef("opencode-go", "m"), None, ())
    )
    SupportRecorder(
        store, FailingRedactor(()), FakeClock(), lambda *_: "shape_mismatch_400"
    ).observe(snapshot(), "run", result, sink)
    assert store.for_run("run") == ()
    assert [notice.code for notice in sink.notices] == ["redaction_failed"]
    assert sink.notices[0].evidence is None


@pytest.mark.parametrize("failure", ["none", "commit", "flush", "fatal"])
def test_persisted_notice_survives_restart_independently_of_memory(failure, tmp_path):
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic._web_runtime_test_helpers import (
        INSTANCE,
        build_runtime_host,
    )
    from agent_alfred.events import BarrierFlushResult, CapturingSink
    from agent_alfred.model import ModelError
    from agent_alfred.runtime.work import SubmitRequest
    from agent_alfred.support_overrides import SupportOverrides
    from agent_alfred.wiring import _trace_sink

    class FaultSink(CapturingSink):
        def commit(self, prepared, event):
            if getattr(event.payload, "code", None) == "model_support_flipped":
                if failure == "commit":
                    raise OSError("injected sink failure")
                if failure == "fatal":
                    raise KeyboardInterrupt("injected fatal after trace commit")
            return super().commit(prepared, event)

        def flush(self, run_id):
            if failure == "flush":
                return BarrierFlushResult(
                    outcome="failed", dropped_events=0, detail="injected flush failure"
                )
            return super().flush(run_id)

    trace = _trace_sink(tmp_path, FakeClock(), INSTANCE)
    witness = CapturingSink(name="surviving-witness")
    store = SupportOverrides()
    host, conn = build_runtime_host(
        [ModelError(False, 400, "synthetic", "a")],
        support_overrides=store,
        support_rule=lambda *_: "shape_mismatch_400",
        extra_sinks=[trace, witness, FaultSink(name="fault", flush_at_run_end=True)],
    )
    host.start()
    try:
        submitted = host.submit(SubmitRequest("hi"))
        result = host.wait(submitted.run_id, timeout=2)
        assert result.outcome == ("interrupted" if failure == "fatal" else "failed")
        current = host.read_run_evidence(submitted.run_id, trace_root=tmp_path)
        assert len(current["support_overrides"]) == 1
        if failure == "commit":
            assert any(
                getattr(event.payload, "code", None) == "sink_disabled"
                for event in witness.events
            )
        if failure == "flush":
            assert current["trace_incomplete"] is True
            assert current["trace_status"] == "available"
        assert (
            len(
                [
                    event
                    for event in current["events"]
                    if event["payload"].get("code") == "model_support_flipped"
                ]
            )
            == 1
        )
    finally:
        host.close()
    restarted, _ = build_runtime_host(conn=conn)
    restarted.start()
    try:
        historical = restarted.read_run_evidence(submitted.run_id, trace_root=tmp_path)
        assert historical["support_overrides"] == []
        if failure == "flush":
            assert historical["trace_incomplete"] is True
            assert historical["trace_status"] == "available"
        assert (
            len(
                [
                    event
                    for event in historical["events"]
                    if event["payload"].get("code") == "model_support_flipped"
                ]
            )
            == 1
        )
    finally:
        restarted.close()
        conn.close()


def test_duplicate_evidence_is_suppressed_by_atomic_store_result():
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic.test_endpoint_factory import snapshot
    from agent_alfred.model import ModelError, ModelRef, ModelRequest, ScriptedModel
    from agent_alfred.redact import Redactor
    from agent_alfred.support_overrides import SupportOverrides, SupportRecorder

    class CountingStore(SupportOverrides):
        results = []

        def record(self, evidence):
            accepted = super().record(evidence)
            self.results.append(accepted)
            return accepted

    store = CountingStore()
    result = ScriptedModel([ModelError(False, 400, "synthetic", "a")]).respond(
        ModelRequest(ModelRef("opencode-go", "m"), None, ())
    )
    recorder = SupportRecorder(
        store, Redactor(()), FakeClock(), lambda *_: "shape_mismatch_400"
    )
    recorder.observe(snapshot(), "run", result, None)
    recorder.observe(snapshot(), "run", result, None)
    assert store.results == [True, False]


def test_no_override_object_is_constructed_before_redaction(monkeypatch):
    from agent_alfred import support_overrides as module
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic.test_endpoint_factory import snapshot
    from agent_alfred.model import ModelError, ModelRef, ModelRequest, ScriptedModel
    from agent_alfred.redact import Redactor

    constructed = []
    real = module.SupportOverride

    def checked(**fields):
        constructed.append(fields)
        assert "synthetic-secret" not in str(fields)
        return real(**fields)

    monkeypatch.setattr(module, "SupportOverride", checked)
    result = ScriptedModel([ModelError(False, 400, "synthetic-secret", "a")]).respond(
        ModelRequest(ModelRef("opencode-go", "m"), None, ())
    )
    store = module.SupportOverrides()
    module.SupportRecorder(
        store,
        Redactor(("synthetic-secret",)),
        FakeClock(),
        lambda *_: "shape_mismatch_400",
    ).observe(snapshot(), "run", result, None)
    assert len(constructed) == 1
    assert store.for_run("run")[0].support == "unsupported"
    assert store.for_run("run")[0].support_basis == "probe_evidence"


@pytest.mark.parametrize("sinks_count", [1, 2])
def test_all_disabled_sinks_do_not_prevent_complete_override(sinks_count):
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic.test_endpoint_factory import snapshot
    from agent_alfred.events import AttemptStarted, CapturingSink, FanOutSink
    from agent_alfred.model import ModelError, ModelRef, ModelRequest, ScriptedModel
    from agent_alfred.redact import Redactor
    from agent_alfred.support_overrides import SupportOverrides, SupportRecorder

    class DisabledSink(CapturingSink):
        def commit(self, prepared, event):
            raise OSError("synthetic sink failure")

    sinks = [DisabledSink(name=str(i)) for i in range(sinks_count)]
    events = FanOutSink(sinks, process_instance_id="test")
    events.emit(AttemptStarted(attempt_id="a"))
    store = SupportOverrides()
    result = ScriptedModel([ModelError(False, 400, "synthetic", "a")]).respond(
        ModelRequest(ModelRef("opencode-go", "m"), None, ())
    )
    SupportRecorder(
        store, Redactor(()), FakeClock(), lambda *_: "shape_mismatch_400"
    ).observe(snapshot(), "run", result, events)
    assert len(store.for_run("run")) == 1
    assert all(sink.events == [] for sink in sinks)


def test_emit_level_process_fatal_keeps_override_without_sink_disabled(
    monkeypatch, tmp_path
):
    from agent_alfred.evals.deterministic._web_runtime_test_helpers import (
        build_runtime_host,
    )
    from agent_alfred.events import ProcessFatalSinkError
    from agent_alfred.model import ModelError
    from agent_alfred.runtime.work import SubmitRequest
    from agent_alfred.support_overrides import SupportOverrides

    store = SupportOverrides()
    host, conn = build_runtime_host(
        [ModelError(False, 400, "synthetic", "a")],
        support_overrides=store,
        support_rule=lambda *_: "shape_mismatch_400",
    )
    original = host._fanout.emit
    codes = []

    def emit(payload, *args, **kwargs):
        code = getattr(payload, "code", None)
        if code is not None:
            codes.append(code)
        if code == "model_support_flipped":
            assert len(store.for_run(run)) == 1
            raise ProcessFatalSinkError("synthetic emitter failure")
        return original(payload, *args, **kwargs)

    # Inject precisely at the existing event publication boundary.
    items = []
    host._publish_work = items.append
    monkeypatch.setattr(host._fanout, "emit", emit)
    host.start()
    try:
        run = host.submit(SubmitRequest("hi")).run_id
        host._executor.execute(items[0])
        assert host.wait(run, timeout=2).outcome == "failed"
        assert (
            len(host.read_run_evidence(run, trace_root=tmp_path)["support_overrides"])
            == 1
        )
        assert "sink_disabled" not in codes
    finally:
        host.close()
        conn.close()


@pytest.mark.parametrize("record_fault", ["none", "before", "after"])
@pytest.mark.parametrize("publication_fault", ["none", "lookup", "call"])
@pytest.mark.parametrize("redaction_fault", [False, True])
def test_failure_combinations_preserve_notice_implies_complete_override(
    record_fault, publication_fault, redaction_fault
):
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic.test_endpoint_factory import snapshot
    from agent_alfred.model import ModelError, ModelRef, ModelRequest, ScriptedModel
    from agent_alfred.redact import Redactor
    from agent_alfred.support_overrides import SupportOverrides, SupportRecorder

    class Store(SupportOverrides):
        def record(self, evidence):
            if record_fault == "before":
                raise KeyboardInterrupt("before insertion")
            result = super().record(evidence)
            if record_fault == "after":
                raise KeyboardInterrupt("after insertion")
            return result

    class Redaction(Redactor):
        def redact_jsonable(self, value):
            if redaction_fault:
                raise ValueError("redaction failure")
            return super().redact_jsonable(value)

    store = Store()
    initiated = []

    class Events:
        @property
        def emit(self):
            if publication_fault == "lookup":
                raise KeyboardInterrupt("record returned, emit not called")
            return self.publish

        def publish(self, payload):
            if payload.code == "model_support_flipped":
                (record,) = store.for_run("run")
                assert record.support == "unsupported"
                assert record.support_basis == "probe_evidence"
                assert record.attempt_id == "a"
                initiated.append(payload)
            if publication_fault == "call":
                raise KeyboardInterrupt("publication interrupted")

    result = ScriptedModel([ModelError(False, 400, "synthetic", "a")]).respond(
        ModelRequest(ModelRef("opencode-go", "m"), None, ())
    )
    recorder = SupportRecorder(
        store, Redaction(()), FakeClock(), lambda *_: "shape_mismatch_400"
    )
    try:
        recorder.observe(snapshot(), "run", result, Events())
    except KeyboardInterrupt:
        pass
    assert not initiated or store.for_run("run")
    assert bool(store.for_run("run")) == (
        not redaction_fault and record_fault != "before"
    )
