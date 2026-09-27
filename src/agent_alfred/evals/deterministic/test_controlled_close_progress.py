"""Actual HTTP and SDK leaves survive interrupted cleanup and contenders."""

import inspect
import sys
import threading
from contextlib import contextmanager
from io import BytesIO
from types import SimpleNamespace

import pytest

from agent_alfred.evals.acceptance.controlled import runtime, serialized_close
from agent_alfred.evals.deterministic.test_controlled_execution import prepare
from agent_alfred.evals.deterministic.test_controlled_persistence import durable_fixture
from agent_alfred.resource_rollback import RollbackSlot


class Leaf(BytesIO):
    def __init__(self, failure):
        super().__init__(b"{}")
        self.failure = failure
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.after = lambda: None

    def close(self):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.active, self.max_active)
        try:
            if self.calls == 1 and self.failure is not None:
                raise self.failure
            self.entered.set()
            assert self.release.wait(5)
            assert not self.closed, "non-repeatable leaf was closed twice"
            super().close()
            self.after()
        finally:
            self.active -= 1

    def close_completed(self):
        return self.closed


@contextmanager
def public_resource(path, tmp_path, monkeypatch, leaf):
    if path == "http":
        f = durable_fixture(tmp_path)
        auth = f["authority"]
        auth.submit_job(f["submission"])
        client, _ = prepare(f)
        rt = f["runtime"]
        made = []
        client_type = runtime.httpx.Client

        class NoStart:
            def __init__(self, **kwargs):
                pass

            def start(self):
                raise RuntimeError("fixture worker not started")

        def create_client(*args, **kwargs):
            result = client_type(*args, **kwargs)
            made.append(result)
            return result

        monkeypatch.setattr(runtime, "Thread", NoStart)
        monkeypatch.setattr(runtime.httpx, "Client", create_client)
        monkeypatch.setattr(rt.transport, "close", leaf.close)
        monkeypatch.setattr(
            rt.transport, "close_completed", leaf.close_completed, raising=False
        )
        with pytest.raises(runtime.TransportFailure):
            client.respond(f["request"])
        before = auth.status("controlled-job")
        assert before["state"]["pending_units"] > 0
        assert before["attempts"][0]["send_state"] == "CANCELLED_BEFORE_SEND"
        try:
            yield SimpleNamespace(first=rt.retry_cleanup, retry=rt.retry_cleanup)
            assert not f["sends"] and len(made) == 1
            assert made[0].is_closed
            assert auth.status("controlled-job") == before
        finally:
            f["ledger"].close()
            f["anchor"].close()
    else:
        calls = []

        class Source:
            def invoke(self, **kwargs):
                calls.append(kwargs)
                return {"StatusCode": 200, "FunctionError": "fixture", "Payload": leaf}

        rt = object.__new__(runtime.InstalledRuntime)
        rt._config = {"deployment_id": "fixture", "source_function_arn": "fixture"}
        rt._source, rt._cleanup, rt._closing = Source(), RollbackSlot(), False

        def observe():
            with pytest.raises(ValueError, match="trusted_readback_unavailable"):
                rt.observe("fixture", {})
            return False

        yield SimpleNamespace(first=observe, retry=rt.retry_cleanup)
        assert len(calls) == 1


def graph_contains(failure, expected):
    pending, seen = [failure], set()
    while pending:
        item = pending.pop()
        if item is expected:
            return True
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        pending.extend(
            [
                item.__cause__,
                item.__context__,
                getattr(item, "failure", None),
                getattr(item, "_restored_cause", None),
            ]
        )
        pending.extend(getattr(item, "errors", ()))
    return False


