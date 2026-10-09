"""A reload's new connections must survive a brief pause before accept."""

import http.client
import threading
from concurrent.futures import ThreadPoolExecutor

from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
    free_loopback_port,
)
from agent_alfred.gateway.web.assets import page_asset
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.wiring import build_dashboard


def test_reload_connection_burst_waits_for_the_listener(tmp_path, monkeypatch):
    dashboard = build_dashboard(
        state_dir=tmp_path / "state", port=free_loopback_port(),
        factory=ScriptedModelFactory(ScriptedModel([])),
    )
    dashboard.start()
    server = dashboard.service.server
    entered, release = threading.Event(), threading.Event()
    accept = server._handle_request_noblock

    def paused_accept():
        entered.set()
        assert release.wait(5)
        return accept()

    monkeypatch.setattr(server, "_handle_request_noblock", paused_accept)
    clients, failures, responses = [], [], []

    def request():
        client = http.client.HTTPConnection("127.0.0.1", dashboard.port, timeout=1)
        try:
            client.request("GET", "/assets/app.js", headers={"Connection": "close"})
            return client, None
        except OSError as error:
            return client, error

    try:
        # Hold acceptance, not an HTTP handler or product response. Reload
        # can overlap the previous page's connections with a new browser pool.
        first, error = request()
        clients.append((first, error))
        assert entered.wait(1)
        with ThreadPoolExecutor(max_workers=12) as pool:
            clients.extend(pool.map(lambda _index: request(), range(12)))
        release.set()
        for client, error in clients:
            if error is not None:
                failures.append(repr(error))
                continue
            try:
                response = client.getresponse()
                responses.append((response.status, response.read()))
            except OSError as error:
                failures.append(repr(error))
    finally:
        release.set()
        for client, _error in clients:
            client.close()
        assert dashboard.close()

    assert failures == []
    asset = page_asset("/assets/app.js")
    assert asset is not None
    assert responses == [(200, asset[0])] * 13
