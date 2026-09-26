"""Verify wheel/sdist in isolated base and MCP environments, outside the source tree."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SMOKE = r'''
import importlib.util, json, sys, hashlib
from pathlib import Path
from agent_alfred.connections import CredentialOverlay
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.wiring import build_default_host
from agent_alfred.runtime.host import SubmitRequest

root = Path.cwd()
package = Path(__import__('agent_alfred').__file__).resolve().parent
assert package.is_relative_to(Path(sys.prefix).resolve())
expected = json.loads(Path(sys.argv[2]).read_text())
for relative, expected_hash in expected.items():
    actual_hash = hashlib.sha256((package / relative).read_bytes()).hexdigest()
    assert actual_hash == expected_hash
assert not any(Path(p).resolve().is_relative_to(Path(sys.argv[3]).resolve())
               for p in sys.path if p)

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
# Installed CLI -> Host -> real local draft tool and durable file.
from io import StringIO
from agent_alfred.gateway.cli import run_injected
from agent_alfred.messages import ToolCallBlock
from agent_alfred.model import ModelResult, ModelResponse, AttemptRecord, Usage
call = ModelResult(
    (AttemptRecord('installed-draft', False, 'committed', Usage()),),
    ModelResponse((ToolCallBlock('installed-draft', 'draft_message',
                                 {'body': 'installed draft'}),),
                  'tool_use', ModelRef('opencode-go', 'deepseek-v4-flash')), None)
cli_state = root / 'installed-cli'
cli_model = ScriptedModel([
    '{"retrieve":false,"query":null,"reason_code":"greeting"}', call, 'draft saved'])
cli_host = build_default_host(state_dir=cli_state,
    factory=ScriptedModelFactory(cli_model), credentials=CredentialOverlay({}, None))
out = StringIO()
assert run_injected(cli_host, 'make a local draft', out=out) == 0
assert 'draft saved' in out.getvalue()
drafts = list((cli_state / 'outbox').glob('*.md'))
assert len(drafts) == 1 and drafts[0].read_text() == 'installed draft\n'
# Installed-package Dashboard and its real subprocess worker, not source imports.
import socket, urllib.request
from agent_alfred.wiring import build_dashboard
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
model = ScriptedModel([])
dashboard = build_dashboard(
    state_dir=root / "diagnostic-dashboard", port=port,
    factory=ScriptedModelFactory(model),
)
dashboard.start()
try:
    origin = f"http://127.0.0.1:{port}"
    def http(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        headers = {} if body is None else {
            "Content-Type": "application/json",
            "Origin": origin,
            "x-agent-alfred-csrf": dashboard.csrf_token,
        }
        with urllib.request.urlopen(urllib.request.Request(
            origin + path, data=data, headers=headers,
        ), timeout=8) as response:
            return response.status, response.read()
    assert http("/database")[0] == 200
    assert b"databasePage" in http("/assets/database.js")[1]
    cat = json.loads(http("/api/database")[1])
    assert cat["available"] and len(cat["objects"]) == 21
    query_id = json.loads(http("/api/database/queries", {})[1])["query_id"]
    identity = {key: cat[key] for key in (
        "instance_id", "memory_revision", "protection_version"
    )}
    response = json.loads(http(f"/api/database/queries/{query_id}/execute", {
        **identity, "sql": "SELECT count(*) AS n FROM diag_sessions",
    })[1])
    assert response["objects"] == ["diag_sessions"]
    assert response["rows"][0][0] == {"type": "integer", "value": "0"}
    assert model.requests == []
    assert dashboard.host.database_console.released()
finally:
    assert dashboard.close()
print(
    json.dumps(
        {
            "mode": sys.argv[1],
            "package": __import__("agent_alfred").__file__,
            "core": "PASS", "cli": "PASS", "files": "PASS",
            "database_dashboard": "PASS",
            "import_path": str(package), "python_path": sys.executable,
            "cwd": str(root), "source_contamination": False,
            "prefix": sys.prefix,
            "sys_path": [str(Path(p).resolve()) for p in sys.path if p],
            "package_files": expected,
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
    source_root = Path(__file__).resolve().parents[1]
    package_root = source_root / "src" / "agent_alfred"
    expected = {
        str(p.relative_to(package_root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in package_root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    }
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
                    ["uv", "venv", "--python", sys.executable, str(root / "env")],
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
                expected_path = root / "expected.json"
                expected_path.write_text(json.dumps(expected))
                smoke = root / "smoke.py"
                smoke.write_text(SMOKE)
                result = subprocess.run(
                    [
                        str(python),
                        str(smoke),
                        mode,
                        str(expected_path),
                        str(source_root),
                    ],
                    env=env,
                    cwd=root,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                results.append(
                    {
                        "artifact": artifact.name,
                        "kind": "wheel" if artifact.suffix == ".whl" else "sdist",
                        "artifact_sha256": hashlib.sha256(
                            artifact.read_bytes()
                        ).hexdigest(),
                        **json.loads(result.stdout),
                    }
                )
                print(json.dumps(results[-1]), flush=True)
    args.output.write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
