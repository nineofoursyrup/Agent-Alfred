"""Subprocess Host entry for real POSIX shutdown and crash acceptance."""

import json
import select
import signal
import sys
import threading
from pathlib import Path

from agent_alfred.connections import CredentialOverlay
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.wiring import build_default_host

stopped = threading.Event()
for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, lambda *_: stopped.set())
host = build_default_host(
    state_dir=Path(sys.argv[1]),
    factory=ScriptedModelFactory(ScriptedModel([])),
    credentials=CredentialOverlay({}, None),
)
try:
    print(json.dumps({"ready": True, "mcp": host.connections()["mcp"]}), flush=True)
    while not stopped.is_set():
        if select.select([sys.stdin], [], [], 0.05)[0]:
            command = sys.stdin.readline().strip()
            if command == "raise":
                raise RuntimeError("synthetic catchable failure")
            break
finally:
    closed = host.close()
    print(json.dumps({"closed": closed}), flush=True)
