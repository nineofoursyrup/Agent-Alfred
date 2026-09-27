"""Public Attempt failures retain evidence without exporting transport objects."""

import json
import traceback
from datetime import timedelta
from threading import Event
from time import monotonic

import httpx2 as httpx
import pytest

from agent_alfred.evals.acceptance.controlled import runtime as runtime_module
from agent_alfred.evals.deterministic.test_controlled_execution import (
    fixture,
    prepare,
    response,
)
from agent_alfred.model import ModelCallInterrupted
from agent_alfred.resource_rollback import IncompleteRollback, ResumableRollback

KEY = "synthetic-controlled-credential"
HTTP_CLIENT = httpx.Client


def exception_graph(failure):
    pending, seen = [failure], set()
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        yield error
        for value in (
            error.__cause__,
            error.__context__,
            getattr(error, "cause", None),
            *getattr(error, "exceptions", ()),
            *error.args,
        ):
            if isinstance(value, BaseException):
                pending.append(value)


def assert_worker_safe(failure):
    for error in exception_graph(failure):
        assert not isinstance(error, (httpx.RequestError, IncompleteRollback))
        assert not hasattr(error, "request")
        assert not any(
            isinstance(value, (httpx.Request, HTTP_CLIENT, ResumableRollback))
            for value in vars(error).values()
        )
        assert KEY not in str(error)
        assert KEY not in repr(vars(error))
    assert KEY not in "".join(traceback.format_exception(failure))


@pytest.mark.parametrize("chained", [False, True])
def test_transport_error_exports_only_safe_causal_diagnostics(tmp_path, chained):
    def failed(request):
        try:
            # The ordinary message alone carries no credential. Its request does.
            raise httpx.ReadTimeout("ordinary fixture timeout", request=request)
        except httpx.ReadTimeout as cause:
            if not chained:
                raise
            raise httpx.ReadError(
                "fixture echoed " + request.headers["Authorization"], request=request
            ) from cause

    f = fixture(tmp_path, handler=failed)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    client, _ = prepare(f)
    with pytest.raises(ModelCallInterrupted) as captured:
        client.respond(f["request"])
    assert_worker_safe(captured.value)
    status = auth.status("controlled-job")
    row = status["attempts"][0]
    diagnostic = auth.read_object(row["error_ref"])
    assert diagnostic["type"] == ("ReadError" if chained else "ReadTimeout")
    causal = diagnostic["diagnostics"]["transport"]
    assert causal["errors"][0]["type"] == diagnostic["type"]
    assert causal["errors"][0]["cause"] == (1 if chained else None)
    assert "ordinary fixture timeout" in json.dumps(causal)
    assert KEY not in json.dumps(diagnostic)
    assert status["state"]["state"] == "SUSPENDED"
    assert status["state"]["pending_units"] == row["worst_units"] > 0
    assert len(f["sends"]) == len(captured.value.result.attempts) == 1


@pytest.mark.parametrize("close_fails", [False, True])
def test_thread_start_failure_closes_or_retains_trusted_cleanup(
    tmp_path, monkeypatch, close_fails
):
    f = fixture(tmp_path)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    model_client, _ = prepare(f)
    created, close_attempts = [], []
    client_type = runtime_module.httpx.Client
    transport = f["runtime"].transport

    def record_client(*args, **kwargs):
        client = client_type(*args, **kwargs)
        created.append(client)
        return client

    class UnableToStart:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError("synthetic thread start failure")

    def close_transport():
        close_attempts.append("close")
        if close_fails and len(close_attempts) == 1:
            raise OSError("synthetic transport cleanup failure")

    monkeypatch.setattr(runtime_module.httpx, "Client", record_client)
    monkeypatch.setattr(runtime_module, "Thread", UnableToStart)
    monkeypatch.setattr(transport, "close", close_transport)
    with pytest.raises(runtime_module.TransportFailure) as captured:
        model_client.respond(f["request"])
    # Both the send worker and cleanup worker failed to start. The trusted
    # owner keeps the unclosed client; the Attempt caller does not block in close.
    assert len(created) == 1 and not created[0].is_closed
    assert close_attempts == []
    assert_worker_safe(captured.value)
    status = auth.status("controlled-job")
    row = status["attempts"][0]
    diagnostic = auth.read_object(row["error_ref"])
    assert diagnostic["type"] == "RuntimeError"
    assert diagnostic["diagnostics"]["cleanup"] is not None
    assert status["state"]["state"] == "SUSPENDED" and not f["sends"]
    assert row["send_state"] == "CANCELLED_BEFORE_SEND"
    assert status["state"]["pending_units"] == row["worst_units"] > 0
    # Trusted recovery retries only the incomplete close, never the Attempt.
    assert f["runtime"].retry_cleanup() is not close_fails
    assert created[0].is_closed and len(close_attempts) == 1
    assert f["runtime"].retry_cleanup()
    assert len(close_attempts) == (2 if close_fails else 1)
    assert f["runtime"].retry_cleanup()
    assert len(close_attempts) == (2 if close_fails else 1)
    assert auth.status("controlled-job") == status


