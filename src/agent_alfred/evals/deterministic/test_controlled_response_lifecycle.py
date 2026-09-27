"""Public response and dispatch lifecycles retain bounded, retryable ownership."""

import sys
import threading
from time import monotonic

import pytest

from agent_alfred.evals.acceptance.controlled import runtime
from agent_alfred.evals.deterministic.test_controlled_execution import prepare
from agent_alfred.evals.deterministic.test_controlled_persistence import durable_fixture
from agent_alfred.evals.deterministic.test_controlled_sdk_resources import (
    readback_fixture,
)


@pytest.mark.parametrize("stage", ["invoke", "read"])
@pytest.mark.parametrize("close_fails", [False, True])
def test_active_sdk_body_remains_owned_until_the_consumer_finishes(
    monkeypatch, stage, close_fails
):
    failure = OSError("fixture body close") if close_fails else None
    rt, source, _ = readback_fixture("success", close_failure=failure)
    original = source.invoke
    entered, release = threading.Event(), threading.Event()
    errors = []
    results = []

    def pause():
        entered.set()
        assert release.wait(5)

    def invoke(**kwargs):
        if stage == "invoke":
            pause()
        result = original(**kwargs)
        if stage == "read":
            read = result["Payload"].read

            def delayed_read(*args):
                pause()
                return read(*args)

            result["Payload"].read = delayed_read
        return result

    monkeypatch.setattr(source, "invoke", invoke)

    def observe():
        try:
            results.append(rt.observe("fixture", {}))
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=observe)
    try:
        worker.start()
        assert entered.wait(5)
        assert rt.retry_cleanup() is False
        assert not source.bodies or not source.bodies[0].raw.closed
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert errors == ([failure] if close_fails else [])
        assert results == ([] if close_fails else [{"fixture": "readback"}])
        assert rt.retry_cleanup() is True
        assert rt.retry_cleanup() is True
        assert len(source.calls) == 1 and len(source.bodies) == 1
        body = source.bodies[0]
        assert body.raw.closed and body.close_calls == (2 if close_fails else 1)
    finally:
        release.set()
        worker.join(5)
        for body in source.bodies:
            body.raw.close()


@pytest.mark.parametrize("delayed_stage", ["thread", "preflight"])
def test_pending_send_cancellation_returns_before_blocking_cleanup(
    tmp_path, monkeypatch, delayed_stage
):
    f = durable_fixture(tmp_path)
    f["authority"].submit_job(f["submission"])
    client, _ = prepare(f)
    real_thread = threading.Thread
    sender_entered, release_sender = threading.Event(), threading.Event()
    close_entered, release_close = threading.Event(), threading.Event()
    caller_finished = threading.Event()
    spawned = []
    observed = {}

    class Deferred:
        def __init__(self, *, target, **kwargs):
            if (
                delayed_stage == "thread"
                and kwargs.get("name") == "controlled-single-attempt"
            ):
                original = target

                def target():
                    sender_entered.set()
                    assert release_sender.wait(5)
                    original()

            self.thread = real_thread(target=target, **kwargs)
            spawned.append(self.thread)

        def start(self):
            self.thread.start()

    original_close = f["runtime"].transport.close

    def close():
        close_entered.set()
        assert release_close.wait(5)
        return original_close()

    monkeypatch.setattr(runtime, "Thread", Deferred)
    monkeypatch.setattr(f["runtime"].transport, "close", close)
    original_send = f["runtime"].send

    def send(payload, *, preflight, timeout):
        def held_preflight(stage):
            if stage == "send" and delayed_stage == "preflight":
                sender_entered.set()
                assert release_sender.wait(5)
            return preflight(stage)

        return original_send(payload, preflight=held_preflight, timeout=timeout)

    monkeypatch.setattr(f["runtime"], "send", send)
    deadline = monotonic() + 1.5

    def watch():
        try:
            assert sender_entered.wait(5)
            if delayed_stage == "thread":
                assert close_entered.wait(5)
            observed["returned"] = caller_finished.wait(
                max(0, deadline - monotonic()) + 0.35
            )
            observed["sends"] = len(f["sends"])
            observed["cleanup_while_held"] = f["runtime"].retry_cleanup()
        finally:
            release_close.set()
            release_sender.set()

    observer = real_thread(target=watch)
    try:
        observer.start()
        try:
            with pytest.raises(runtime.TransportFailure) as caught:
                client.respond(f["request"], deadline=deadline)
        finally:
            caller_finished.set()
        observer.join(5)
        assert observed == {
            "returned": True,
            "sends": 0,
            "cleanup_while_held": False,
        }, repr(caught.value)
        for thread in spawned:
            thread.join(5)
        assert len(f["sends"]) == 0
        assert f["runtime"].retry_cleanup() is True
        state = f["authority"].status(f["submission"]["job_id"])["state"]
        assert state["state"] == "SUSPENDED" and state["pending_units"] > 0
    finally:
        release_close.set()
        release_sender.set()
        observer.join(5)
        for thread in spawned:
            thread.join(5)
        f["ledger"].close()
        f["anchor"].close()