@pytest.mark.parametrize("path", ["http", "sdk"])
@pytest.mark.parametrize("failure_type", [OSError, ValueError])
@pytest.mark.parametrize("interrupt", [False, True])
def test_failed_close_then_interrupted_unwind_can_retry_same_leaf(
    tmp_path, monkeypatch, path, failure_type, interrupt
):
    failure = failure_type("fixture first leaf close failed")
    leaf = Leaf(failure)
    fired = []
    source, start = inspect.getsourcelines(serialized_close._CloseAttempt._run)
    target = next(
        start + i for i, line in enumerate(source) if "reraise_failure(failure)" in line
    )

    def trace(frame, event, value):
        if (
            event == "line"
            and frame.f_code is serialized_close._CloseAttempt._run.__code__
            and frame.f_lineno == target
            and not fired
        ):
            assert frame.f_locals["self"].execution.gi_running
            assert frame.f_locals["failure"] is failure
            assert leaf.calls == 1 and not leaf.closed
            fired.append((frame.f_code.co_filename, target))
            sys.settrace(None)
            raise KeyboardInterrupt("fixture close failure unwind interruption")
        return trace

    try:
        with public_resource(path, tmp_path, monkeypatch, leaf) as public:
            try:
                if interrupt:
                    sys.settrace(trace)
                    with pytest.raises(KeyboardInterrupt) as caught:
                        public.first()
                    assert graph_contains(caught.value, failure)
                else:
                    assert public.first() is False
            finally:
                sys.settrace(None)
            assert bool(fired) is interrupt
            assert leaf.calls == 1 and not leaf.closed
            assert [public.retry() for _ in range(3)] == [True, True, True]
            assert leaf.closed and leaf.calls == 2
    finally:
        sys.settrace(None)
        BytesIO.close(leaf)


@pytest.mark.parametrize("path", ["http", "sdk"])
@pytest.mark.parametrize("at_native_entry", [False, True])
def test_two_public_cleanup_callers_do_not_take_over_a_live_close(
    tmp_path, monkeypatch, path, at_native_entry
):
    leaf = Leaf(OSError("fixture first close fails"))
    results = []
    with public_resource(path, tmp_path, monkeypatch, leaf) as public:
        assert public.first() is False
        leaf.release.clear()
        rendezvous = threading.Barrier(2)
        done = threading.Event()
        fired = []
        source, start = inspect.getsourcelines(serialized_close.SerializedClose.close)
        target = next(
            start + i
            for i, line in enumerate(source)
            if "return next(execution)" in line
        )

        def trace(frame, event, value):
            if (
                event == "line"
                and frame.f_code is serialized_close.SerializedClose.close.__code__
                and frame.f_lineno == target
            ):
                fired.append(threading.get_ident())
                rendezvous.wait(5)
            return trace

        def retry():
            results.append(public.retry())
            done.set()

        workers = [
            threading.Thread(target=retry) for _ in range(2 if at_native_entry else 1)
        ]
        try:
            if at_native_entry:
                threading.settrace(trace)
            for worker in workers:
                worker.start()
            assert leaf.entered.wait(5)
            if at_native_entry:
                assert done.wait(5)
                assert results == [False] and len(fired) == 2
            else:
                assert public.retry() is False
                assert results == []
            assert any(worker.is_alive() for worker in workers)
            assert not leaf.closed and leaf.calls == 2
            leaf.release.set()
            for worker in workers:
                worker.join(5)
            assert not any(worker.is_alive() for worker in workers)
            assert sorted(results) == ([False, True] if at_native_entry else [True])
            assert public.retry() is True
            assert leaf.closed and leaf.calls == 2 and leaf.max_active == 1
        finally:
            threading.settrace(None)
            leaf.release.set()
            for worker in workers:
                worker.join(5)
            BytesIO.close(leaf)


@pytest.mark.parametrize("path", ["http", "sdk"])
def test_published_nonrepeatable_leaf_completion_is_not_repeated(
    tmp_path, monkeypatch, path
):
    leaf = Leaf(None)
    interruption = KeyboardInterrupt("fixture after non-repeatable close")

    def interrupt():
        raise interruption

    leaf.after = interrupt
    try:
        with public_resource(path, tmp_path, monkeypatch, leaf) as public:
            with pytest.raises(KeyboardInterrupt) as caught:
                public.first()
            assert caught.value is interruption
            assert [public.retry() for _ in range(3)] == [True, True, True]
            assert leaf.calls == 1 and leaf.closed
    finally:
        BytesIO.close(leaf)
