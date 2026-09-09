"""Explicit gate assignments select their own codec and route within an endpoint."""

import json

import httpx2

from agent_alfred.clock import FakeClock
from agent_alfred.endpoint_factory import EndpointClientFactory
from agent_alfred.evals.deterministic.test_runtime_memory_gate import runtime
from agent_alfred.model import ModelAssignment
from agent_alfred.runtime.config import MutableAssignmentProvider
from agent_alfred.runtime.host import SubmitRequest
from agent_alfred.settings import Settings


def test_same_endpoint_gate_uses_assigned_codec_and_keeps_answer_client():
    sent = []
    identities = []
    gate = '{"retrieve":false,"query":null,"reason_code":"greeting"}'

    def dispatch(request):
        identities.append((request.headers["user-agent"],
                           request.headers["x-opencode-session"]))
        body = json.loads(request.content)
        sent.append(
            (
                request.url.path,
                body["model"],
                "x-api-key" in request.headers,
                "authorization" in request.headers,
            )
        )
        if body["model"] == "qwen3.7-max":
            return httpx2.Response(
                200,
                json={
                    "id": "gate",
                    "type": "message",
                    "role": "assistant",
                    "model": "qwen3.7-max",
                    "content": [{"type": "text", "text": gate}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 3, "output_tokens": 7},
                },
            )
        return httpx2.Response(
            200,
            json={
                "id": "answer",
                "choices": [
                    {"message": {"content": "answer"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 11},
            },
        )

    clock = FakeClock()
    settings = Settings()
    with httpx2.Client(transport=httpx2.MockTransport(dispatch)) as http:
        factory = EndpointClientFactory(clock=clock, http_client=http)
        provider = MutableAssignmentProvider(
            endpoint_id="opencode-go",
            model_id="deepseek-v4-flash",
            wire_style="openai",
            api_key="fixture",
            settings=settings,
            retrieval_gate=ModelAssignment("opencode-go", "qwen3.7-max", "anthropic"),
        )
        with runtime(
            [],
            factory=factory,
            clock=clock,
            settings=settings,
            snapshot_provider=provider,
            no_sinks=True,
        ) as (host, _, _):
            submitted = host.submit(SubmitRequest("hello"))
            result = host.wait(submitted.run_id)
            assert result.outcome == "completed"
            assert identities[0] == identities[1]
            assert identities[0][0].startswith("agent-alfred/")
            assert sent == [
                ("/zen/go/v1/messages", "qwen3.7-max", True, False),
                ("/zen/go/v1/chat/completions", "deepseek-v4-flash", False, True),
            ]
            assert [
                a.usage.output_tokens for r in result.model_results for a in r.attempts
            ] == [7, 11]
