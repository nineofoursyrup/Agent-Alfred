"""Absolute send deadlines through RuntimeHost, real SDK and MemoryBIO TLS."""

import gc
import os
import socket
import ssl
import weakref
from unittest.mock import patch

import pytest
import truststore

from agent_alfred import attempt_io
from agent_alfred.bounded_connect import BoundedConnector
from agent_alfred.clock import FakeClock
from agent_alfred.endpoint_factory import EndpointClientFactory
from agent_alfred.evals.deterministic.test_runtime_memory_gate import runtime, save_fact
from agent_alfred.evals.deterministic.test_tls_cleanup_ownership import (
    local_certificate as _local_certificate,
)
from agent_alfred.model import ModelAssignment, ModelRef, ScriptedModel
from agent_alfred.runtime.config import MutableAssignmentProvider
from agent_alfred.runtime.host import SubmitRequest
from agent_alfred.settings import Settings

local_certificate = _local_certificate


@pytest.mark.parametrize("style", ["openai", "anthropic"])
@pytest.mark.parametrize("phase", ["handshake", "application"])
@pytest.mark.parametrize("run_limit,close_failures", [(10, 0), (3, 0), (4, 0), (10, 2)])
def test_tls_partial_writes_share_gate_and_run_deadlines(
    local_certificate, tmp_path, style, phase, run_limit, close_failures
):
    clock = FakeClock()
    sends, refs, cleanup_errors = [], [], []
    closes = []
    key, cert, root_cert = local_certificate
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    client_context = ssl.create_default_context(cafile=str(root_cert))

    class Resolver:
        def resolve(self, host, port, budget):
            budget.check()
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    ("127.0.0.1", port),
                )
            ]

    class Socket:
        def __init__(self, *args):
            refs.append(weakref.ref(self))
            self.timeout = None
            self.chunk_size = None
            self.ready = False
            self.incoming, self.outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
            self.server = server_context.wrap_bio(
                self.incoming, self.outgoing, server_side=True
            )

        def setsockopt(self, *args):
            pass

        def settimeout(self, value):
            self.timeout = value

        def connect(self, address):
            pass

        def send(self, data):
            slow = phase == "handshake" or self.ready
            if slow:
                if self.chunk_size is None:
                    self.chunk_size = (len(data) + 5) // 6
                sends.append((clock.monotonic(), self.timeout))
                clock.monotonic_value += min(2, self.timeout)
                if self.timeout < 2:
                    raise socket.timeout("partial send timeout")
                count = min(self.chunk_size, len(data))
            else:
                count = len(data)
            self.incoming.write(data[:count])
            if not self.ready:
                try:
                    self.server.do_handshake()
                    self.ready = True
                except ssl.SSLWantReadError:
                    pass
            return count

        def recv(self, count):
            assert phase == "application", "expired handshake must not reach recv"
            data = self.outgoing.read(count)
            assert data, "expired application send must not wait for a response"
            return data

        def close(self):
            closes.append(True)
            if len(closes) <= close_failures:
                error = OSError("injected cleanup failure")
                cleanup_errors.append(error)
                raise error

    settings = Settings(overall_deadline_s=run_limit)
    answer = ScriptedModel(["answer"])

    # Use the real default owned clients. The TLS context remains real and
    # verifies the in-memory server against our local test CA and hostname.
    def wrap_bio(context, incoming, outgoing, **kwargs):
        kwargs["server_hostname"] = "localhost"
        return client_context.wrap_bio(incoming, outgoing, **kwargs)

    with (
        patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}),
        patch.object(
            attempt_io,
            "BoundedConnector",
            lambda: BoundedConnector(resolver=Resolver(), socket_factory=Socket),
        ),
        patch.object(truststore.SSLContext, "wrap_bio", wrap_bio),
    ):
        real_factory = EndpointClientFactory(clock=clock)

        class Factory:
            def create(self, snapshot):
                return (
                    real_factory.create(snapshot)
                    if snapshot.endpoint_id == "opencode-go"
                    else answer
                )

            def invalidate_all(self):
                real_factory.invalidate_all()

        model = "deepseek-v4-flash" if style == "openai" else "qwen3.7-max"
        provider = MutableAssignmentProvider(
            endpoint_id="answer-endpoint",
            model_id="answer-model",
            wire_style="openai",
            api_key="fixture",
            settings=settings,
            retrieval_gate=ModelAssignment("opencode-go", model, style),
        )
        with runtime(
            [],
            factory=Factory(),
            clock=clock,
            settings=settings,
            snapshot_provider=provider,
            no_sinks=True,
        ) as (host, _, _):
            saved = save_fact(host)
            submitted = host.submit(SubmitRequest("coriander"))
            result = host.wait(submitted.run_id)
            expected = {
                10: [(0, 5), (2, 3), (4, 1)],
                3: [(0, 3), (2, 1)],
                4: [(0, 4), (2, 2)],
            }[run_limit]
            assert sends == expected
            assert clock.monotonic() == min(5, run_limit)
            can_answer = run_limit > 5
            assert (result.outcome == "completed") is can_answer
            assert answer.deadlines == ([run_limit] if can_answer else [])
            attempts = [a for r in result.model_results for a in r.attempts]
            inputs = result.memory_telemetry["input_attempts"]
            assert [a.attempt_id for a in attempts] == [i["attempt_id"] for i in inputs]
            assert [i["purpose"] for i in inputs] == (
                ["gate", "answer"] if can_answer else ["gate"]
            )
            assert attempts[0].model == ModelRef("opencode-go", model)
            assert attempts[0].usage.output_tokens is None
            assert inputs[0]["references"] == []
            if can_answer:
                assert (
                    result.memory_telemetry["gate"]["fallback_reason"]
                    == "model_deadline"
                )
                assert inputs[1]["references"][0]["memory_id"] == saved["memory_id"]
            evidence = host.read_run_evidence(submitted.run_id, trace_root=tmp_path)
            assert evidence["events"] == []
            assert evidence["memory"]["input_attempts"] == inputs
            assert [a["attempt_id"] for a in evidence["attempts"]] == [
                a.attempt_id for a in attempts
            ]
            assert evidence["attempts"][0]["usage"]["output_tokens"] is None
            if close_failures:
                pending, seen, messages = [cleanup_errors[0]], set(), []
                while pending:
                    error = pending.pop()
                    if error is None or id(error) in seen:
                        continue
                    seen.add(id(error))
                    messages.append(str(error))
                    pending.extend((error.__cause__, error.__context__))
                assert "partial send timeout" in messages
                gc.collect()
                assert refs[0]() is not None
                with pytest.raises(OSError, match="cleanup failure"):
                    real_factory.invalidate_all()
                assert len(closes) == 2 and refs[0]() is not None
                # Drop secondary exceptions too: the factory remains the owner.
                cleanup_errors.clear()
            real_factory.close()
            gc.collect()
            assert len(closes) == close_failures + 1
            assert all(ref() is None for ref in refs)
            real_factory.close()
            assert len(closes) == close_failures + 1


