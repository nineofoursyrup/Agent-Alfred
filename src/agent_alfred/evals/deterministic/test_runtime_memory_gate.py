"""Memory retrieval budget and input evidence at the public RuntimeHost seam."""

import json
import sqlite3
from contextlib import contextmanager

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.events import CapturingSink, FanOutSink
from agent_alfred.messages import message_plain_text
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.config import MutableAssignmentProvider
from agent_alfred.runtime.host import RuntimeHost, SubmitRequest
from agent_alfred.settings import Settings

SKIP = '{"retrieve":false,"query":null,"reason_code":"greeting"}'


@contextmanager
def runtime(
    script,
    *,
    settings=None,
    factory=None,
    snapshot_provider=None,
    clock=None,
    publish_work=None,
    no_sinks=False,
    extra_sinks=(),
    memory_notifier=None,
    database=":memory:",
):
    conn = sqlite3.connect(database, check_same_thread=False)
    schema.migrate(conn)
    capture = CapturingSink(flush_at_run_end=not bool(extra_sinks))
    model = ScriptedModel(script)
    host = RuntimeHost(
        conn=conn,
        factory=factory or ScriptedModelFactory(model),
        settings=settings or Settings(),
        clock=clock or FakeClock(),
        fanout=FanOutSink(
            [] if no_sinks else [capture, *extra_sinks],
            process_instance_id="memory-test",
        ),
        publish_work=publish_work,
        memory_notifier=memory_notifier,
        process_instance_id="memory-test",
        snapshot_provider=snapshot_provider
        or MutableAssignmentProvider(
            endpoint_id="test",
            model_id="m",
            wire_style="openai",
            api_key="test-key",
            settings=settings,
        ),
    )
    host.start()
    try:
        yield host, model, capture
    finally:
        host.close()
        conn.close()


def test_gate_spends_one_shared_step_before_the_answer_and_is_not_a_reply():
    with runtime([SKIP, "answer"]) as (host, model, capture):
        submitted = host.submit(SubmitRequest("hello"))
        result = host.wait(submitted.run_id)
        assert result.outcome == "completed"
        assert message_plain_text(result.reply) == "answer"
        assert result.step_count == 2
        assert len(model.requests) == 2
        assert model.requests[0].tools == ()
        evaluated = [e for e in capture.events if e.payload.name == "gate.evaluated"]
        assert len(evaluated) == 1
        assert evaluated[0].payload.outcome == "skip"


def test_zero_and_one_step_budgets_never_infer_selected_means_answer_sent():
    for max_steps, expected_calls, expected_state in (
        (0, 0, "not_evaluated"),
        (1, 1, "evaluated"),
    ):
        with runtime([SKIP], settings=Settings(max_steps=max_steps)) as (
            host,
            model,
            _,
        ):
            result = host.wait(host.submit(SubmitRequest("hello")).run_id)
            assert result.outcome == "max_steps"
            assert len(model.requests) == expected_calls
            assert result.memory_telemetry["gate_state"] == expected_state
            assert all(
                attempt["purpose"] == "gate"
                for attempt in result.memory_telemetry["input_attempts"]
            )


def test_next_chat_run_reevaluates_but_probe_never_retrieves():
    with runtime([SKIP, "first", SKIP, "second", "probe"]) as (host, model, capture):
        first = host.submit(SubmitRequest("hello"))
        assert host.wait(first.run_id).outcome == "completed"
        second = host.submit(SubmitRequest("hello", session_id=first.session_id))
        result = host.wait(second.run_id)
        assert result.memory_telemetry["input_attempts"][0][
            "working_history_groups"
        ] == [first.run_id]
        probe = host.submit(
            SubmitRequest(
                "probe", purpose="inference_probe", endpoint_id="test", model_id="m"
            )
        )
        assert message_plain_text(host.wait(probe.run_id).reply) == "probe"
        assert len(model.requests) == 5
        assert (
            len([e for e in capture.events if e.payload.name == "gate.evaluated"]) == 2
        )


