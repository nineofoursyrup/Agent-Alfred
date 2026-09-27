"""Public inert SDK/HTTP/SQLite boundaries retain their resource ownership."""

import sqlite3
import sys

import httpx2 as httpx
import pytest

from agent_alfred.evals.acceptance.controlled import capacity, persistence
from agent_alfred.evals.acceptance.controlled.aws_persistence import AwsExecutionStore
from agent_alfred.evals.acceptance.controlled.control import HTTPControlTransport
from agent_alfred.resource_rollback import IncompleteRollback

from ._monitoring_test_helpers import interrupt_py_return_once
from .test_controlled_aws_persistence import DynamoStub, S3Stub
from .test_controlled_sdk_resources import readback_fixture


def interrupt_caller_after_return(factory, action, failure):
    """Interrupt the caller's first opcode, not the third-party factory frame."""
    target = getattr(factory, "__func__", factory).__code__
    caller = None
    fired = False

    def trace(frame, event, value):
        nonlocal caller, fired
        if frame.f_code is target and event == "return":
            caller = frame.f_back
            caller.f_trace = trace
            caller.f_trace_opcodes = True
        elif frame is caller and event == "opcode":
            fired = True
            sys.settrace(None)
            raise failure
        return trace

    sys.settrace(trace)
    try:
        with pytest.raises(type(failure)) as caught:
            action()
        assert caught.value is failure
    finally:
        sys.settrace(None)
    assert fired


@pytest.mark.parametrize("kind", ["lambda", "s3"])
@pytest.mark.parametrize("failure_type", [OSError, KeyboardInterrupt])
def test_sdk_return_is_owned_before_first_caller_instruction(
    monkeypatch, kind, failure_type
):
    failure = failure_type("fixture SDK return-to-store interrupted")
    if kind == "lambda":
        runtime, source, _ = readback_fixture("function_error")

        def action():
            return runtime.observe("fixture", {})

        factory, cleanup, bodies = source.invoke, runtime.retry_cleanup, source.bodies
    else:
        s3 = S3Stub()
        store = AwsExecutionStore(
            dynamodb=DynamoStub(),
            s3=s3,
            table="fixture",
            bucket="fixture",
            retention_days=1,
        )
        identity = store.put_object({"fixture": "body ownership"})
        original, bodies = s3.get_object, []

        def factory(**kwargs):
            result = original(**kwargs)
            bodies.append(result["Body"])
            return result

        monkeypatch.setattr(s3, "get_object", factory)

        def action():
            return store.get_object(identity)

        cleanup = store.backend.retry_cleanup
    try:
        interrupt_caller_after_return(factory, action, failure)
        assert len(bodies) == 1
        assert cleanup() is True
        assert all(getattr(body, "raw", body).closed for body in bodies)
        assert cleanup() is True
    finally:
        for body in bodies:
            getattr(body, "raw", body).close()


@pytest.mark.parametrize("failure_type", [None, OSError, KeyboardInterrupt])
def test_control_response_cleanup_keeps_underlying_stream_retryable(failure_type):
    class Stream(httpx.SyncByteStream):
        closed = False
        closes = 0

        def __iter__(self):
            yield b'{"fixture":"value"}'

        def close(self):
            self.closes += 1
            if failure_type and self.closes <= 2:
                raise failure_type("fixture control stream cleanup interrupted")
            self.closed = True

    stream, sends = Stream(), []

    def send(request):
        sends.append(request)
        return httpx.Response(200, stream=stream)

    client = httpx.Client(transport=httpx.MockTransport(send), trust_env=False)
    control = HTTPControlTransport("https://control.invalid", client)
    try:
        if failure_type:
            with pytest.raises(failure_type) as caught:
                control.read_result("a" * 64, timeout=1)
            assert isinstance(caught.value.__cause__, IncompleteRollback)
        else:
            assert control.read_result("a" * 64, timeout=1) == {"fixture": "value"}
        assert control.retry_cleanup() is True
        assert stream.closed and len(sends) == 1
        assert stream.closes == (3 if failure_type else 1)
        assert control.retry_cleanup() is True
        assert stream.closes == (3 if failure_type else 1)
    finally:
        client.close()


def track_connections(monkeypatch):
    original, connections = sqlite3.connect, []

    def connect(*args, **kwargs):
        result = original(*args, **kwargs)
        connections.append(result)
        return result

    monkeypatch.setattr(sqlite3, "connect", connect)
    return connections


def assert_connections_closed(connections):
    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_sqlite_connect_return_is_owned_before_caller_storage(tmp_path, monkeypatch):
    connections = track_connections(monkeypatch)
    failure = KeyboardInterrupt("fixture native connection return interrupted")
    try:
        interrupt_caller_after_return(
            sqlite3.connect,
            lambda: persistence.SQLiteExecutionStore(tmp_path / "connect.sqlite"),
            failure,
        )
        assert_connections_closed(connections)
    finally:
        for connection in connections:
            connection.close()


