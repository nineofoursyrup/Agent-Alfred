"""Local-only native-input evidence harness; all product JS/CSS bytes unchanged."""
import hashlib
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path('/Users/nineofour/.codex/worktrees/dashboard-integration/Agent-Alfred')
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests/browser'))
from agent_alfred.gateway.web import handler
from server import BrowserModel
import chat_server

original_asset = handler.page_asset
original_post = handler.DashboardHandler.do_POST
original_respond = BrowserModel.respond
original_send_response = handler.DashboardHandler.send_response
lock = threading.Lock()

def instrumented_asset(path):
    if path == '/native-evidence.js':
        return (HERE / 'instrument.js').read_bytes(), 'text/javascript; charset=utf-8'
    asset = original_asset(path)
    if asset and asset[1].startswith('text/html'):
        return asset[0].replace(b'</head>', b'<script defer src="/native-evidence.js"></script></head>'), asset[1]
    return asset

def observed_post(self):
    if self.path != '/__native_evidence__':
        if self.path == '/api/runs':
            with lock, (HERE / 'dispatches.jsonl').open('a') as out:
                out.write(json.dumps({'path':self.path,'at':time.time(),'method':'POST'})+'\n')
        return original_post(self)
    if self.headers.get('Origin') != 'http://127.0.0.1:18133':
        self.send_error(403)
        return
    count = int(self.headers.get('Content-Length', '0'))
    if not 0 < count < 16384:
        self.send_error(400)
        return
    data = json.loads(self.rfile.read(count))
    with lock, (HERE / 'events.jsonl').open('a') as out:
        out.write(json.dumps(data, ensure_ascii=False) + '\n')
    self.send_response(204)
    self.send_header('Content-Length', '0')
    self.end_headers()

def delayed_respond(self, *args, **kwargs):
    # Gives the human/operator time to leave the composer before the reply.
    time.sleep(12)
    return original_respond(self, *args, **kwargs)

def delayed_response(self, code, message=None):
    if getattr(self, 'path', None) == '/api/runs' and code == 202:
        (HERE / 'accepted-before-response.json').write_text(json.dumps({'status':code,'at':time.time()}))
        end = time.monotonic()+90
        while not (HERE / 'release-202').exists():
            if time.monotonic()>end: raise TimeoutError('test-only 202 barrier was not released')
            time.sleep(0.05)
    return original_send_response(self, code, message)

handler.DashboardHandler.send_response = delayed_response
handler.page_asset = instrumented_asset
handler.DashboardHandler.do_POST = observed_post
BrowserModel.respond = delayed_respond
static = ROOT / 'src/agent_alfred/ops/static'
identity = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in static.iterdir() if p.is_file()}
(HERE / 'served-product-manifest.json').write_text(json.dumps(identity, indent=2) + '\n')
chat_server.main()