@pytest.mark.parametrize("fault", [None, "invalid_response", "usage_unknown"])
def test_response_survives_cleanup_failure_and_settles_only_verified_usage(
    tmp_path, monkeypatch, fault
):
    def received(request):
        result = response(request, usage=fault != "usage_unknown")
        if fault == "invalid_response":
            raw = result.json()
            raw["choices"] = []
            return httpx.Response(200, json=raw)
        return result

    f = fixture(tmp_path, handler=received)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    client, _ = prepare(f)
    close_attempts = []

    def close_transport():
        close_attempts.append("close")
        if len(close_attempts) == 1:
            raise OSError("synthetic transport cleanup failure")

    monkeypatch.setattr(f["runtime"].transport, "close", close_transport)
    result = client.respond(f["request"])
    assert result.final_error is not None
    status = auth.status("controlled-job")
    row = status["attempts"][0]
    assert row["error"] == (fault or "transport_cleanup_failed")
    assert status["state"]["state"] == "SUSPENDED"
    assert result.attempts[0].outcome == "aborted"
    assert (
        result.final_error.body_excerpt == auth.read_object(row["response_ref"])["raw"]
    )
    assert json.loads(result.final_error.body_excerpt)["id"] == "synthetic-completion"
    cleanup = auth.read_object(row["cleanup_error_ref"])
    assert cleanup["diagnostics"]["cleanup"]["errors"][0]["type"] == "OSError"
    assert cleanup["diagnostics"]["transport"] is None
    if fault == "usage_unknown":
        assert row["send_state"] == "MAY_HAVE_SENT"
        assert status["state"]["pending_units"] == row["worst_units"] > 0
        assert status["state"]["spent_units"] == 0
    else:
        assert row["send_state"] == "SETTLED"
        assert row["usage"] == {"input_tokens": 10, "output_tokens": 2}
        assert result.attempts[0].usage.total_input_tokens == 10
        assert status["state"]["spent_units"] == row["actual_units"] == 5400000
        assert status["state"]["pending_units"] == 0
    assert f["runtime"].retry_cleanup()
    assert close_attempts == ["close", "close"]
    assert f["runtime"].retry_cleanup()
    assert close_attempts == ["close", "close"]
    assert auth.status("controlled-job") == status
    with pytest.raises(ValueError):
        prepare(f)
    assert len(f["sends"]) == 1


def test_first_transport_failure_and_cleanup_gap_are_both_preserved(
    tmp_path, monkeypatch
):
    def failed(request):
        raise httpx.ReadTimeout("ordinary fixture timeout", request=request)

    f = fixture(tmp_path, handler=failed)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    client, _ = prepare(f)
    original_close = f["runtime"].transport.close

    def failed_close():
        raise OSError("cleanup echoed " + KEY)

    monkeypatch.setattr(f["runtime"].transport, "close", failed_close)
    with pytest.raises(ModelCallInterrupted) as captured:
        client.respond(f["request"])
    assert_worker_safe(captured.value)
    status = auth.status("controlled-job")
    row = status["attempts"][0]
    diagnostic = auth.read_object(row["error_ref"])
    assert diagnostic["type"] == "ReadTimeout"
    assert diagnostic["diagnostics"]["transport"]["errors"][0]["type"] == "ReadTimeout"
    assert diagnostic["diagnostics"]["cleanup"]["errors"][0]["type"] == "OSError"
    assert KEY not in json.dumps(diagnostic)
    monkeypatch.setattr(f["runtime"].transport, "close", original_close)
    assert f["runtime"].retry_cleanup()
    assert auth.status("controlled-job") == status
    assert len(f["sends"]) == 1


def test_cleanup_deadline_preserves_already_received_response(tmp_path, monkeypatch):
    f = fixture(tmp_path)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    client, _ = prepare(f)
    release, entered = Event(), Event()
    threads, closes = [], []
    thread_type = runtime_module.Thread

    def record_thread(**kwargs):
        thread = thread_type(**kwargs)
        threads.append(thread)
        return thread

    def slow_close():
        closes.append("close")
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(runtime_module, "Thread", record_thread)
    monkeypatch.setattr(f["runtime"].transport, "close", slow_close)
    try:
        result = client.respond(f["request"], deadline=monotonic() + 1.5)
        assert entered.is_set()
        assert result.final_error is not None
        status = auth.status("controlled-job")
        row = status["attempts"][0]
        assert row["send_state"] == "SETTLED" and row["outcome"] == "aborted"
        assert row["error"] == "transport_cleanup_failed"
        assert status["state"]["state"] == "SUSPENDED"
        assert status["state"]["spent_units"] == 5400000
        assert status["state"]["pending_units"] == 0
        assert json.loads(auth.read_object(row["response_ref"])["raw"])["id"]
        cleanup = auth.read_object(row["cleanup_error_ref"])
        assert "controlled_cleanup_deadline" in json.dumps(cleanup)
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
    assert f["runtime"].retry_cleanup()
    assert closes == ["close"] and len(f["sends"]) == 1
    assert auth.status("controlled-job") == status