def test_http_return_interruption_retains_stream_before_body_iteration(monkeypatch):
    class Stream(httpx.SyncByteStream):
        closes = 0
        closed = False

        def __iter__(self):
            yield b"{}"

        def close(self):
            self.closes += 1
            if self.closes == 1:
                raise OSError("fixture first stream close failed")
            self.closed = True

    stream, sends = Stream(), []

    def handler(request):
        sends.append(request)
        return httpx.Response(200, stream=stream)

    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    original = client.send

    def returned_response(*args, **kwargs):
        return original(*args, **kwargs)

    monkeypatch.setattr(client, "send", returned_response)
    control = HTTPControlTransport("https://control.invalid", client)
    try:
        interrupt_caller_after_return(
            returned_response,
            lambda: control.read_result("b" * 64, timeout=1),
            KeyboardInterrupt("fixture HTTP response return interrupted"),
        )
        assert control.retry_cleanup() is True
        assert stream.closed and stream.closes == 2 and len(sends) == 1
        assert control.retry_cleanup() is True and stream.closes == 2
    finally:
        client.close()


@pytest.mark.parametrize(
    "constructor",
    [
        persistence.SQLiteDocuments,
        persistence.SQLiteExecutionStore,
        persistence.SQLiteExecutionAnchor,
    ],
)
def test_sqlite_factory_owns_each_constructor_return(
    tmp_path, monkeypatch, constructor
):
    factory, *_ = capacity.capacity_fixture(tmp_path / "constructor")
    connections = track_connections(monkeypatch)
    failure = KeyboardInterrupt("fixture SQLite constructor return interrupted")
    try:
        with interrupt_py_return_once(
            "controlled-sqlite-return", constructor.__init__.__code__, failure
        ) as armed:
            with pytest.raises(KeyboardInterrupt) as caught:
                factory()
        assert not armed[0] and caught.value is failure
        assert_connections_closed(connections)
    finally:
        for connection in connections:
            connection.close()


@pytest.mark.parametrize("stage", ["normal", "anchor", "submit"])
def test_capacity_factory_and_demonstration_own_partial_construction(
    tmp_path, monkeypatch, stage
):
    factory, *_ = capacity.capacity_fixture(tmp_path / "material")
    connections = track_connections(monkeypatch)
    try:
        if stage == "normal":
            authority = factory()
            assert len(connections) == 2
            assert all(c.execute("SELECT 1").fetchone() == (1,) for c in connections)
            authority.anchor.close()
            authority.store.close()
        else:

            def fail(*args, **kwargs):
                raise OSError("fixture " + stage + " failed")

            if stage == "anchor":
                monkeypatch.setattr(capacity, "SQLiteExecutionAnchor", fail)
                action = factory
            else:
                monkeypatch.setattr(
                    capacity.PersistentControlledAuthority, "submit_job", fail
                )

                def action():
                    return capacity.demonstrate(tmp_path / "demonstration")

            with pytest.raises(OSError, match="fixture " + stage):
                action()
        assert_connections_closed(connections)
    finally:
        for connection in connections:
            connection.close()


@pytest.mark.parametrize("cleanup_type", [OSError, KeyboardInterrupt])
def test_capacity_failure_retains_cleanup_progress_and_first_error(
    tmp_path, monkeypatch, cleanup_type
):
    connections = track_connections(monkeypatch)
    original_close = persistence.SQLiteDocuments.close
    attempts = {}
    first = OSError("fixture submit failed before any send")
    cleanup_error = cleanup_type("fixture anchor close interrupted")

    def close(backend):
        name = backend.path.name
        attempts[name] = attempts.get(name, 0) + 1
        if name == "anchor.sqlite" and attempts[name] == 1:
            raise cleanup_error
        return original_close(backend)

    def fail(*args, **kwargs):
        raise first

    monkeypatch.setattr(persistence.SQLiteDocuments, "close", close)
    monkeypatch.setattr(capacity.PersistentControlledAuthority, "submit_job", fail)
    try:
        with pytest.raises(BaseException) as caught:
            capacity.demonstrate(tmp_path / "incomplete-cleanup")
        assert caught.value is (
            cleanup_error if cleanup_type is KeyboardInterrupt else first
        )
        pending = caught.value.__cause__
        assert isinstance(pending, IncompleteRollback)
        assert pending.failure is first
        assert pending.retry() is True
        assert pending.retry() is True
        assert attempts == {"anchor.sqlite": 2, "authority.sqlite": 1}
        assert_connections_closed(connections)
    finally:
        for connection in connections:
            connection.close()
