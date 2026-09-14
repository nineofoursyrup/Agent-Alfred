"""Identical real HTTP/recording fixture for CE-01 baseline comparison."""

import json
import sys
import uuid
from itertools import count
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
    free_loopback_port,
)
from agent_alfred.evals.deterministic.test_runtime_skills import SKIP
from agent_alfred.events import CapturingSink, event_json_default
from agent_alfred.graph.types import thaw
from agent_alfred.messages import blocks_to_jsonable
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.wiring import build_dashboard

root = Path(sys.argv[1])
sequence = count(1)
with patch("uuid.uuid4", side_effect=lambda: uuid.UUID(int=next(sequence))):
    model = ScriptedModel([SKIP, "same web answer"])
    sink = CapturingSink()
    port = free_loopback_port()
    dashboard = build_dashboard(
        state_dir=root,
        factory=ScriptedModelFactory(model),
        clock=FakeClock(),
        instance_id="baseline-web",
        extra_sinks=(sink,),
        port=port,
        skill_builtin=root / "empty-skills",
    )
    dashboard.start()

    def request(path, body=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["x-agent-alfred-csrf"] = token
        with urlopen(
            Request(
                f"http://127.0.0.1:{port}" + path,
                data=None if body is None else json.dumps(body).encode(),
                headers=headers,
            ),
            timeout=10,
        ) as response:
            return response.status, json.load(response)

    try:
        entry = request("/api/entry")[1]
        created = request("/api/sessions", {}, entry["csrf_token"])
        session = created[1]["session_id"]
        accepted = request(
            "/api/runs",
            {"message": "/skills off\nhello", "session_id": session},
            entry["csrf_token"],
        )
        assert accepted[0] == 202, accepted
        assert dashboard.host.close()
        messages = request("/api/sessions/messages?session_id=" + session)
        events = [
            json.loads(json.dumps(e, default=event_json_default)) for e in sink.events
        ]
        requests = [
            dict(
                model=dict(endpoint_id=r.model.endpoint_id, model_id=r.model.model_id),
                system=blocks_to_jsonable(r.system or ()),
                messages=[
                    dict(role=m.role, blocks=blocks_to_jsonable(m.blocks))
                    for m in r.messages
                ],
                tools=[
                    dict(
                        name=t.name,
                        description=t.description,
                        input_schema=thaw(t.input_schema),
                    )
                    for t in r.tools
                ],
                max_tokens=r.max_tokens,
                tool_choice=r.tool_choice,
                conversation_id=r.conversation_id,
            )
            for r in model.requests
        ]
        print(
            json.dumps(
                dict(
                    accepted=accepted,
                    messages=messages,
                    requests=requests,
                    events=events,
                ),
                ensure_ascii=False,
            )
        )
    finally:
        dashboard.close()
