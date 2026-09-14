"""Same public CLI fixture runs against the pinned baseline and the candidate."""

import io
import json
import sys
import uuid
from itertools import count
from pathlib import Path
from unittest.mock import patch

from agent_alfred.evals.deterministic.test_runtime_skills import SKIP, runtime, skill
from agent_alfred.events import event_json_default
from agent_alfred.gateway.cli import _send
from agent_alfred.gateway.web.api import DashboardApi
from agent_alfred.graph.types import thaw
from agent_alfred.messages import blocks_to_jsonable

root = Path(sys.argv[1])
mode = sys.argv[2]
if mode in ("explicit", "automatic"):
    skill(root / "builtin", "A", "fixed procedure")
script = [SKIP, "same answer"]
message = "/skills A\nhello" if mode == "explicit" else "/skills off\nhello"
if mode == "automatic":
    script.insert(0, '{"skills":["A"]}')
    message = "hello"
elif mode in ("command", "recovery"):
    script = []
    message = "查看操作 abc" if mode == "command" else "恢复操作 abc"
elif mode == "probe":
    script = ["same probe answer"]
sequence = count(1)
with patch("uuid.uuid4", side_effect=lambda: uuid.UUID(int=next(sequence))):
    with runtime(root, script) as (host, model, sink):
        session = host.create_session()
        out = io.StringIO()
        if mode == "probe":
            from agent_alfred.messages import message_plain_text
            from agent_alfred.runtime.host import SubmitRequest

            accepted = host.submit(
                SubmitRequest(
                    "probe", purpose="inference_probe", endpoint_id="test", model_id="m"
                )
            )
            result = host.wait(accepted.run_id)
            code = 0 if result.outcome == "completed" else 1
            out.write(message_plain_text(result.reply))
        else:
            code = _send(
                host,
                message,
                session,
                out,
                renderer=lambda text, output: output.write(text),
            )
        api = DashboardApi(facade=host)
        messages = api.session_messages(session, {})
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
                    code=code,
                    reply=out.getvalue(),
                    requests=requests,
                    messages=messages,
                    events=events,
                ),
                ensure_ascii=False,
            )
        )
