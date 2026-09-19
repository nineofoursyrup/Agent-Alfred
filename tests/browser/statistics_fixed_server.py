"""CE-16 real a-p producer shared with deterministic acceptance; real Dashboard."""

import argparse
import sqlite3
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from agent_alfred.evals.deterministic.test_routing_statistics_lifecycle import (
    prepare_fixed_sample,
)
from agent_alfred.evals.deterministic.test_runtime_skills import SKIP
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.host import SubmitRequest
from agent_alfred.wiring import build_dashboard

parser = argparse.ArgumentParser()
parser.add_argument("--state", type=Path)
parser.add_argument("--port", type=int)
parser.add_argument("--threshold")
args = parser.parse_args()
clock = prepare_fixed_sample(args.state)
state = args.state / "state"
with (
    sqlite3.connect(args.state / "runs.sqlite3") as source,
    sqlite3.connect(state / "db.sqlite3") as dest,
):
    source.backup(dest)
barrier = threading.Event()
dashboard = build_dashboard(
    state_dir=state,
    port=args.port,
    clock=clock,
    factory=ScriptedModelFactory(ScriptedModel([SKIP, "greeting"])),
    before_recording_commit=barrier,
)
try:
    dashboard.start()
    assert dashboard.host.submit(SubmitRequest("你好")).kind == "accepted"
    from datetime import timedelta

    clock.wall += timedelta(days=3650)
    print("ready", flush=True)
    for line in sys.stdin:
        if line.strip() == "stop":
            break
        print("ok " + line.strip(), flush=True)
finally:
    barrier.set()
    assert dashboard.close()