def test_explicit_unavailable_gate_falls_back_without_spending_a_step_or_switching():
    from agent_alfred.model import EndpointUnconfigured, ModelAssignment
    from agent_alfred.runtime.config import MutableAssignmentProvider

    answer = ScriptedModel(["answer"])

    class AssignedFactory:
        snapshots = []

        def create(self, snapshot):
            self.snapshots.append(snapshot)
            if snapshot.endpoint_id == "missing":
                raise EndpointUnconfigured("private exception text")
            return answer

    factory = AssignedFactory()
    provider = MutableAssignmentProvider(
        endpoint_id="primary",
        model_id="answer",
        wire_style="openai",
        api_key="key",
        retrieval_gate=ModelAssignment("missing", "gate", "openai"),
    )
    with runtime([], factory=factory, snapshot_provider=provider) as (host, _, _):
        result = host.wait(host.submit(SubmitRequest("hello")).run_id)
        assert result.outcome == "completed"
        assert result.step_count == 1
        evidence = result.memory_telemetry["gate"]
        assert evidence["fallback_reason"] == "model_unavailable"
        assert evidence["decision_source"] == "deterministic_fallback"
        assert evidence["gate_step_index"] is None
        assert evidence["model_ref"] is None
        assert [s.endpoint_id for s in factory.snapshots] == ["primary", "missing"]
        assert "private exception text" not in json.dumps(result.memory_telemetry)


def save_fact(host, *, fact="I prefer coriander"):
    from agent_alfred.memory.commands import CommandContext
    from agent_alfred.memory.types import ManualOrigin

    receipt = host.memory_service.execute(
        {
            "operation_id": "save-one",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "private-subject-marker", "fact": fact},
        },
        CommandContext(origin=ManualOrigin("web"), source="web"),
    )
    assert receipt["status"] == "saved", receipt
    return receipt


def test_real_fts_reference_is_between_work_window_and_current_question_only():
    retrieve = (
        '{"retrieve":true,"query":"coriander","reason_code":"personal_information"}'
    )
    with runtime([SKIP, "previous answer", retrieve, "current answer"]) as (
        host,
        model,
        capture,
    ):
        saved = save_fact(host)
        first = host.submit(SubmitRequest("hello"))
        host.wait(first.run_id)
        next_run = host.submit(
            SubmitRequest("What herb do I like?", session_id=first.session_id)
        )
        result = host.wait(next_run.run_id)
        assert result.outcome == "completed"
        gate = result.memory_telemetry["gate"]
        assert gate["outcome"] == "hit"
        assert gate["selected_count"] == 1
        answer_request = model.requests[-1]
        text = [message_plain_text(message) for message in answer_request.messages]
        assert text[:2] == ["hello", "previous answer"]
        assert "检索参考资料" in text[2]
        assert "I prefer coriander" in text[2]
        assert text[3] == "What herb do I like?"
        assert answer_request.messages[2].role == "user"
        attempt = result.memory_telemetry["input_attempts"][-1]
        assert attempt["purpose"] == "answer"
        assert attempt["references"] == [
            {
                "kind": "semantic",
                "memory_id": saved["memory_id"],
                "record_version": 1,
            }
        ]
        assert "coriander" not in json.dumps(result.memory_telemetry)
        assert "private-subject-marker" not in json.dumps(result.memory_telemetry)
        # Next gate receives the recorded dialogue, never the reference message.
        # The next Run uses an explicit script, preserving actual gate accounting.


def test_selected_reference_with_one_step_has_zero_answer_attempts():
    retrieve = (
        '{"retrieve":true,"query":"coriander","reason_code":"personal_information"}'
    )
    with runtime([retrieve], settings=Settings(max_steps=1)) as (host, model, _):
        save_fact(host)
        result = host.wait(host.submit(SubmitRequest("coriander")).run_id)
        assert result.outcome == "max_steps"
        assert result.memory_telemetry["gate"]["selected_count"] == 1
        assert [a["purpose"] for a in result.memory_telemetry["input_attempts"]] == [
            "gate"
        ]
        assert len(model.requests) == 1


