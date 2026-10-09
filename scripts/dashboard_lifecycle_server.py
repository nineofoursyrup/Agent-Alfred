"""Offline installed-package server for sequential same-state lifecycle checks.

Copy this file outside the checkout and invoke it with each installed Python.
Only stdin 'stop' requests normal close; no package or state restoration occurs.
"""

import argparse
import hashlib
import json
import sys
import uuid
from decimal import Decimal
from pathlib import Path

from agent_alfred.connections import CredentialOverlay
from agent_alfred.events import AttemptCommitted, AttemptStarted
from agent_alfred.messages import TextBlock
from agent_alfred.model import (
    AttemptRecord,
    ModelResponse,
    ModelResult,
    ScriptedModel,
    ScriptedModelFactory,
    Usage,
)
from agent_alfred.runtime.memory import GATE_SYSTEM
from agent_alfred.wiring import build_dashboard


class LifecycleModel(ScriptedModel):
    def respond(self, request, *, events=None, deadline=None):
        self.requests.append(request)
        system = request.system[0].text if request.system else ""
        text = (
            '{"retrieve":false,"query":null,"reason_code":"greeting"}'
            if system == GATE_SYSTEM
            else "离线生命周期回复"
        )
        attempt = str(uuid.uuid4())
        blocks = (TextBlock(text),)
        usage = Usage(output_tokens=4, endpoint_reported_cost_usd=Decimal("0.125"))
        if events:
            events.emit(AttemptStarted(attempt_id=attempt, model=request.model))
            events.emit(
                AttemptCommitted(attempt_id=attempt, blocks=blocks, usage=usage)
            )
        return ModelResult(
            (AttemptRecord(attempt, False, "committed", usage, model=request.model),),
            ModelResponse(blocks, "end_turn", request.model),
            None,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    package = Path(__import__("agent_alfred").__file__).resolve().parent
    assert package.is_relative_to(Path(sys.prefix).resolve()), str(package)
    model = LifecycleModel([])
    dashboard = build_dashboard(
        state_dir=args.state,
        port=args.port,
        factory=ScriptedModelFactory(model),
        credentials=CredentialOverlay({}, None),
    )
    dashboard.start()
    print(
        json.dumps(
            {
                "event": "ready",
                "instance": dashboard.host.process_instance_id,
                "package": str(package),
                "python": sys.executable,
                "html_sha256": hashlib.sha256(
                    (package / "ops/static/index.html").read_bytes()
                ).hexdigest(),
            }
        ),
        flush=True,
    )
    try:
        for line in sys.stdin:
            if line.strip() == "stop":
                break
    finally:
        closed = dashboard.close()
        print(
            json.dumps(
                {
                    "event": "closed",
                    "closed": closed,
                    "model_calls": len(model.requests),
                }
            ),
            flush=True,
        )
        assert closed, "Normal close incomplete; preserve state ownership"


if __name__ == "__main__":
    main()
