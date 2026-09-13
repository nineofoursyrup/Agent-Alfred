"""Real Dashboard/Host + loopback Tavily, with explicit IO barriers."""

import json
import signal
import sys
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from agent_alfred.connections import CredentialOverlay
from agent_alfred.evals.deterministic.test_auth_probe import _endpoint
from agent_alfred.integrations_http import TavilyHTTP
from agent_alfred.messages import TextBlock, ToolCallBlock, ToolResultBlock
from agent_alfred.model import ModelResponse, ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.transport import VersionedTransportPool
from agent_alfred.wiring import build_dashboard


class Model(ScriptedModel):
    def respond(self, request, *, events=None, deadline=None):
        result = ScriptedModel(["离线回复"]).respond(
            request, events=events, deadline=deadline
        )
        if request.tools:
            last = request.messages[-1].blocks
            if any(isinstance(b, TextBlock) and b.text == "搜索验收" for b in last):
                result = replace(
                    result,
                    response=ModelResponse(
                        (ToolCallBlock("search", "web_search", {"query": "q"}),),
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
    release = threading.Event()
    release.set()
    entered = threading.Event()
    requests = []
    current = {"status": 200}
    with TemporaryDirectory(prefix="alfred-integrations-") as directory:
        path = Path(directory) / ".env"
        path.write_text(
            "TAVILY_API_KEY=browser-key-original\nOPENAI_API_KEY=browser-auth-synthetic\n"
        )

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/control":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(
                        json.dumps(
                            {"requests": requests, "entered": entered.is_set()}
                        ).encode()
                    )
                    return
                self.tavily()

            def do_POST(self):
                data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/control":
                    value = json.loads(data)
                    if "fault_publication" in value:
                        current["fault_publication"] = value["fault_publication"]
                    if "key" in value:
                        path.write_text("TAVILY_API_KEY=" + value["key"] + "\n")
                    if "echo_keys" in value:
                        current["echo_keys"] = value["echo_keys"]
                    if "large_number" in value:
                        current["large_number"] = value["large_number"]
                    if "status" in value:
                        current["status"] = value["status"]
                    if value.get("block"):
                        entered.clear()
                        release.clear()
                    if value.get("release"):
                        release.set()
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"{}")
                    return
                self.tavily()

            def tavily(self):
                requests.append(self.path)
                entered.set()
                assert release.wait(10)
                self.send_response(current["status"])
                self.end_headers()
                body = (
                    {"key": {"plan_usage": 2}}
                    if self.path == "/usage"
                    else {
                        "results": [
                            {
                                "title": "<img src=x onerror=alert(1)>",
                                "url": "https://source.invalid/",
                                "content": "<script>window.injected=true</script>",
                            }
                        ],
                        "usage": {"credits": 0.125},
                    }
                )
                if self.path == "/search" and current.get("echo_keys"):
                    body["results"][0]["content"] += (
                        " browser-key-original browser-key-replacement"
                    )
                encoded = json.dumps(body)
                if current.get("large_number"):
                    encoded = encoded.replace(
                        '"plan_usage": 2', '"plan_usage": 1e1000000'
                    )
                    encoded = encoded.replace(
                        '"credits": 0.125', '"credits": 1e1000000'
                    )
                self.wfile.write(encoded.encode())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.01)
        )
        thread.start()
        factory = ScriptedModelFactory(Model([]))
        factory._endpoints = (_endpoint(),)
        factory.transport_pool = VersionedTransportPool(lambda snapshot: snapshot)

        class AuthTransport:
            def request(self, *args, **kwargs):
                requests.append("/auth-probe")
                entered.set()
                assert release.wait(10)
                return {"status": 204}

        dashboard = build_dashboard(
            state_dir=Path(directory) / "state",
            port=17744,
            credentials=CredentialOverlay({}, str(path)),
            factory=factory,
        )
        signal.signal(signal.SIGTERM, lambda *_: stopped.set())
        try:
            dashboard.start()
            dashboard.host._auth_probe_transport = AuthTransport()
            original_publish = dashboard.host._publish_tools

            def publish_with_io_fault(registry):
                original_publish(registry)
                if current.get("fault_publication"):
                    raise OSError("synthetic publication and rollback IO fault")

            dashboard.host._publish_tools = publish_with_io_fault
            dashboard.host._integrations.transport = TavilyHTTP(
                base_url=f"http://127.0.0.1:{server.server_port}",
                proxy_handler=ProxyHandler({}),
            )
            descriptor = {"port": dashboard.service.descriptor.port}
            print(
                json.dumps(
                    {
                        "entry": descriptor,
                        "control": f"http://127.0.0.1:{server.server_port}/control",
                    }
                ),
                flush=True,
            )
            stopped.wait()
        finally:
            release.set()
            dashboard.close()
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    main()
