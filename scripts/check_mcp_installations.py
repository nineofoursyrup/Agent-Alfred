"""Verify wheel/sdist in isolated base and MCP environments, outside the source tree."""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

SMOKE = r'''
import importlib.util, json, sys
from pathlib import Path
from agent_alfred.connections import CredentialOverlay
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.wiring import build_default_host
from agent_alfred.runtime.host import SubmitRequest

root = Path.cwd()
assert "site-packages" in str(Path(__import__("agent_alfred").__file__))
from agent_alfred.graph import (
    GraphBuilder, GraphRegistry, NodeOutcome, TerminalSpec, fn_node,
    GraphRunContext, llm_node, agent_node, tool_node, settle_graph,
)
graph = GraphBuilder('installed').declare_input('value')
graph.add_node(
    'reply', fn_node(lambda state, context: NodeOutcome({'reply': state['value']})),
    required_reads=('value',), writes=('reply',), terminal=TerminalSpec.result('reply'),
)
compiled = GraphRegistry({'installed': graph}).get('installed')
assert compiled.invoke({'value': 'graph works'}).output == 'graph works'
assert len(compiled.describe()['topology_hash']) == 64
import sqlite3, threading
from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.loop.assistant import Assistant
from agent_alfred.loop.budget import RunBudget
from agent_alfred.model import ModelRef
from agent_alfred.settings import Settings
from agent_alfred.runtime.recording import RecordingStore
from agent_alfred.tools import ToolRegistry
from agent_alfred.tools.calendar import CalendarTools
from agent_alfred.tools.metering import ToolMetering
conn = sqlite3.connect(':memory:')
schema.migrate(conn)
clock = FakeClock()
store = RecordingStore(conn, threading.Lock())
tools = ToolRegistry(CalendarTools(store, clock).declarations(), clock=clock,
                     metering=ToolMetering(store, clock))
model = ScriptedModel(['classify', 'answer'])
args = dict(client=model, model=ModelRef('offline', 'installed'),
            assistant=Assistant(clock=clock, settings=Settings()))
graph = GraphBuilder('four', tools=tools)
graph.add_node('llm', llm_node('x', output_key='llm', **args), writes=('llm',))
graph.add_node('agent', agent_node('x', output_key='agent', **args),
               writes=('agent',))
graph.add_node('fn', fn_node(lambda s, c: NodeOutcome()))
graph.add_node('tool', tool_node('query_events', {}, output_key='tool'),
               writes=('tool',), terminal=TerminalSpec.result('tool'))
graph.add_edge('llm', 'agent').add_edge('agent', 'fn').add_edge('fn', 'tool')
context = GraphRunContext(run_id='installed', budget=RunBudget(2),
                          clock=clock, tools=tools)
assert graph.compile().invoke({}, context=context).output
assert context.budget.used == 2 and len(tools.read_metering('installed')) == 1
conn.close()
extra = sys.argv[1] == "mcp"
assert (importlib.util.find_spec("jsonschema") is not None) == extra
server = root / "server.py"
server.write_text("""import json,sys
from pathlib import Path
Path('spawned').write_text('yes')
for line in sys.stdin:
 r=json.loads(line)
 if 'id' not in r:continue
 v={'protocolVersion':'2025-11-25','capabilities':{},'serverInfo':{'name':'artifact','version':'1'}}
 print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':v}),flush=True)
""")
for configured in (False, True):
    state = root / ("enabled" if configured else "unconfigured")
    state.mkdir()
    if configured:
        (state / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "artifact": {
                            "command": sys.executable,
                            "args": [str(server)],
                            "enabled": True,
                        }
                    }
                }
            )
        )
    skill_path = state / "skills" / "installed-skill" / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(
        "---\nname: installed-skill\ndescription: installed procedure\n"
        "---\nWhole installed body\n"
    )
    model = ScriptedModel(
        ['{"skills":["installed-skill"]}',
         '{"retrieve":false,"query":null,"reason_code":"greeting"}', "core works",
         '{"retrieve":false,"query":null,"reason_code":"greeting"}', "explicit works"]
    )
    host = build_default_host(
        state_dir=state,
        factory=ScriptedModelFactory(model),
        credentials=CredentialOverlay({}, None),
    )
    try:
        rows = host.connections()["mcp"]["servers"]
        if configured:
            assert rows[0]["state"] == ("connected" if extra else "error"), rows
            if not extra:
                assert rows[0]["reason"] == "dependency_missing"
            assert (state / "spawned").exists() == extra
        else:
            assert rows == []
        host.start()
        run = host.submit(SubmitRequest(message="hello"))
        result = host.wait(run.run_id, 10)
        assert result.outcome == "completed"
        loaded = result.memory_telemetry["skills"]["loaded"]
        assert loaded[0]["name"] == "installed-skill"
        assert "Whole installed body\n" in model.requests[-1].system[-1].text
        explicit = host.submit(SubmitRequest(message="/skills installed-skill\nhello"))
        assert host.wait(explicit.run_id, 10).outcome == "completed"
        assert len(model.requests) == 5
    finally:
        assert host.close()
print(
    json.dumps(
        {
            "mode": sys.argv[1],
            "package": __import__("agent_alfred").__file__,
            "core": "PASS",
            "configured": "PASS",
            "unconfigured": "PASS",
        }
    )
)
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    artifacts = sorted(args.dist.resolve().glob("agent_alfred-*"))
    assert any(p.suffix == ".whl" for p in artifacts)
    assert any(p.name.endswith(".tar.gz") for p in artifacts)
    results = []
    for artifact in artifacts:
        for mode in ("base", "mcp"):
            with tempfile.TemporaryDirectory(prefix="alfred-mcp-install-") as directory:
                root = Path(directory)
                env = {
                    k: v
                    for k, v in os.environ.items()
                    if k not in ("PYTHONPATH", "PYTHONHOME")
                }
                subprocess.run(
                    ["uv", "venv", "--python", "3.14", str(root / "env")],
                    check=True,
                    env=env,
                )
                python = root / "env/bin/python"
                target = str(artifact) + ("[mcp]" if mode == "mcp" else "")
                subprocess.run(
                    ["uv", "pip", "install", "--python", str(python), target],
                    check=True,
                    env=env,
                    cwd=root,
                )
                smoke = root / "smoke.py"
                smoke.write_text(SMOKE)
                result = subprocess.run(
                    [str(python), str(smoke), mode],
                    env=env,
                    cwd=root,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                results.append({"artifact": artifact.name, **json.loads(result.stdout)})
                print(json.dumps(results[-1]), flush=True)
    args.output.write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
