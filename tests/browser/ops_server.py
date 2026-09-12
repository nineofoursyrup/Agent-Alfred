"""Injected external declarations over the real Dashboard. Never production tools."""

import argparse
import sys
from pathlib import Path

from server import BrowserModel

from agent_alfred.messages import TextBlock
from agent_alfred.model import ScriptedModelFactory
from agent_alfred.tools import Tool, ToolPolicy, ToolSuccess
from agent_alfred.tools.calendar import object_schema
from agent_alfred.wiring import build_dashboard


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--threshold")
    args = parser.parse_args()
    fixture = Tool(
        "external_fixture",
        "Browser fixture only",
        object_schema({}),
        lambda a, c: ToolSuccess((TextBlock("fixture"),)),
        "external",
        source_id="test-service",
        capability_id="test-capability",
    )
    dashboard = build_dashboard(
        state_dir=args.state,
        port=args.port,
        factory=ScriptedModelFactory(BrowserModel([])),
        extra_tools=(fixture,),
        tool_policies={"external_fixture": ToolPolicy(configured=True)},
    )
    dashboard.start()
    host = dashboard.host
    publish = host._tool_authorization.publish
    print("ready", flush=True)
    try:
        for line in sys.stdin:
            command = line.strip()
            if command == "stop":
                break
            if command == "busy":
                host.try_begin_mutation()
            elif command == "idle":
                host.end_mutation()
            elif command == "apply-fail":

                def fail(registry):
                    raise RuntimeError("test application failure")

                host._tool_authorization.publish = fail
            elif command == "apply-heal":
                host._tool_authorization.publish = publish
            elif command == "expire":
                with host._accounting.lock:
                    host._accounting.snapshots.clear()
            elif command == "bulk":
                from agent_alfred.runtime.host import SubmitRequest

                for _ in range(55):
                    accepted = host.submit(SubmitRequest(message="hello"))
                    assert accepted.kind == "accepted"
                    host.wait(accepted.run_id)
            elif command == "prune":
                from agent_alfred.schema import record_trace_prune

                with host._store.transaction() as conn:
                    for row in conn.execute("SELECT run_id FROM runs").fetchall():
                        record_trace_prune(
                            conn,
                            run_id=row[0],
                            prune_requested_at="2026-09-12T00:00:00Z",
                            absence_confirmed_at="2026-09-12T00:00:00Z",
                            prune_reason="manual",
                        )
                    conn.commit()
            print("ok " + command, flush=True)
    finally:
        if not dashboard.close():
            raise RuntimeError("Dashboard close did not drain")


if __name__ == "__main__":
    main()
