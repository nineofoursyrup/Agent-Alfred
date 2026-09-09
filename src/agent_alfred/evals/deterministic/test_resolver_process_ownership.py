"""Real resolver children never outlive timeout or interrupted ownership scopes."""

import pytest

from agent_alfred import bounded_connect
from agent_alfred.attempt_io import AttemptIOBudget
from agent_alfred.clock import FakeClock


@pytest.mark.parametrize("failure", ["deadline", "interrupt"])
def test_live_resolver_child_is_reaped_on_expiry_or_interrupt(monkeypatch, failure):
    clock = FakeClock()
    children = []
    monkeypatch.setattr(bounded_connect, "_RESOLVE", "import signal; signal.pause()")

    class Child(bounded_connect._ResolverChild):
        def start(self, host, port):
            super().start(host, port)
            children.append(self.process)
            if failure == "deadline":
                clock.monotonic_value = 5

        def communicate(self, timeout):
            raise KeyboardInterrupt("injected interrupt after real process creation")

    expected = TimeoutError if failure == "deadline" else KeyboardInterrupt
    try:
        with pytest.raises(expected):
            bounded_connect.ProcessResolver(child_factory=Child).resolve(
                "fixture.invalid", 443, AttemptIOBudget(clock, 5)
            )
        assert len(children) == 1
        child = children[0]
        assert child.poll() is not None
        assert child.stdout.closed
    finally:
        # Keep this regression itself safe even if the production cleanup regresses.
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()
            if child.stdout is not None:
                child.stdout.close()