@pytest.mark.parametrize("style", ["openai", "anthropic"])
@pytest.mark.parametrize("control", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize(
    "close_failures,close_control", [(0, None), (2, None), (2, SystemExit)]
)
def test_application_send_control_and_cause_survive_sdk_cleanup(
    style, control, close_failures, close_control
):
    from agent_alfred.evals.deterministic.test_tls_cleanup_ownership import (
        finish_cleanup,
        tls_factory,
    )
    from agent_alfred.model import ModelCallInterrupted

    with tls_factory(
        style, close_failures=close_failures, close_control=close_control
    ) as (factory, config, request, state):
        original = ValueError("send original")
        failure = control("send control")
        signals = [failure, original]

        class TLS:
            def __init__(self, outgoing):
                self.outgoing = outgoing

            def do_handshake(self):
                pass

            def selected_alpn_protocol(self):
                return "http/1.1"

            def write(self, data):
                def send(data):
                    raise signals[0] from signals[1]

                state["refs"][0]().send = send
                self.outgoing.write(b"ciphertext")
                return len(data)

        def wrap_bio(context, incoming, outgoing, **kwargs):
            return TLS(outgoing)

        with (
            patch.object(ssl.SSLContext, "wrap_bio", wrap_bio),
            patch.object(truststore.SSLContext, "wrap_bio", wrap_bio),
        ):
            client = factory.create(config)
            try:
                with pytest.raises(ModelCallInterrupted) as caught:
                    client.respond(request, deadline=5)
                assert caught.value.cause is failure
                assert len(caught.value.result.attempts) == 1
                messages, seen, active = [], set(), set()

                def visit(error):
                    if error is None:
                        return
                    assert id(error) not in active, "failure graph must not cycle"
                    if id(error) in seen:
                        return
                    active.add(id(error))
                    seen.add(id(error))
                    messages.append(str(error))
                    visit(error.__cause__)
                    visit(error.__context__)
                    active.remove(id(error))

                visit(failure)
                assert "send original" in messages
                signals.clear()
                del caught, client, failure, original
                gc.collect()
                if close_failures:
                    assert state["refs"][0]() is not None
                finish_cleanup(factory, state, close_failures)
            finally:
                # Even on red, finish the known fixture's cleanup without
                # masking the test assertion or leaving an SDK destructor owner.
                for _ in range(close_failures + 1):
                    try:
                        factory.close()
                        break
                    except OSError:
                        pass
