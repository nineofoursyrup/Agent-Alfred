"""Declared auth_probe execution: HTTP/UI → Host → transport → observation."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agent_alfred import schema
from agent_alfred.clock import FakeClock, format_instant
from agent_alfred.endpoints import AuthProbe, ModelEndpoint
from agent_alfred.events import CapturingSink, FanOutSink
from agent_alfred.gateway.web.api import DashboardApi
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.config import StoreBackedSnapshotProvider
from agent_alfred.runtime.host import RuntimeHost
from agent_alfred.runtime.model_settings import ModelSettingsStore
from agent_alfred.runtime.transport import VersionedTransportPool
from agent_alfred.settings import Settings

SECRET = "sk-auth-probe-synthetic"
CHECKED_AT = "2026-08-28T12:00:00Z"


class ScriptedAuthTransport:
    def __init__(self, responses=None):
        self.calls = []
        self._responses = list(responses or [])

    def request(self, method, url, *, headers, timeout):
        secret_names = {"authorization", "x-api-key"}
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": {
                    key: ("<redacted>" if key.lower() in secret_names else value)
                    for key, value in headers.items()
                },
                "raw_headers": dict(headers),
                "timeout": timeout,
            }
        )
        if not self._responses:
            raise AssertionError(f"unexpected auth probe {method} {url}")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _endpoint(**kwargs) -> ModelEndpoint:
    values = {
        "endpoint_id": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "auth_probe": AuthProbe(
            "GET",
            "/status",
            (204,),
            2.0,
            {401: "authentication_failed"},
            "bearer",
        ),
    }
    values.update(kwargs)
    return ModelEndpoint(**values)


def _host(tmp_path: Path, *, endpoint=None, environ=None, transport=None):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    store = ModelSettingsStore(tmp_path / "model_settings.json")
    store.load()
    settings = Settings()
    pool = VersionedTransportPool(lambda snapshot: snapshot)
    env = {"OPENAI_API_KEY": SECRET} if environ is None else environ
    factory = ScriptedModelFactory(ScriptedModel(["unused"]))
    factory._endpoints = (endpoint or _endpoint(),)
    factory.transport_pool = pool
    host = RuntimeHost(
        conn=conn,
        factory=factory,
        settings=settings,
        clock=FakeClock(),
        fanout=FanOutSink(
            [CapturingSink(name="c", flush_at_run_end=True)],
            process_instance_id="proc-auth-probe",
        ),
        process_instance_id="proc-auth-probe",
        snapshot_provider=StoreBackedSnapshotProvider(store, settings, environ=env),
        model_settings=store,
        secrets=(SECRET,),
    )
    host._auth_probe_transport = transport or ScriptedAuthTransport(
        [{"status": 204}]
    )
    return host, store, pool, host._auth_probe_transport


def test_complete_declaration_posts_declared_get_and_records_auth_probe(
    tmp_path: Path,
) -> None:
    host, store, pool, transport = _host(tmp_path)
    assignments = store.snapshot().assignments
    catalog_before = pool.cached_catalog("openai")
    status, payload = DashboardApi(facade=host).probe_auth({"endpoint_id": "openai"})
    dumped = json.dumps(payload)
    assert SECRET not in dumped
    assert status == 200
    assert [call["method"] for call in transport.calls] == ["GET"]
    assert [call["url"] for call in transport.calls] == [
        "https://api.openai.com/v1/status"
    ]
    assert transport.calls[0]["timeout"] == 2.0
    assert transport.calls[0]["raw_headers"]["Authorization"] == f"Bearer {SECRET}"
    assert "Content-Type" not in transport.calls[0]["raw_headers"]
    assert "content-type" not in {
        name.lower() for name in transport.calls[0]["raw_headers"]
    }
    row = payload["endpoints"][0]
    assert row["observation"] == {
        "state": "connected",
        "checked_at": CHECKED_AT,
        "checked_via": "auth_probe",
        "reason": None,
    }
    assert row["observation"]["checked_at"] == format_instant(
        FakeClock().wall_utc()
    )
    assert store.snapshot().assignments == assignments
    assert pool.cached_catalog("openai") == catalog_before
    assert host._factory.snapshots == []


def test_x_api_key_declaration_sends_that_header_only(tmp_path: Path) -> None:
    probe = AuthProbe(
        "GET",
        "/status",
        (200,),
        2.0,
        {401: "authentication_failed"},
        "x-api-key",
        {"anthropic-version": "2023-06-01"},
    )
    host, _store, _pool, transport = _host(
        tmp_path, endpoint=_endpoint(auth_probe=probe)
    )
    status, payload = DashboardApi(facade=host).probe_auth({"endpoint_id": "openai"})
    assert status == 200
    headers = transport.calls[0]["raw_headers"]
    assert headers["x-api-key"] == SECRET
    assert headers["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in headers
    assert payload["endpoints"][0]["observation"]["checked_via"] == "auth_probe"


def test_static_authorization_cannot_override_or_duplicate_auth(
    tmp_path: Path,
) -> None:
    probe = AuthProbe(
        "GET",
        "/status",
        (204,),
        2.0,
        {401: "authentication_failed"},
        "bearer",
        {
            "Authorization": "Bearer injected",
            "authorization": "Bearer other",
            "X-Api-Key": "also-secret",
            "X-Request-Id": "probe-1",
        },
    )
    host, _store, _pool, transport = _host(
        tmp_path, endpoint=_endpoint(auth_probe=probe)
    )
    DashboardApi(facade=host).probe_auth({"endpoint_id": "openai"})
    headers = transport.calls[0]["raw_headers"]
    assert headers["Authorization"] == f"Bearer {SECRET}"
    assert headers["X-Request-Id"] == "probe-1"
    lowered = {name.lower(): value for name, value in headers.items()}
    assert list(lowered).count("authorization") == 1
    assert "x-api-key" not in lowered


def test_missing_declaration_unknown_endpoint_and_blank_key_send_zero_io(
    tmp_path: Path,
) -> None:
    transport = ScriptedAuthTransport([{"status": 204}])
    host, _store, _pool, _t = _host(
        tmp_path, endpoint=_endpoint(auth_probe=None), transport=transport
    )
    api = DashboardApi(facade=host)
    status, payload = api.probe_auth({"endpoint_id": "openai"})
    assert status == 400
    assert payload == {"code": "auth_probe_unavailable"}
    assert transport.calls == []
    status, payload = api.probe_auth({"endpoint_id": "missing"})
    assert status == 404
    assert payload == {"code": "unknown_endpoint"}
    assert transport.calls == []
    host, _store, pool, transport = _host(
        tmp_path, environ={"OPENAI_API_KEY": "  "}, transport=ScriptedAuthTransport()
    )
    status, payload = DashboardApi(facade=host).probe_auth({"endpoint_id": "openai"})
    assert status == 409
    assert payload == {"code": "endpoint_unconfigured"}
    assert transport.calls == []
    assert pool.cached_observation("openai") is None


def test_client_cannot_supply_url_headers_or_body(tmp_path: Path) -> None:
    host, _store, _pool, transport = _host(tmp_path)
    status, payload = DashboardApi(facade=host).probe_auth(
        {
            "endpoint_id": "openai",
            "method": "DELETE",
            "path": "https://evil.example/x",
            "headers": {"Authorization": "Bearer other"},
            "url": "https://evil.example/x",
        }
    )
    assert status == 400
    assert payload == {"code": "unexpected_fields"}
    assert transport.calls == []


def test_same_origin_absolute_url_sends_and_cross_origin_is_zero_io(
    tmp_path: Path,
) -> None:
    same = AuthProbe(
        "GET",
        "https://api.openai.com/v1/status",
        (204,),
        2.0,
        {401: "authentication_failed"},
        "bearer",
    )
    host, _store, _pool, transport = _host(
        tmp_path, endpoint=_endpoint(auth_probe=same)
    )
    status, payload = DashboardApi(facade=host).probe_auth({"endpoint_id": "openai"})
    assert status == 200
    assert transport.calls[0]["url"] == "https://api.openai.com/v1/status"
    assert payload["endpoints"][0]["observation"]["state"] == "connected"
    cross = AuthProbe(
        "GET",
        "https://evil.example/status",
        (204,),
        2.0,
        {401: "authentication_failed"},
        "bearer",
    )
    host, _store, pool, transport = _host(
        tmp_path, endpoint=_endpoint(auth_probe=cross)
    )
    status, payload = DashboardApi(facade=host).probe_auth({"endpoint_id": "openai"})
    assert status == 400
    assert payload == {"code": "auth_probe_target_rejected"}
    assert transport.calls == []
    assert pool.cached_observation("openai") is None
    similar = AuthProbe(
        "GET",
        "https://api.openai.com.evil.test/status",
        (204,),
        2.0,
        {401: "authentication_failed"},
        "bearer",
    )
    host, _store, pool, transport = _host(
        tmp_path, endpoint=_endpoint(auth_probe=similar)
    )
    status, payload = DashboardApi(facade=host).probe_auth({"endpoint_id": "openai"})
    assert status == 400
    assert transport.calls == []
    scheme_relative = AuthProbe(
        "GET",
        "//evil.example/status",
        (204,),
        2.0,
        {401: "authentication_failed"},
        "bearer",
    )
    host, _store, pool, transport = _host(
        tmp_path, endpoint=_endpoint(auth_probe=scheme_relative)
    )
    status, payload = DashboardApi(facade=host).probe_auth({"endpoint_id": "openai"})
    assert status == 400
    assert payload == {"code": "auth_probe_target_rejected"}
    assert transport.calls == []


def test_redirect_is_not_followed_and_is_not_success(tmp_path: Path) -> None:
    transport = ScriptedAuthTransport([{"status": 302}, {"status": 204}])
    host, _store, _pool, _t = _host(tmp_path, transport=transport)
    status, payload = DashboardApi(facade=host).probe_auth({"endpoint_id": "openai"})
    assert status == 200
    assert len(transport.calls) == 1
    observed = payload["endpoints"][0]["observation"]
    assert observed["state"] == "error"
    assert observed["reason"] == "http_302"
    assert observed["checked_via"] == "auth_probe"


def test_head_uses_declared_method(tmp_path: Path) -> None:
    probe = AuthProbe(
        "HEAD", "/status", (204,), 2.0, {401: "authentication_failed"}, "bearer"
    )
    host, _store, _pool, transport = _host(
        tmp_path, endpoint=_endpoint(auth_probe=probe)
    )
    status, _payload = DashboardApi(facade=host).probe_auth({"endpoint_id": "openai"})
    assert status == 200
    assert transport.calls[0]["method"] == "HEAD"


def test_post_uses_declared_method_with_empty_body_and_no_content_type(
    tmp_path: Path,
) -> None:
    probe = AuthProbe(
        "POST", "/status", (204,), 2.0, {401: "authentication_failed"}, "bearer"
    )
    host, _store, _pool, transport = _host(
        tmp_path, endpoint=_endpoint(auth_probe=probe)
    )
    status, payload = DashboardApi(facade=host).probe_auth({"endpoint_id": "openai"})
    assert status == 200
    assert transport.calls[0]["method"] == "POST"
    assert "Content-Type" not in transport.calls[0]["raw_headers"]
    assert payload["endpoints"][0]["observation"]["state"] == "connected"


def test_mapped_error_timeout_and_interrupt_reasons(tmp_path: Path) -> None:
    import pytest

    transport = ScriptedAuthTransport([{"status": 401}])
    host, _store, _pool, _t = _host(tmp_path, transport=transport)
    status, payload = DashboardApi(facade=host).probe_auth({"endpoint_id": "openai"})
    assert payload["endpoints"][0]["observation"]["reason"] == "authentication_failed"
    transport = ScriptedAuthTransport([TimeoutError()])
    host, _store, pool, _t = _host(tmp_path, transport=transport)
    status, payload = DashboardApi(facade=host).probe_auth({"endpoint_id": "openai"})
    assert payload["endpoints"][0]["observation"]["reason"] == "TimeoutError"
    assert payload["endpoints"][0]["observation"]["state"] == "error"
    transport = ScriptedAuthTransport([KeyboardInterrupt()])
    host, _store, pool, _t = _host(tmp_path, transport=transport)
    with pytest.raises(KeyboardInterrupt):
        DashboardApi(facade=host).probe_auth({"endpoint_id": "openai"})
    assert pool.cached_observation("openai") is None
    assert host.try_begin_mutation() is None
    host.end_mutation()


def test_in_flight_mutation_blocks_probe_with_zero_io(tmp_path: Path) -> None:
    host, _store, _pool, transport = _host(tmp_path)
    assert host.try_begin_mutation() is None
    try:
        status, payload = DashboardApi(facade=host).probe_auth(
            {"endpoint_id": "openai"}
        )
        assert status == 409
        assert payload == {"code": "mutation_in_flight"}
        assert transport.calls == []
    finally:
        host.end_mutation()


def test_probe_does_not_consume_catalog_or_create_runs(tmp_path: Path) -> None:
    from agent_alfred.catalog import CatalogState

    host, store, pool, transport = _host(tmp_path)
    catalog = CatalogState(health="fresh", last_success_at=CHECKED_AT)
    pool.store_catalog("openai", catalog)
    before = store.snapshot().assignments
    DashboardApi(facade=host).probe_auth({"endpoint_id": "openai"})
    DashboardApi(facade=host).probe_auth({"endpoint_id": "openai"})
    assert len(transport.calls) == 2
    assert pool.cached_catalog("openai") is catalog
    assert store.snapshot().assignments == before
    assert host._factory.snapshots == []


def test_handler_routes_probe_post_to_api() -> None:
    from agent_alfred.gateway.web import handler as handler_module

    write = Path(handler_module.__file__).read_text(encoding="utf-8")
    route = write.split("def _route_write", 1)[1]
    assert "AUTH_PROBE_PATH" in route
    assert handler_module.AUTH_PROBE_PATH == "/api/connections/probe"


def test_urllib_transport_does_not_follow_redirects_or_set_content_type() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from agent_alfred.auth_probe import UrllibAuthProbeTransport

    hits: list[tuple[str, str, dict[str, str]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(("GET", self.path, dict(self.headers)))
            self.send_response(302)
            self.send_header("Location", "/to")
            self.end_headers()

        def do_POST(self):
            hits.append(("POST", self.path, dict(self.headers)))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        transport = UrllibAuthProbeTransport()
        url = f"http://127.0.0.1:{server.server_address[1]}"
        redirected = transport.request(
            "GET", f"{url}/from", headers={"X-Probe": "1"}, timeout=2.0
        )
        posted = transport.request("POST", f"{url}/empty", headers={}, timeout=2.0)
    finally:
        server.shutdown()
        thread.join(2)
    assert redirected == {"status": 302}
    assert posted == {"status": 204}
    assert [item[0:2] for item in hits] == [("GET", "/from"), ("POST", "/empty")]
    post_headers = {name.lower(): value for name, value in hits[1][2].items()}
    assert "content-type" not in post_headers


def test_connections_script_posts_only_endpoint_id() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "ops" / "static" / "pages.js"
    ).read_text(encoding="utf-8")
    connections = source.split("export function connectionsPage", 1)[1]
    connections = connections.split("export function modelsPage", 1)[0]
    assert '"验证凭据"' in connections or "验证凭据" in connections
    assert "/api/connections/probe" in connections
    assert "endpoint_id" in connections
    assert "button.isConnected" in connections
    assert "button.disabled = true" in connections
