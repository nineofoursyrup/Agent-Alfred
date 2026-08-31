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

    def close(self) -> None:
        self.close_calls += 1


class _InterruptingWaiter(threading.Event):
    def __init__(self) -> None:
        super().__init__()
        self.wait_calls: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.wait_calls.append(timeout)
        raise KeyboardInterrupt


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
