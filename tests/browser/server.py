"""A real Dashboard in an ephemeral state directory, with an offline model."""

import argparse
import json
import signal
import sqlite3
import sys
import threading
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

# Child-process tests deliberately clear PYTHONPATH. Keep their application
# import tied to this checkout when dependencies come from a shared runtime.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_alfred import schema
from agent_alfred.events import AttemptCommitted, AttemptStarted
from agent_alfred.model import (
    AttemptRecord,
    ModelResult,
    ScriptedModel,
    ScriptedModelFactory,
    Usage,
)
from agent_alfred.wiring import build_dashboard


class BrowserModel(ScriptedModel):
    """Each real model call emits ordinary facts, never a browser-only API."""

    def respond(self, request, *, events=None, deadline=None):
        result = ScriptedModel(["离线模型回复"]).respond(request, deadline=deadline)
        attempt = result.attempts[0]
        usage = Usage(output_tokens=4, endpoint_reported_cost_usd=Decimal("0.125"))
        if events is not None:
            events.emit(AttemptStarted(
                attempt_id=attempt.attempt_id, model=request.model,
            ))
            events.emit(AttemptCommitted(
                attempt_id=attempt.attempt_id, blocks=result.response.blocks,
                usage=usage,
            ))
        return ModelResult(
            attempts=(AttemptRecord(attempt.attempt_id, False, "committed", usage),),
            response=result.response, final_error=None,
        )


def seed_v2(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        schema.configure_connection(conn)
        for migration in schema.MIGRATIONS:
            if migration.version > 2:
                break
            migration.apply(conn)
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?, ?)",
                (migration.version, "2020-01-01"),
            )
        for index in range(55):
            conn.execute(
                "INSERT INTO agent_log "
                "(session_id, role, content, source, created_at) "
                "VALUES (?, 'user', ?, 'cli', ?)",
                ("legacy /会话?", json.dumps([
                    {"type": "text", "text": f"升级前消息 {index + 1:02}"}
                ]), "非规范旧时间"),
            )
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    state_context = (
        nullcontext(args.state) if args.state
        else TemporaryDirectory(prefix="alfred-browser-")
    )
    with state_context as state:
        if not (Path(state) / "db.sqlite3").exists():
            seed_v2(Path(state) / "db.sqlite3")
        dashboard = build_dashboard(
            state_dir=Path(state),
            port=args.port,
            factory=ScriptedModelFactory(BrowserModel([])),
        )
        try:
            dashboard.start()
            print("ready", flush=True)
            stopped.wait()
        finally:
            if not dashboard.close():
                raise RuntimeError("Dashboard did not finish closing")


if __name__ == "__main__":
    main()
