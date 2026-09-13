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
    model = ScriptedModel(
        ['{"retrieve":false,"query":null,"reason_code":"greeting"}', "core works"]
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
        assert host.wait(run.run_id, 10).outcome == "completed"
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