@pytest.mark.parametrize("interrupt", [False, True])
def test_close_guard_interruption_retains_public_transport(
    tmp_path, monkeypatch, interrupt
):
    f = durable_fixture(tmp_path)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    client, _ = prepare(f)
    created, closes = [], []
    client_type = runtime.httpx.Client
    original_close = f["runtime"].transport.close

    def record_client(*args, **kwargs):
        result = client_type(*args, **kwargs)
        created.append(result)
        return result

    def close():
        closes.append(True)
        return original_close()

    monkeypatch.setattr(runtime.httpx, "Client", record_client)
    monkeypatch.setattr(f["runtime"].transport, "close", close)
    fired = []

    def trace(frame, event, value):
        if frame.f_code.co_name == "_run" and frame.f_code.co_filename.endswith(
            "serialized_close.py"
        ):
            frame.f_trace_opcodes = True
            attempt = frame.f_locals.get("self")
            if event == "opcode" and attempt.execution.gi_running and not fired:
                fired.append(True)
                sys.settrace(None)
                raise KeyboardInterrupt("fixture close guard return edge")
        return trace

    if interrupt:
        threading.settrace(trace)
    try:
        result = client.respond(f["request"])
        assert (result.final_error is not None) is interrupt
        assert bool(fired) is interrupt
        assert len(created) == len(f["sends"]) == 1
        before = auth.status(f["submission"]["job_id"])
        if interrupt:
            error = auth.read_object(before["attempts"][0]["cleanup_error_ref"])
            assert (
                error["diagnostics"]["cleanup"]["errors"][0]["type"]
                == "KeyboardInterrupt"
            )
            assert before["state"]["state"] == "SUSPENDED"
        assert f["runtime"].retry_cleanup() is True
        assert f["runtime"].retry_cleanup() is True
        assert created[0].is_closed and len(closes) == 1
        assert len(f["sends"]) == 1 and auth.status(f["submission"]["job_id"]) == before
    finally:
        threading.settrace(None)
        sys.settrace(None)
        for made in created:
            made.close()
        f["ledger"].close()
        f["anchor"].close()


def test_interrupted_startup_guard_keeps_public_transport_releasable(
    tmp_path, monkeypatch
):
    f = durable_fixture(tmp_path)
    f["authority"].submit_job(f["submission"])
    client, _ = prepare(f)
    fired = []

    def trace(frame, event, value):
        if frame.f_code.co_name == "_post_once":
            frame.f_trace_opcodes = True
            if (
                event == "opcode"
                and frame.f_locals.get("cleanup_ready") is False
                and not fired
            ):
                fired.append(True)
                sys.settrace(None)
                raise KeyboardInterrupt("fixture startup guard interruption")
        return trace

    try:
        sys.settrace(trace)
        with pytest.raises(runtime.TransportFailure):
            client.respond(f["request"])
        sys.settrace(None)
        assert fired and not f["sends"]
        assert f["runtime"].retry_cleanup() is True
        assert f["runtime"].retry_cleanup() is True
        result = f["authority"].status(f["submission"]["job_id"])
        assert result["state"]["state"] == "SUSPENDED"
        assert result["state"]["pending_units"] > 0
        assert result["attempts"][0]["send_state"] == "CANCELLED_BEFORE_SEND"
    finally:
        sys.settrace(None)
        f["ledger"].close()
        f["anchor"].close()
