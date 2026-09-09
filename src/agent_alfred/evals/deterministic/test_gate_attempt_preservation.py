"""A later unsent fallback cannot erase a real gate Attempt from the Run ledger."""

import json

import httpx2
import pytest
from openai import OpenAI

from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic.test_runtime_memory_gate import runtime
from agent_alfred.model import ModelAssignment, ModelRef, ScriptedModel
from agent_alfred.openai_compatible import OpenAICompatibleAdapter
from agent_alfred.retry import RetryPolicy
from agent_alfred.runtime.config import MutableAssignmentProvider
from agent_alfred.runtime.host import SubmitRequest
from agent_alfred.settings import Settings
from agent_alfred.stream_fallback import StreamFallback


@pytest.mark.parametrize(
    "strategy", ["fallback", "retry", "nested", "backoff", "interrupt"]
)
def test_unsent_gate_fallback_retains_prior_usage_identity_model_and_references(
    strategy, tmp_path
):
    clock = FakeClock()
    sent, hooks = [], []
    answer = ScriptedModel(["answer"])

    def dispatch(request):
        sent.append(request)
        if strategy == "nested" and len(sent) == 2:
            return httpx2.Response(500, json={"error": {"message": "fixture"}})
        chunk = {
            "choices": [{"delta": {"content": "partial"}, "finish_reason": None}],
            "usage": {"completion_tokens": 11 if len(sent) == 3 else 7},
        }
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(chunk)}\n\n".encode(),
        )

    def request_hook(request):
        hooks.append(request)
        if len(hooks) == (4 if strategy == "nested" else 2):
            if strategy == "interrupt":
                raise KeyboardInterrupt("injected before fallback send")
            clock.monotonic_value = 5.0

    with httpx2.Client(
        transport=httpx2.MockTransport(dispatch),
        event_hooks={"request": [request_hook]},
    ) as http:
        sdk = OpenAI(
            api_key="fixture",
            base_url="http://fixture.invalid",
            http_client=http,
            max_retries=0,
        )
        model = ModelRef("gate-endpoint", "gate-model")
        gate = StreamFallback(
            OpenAICompatibleAdapter(client=sdk, model=model, stream=True),
            nonstream=OpenAICompatibleAdapter(client=sdk, model=model),
            clock=clock,
            stream=True,
            stream_fallback=strategy not in ("retry", "backoff"),
        )
        if strategy in ("retry", "nested", "backoff"):

            class NoSleep:
                def sleep(self, seconds):
                    if strategy == "backoff":
                        raise OSError("injected backoff failure")
                    raise AssertionError("zero delay must never sleep")

            gate = RetryPolicy(
                gate,
                clock=clock,
                sleeper=NoSleep(),
                retry_delay_s=0.25 if strategy == "backoff" else 0,
            )

        class Factory:
            def create(self, snapshot):
                return gate if snapshot.endpoint_id == "gate-endpoint" else answer

        settings = Settings(overall_deadline_s=10)
        provider = MutableAssignmentProvider(
            endpoint_id="answer-endpoint",
            model_id="answer-model",
            wire_style="openai",
            api_key="fixture",
            settings=settings,
            retrieval_gate=ModelAssignment("gate-endpoint", "gate-model", "openai"),
        )
        with runtime(
            [],
            factory=Factory(),
            clock=clock,
            settings=settings,
            snapshot_provider=provider,
            no_sinks=True,
        ) as (host, _, _):
            submitted = host.submit(SubmitRequest("hello"))
            result = host.wait(submitted.run_id)
            interrupted = strategy == "interrupt"
            gate_count = 3 if strategy == "nested" else 1
            assert result.outcome == ("interrupted" if interrupted else "completed")
            assert len(hooks) == (
                4 if strategy == "nested" else 1 if strategy == "backoff" else 2
            )
            assert len(sent) == gate_count
            if strategy in ("fallback", "retry", "nested"):
                assert (
                    result.memory_telemetry["gate"]["fallback_reason"]
                    == "model_deadline"
                )
            inputs = result.memory_telemetry["input_attempts"]
            assert [item["purpose"] for item in inputs] == (
                ["gate"] * gate_count + ([] if interrupted else ["answer"])
            )
            assert len(result.model_results) == (1 if interrupted else 2)
            attempts = result.model_results[0].attempts
            assert len(attempts) == gate_count
            assert [a.attempt_id for a in attempts] == [
                item["attempt_id"] for item in inputs[:gate_count]
            ]
            assert len({a.attempt_id for a in attempts}) == gate_count
            assert [a.usage.output_tokens for a in attempts] == (
                [7, None, 11] if strategy == "nested" else [7]
            )
            assert all(a.model == model for a in attempts)
            assert all(item["references"] == [] for item in inputs[:gate_count])
            assert answer.deadlines == ([] if interrupted else [10.0])
            evidence = host.read_run_evidence(submitted.run_id, trace_root=tmp_path)
            assert evidence["events"] == []
            assert evidence["recording_state"] == "recorded"
            assert evidence["memory"]["input_attempts"] == inputs
            assert [a["attempt_id"] for a in evidence["attempts"]] == [
                item["attempt_id"] for item in inputs
            ]
            assert [
                a["usage"]["output_tokens"] for a in evidence["attempts"][:gate_count]
            ] == ([7, None, 11] if strategy == "nested" else [7])
