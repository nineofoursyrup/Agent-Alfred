"""Real Skill Dashboard; only the model boundary is deterministic."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_alfred.events import AttemptCommitted, AttemptStarted
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.skill_selector import SELECTOR_SYSTEM
from agent_alfred.wiring import build_dashboard


class RoutingModel:
    def __init__(self):
        self.failed = set()

    def respond(self, request, *, events=None, deadline=None):
        from agent_alfred.messages import message_plain_text

        task = message_plain_text(request.messages[-1])
        system = "\n".join(b.text for b in request.system or ())
        if system.startswith(SELECTOR_SYSTEM):
            text = '{"skills":["A","B","C"]}'
        elif "Decide whether long-term memory" in system:
            text = '{"retrieve":false,"query":null,"reason_code":"greeting"}'
        elif system.startswith("Classify only the current task"):
            from agent_alfred.messages import message_plain_text

            task = message_plain_text(request.messages[-1])
            if task == "STATS classifier failure":
                raise RuntimeError("controlled classifier failure")
            text = (
                "greeting"
                if task == "你好"
                else "unknown"
                if task == "STATS graph fallback"
                else "no_reply"
                if task == "不用回复"
                else "full"
            )
        else:
            if task == "STATS full failure" and task not in self.failed:
                self.failed.add(task)
                raise RuntimeError("controlled answer failure")
            text = "离线路由回复"
        result = ScriptedModel([text]).respond(request, deadline=deadline)
        attempt = result.attempts[0]
        if events is not None:
            events.emit(
                AttemptStarted(attempt_id=attempt.attempt_id, model=request.model)
            )
            events.emit(
                AttemptCommitted(
                    attempt_id=attempt.attempt_id,
                    blocks=result.response.blocks,
                    usage=attempt.usage,
                )
            )
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--threshold", type=int)
    parser.add_argument("--working-memory-rounds", type=int)
    args = parser.parse_args()
    from agent_alfred.runtime.routing import build_routing_graph, project_context

    def build(tools):
        def projection(snapshot):
            if (args.state / "corrupt-context").exists():
                return project_context({**snapshot, "context_version": 999})
            return project_context(snapshot)

        return build_routing_graph(tools, projection=projection)

    import json
    import sqlite3
    from datetime import datetime

    from agent_alfred.clock import FakeClock
    from agent_alfred.settings import Settings

    clock = FakeClock() if (args.state / "fixed-clock").exists() else None
    lock = None
    saved = None
    dashboard = build_dashboard(
        clock=clock,
        settings=Settings(working_memory_rounds=args.working_memory_rounds)
        if args.working_memory_rounds is not None
        else None,
        routing_graph_builder=build,
        state_dir=args.state,
        port=args.port,
        skill_builtin=args.state / "builtin",
        factory=ScriptedModelFactory(RoutingModel()),
    )
    try:
        dashboard.start()
        print("ready", flush=True)
        for line in sys.stdin:
            command = line.strip()
            if command == "stop":
                break
            if command.startswith("clock "):
                clock.wall = datetime.fromisoformat(command[6:])
            if command == "lock":
                lock = sqlite3.connect(args.state / "db.sqlite3")
                lock.execute("PRAGMA journal_mode=DELETE")
                lock.execute("BEGIN EXCLUSIVE")
            if command == "offline-data":
                (args.state / "db.sqlite3").rename(args.state / "offline.sqlite3")
            if command == "online-data":
                (args.state / "offline.sqlite3").rename(args.state / "db.sqlite3")
            if command == "unlock":
                lock.rollback()
                lock.close()
                lock = None
            if command in (
                "legacy",
                "future-version",
                "unknown-results",
                "bad-time",
                "restore-data",
            ):
                with sqlite3.connect(args.state / "db.sqlite3") as conn:
                    if saved is None:
                        saved = conn.execute(
                            "SELECT run_id, routing_admission, telemetry, "
                            "accepted_at FROM runs"
                        ).fetchall()
                    for run_id, admission, telemetry, accepted in saved:
                        body = json.loads(telemetry)
                        if command == "legacy":
                            admission = None
                        if command == "future-version":
                            version = json.loads(admission)
                            version["policy_version"] = "future"
                            admission = json.dumps(version)
                        if command == "unknown-results":
                            body["memory"].pop("routing_statistics", None)
                        if command == "bad-time":
                            accepted = "invalid"
                        conn.execute(
                            "UPDATE runs SET routing_admission=?, telemetry=?, "
                            "accepted_at=? WHERE run_id=?",
                            (admission, json.dumps(body), accepted, run_id),
                        )
            if line.strip() == "corrupt-context":
                (args.state / "corrupt-context").touch()
            if line.strip() == "trim-traces":
                for trace in (args.state / "traces").rglob("trace.jsonl"):
                    trace.write_bytes(b"")
            if line.strip() == "repair-recording":
                import sqlite3

                with sqlite3.connect(args.state / "db.sqlite3") as conn:
                    conn.execute("DROP TRIGGER fail_recording")
            if line.strip() == "fail-recording":
                import sqlite3

                with sqlite3.connect(args.state / "db.sqlite3") as conn:
                    conn.execute(
                        "CREATE TRIGGER fail_recording BEFORE UPDATE OF phase ON runs "
                        "WHEN NEW.phase = 'finished' "
                        "BEGIN SELECT RAISE(ABORT, 'recording IO fault'); END"
                    )
            print("ok " + line.strip(), flush=True)
    finally:
        if lock is not None:
            lock.close()
        assert dashboard.close()


if __name__ == "__main__":
    main()