def test_all_excluded_stops_answer_but_preserves_evaluated_hit():
    retrieve = (
        '{"retrieve":true,"query":"coriander","reason_code":"personal_information"}'
    )
    with runtime([retrieve], settings=Settings(per_store_character_budget=1)) as (
        host,
        model,
        _,
    ):
        save_fact(host)
        result = host.wait(host.submit(SubmitRequest("coriander")).run_id)
        assert result.outcome == "failed"
        assert result.error == "memory_input_unavailable"
        assert result.memory_telemetry["gate"]["outcome"] == "hit"
        assert result.memory_telemetry["gate"]["input_disposition"] == "all_excluded"
        assert len(model.requests) == 1


def test_failed_gate_retry_costs_and_input_attempts_share_a_single_step():
    from decimal import Decimal

    from agent_alfred.model import AttemptRecord, ModelError, ModelResult, Usage
    from agent_alfred.retry import RetryPolicy

    clock = FakeClock()
    error = ModelError(True, 500, "private upstream failure", "failed-gate")
    aborted = ModelResult(
        (
            AttemptRecord(
                "failed-gate",
                False,
                "aborted",
                Usage(output_tokens=2, endpoint_reported_cost_usd=Decimal("0.002")),
                error,
            ),
        ),
        None,
        error,
    )
    script = ScriptedModel([aborted, SKIP, "answer"])

    class Sleeper:
        def sleep(self, seconds):
            clock.monotonic_value += seconds

    class Factory:
        def create(self, snapshot):
            return RetryPolicy(script, clock=clock, sleeper=Sleeper())

    with runtime([], factory=Factory(), clock=clock) as (host, _, _):
        result = host.wait(host.submit(SubmitRequest("hello")).run_id)
        assert result.step_count == 2
        assert len(result.model_results) == 2
        assert len(result.model_results[0].attempts) == 2
        assert result.model_results[0].attempts[
            0
        ].usage.endpoint_reported_cost_usd == Decimal("0.002")
        attempts = result.memory_telemetry["input_attempts"]
        assert [a["purpose"] for a in attempts] == ["gate", "gate", "answer"]
        assert [a["step_index"] for a in attempts] == [0, 0, 1]
        assert script.deadlines[:2] == [5.0, 5.0]


def test_gate_deadline_falls_back_but_run_deadline_never_resets():
    from agent_alfred.model import AttemptRecord, ModelError, ModelResult, Usage

    for overall, expected_calls, expected_state in (
        (10.0, 2, "evaluated"),
        (3.0, 1, "incomplete"),
    ):
        clock = FakeClock()
        error = ModelError(False, None, "timeout", "timeout-attempt")
        failure = ModelResult(
            (AttemptRecord("timeout-attempt", False, "aborted", Usage(), error),),
            None,
            error,
        )

        class DeadlineModel(ScriptedModel):
            def respond(self, request, *, events=None, deadline=None):
                result = super().respond(request, events=events, deadline=deadline)
                if len(self.requests) == 1:
                    clock.monotonic_value = deadline
                return result

        model = DeadlineModel([failure, "answer"])
        with runtime(
            [],
            factory=ScriptedModelFactory(model),
            clock=clock,
            settings=Settings(overall_deadline_s=overall),
        ) as (host, _, capture):
            result = host.wait(host.submit(SubmitRequest("hello")).run_id)
            assert len(model.requests) == expected_calls
            assert result.memory_telemetry["gate_state"] == expected_state
            if overall == 10.0:
                assert (
                    result.memory_telemetry["gate"]["fallback_reason"]
                    == "model_deadline"
                )
                assert model.deadlines == [5.0, 10.0]
            else:
                assert result.error == "overall_deadline"
                assert not any(
                    e.payload.name == "gate.evaluated" for e in capture.events
                )