@pytest.mark.parametrize("interrupt", [False, True])
@pytest.mark.parametrize("delayed_stage", ["thread", "request"])
def test_wait_failure_cancels_provider_thread_before_first_send(
    tmp_path, monkeypatch, interrupt, delayed_stage
):
    from agent_alfred.evals.deterministic.test_controlled_persistence import (
        durable_fixture,
    )

    f = durable_fixture(tmp_path)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    f["clock"].value += timedelta(seconds=10799.5)
    client, _ = prepare(f)
    release, entered = Event(), Event()
    threads = []
    thread_type = runtime_module.Thread

    class DeferredThread:
        def __init__(self, *, target, **kwargs):
            def delayed():
                if delayed_stage == "thread":
                    entered.set()
                    assert release.wait(5)
                target()

            self.thread = thread_type(target=delayed, **kwargs)
            threads.append(self.thread)

        def start(self):
            self.thread.start()

    class InterruptedWait(Event):
        def wait(self, timeout=None):
            assert entered.wait(5)
            raise KeyboardInterrupt("synthetic caller interruption")

    monkeypatch.setattr(runtime_module, "Thread", DeferredThread)
    if delayed_stage == "request":
        build = HTTP_CLIENT.build_request

        def delayed_build(self, *args, **kwargs):
            request = build(self, *args, **kwargs)
            entered.set()
            assert release.wait(5)
            return request

        monkeypatch.setattr(HTTP_CLIENT, "build_request", delayed_build)
    if interrupt:
        monkeypatch.setattr(runtime_module, "Event", InterruptedWait)
    try:
        with pytest.raises(runtime_module.TransportFailure):
            client.respond(f["request"], deadline=monotonic() + 1.5)
        stopped = auth.status("controlled-job")
        assert entered.is_set() and len(f["sends"]) == 0
        assert stopped["state"]["state"] == "SUSPENDED"
        assert stopped["state"]["pending_units"] > 0
        f["clock"].value += timedelta(seconds=2)
        release.set()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
        assert len(f["sends"]) == 0
        assert f["runtime"].retry_cleanup()
        assert auth.status("controlled-job") == stopped
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
        f["runtime"].retry_cleanup()
        f["ledger"].close()
        f["anchor"].close()


def test_actual_transport_checks_deadline_after_http_request_preparation(
    tmp_path, monkeypatch
):
    f = fixture(tmp_path)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    client, _ = prepare(f)
    build = HTTP_CLIENT.build_request

    def delayed_build(self, *args, **kwargs):
        request = build(self, *args, **kwargs)
        f["clock"].value += timedelta(seconds=121)
        return request

    monkeypatch.setattr(HTTP_CLIENT, "build_request", delayed_build)
    with pytest.raises(runtime_module.TransportFailure):
        client.respond(f["request"])
    assert not f["sends"]
    stopped = auth.status("controlled-job")
    assert stopped["state"]["state"] == "SUSPENDED"
    assert stopped["state"]["pending_units"] > 0
    assert f["runtime"].retry_cleanup()


def test_already_inflight_late_response_keeps_unknown_debt_without_second_send(
    tmp_path, monkeypatch
):
    entered, release = Event(), Event()
    threads = []
    thread_type = runtime_module.Thread

    def send(request):
        entered.set()
        assert release.wait(5)
        return response(request)

    def record_thread(**kwargs):
        thread = thread_type(**kwargs)
        threads.append(thread)
        return thread

    f = fixture(tmp_path, handler=send)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    client, _ = prepare(f)
    monkeypatch.setattr(runtime_module, "Thread", record_thread)
    try:
        with pytest.raises(ModelCallInterrupted):
            client.respond(f["request"], deadline=monotonic() + 1.5)
        stopped = auth.status("controlled-job")
        assert entered.is_set() and len(f["sends"]) == 1
        assert stopped["attempts"][0]["send_state"] == "MAY_HAVE_SENT"
        assert stopped["state"]["pending_units"] > 0
        assert f["runtime"].retry_cleanup() is False
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
    assert f["runtime"].retry_cleanup() is True
    assert auth.status("controlled-job") == stopped
    assert len(f["sends"]) == 1
