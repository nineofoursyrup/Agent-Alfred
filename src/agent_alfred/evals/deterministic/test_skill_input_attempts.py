"""SKILL-SPEC-r1 P2: real SDK transport, provenance and absolute deadlines."""

import json

import httpx2
import pytest
from openai import OpenAI

from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic.test_runtime_skills import runtime, skill, submit
from agent_alfred.model import ModelRef, ScriptedModelFactory
from agent_alfred.openai_compatible import OpenAICompatibleAdapter
from agent_alfred.settings import Settings
from agent_alfred.stream_fallback import StreamFallback


def response(text):
    return httpx2.Response(
        200,
        json={
            "choices": [
                {
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


@pytest.mark.parametrize(
    "failure", ["registration", "encoding", "total_deadline", "cancel"]
)
def test_ce17_ce20_ce21_selector_presend_failure_never_becomes_an_attempt(
    tmp_path, failure
):
    skill(tmp_path / "builtin", "A", "private body")
    clock, wire = FakeClock(), []

    def dispatch(request):
        wire.append(request)
        return response('{"skills":["A"]}')

    def encoding_failure(request):
        if failure == "encoding":
            raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "fixture encoding")

    with httpx2.Client(
        transport=httpx2.MockTransport(dispatch),
        event_hooks={"request": [encoding_failure]},
    ) as http:
        sdk = OpenAI(
            api_key="fixture",
            base_url="http://fixture.invalid",
            http_client=http,
            max_retries=0,
        )
        client = StreamFallback(
            OpenAICompatibleAdapter(client=sdk, model=ModelRef("test", "m")),
            clock=clock,
        )
        with runtime(
            tmp_path,
            [],
            factory=ScriptedModelFactory(client),
            clock=clock,
            settings=Settings(max_steps=2, overall_deadline_s=3),
        ) as (host, _, capture):
            original = host.memory_service.forgetting.register_read

            def registration(*args, **kwargs):
                if failure == "registration":
                    return {"error": {"code": "storage_write_failed"}}
                if failure == "cancel":
                    raise KeyboardInterrupt()
                value = original(*args, **kwargs)
                if failure == "total_deadline":
                    clock.monotonic_value = 3
                return value

            host.memory_service.forgetting.register_read = registration
            _, result = submit(host, "hello")
            assert result.outcome == (
                "interrupted" if failure == "cancel" else "failed"
            )
            assert wire == []
            assert result.model_results == ()
            assert result.memory_telemetry["input_attempts"] == []
            assert not any(
                e.payload.name in ("attempt.committed", "attempt.aborted")
                for e in capture.events
            )
            assert result.step_count == 1


@pytest.mark.parametrize("ending", ["local", "total", "cancel"])
def test_ce17_sent_selector_cost_survives_timeout_without_resetting_run_deadline(
    tmp_path, ending
):
    skill(tmp_path / "builtin", "A", "whole body")
    clock, wire = FakeClock(), []

    def dispatch(request):
        wire.append(json.loads(request.content))
        if len(wire) == 1:
            if ending == "cancel":
                raise KeyboardInterrupt()
            clock.monotonic_value = 5 if ending == "local" else 10
            return response('{"skills":["A"]}')
        return response("answer")

    with httpx2.Client(transport=httpx2.MockTransport(dispatch)) as http:
        sdk = OpenAI(
            api_key="fixture",
            base_url="http://fixture.invalid",
            http_client=http,
            max_retries=0,
        )
        client = StreamFallback(
            OpenAICompatibleAdapter(client=sdk, model=ModelRef("test", "m")),
            clock=clock,
        )
        with runtime(
            tmp_path,
            [],
            factory=ScriptedModelFactory(client),
            clock=clock,
            settings=Settings(max_steps=2, overall_deadline_s=10),
        ) as (host, _, _):
            accepted, result = submit(host, "使用 A")
            assert len(wire) == (2 if ending == "local" else 1)
            assert (
                result.outcome
                == {"local": "completed", "total": "failed", "cancel": "interrupted"}[
                    ending
                ]
            )
            evidence = host.read_run_evidence(accepted.run_id, trace_root=tmp_path)
            assert len(evidence["attempts"]) == len(wire)
            assert (
                evidence["memory"]["input_attempts"][0]["purpose"] == "skill_selector"
            )
            if ending == "local":
                assert evidence["memory"]["skills"]["reason"] == "model_deadline"
                assert "whole body" in json.dumps(wire[1])


def test_ce19_fixed_selector_input_limit_is_not_a_model_fallback(tmp_path):
    skill(tmp_path / "builtin", "A", "body", "description")
    with runtime(tmp_path, [], settings=Settings(gate_input_character_limit=100)) as (
        host,
        model,
        _,
    ):
        _, result = submit(host, "hello")
        assert result.error == "input_limit_exceeded"
        assert result.outcome == "failed" and model.requests == []
        assert result.memory_telemetry["skill_input_preparation"]["status"] == "failed"
        assert result.memory_telemetry["skills"]["mode"] != "fallback"


def test_ce19_loaded_skill_fixed_input_failure_keeps_selector_attempt(tmp_path):
    skill(tmp_path / "builtin", "A", "body" * 1900)
    with runtime(
        tmp_path,
        ['{"skills":["A"]}'],
        settings=Settings(
            input_character_limit=10000,
            per_store_character_budget=1,
        ),
    ) as (host, model, _):
        _, result = submit(host, "hello")
        assert result.error == "input_limit_exceeded"
        assert len(model.requests) == 1
        assert (
            result.memory_telemetry["input_attempts"][0]["purpose"] == "skill_selector"
        )


def test_ce20_selector_source_invalidated_before_registration_is_never_sent(tmp_path):
    from agent_alfred.evals.deterministic.test_runtime_skills import SKIP

    skill(tmp_path / "builtin", "A", "body")
    with runtime(tmp_path, [SKIP, "safe previous", '{"skills":["A"]}']) as (host, _, _):
        first, _ = submit(host, "/skills off\nhello previous")
        original = host.memory_service.forgetting.register_read
        invalidated = []

        def register(*args, **kwargs):
            if kwargs["purpose"] == "skill_selector":
                value = host.memory_service.forgetting.register_group(
                    first.run_id,
                    kind="run",
                    container_id=first.session_id,
                    evidence="unknown",
                    transaction=kwargs["transaction"],
                    context=kwargs["context"],
                )
                assert "error" not in value, value
                invalidated.append(True)
            return original(*args, **kwargs)

        host.memory_service.forgetting.register_read = register
        _, result = submit(host, "hello now", first.session_id)
        assert invalidated == [True]
        assert result.error == "input_evidence_unavailable"
        assert result.memory_telemetry["input_attempts"] == []
        assert len(result.model_results) == 0
