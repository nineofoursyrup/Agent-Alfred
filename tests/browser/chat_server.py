"""Isolated real chat Dashboard with observable Run settlement and IO barriers."""

import argparse
import sys
import threading
from pathlib import Path

from server import BrowserModel

from agent_alfred.model import ScriptedModelFactory
from agent_alfred.wiring import build_dashboard


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--threshold")
    args = parser.parse_args()
    builtin = args.state / "test-builtin"
    for name in ("browser-confirm", "browser-cancel"):
        path = builtin / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: {name}\ndescription: Builtin\n---\nOriginal")
    dashboard = build_dashboard(
        skill_builtin=builtin,
        state_dir=args.state,
        port=args.port,
        factory=ScriptedModelFactory(BrowserModel([])),
    )
    release = threading.Event()
    release.set()
    entered = threading.Event()
    settled = set()
    completed = threading.Condition()
    dashboard.start()
    host = dashboard.host
    notify = host.notify_run_done
    scheduling = host.memory_service.consolidation.scheduling
    claim = scheduling.claim

    def notified(run_id):
        # Observe the real final notification, after recording and scheduling.
        # HTTP Runs have no Host.wait consumer; retain no production result slot.
        notify(run_id)
        with completed:
            settled.add(run_id)
            completed.notify_all()

    def held_claim(run_id):
        # Pause only at the real scheduling IO boundary, inside its real gate.
        entered.set()
        assert release.wait(10), "scheduling barrier was not released"
        return claim(run_id)

    host.notify_run_done = notified
    scheduling.claim = held_claim
    print("ready", flush=True)
    try:
        for line in sys.stdin:
            command = line.strip()
            if command == "stop":
                break
            if command.startswith("wait-settled "):
                run_id = command.removeprefix("wait-settled ")
                with completed:
                    assert completed.wait_for(lambda: run_id in settled, 5), (
                        "Run did not finish recording and scheduling"
                    )
            elif command == "hold-scheduling":
                entered.clear()
                release.clear()
            elif command == "wait-scheduling":
                assert entered.wait(5), "Run did not reach scheduling"
            elif command == "release-scheduling":
                release.set()
            else:
                raise ValueError(f"unknown fixture command: {command}")
            print("ok " + command, flush=True)
    finally:
        release.set()
        assert dashboard.close(), "Dashboard did not drain"


if __name__ == "__main__":
    main()
