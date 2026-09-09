"""Control failures retain current/prior real usage even if accounting also fails."""

import json
import threading

import httpx2
import pytest
from anthropic import Anthropic
from openai import OpenAI

from agent_alfred.anthropic_native import AnthropicAdapter
from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic.test_runtime_memory_gate import runtime
from agent_alfred.model import ModelAssignment, ModelRef, ScriptedModel
from agent_alfred.openai_compatible import OpenAICompatibleAdapter
from agent_alfred.runtime.config import MutableAssignmentProvider
from agent_alfred.runtime.host import SubmitRequest
from agent_alfred.settings import Settings
from agent_alfred.stream_fallback import StreamFallback
from agent_alfred.support_overrides import SupportRecorder


@pytest.mark.parametrize("style", ["openai", "anthropic"])
@pytest.mark.parametrize("stage", ["stream", "fallback"])
@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("observation_fails", [False, True])
def test_interrupted_attempt_keeps_usage_and_control_on_public_run(
    style, stage, control_type, observation_fails, monkeypatch, tmp_path
):
    clock = FakeClock()
    control = control_type("injected model interruption")
    original_cause = ValueError("original cause must survive")
    control.__cause__ = original_cause
    observation_error = OSError("injected bookkeeping failure")
    propagated, sent, hooks, closed = [], [], [], []
    monkeypatch.setattr(
        threading, "excepthook", lambda args: propagated.append(args.exc_value)
    )
    if observation_fails:

        def fail_observation(self, snapshot, run_id, result, events):
            raise observation_error

        monkeypatch.setattr(SupportRecorder, "observe", fail_observation)
    if style == "openai":
        chunk = (
            b"data: "
            + json.dumps(
                {
                    "choices": [
                        {"delta": {"content": "partial"}, "finish_reason": None}
                    ],
                    "usage": {"completion_tokens": 7},
                }
            ).encode()
            + b"\n\n"
        )
    else:
        chunk = (
            b'event: message_start\ndata: {"type":"message_start",'
            b'"message":{"usage":{"output_tokens":7}}}\n\n'
        )

    class Stream(httpx2.SyncByteStream):
        def __iter__(self):
            yield chunk
            if stage == "stream":
                raise control

        def close(self):
            closed.append(True)

    def dispatch(request):
        sent.append(request)
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Stream()
        )

    def hook(request):
        hooks.append(request)
        if len(hooks) == 2:
            raise control

    answer = ScriptedModel(["must not answer"])
    with httpx2.Client(
        transport=httpx2.MockTransport(dispatch), event_hooks={"request": [hook]}
    ) as http:
        sdk = (OpenAI if style == "openai" else Anthropic)(
            api_key="fixture",
            base_url="http://fixture.invalid",
            http_client=http,
            max_retries=0,
        )
        model = ModelRef("gate-endpoint", "gate-model")
        adapter = OpenAICompatibleAdapter if style == "openai" else AnthropicAdapter
        gate = StreamFallback(
            adapter(client=sdk, model=model, stream=True),
            nonstream=adapter(client=sdk, model=model),
            clock=clock,
            stream=True,
        )

        class Factory:
            def create(self, snapshot):
                return gate if snapshot.endpoint_id == model.endpoint_id else answer

        settings = Settings(overall_deadline_s=10)
        provider = MutableAssignmentProvider(
            endpoint_id="answer",
            model_id="answer",
            wire_style="openai",
            api_key="fixture",
            settings=settings,
            retrieval_gate=ModelAssignment(model.endpoint_id, model.model_id, style),
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
            assert result.outcome == "interrupted"
            assert len(sent) == 1
            assert not answer.requests
            assert len(result.model_results) == 1
            (attempt,) = result.model_results[0].attempts
            assert attempt.usage.output_tokens == 7
            assert attempt.model == model
            inputs = result.memory_telemetry["input_attempts"]
            assert len(inputs) == 1
            assert inputs[0]["attempt_id"] == attempt.attempt_id
            evidence = host.read_run_evidence(submitted.run_id, trace_root=tmp_path)
            assert evidence["events"] == []
            assert evidence["attempts"][0]["usage"]["output_tokens"] == 7
        assert closed
    if control_type is SystemExit:
        assert propagated == [control]
    seen, active = set(), set()

    def visit(error):
        if error is None:
            return
        assert id(error) not in active, "exception graph must not cycle"
        if id(error) in seen:
            return
        active.add(id(error))
        seen.add(id(error))
        visit(error.__cause__)
        visit(error.__context__)
        for child in getattr(error, "exceptions", ()):
            visit(child)
        active.remove(id(error))

    visit(control)
    assert id(original_cause) in seen
    if observation_fails:
        assert id(observation_error) in seen
