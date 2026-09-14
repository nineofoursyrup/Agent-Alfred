"""CE-05/08/10: real SDK and SQLite failure before actual HTTP dispatch."""

import json
import sqlite3

import httpx2
import pytest
from openai import OpenAI

from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic.test_message_routing import enable
from agent_alfred.evals.deterministic.test_runtime_skills import SKIP, runtime, submit
from agent_alfred.evals.deterministic.test_skill_input_attempts import response
from agent_alfred.model import ModelRef, ScriptedModelFactory
from agent_alfred.openai_compatible import OpenAICompatibleAdapter
from agent_alfred.stream_fallback import StreamFallback


@pytest.mark.parametrize(
    "purpose,expected_requests", [("message_classifier", 1), ("answer", 2)]
)
def test_ce08_real_registration_io_failure_blocks_all_fallback(
    tmp_path, purpose, expected_requests
):
    wire = []
    clock = FakeClock()

    def dispatch(request):
        body = json.loads(request.content)
        wire.append(body)
        system = str(body["messages"][0])
        return response(
            SKIP
            if "Decide whether" in system
            else "full"
            if "Classify only" in system
            else "answer"
        )

    with httpx2.Client(transport=httpx2.MockTransport(dispatch)) as http:
        client = StreamFallback(
            OpenAICompatibleAdapter(
                client=OpenAI(
                    api_key="fixture",
                    base_url="http://fixture.invalid",
                    http_client=http,
                    max_retries=0,
                ),
                model=ModelRef("test", "m"),
            ),
            clock=clock,
        )
        with runtime(
            tmp_path, [], factory=ScriptedModelFactory(client), clock=clock
        ) as (host, _, sink):
            enable(host)
            with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
                conn.execute(
                    "CREATE TRIGGER input_io_failure BEFORE INSERT "
                    "ON run_input_explanations "
                    f"WHEN json_extract(NEW.explanation, '$.purpose') = '{purpose}' "
                    "BEGIN SELECT RAISE(ABORT, 'input evidence IO fault'); END"
                )
            _, result = submit(host, "task")
            assert result.error == "input_evidence_unavailable"
            assert len(wire) == expected_requests
            assert (
                result.memory_telemetry["routing"]["fallback"]["decision"] == "blocked"
            )
            assert (
                result.memory_telemetry["routing"]["fallback"]["reason"]
                == "input_evidence_unavailable"
            )


def test_ce08_real_source_invalidation_before_answer_blocks_fallback(tmp_path):
    with runtime(
        tmp_path, [SKIP, "full", "A answer", SKIP, "full", "must not send"]
    ) as (host, model, _):
        enable(host)
        first, _ = submit(host, "A")
        original = host.memory_service.forgetting.register_read
        invalidated = []

        def register(*args, **kwargs):
            if kwargs["purpose"] == "answer":
                value = host.memory_service.forgetting.register_group(
                    first.run_id,
                    kind="run",
                    container_id=first.session_id,
                    evidence="unknown",
                    transaction=kwargs["transaction"],
                    context=kwargs["context"],
                )
                assert "error" not in value
                invalidated.append(True)
            return original(*args, **kwargs)

        host.memory_service.forgetting.register_read = register
        _, result = submit(host, "B", first.session_id)
        assert invalidated == [True]
        assert result.error == "input_evidence_unavailable"
        # ScriptedModel captures the attempted request before its preflight.
        # Actual dispatch prevention is asserted by input evidence below.
        assert len(model.requests) == 6
        assert len(result.model_results) == 2
        assert result.memory_telemetry["routing"]["fallback"]["decision"] == "blocked"


def test_ce10_classifier_retry_spends_one_step_and_preserves_both_attempts(tmp_path):
    clock, wire, classifiers = FakeClock(), [], []

    def dispatch(request):
        body = json.loads(request.content)
        wire.append(body)
        system = str(body["messages"][0])
        if "Classify only" in system:
            classifiers.append(body)
            if len(classifiers) == 1:
                return httpx2.Response(503, json={"error": {"message": "temporary"}})
            return response("full")
        return response(SKIP if "Decide whether" in system else "answer")

    with httpx2.Client(transport=httpx2.MockTransport(dispatch)) as http:
        client = StreamFallback(
            OpenAICompatibleAdapter(
                client=OpenAI(
                    api_key="fixture",
                    base_url="http://fixture.invalid",
                    http_client=http,
                    max_retries=0,
                ),
                model=ModelRef("test", "m"),
            ),
            clock=clock,
        )
        from agent_alfred.retry import RetryPolicy, SystemSleeper

        client = RetryPolicy(
            client, clock=clock, sleeper=SystemSleeper(), retry_delay_s=0
        )
        with runtime(
            tmp_path, [], factory=ScriptedModelFactory(client), clock=clock
        ) as (host, _, _):
            enable(host)
            _, result = submit(host, "task")
            assert result.outcome == "completed"
            assert len(wire) == 4
            facts = result.memory_telemetry["routing"]["classification"]
            assert len(facts["attempt_ids"]) == 2
            assert result.step_count == 3
