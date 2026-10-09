"""Real settings, catalog scheduler, Host, SQLite and HTTP; external IO is offline."""

import json
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

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
from agent_alfred.runtime.transport import VersionedTransportPool
from agent_alfred.settings import DEFAULT_MODEL_ID, OPENCODE_API_KEY_ENV
from agent_alfred.wiring import build_dashboard


def main():
    stopped = threading.Event()
    requests = []
    model_calls = []

    class Catalog:
        def get(self, url, *, headers, timeout):
            requests.append(url)
            return {
                "status": 200,
                "body": {"data": [{"id": DEFAULT_MODEL_ID, "name": "目录模型名称"}]},
            }

    class Model(ScriptedModel):
        def respond(self, request, *, events=None, deadline=None):
            model_calls.append(
                {
                    "endpoint_id": request.model.endpoint_id,
                    "model_id": request.model.model_id,
                }
            )
            usage = Usage(uncached_input_tokens=1000, output_tokens=1000)
            blocks = (TextBlock("离线具体探针"),)
            if events is not None:
                events.emit(
                    AttemptStarted(attempt_id="settings-probe", model=request.model)
                )
                events.emit(
                    AttemptCommitted(
                        attempt_id="settings-probe", blocks=blocks, usage=usage
                    )
                )
            return ModelResult(
                (
                    AttemptRecord(
                        "settings-probe", False, "committed", usage, model=request.model
                    ),
                ),
                ModelResponse(blocks, "end_turn", request.model),
                None,
            )

    class Control(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {"catalog_requests": requests, "model_calls": model_calls}
                ).encode()
            )

        def log_message(self, *args):
            pass

    control = ThreadingHTTPServer(("127.0.0.1", 0), Control)
    worker = threading.Thread(target=lambda: control.serve_forever(poll_interval=0.01))
    worker.start()
    with TemporaryDirectory(prefix="alfred-settings-") as directory:
        factory = ScriptedModelFactory(Model([]))
        factory.transport_pool = VersionedTransportPool(lambda snapshot: snapshot)
        dashboard = build_dashboard(
            state_dir=Path(directory),
            port=int(os.environ.get("ALFRED_BROWSER_TEST_PORT", "17736")) + 8,
            factory=factory,
            credentials=CredentialOverlay(
                {
                    OPENCODE_API_KEY_ENV: "fixture-only",
                    "OPENAI_API_KEY": "fixture-openai",
                },
                None,
            ),
        )
        signal.signal(signal.SIGTERM, lambda *_: stopped.set())
        try:
            dashboard.start()
            dashboard.host._catalog_transport = Catalog()
            print(
                json.dumps(
                    {
                        "origin": f"http://127.0.0.1:{dashboard.port}",
                        "control": f"http://127.0.0.1:{control.server_port}",
                    }
                ),
                flush=True,
            )
            stopped.wait()
        finally:
            assert dashboard.close()
            control.shutdown()
            control.server_close()
            worker.join()


if __name__ == "__main__":
    main()
