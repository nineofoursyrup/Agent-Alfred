"""Real MCP Dashboard with test-only disk controls; no lifecycle substitutes."""

import json
import os
import signal
import sys
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from agent_alfred.connections import CredentialOverlay
from agent_alfred.messages import TextBlock, ToolCallBlock, ToolResultBlock
from agent_alfred.model import ModelResponse, ScriptedModel, ScriptedModelFactory
from agent_alfred.wiring import build_dashboard


class Model(ScriptedModel):
    def respond(self, request, *, events=None, deadline=None):
        result = ScriptedModel(["离线回复"]).respond(
            request, events=events, deadline=deadline
        )
        if request.tools:
            last = request.messages[-1].blocks
            if any(isinstance(b, TextBlock) and b.text == "MCP 验收" for b in last):
                result = replace(
                    result,
                    response=ModelResponse(
                        (ToolCallBlock("echo", "mcp_test_echo", {}),),
                        "tool_use",
                        request.model,
                    ),
                )
            elif any(isinstance(b, ToolResultBlock) for b in last):
                result = replace(
                    result,
                    response=ModelResponse(
                        (TextBlock(last[0].content[0].text),), "end_turn", request.model
                    ),
                )
        return result


def main():
    stopped = threading.Event()
    with TemporaryDirectory(prefix="alfred-mcp-browser-") as directory:
        root = Path(directory)
        state = root / "state"
        state.mkdir(mode=0o700)
        env = root / ".env"
        env.write_text("MCP_TOKEN=browser-old\n")
        behavior = root / "behavior.json"
        behavior.write_text(
            json.dumps(
                {
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "<script>window.injected=true</script> "
                                    "browser-old browser-new"
                                ),
                            },
                            {"type": "image", "data": "AAAA", "mimeType": "image/png"},
                        ]
                    }
                }
            )
        )
        fixture = (
            Path(__file__).resolve().parents[2]
            / "src/agent_alfred/evals/deterministic/mcp_server_fixture.py"
        )
        definition = {
            "command": sys.executable,
            "args": [str(fixture), str(behavior)],
            "enabled": True,
            "env": {"TOKEN": "${MCP_TOKEN}"},
        }

        def config():
            (state / "mcp.json").write_text(
                json.dumps({"mcpServers": {"test": definition}})
            )

        config()
        if len(sys.argv) > 1 and sys.argv[1] == "invalid":
            (state / "mcp.json").write_text("{bad")
        init_fifo = root / "initialize.fifo"
        os.mkfifo(init_fifo)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                value = {
                    "requests": [
                        json.loads(s)
                        for s in ((state / "requests.jsonl").read_text().splitlines()
                              if (state / "requests.jsonl").exists() else [])
                    ]
                }
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(value).encode())

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if "token" in body:
                    env.write_text("MCP_TOKEN=" + body["token"] + "\n")
                if "hold_initialize" in body:
                    data = json.loads(behavior.read_text())
                    if body["hold_initialize"]:
                        data["init_release_fifo"] = str(init_fifo)
                    else:
                        data.pop("init_release_fifo", None)
                    behavior.write_text(json.dumps(data))
                if body.get("release_initialize"):
                    fd = os.open(init_fifo, os.O_WRONLY | os.O_NONBLOCK)
                    os.write(fd, b"x")
                    os.close(fd)
                if "enabled" in body:
                    definition["enabled"] = body["enabled"]
                    config()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.02)
        )
        thread.start()
        dashboard = build_dashboard(
            state_dir=state,
            port=17749,
            credentials=CredentialOverlay({}, str(env)),
            factory=ScriptedModelFactory(Model([])),
        )
        signal.signal(signal.SIGTERM, lambda *_: stopped.set())
        try:
            dashboard.start()
            print(
                json.dumps(
                    {
                        "entry": {"port": dashboard.service.descriptor.port},
                        "control": f"http://127.0.0.1:{server.server_port}",
                    }
                ),
                flush=True,
            )
            stopped.wait()
        finally:
            assert dashboard.close()
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    main()
