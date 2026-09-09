"""Real SDK IO expiry reaches the public Run gate and shared deadline."""

import json

import httpcore2
import httpx2
import pytest
from openai import OpenAI

from agent_alfred.attempt_io import ModelHTTPTransport
from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic.test_runtime_memory_gate import runtime
from agent_alfred.model import ModelRef
from agent_alfred.openai_compatible import OpenAICompatibleAdapter
from agent_alfred.runtime.host import SubmitRequest
from agent_alfred.settings import Settings
from agent_alfred.stream_fallback import StreamFallback


@pytest.mark.parametrize("overall", [3.0, 10.0])
def test_real_io_expiry_falls_back_only_with_remaining_run_budget(overall):
    clock = FakeClock()
    connections = []
    closed = []
    budgets = []

    class Stream(httpcore2.NetworkStream):
        def __init__(self, index):
            self.index = index

        def read(self, max_bytes, timeout=None):
            if self.index == 1:
                clock.monotonic_value += timeout
                raise httpcore2.ReadTimeout("deadline reached at network boundary")
            body = json.dumps(
                {
                    "id": "answer",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "m",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "answer"},
                        }
                    ],
                }
            ).encode()
            return (
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )

        def write(self, buffer, timeout=None):
            pass

        def close(self):
            closed.append(self.index)

        def get_extra_info(self, info):
            return None

    class Backend(httpcore2.NetworkBackend):
        def connect_tcp(self, host, port, timeout=None, **kwargs):
            connections.append(host)
            budgets.append(timeout)
            return Stream(len(connections))

    transport = ModelHTTPTransport(httpx2.HTTPTransport(), network_backend=Backend())
    with httpx2.Client(transport=transport, trust_env=False) as http:
        sdk = OpenAI(
            api_key="fixture",
            base_url="http://fixture.invalid",
            http_client=http,
            max_retries=0,
        )
        adapter = OpenAICompatibleAdapter(client=sdk, model=ModelRef("test", "m"))
        client = StreamFallback(adapter, clock=clock)

        class Factory:
            def create(self, snapshot):
                return client

        with runtime(
            [],
            factory=Factory(),
            clock=clock,
            settings=Settings(overall_deadline_s=overall),
        ) as (host, _, capture):
            result = host.wait(host.submit(SubmitRequest("hello")).run_id)
            assert clock.monotonic_value == min(5.0, overall)
            assert 1 in closed
            attempts = result.memory_telemetry["input_attempts"]
            if overall == 3.0:
                assert budgets == [3.0]
                assert result.error == "overall_deadline"
                assert result.memory_telemetry["gate_state"] == "incomplete"
                assert [a["purpose"] for a in attempts] == ["gate"]
                assert not any(
                    e.payload.name == "gate.evaluated" for e in capture.events
                )
            else:
                assert budgets == [5.0, 5.0]
                assert result.outcome == "completed"
                assert (
                    result.memory_telemetry["gate"]["fallback_reason"]
                    == "model_deadline"
                )
                assert [a["purpose"] for a in attempts] == ["gate", "answer"]
