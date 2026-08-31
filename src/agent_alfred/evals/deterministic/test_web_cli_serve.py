"""CLI dashboard ownership and serve-loop contracts."""

from __future__ import annotations

import io
import socket
import threading

from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
    free_loopback_port,
)
from agent_alfred.evals.deterministic._web_startup_test_helpers import (
    file_database,
    scripted_factory,
)
from agent_alfred.gateway.web.api import DashboardApi
from agent_alfred.gateway.web.lifecycle import (
    LOCK_NAME,
    EntryDescriptor,
    ProcessLock,
    read_entry_descriptor,
)
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.work import SubmitRequest
from agent_alfred.settings import Settings
from agent_alfred.wiring import build_dashboard

# --- the CLI hosts the Dashboard ------------------------------------------


class _CapturingRuntime:
    """Remembers what ``main`` built, after ``main`` has closed it."""

    def __init__(self, inner):
        self._inner = inner
        self.descriptor = None
        self.host = None
        self.broker = None
        self.closed = False

    def start(self):
        descriptor = self._inner.start()
        self.descriptor = descriptor
        self.host = self._inner.host
        self.broker = self._inner.broker
        return descriptor

    def close(self, *args, **kwargs):
        self.closed = True
        return self._inner.close(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _cli_build(**kwargs):
    """The ``main`` seam: a real Dashboard, wrapped so a test can see it."""
    return _CapturingRuntime(
        build_dashboard(**kwargs, open_database=file_database)
    )


def test_the_default_cli_starts_the_dashboard(tmp_path, capsys) -> None:
    """#23 §1: the default command is not the REPL alone."""
    from agent_alfred.gateway import cli as cli_module

    port = free_loopback_port()
    code = cli_module.main(
        ["--state-dir", str(tmp_path), "--port", str(port), "-m", "hi"],
        factory=scripted_factory(),
        build=_cli_build,
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert f"dashboard on 127.0.0.1:{port}" in printed
    assert "pong" in printed


def test_cli_events_reach_the_same_broker(tmp_path) -> None:
    """One Broker, so the CLI's Run is observable in a browser at all.

    Its transient events are never written to the database, which is why a
    second process tailing the trace could never show them.
    """
    from agent_alfred.gateway import cli as cli_module

    captured: list[_CapturingRuntime] = []

    def build(**kwargs):
        runtime = _cli_build(**kwargs)
        captured.append(runtime)
        return runtime

    port = free_loopback_port()
    assert (
        cli_module.main(
            ["--state-dir", str(tmp_path), "--port", str(port), "-m", "hi"],
            factory=scripted_factory(),
            build=build,
        )
        == 0
    )
    runtime = captured[0]
    assert runtime.broker is not None
    # The CLI's Run went through the FanOut the browser reads.
    assert runtime.broker._ring.high_water_seq() >= 2
    assert runtime.broker._latest.state_revision >= 1


def test_the_cli_and_the_web_compete_for_one_coordinator(tmp_path) -> None:
    """Both surfaces call the same ``submit``; neither has its own Run."""
    gate = threading.Event()
    runtime = build_dashboard(
        state_dir=tmp_path,
        factory=ScriptedModelFactory(ScriptedModel(["pong"], gate=gate)),
        clock=FakeClock(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    runtime.start()
    try:
        host = runtime.host
        api = DashboardApi(facade=host)
        session_id = host.create_session()
        web = host.submit(
            SubmitRequest(
                message="from the web", session_id=session_id, gateway="web"
            )
        )
        assert web.kind == "accepted"
        # A web Run holds the lease, so the CLI's Run is refused -- refused,
        # not queued, and not given its own coordinator.
        cli_attempt = host.submit(
            SubmitRequest(
                message="from the cli", session_id=session_id, gateway="cli"
            )
        )
        assert cli_attempt.kind == "run_in_progress"
        # ... and the same in the other direction.
        assert api.submit({"message": "again", "session_id": session_id}).status == 409
        gate.set()
        host.wait(web.run_id)
    finally:
        runtime.close()


def test_cli_initial_session_is_refused_while_a_web_run_owns_the_gate(
    tmp_path,
) -> None:
    """CLI Session creation uses the same non-queueing gate as the Web.

    The Dashboard is already serving when the foreground CLI creates its
    initial Session.  A model barrier keeps a Web Run's lease across that
    boundary: the CLI must return without a second Session write, rather
    than bypassing or waiting for the Run.
    """
    from agent_alfred.gateway import cli as cli_module

    run_gate = threading.Event()
    run_admitted = threading.Event()

    class HeldRunRuntime(_CapturingRuntime):
        def __init__(self, inner):
            super().__init__(inner)
            self.session_count_before_close: int | None = None

        def start(self):
            descriptor = super().start()
            api = DashboardApi(facade=self.host)
            session_id = api.create_session().session_id
            assert session_id is not None
            outcome = api.submit(
                {"message": "from the web", "session_id": session_id}
            )
            assert outcome.status == 202
            run_admitted.set()
            return descriptor

        def close(self, *args, **kwargs):
            assert run_admitted.is_set()
            self.session_count_before_close = len(
                self.host.list_sessions(limit=10, cursor=None).sessions
            )
            run_gate.set()
            return super().close(*args, **kwargs)

    out = io.StringIO()
    runtime = HeldRunRuntime(
        build_dashboard(
            state_dir=tmp_path,
            settings=Settings(),
            factory=ScriptedModelFactory(
                ScriptedModel(["pong"], gate=run_gate)
            ),
            port=free_loopback_port(),
            trace_root=tmp_path / "traces",
            open_database=file_database,
        )
    )
    args = type("Args", (), {"message": "hi"})()

    assert (
        cli_module._chat_in_the_foreground(
            runtime, args, Settings(), out=out
        )
        == 1
    )
    assert runtime.session_count_before_close == 1
    assert "Session was not created" in out.getvalue()


def test_the_cli_releases_the_socket_the_descriptor_and_the_lock(tmp_path) -> None:
    """Exiting the CLI leaves the state directory free.

    Otherwise a second terminal would be refused for as long as the first is
    open -- the one thing a single-instance lock must not do to someone who
    simply started the assistant twice.
    """
    from agent_alfred.gateway import cli as cli_module

    port = free_loopback_port()
    assert (
        cli_module.main(
            ["--state-dir", str(tmp_path), "--port", str(port), "-m", "hi"],
            factory=scripted_factory(),
            build=_cli_build,
        )
        == 0
    )
    assert read_entry_descriptor(tmp_path) is None
    # The port answers again, so the same port can be bound immediately.
    probe = socket.socket()
    probe.bind(("127.0.0.1", port))
    probe.close()
    # And so does the lock.
    fresh = ProcessLock(tmp_path / LOCK_NAME)
    fresh.acquire()
    fresh.release()


def test_serve_never_enters_the_repl(tmp_path, monkeypatch) -> None:
    """``--serve`` is the Dashboard and nothing else."""
    from agent_alfred.gateway import cli as cli_module

    def refuse_input(*args, **kwargs):
        raise AssertionError("--serve entered the REPL")

    monkeypatch.setattr("builtins.input", refuse_input)
    stop = threading.Event()
    stop.set()
    assert (
        cli_module.serve_dashboard(
            state_dir=tmp_path,
            settings=Settings(),
            port=free_loopback_port(),
            out=io.StringIO(),
            stop=stop,
            build=_cli_build,
        )
        == 0
    )


class _ServeWaitRuntime:
    descriptor = EntryDescriptor("serve-wait", 123, 17717)

    def __init__(self) -> None:
        self.close_calls = 0

    def start(self) -> None:
        return None

    def close(self) -> bool:
        self.close_calls += 1
        return True


class _CloseProgressRuntime:
    """A CLI-owned runtime whose close makes scripted, observable progress."""

    descriptor = EntryDescriptor("close-progress", 123, 17717)

    class _Host:
        def __init__(self) -> None:
            self.mutating = False

        @staticmethod
        def create_session() -> str:
            return "cli-session"

        def try_begin_mutation(self) -> str | None:
            if self.mutating:
                return "mutation_in_flight"
            self.mutating = True
            return None

        def end_mutation(self) -> None:
            self.mutating = False

    def __init__(
        self,
        close_results: list[bool],
        *,
        start_error: BaseException | None = None,
    ) -> None:
        self.host = self._Host()
        self._close_results = iter(close_results)
        self._start_error = start_error
        self.close_calls = 0
        self.close_retried = threading.Event()
        self.owns_runtime = True

    def start(self) -> None:
        if self._start_error is not None:
            raise self._start_error

    def close(self) -> bool:
        self.close_calls += 1
        if self.close_calls >= 2:
            self.close_retried.set()
        closed = next(self._close_results)
        if closed:
            self.owns_runtime = False
        return closed


class _InterruptingWaiter(threading.Event):
    def __init__(self) -> None:
        super().__init__()
        self.wait_calls: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.wait_calls.append(timeout)
        raise KeyboardInterrupt


def test_cli_maps_session_gate_unavailability_to_safe_failures() -> None:
    """Closed admission is non-zero and never exposes exception detail."""
    from agent_alfred.gateway import cli as cli_module

    class RefusingHost:
        def __init__(self, code: str) -> None:
            self.code = code

        def try_begin_mutation(self) -> str:
            return self.code

        @staticmethod
        def create_session() -> str:
            raise RuntimeError("secret database detail")

    class RefusingRuntime:
        descriptor = EntryDescriptor("refused-session", 123, 17717)

        def __init__(self, code: str) -> None:
            self.host = RefusingHost(code)
            self.closed = False

        def start(self) -> None:
            return None

        def close(self) -> bool:
            self.closed = True
            return True

    expected = {
        "recording_unavailable": "Recording unavailable; Session was not created.",
        "admission_failed": "Admission failed; Session was not created.",
    }
    args = type("Args", (), {"message": "hi"})()
    for code, message in expected.items():
        runtime = RefusingRuntime(code)
        out = io.StringIO()
        assert (
            cli_module._chat_in_the_foreground(
                runtime, args, Settings(), out=out
            )
            == 1
        )
        assert runtime.closed is True
        assert message in out.getvalue()
        assert "secret database detail" not in out.getvalue()


def test_serve_waits_directly_on_the_callers_stop_event(tmp_path) -> None:
    """An idle Dashboard sleeps until its caller asks it to stop."""
    from agent_alfred.gateway import cli as cli_module

    runtime = _ServeWaitRuntime()
    stop = _InterruptingWaiter()

    assert (
        cli_module.serve_dashboard(
            state_dir=tmp_path,
            settings=Settings(),
            out=io.StringIO(),
            stop=stop,
            build=lambda **kwargs: runtime,
        )
        == 0
    )
    assert stop.wait_calls == [None]
    assert runtime.close_calls == 1


def test_serve_without_a_stop_event_uses_one_permanent_waiter(
    tmp_path, monkeypatch
) -> None:
    """Ctrl-C interrupts one indefinite wait; no hourly timers are created."""
    from agent_alfred.gateway import cli as cli_module

    runtime = _ServeWaitRuntime()
    waiters: list[_InterruptingWaiter] = []

    def event_factory() -> _InterruptingWaiter:
        waiter = _InterruptingWaiter()
        waiters.append(waiter)
        return waiter

    monkeypatch.setattr(cli_module.threading, "Event", event_factory)
    assert (
        cli_module.serve_dashboard(
            state_dir=tmp_path,
            settings=Settings(),
            out=io.StringIO(),
            build=lambda **kwargs: runtime,
        )
        == 0
    )
    assert len(waiters) == 1
    assert waiters[0].wait_calls == [None]
    assert runtime.close_calls == 1


def test_repl_exit_advances_retryable_close_until_it_finishes(
    tmp_path, monkeypatch
) -> None:
    """A normal CLI exit does not drop two honest "not closed yet" answers."""
    from agent_alfred.gateway import cli as cli_module

    runtime = _CloseProgressRuntime([False, False, True])
    monkeypatch.setattr(
        "builtins.input", lambda *args, **kwargs: (_ for _ in ()).throw(EOFError)
    )

    assert (
        cli_module.main(
            ["--state-dir", str(tmp_path)],
            build=lambda **kwargs: runtime,
        )
        == 0
    )
    assert runtime.close_retried.is_set()
    assert runtime.close_calls == 3
    assert runtime.owns_runtime is False


def test_serve_interrupt_advances_retryable_close_until_it_finishes(tmp_path) -> None:
    """Ctrl-C remains a successful stop only after close confirms completion."""
    from agent_alfred.gateway import cli as cli_module

    runtime = _CloseProgressRuntime([False, False, True])
    stop = _InterruptingWaiter()

    assert (
        cli_module.serve_dashboard(
            state_dir=tmp_path,
            settings=Settings(),
            out=io.StringIO(),
            stop=stop,
            build=lambda **kwargs: runtime,
        )
        == 0
    )
    assert runtime.close_retried.is_set()
    assert runtime.close_calls == 3
    assert runtime.owns_runtime is False


def test_start_failure_advances_retryable_rollback_until_it_finishes(
    tmp_path,
) -> None:
    """A refused start is reported only after its resumable undo is driven."""
    from agent_alfred.gateway import cli as cli_module

    runtime = _CloseProgressRuntime(
        [False, False, True], start_error=RuntimeError("bind refused")
    )
    out = io.StringIO()

    assert (
        cli_module.serve_dashboard(
            state_dir=tmp_path,
            settings=Settings(),
            out=out,
            stop=threading.Event(),
            build=lambda **kwargs: runtime,
        )
        == 1
    )
    assert "dashboard unavailable: bind refused" in out.getvalue()
    assert runtime.close_retried.is_set()
    assert runtime.close_calls == 3
    assert runtime.owns_runtime is False


def test_permanently_incomplete_close_is_an_explicit_cli_failure(tmp_path) -> None:
    """A bounded retry budget never turns retained ownership into success."""
    from agent_alfred.gateway import cli as cli_module

    runtime = _CloseProgressRuntime([False, False, False])
    stop = threading.Event()
    stop.set()
    out = io.StringIO()

    assert (
        cli_module.serve_dashboard(
            state_dir=tmp_path,
            settings=Settings(),
            out=out,
            stop=stop,
            build=lambda **kwargs: runtime,
        )
        == 1
    )
    assert runtime.close_calls == 3
    assert runtime.owns_runtime is True
    assert "dashboard shutdown incomplete" in out.getvalue()


def test_the_serve_flag_runs_the_serve_path(tmp_path, monkeypatch) -> None:
    """And nothing else: no REPL, and no address to choose."""
    from agent_alfred.gateway import cli as cli_module

    seen: dict = {}

    def fake_serve(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(cli_module, "serve_dashboard", fake_serve)
    monkeypatch.setattr(
        "builtins.input",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("entered REPL")),
    )
    assert cli_module.main(["--serve", "--port", "1234"]) == 0
    assert seen["port"] == 1234
    assert not [key for key in seen if "host" in key or "bind" in key]