def test_real_host_run_permission_inherits_lease_and_expires_after_handoff_failure():
    from dataclasses import replace

    from agent_alfred.memory.commands import CommandContext
    from agent_alfred.memory.types import ManualOrigin, ToolOrigin

    results = {}
    contexts = []
    command = {
        "operation_id": "owned-save",
        "action": "save",
        "kind": "semantic",
        "payload": {"subject": "me", "fact": "coriander"},
    }

    def accepted_work(item):
        context = CommandContext(
            origin=ToolOrigin("tool-call"),
            source="tool",
            run_id=item.run_id,
            session_id=item.session_id,
            call_id="tool-call",
            permission=item.memory_permission,
        )
        contexts.append(context)
        results["manual"] = host.memory_service.execute(
            command,
            CommandContext(
                origin=ManualOrigin("web"),
                source="web",
            ),
        )
        results["forged"] = host.memory_service.execute(
            command, replace(context, permission=object())
        )
        results["tool"] = host.memory_service.execute(command, context)
        raise RuntimeError("deterministic accepted handoff failure")

    with runtime([], publish_work=accepted_work) as (host, model, _):
        submitted = host.submit(SubmitRequest("save"))
        assert submitted.kind == "handoff_failed"
        assert results["manual"]["error"]["code"] == "busy"
        assert results["forged"]["error"]["code"] == "busy"
        assert results["tool"]["status"] == "saved"
        assert model.requests == []
        expired = host.memory_service.execute(
            {**command, "operation_id": "expired"}, contexts[0]
        )
        assert expired["error"]["code"] == "busy"
        record = host.memory_service.get("semantic", results["tool"]["memory_id"])
        assert record.fact == "coriander"


def test_gate_and_input_business_evidence_survives_absent_event_sinks(tmp_path):
    with runtime([SKIP, "answer"], no_sinks=True) as (host, _, capture):
        submitted = host.submit(SubmitRequest("hello"))
        host.wait(submitted.run_id)
        evidence = host.read_run_evidence(submitted.run_id, trace_root=tmp_path)
        assert capture.events == []
        assert evidence["memory"]["gate_state"] == "evaluated"
        assert evidence["memory"]["gate"]["outcome"] == "skip"
        assert [a["purpose"] for a in evidence["memory"]["input_attempts"]] == [
            "gate",
            "answer",
        ]
        assert evidence["trace_incomplete"] is True


def test_memory_settings_load_from_environment_and_reject_illegal_startup_values():
    import pytest

    from agent_alfred.settings import SettingsError, load_settings

    settings = load_settings(
        {
            "AGENT_ALFRED_PER_STORE_LIMIT": "3",
            "AGENT_ALFRED_PER_STORE_CHARACTER_BUDGET": "250",
            "AGENT_ALFRED_GATE_MODEL_BUDGET_S": "1.5",
        }
    )
    assert (
        settings.per_store_limit,
        settings.per_store_character_budget,
        settings.gate_model_budget_s,
    ) == (3, 250, 1.5)
    for options in (
        {"per_store_limit": 0},
        {"per_store_limit": True},
        {"per_store_character_budget": -1},
        {"gate_model_budget_s": float("nan")},
        {"gate_model_budget_s": float("inf")},
        {"gate_model_budget_s": 0},
    ):
        with pytest.raises(SettingsError):
            Settings(**options)
    for raw in ("nan", "inf", "0", "-1"):
        with pytest.raises(SettingsError):
            load_settings({"AGENT_ALFRED_GATE_MODEL_BUDGET_S": raw})


def test_explicit_gate_model_keeps_its_own_identity_in_the_durable_attempt_ledger(
    tmp_path,
):
    from agent_alfred.model import ModelAssignment

    gate = ScriptedModel([SKIP])
    answer = ScriptedModel(["answer"])

    class Factory:
        def create(self, snapshot):
            return gate if snapshot.model_id == "gate-model" else answer

    provider = MutableAssignmentProvider(
        endpoint_id="answer-endpoint",
        model_id="answer-model",
        wire_style="openai",
        api_key="test-key",
        retrieval_gate=ModelAssignment("gate-endpoint", "gate-model", "anthropic"),
    )
    with runtime([], factory=Factory(), snapshot_provider=provider, no_sinks=True) as (
        host,
        _,
        _,
    ):
        submitted = host.submit(SubmitRequest("hello"))
        result = host.wait(submitted.run_id)
        assert [r.attempts[0].model.model_id for r in result.model_results] == [
            "gate-model",
            "answer-model",
        ]
        evidence = host.read_run_evidence(submitted.run_id, trace_root=tmp_path)
        assert evidence["memory"]["gate"]["model_ref"] == {
            "endpoint_id": "gate-endpoint",
            "model_id": "gate-model",
            "wire_style": "anthropic",
        }
        assert gate.requests[0].model.model_id == "gate-model"
        assert answer.requests[0].model.model_id == "answer-model"
