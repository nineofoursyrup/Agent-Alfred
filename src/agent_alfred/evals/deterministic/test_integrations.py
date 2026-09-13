"""#20 confirmed public Host acceptance; only external IO/model are replaced."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_alfred.connections import CredentialOverlay
from agent_alfred.evals.deterministic.test_runtime_tools import calls
from agent_alfred.messages import ToolCallBlock
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.host import SubmitRequest
from agent_alfred.wiring import build_default_host

GATE = '{"retrieve":false,"query":null,"reason_code":"greeting"}'


def test_ce01_unconfigured_guidance_reaches_next_step(tmp_path):
    model = ScriptedModel(
        [GATE, calls(ToolCallBlock("search", "web_search", {})), "Done"]
    )
    host = build_default_host(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(model),
        credentials=CredentialOverlay({}, None),
    )
    host.start()
    try:
        row = next(
            r for r in host.tools_catalog()["tools"] if r["name"] == "web_search"
        )
        assert row["exposure"] == "guidance"
        run = host.submit(SubmitRequest(message="搜索测试"))
        assert host.wait(run.run_id, timeout=5).outcome == "completed"
        assert "configuration_required" in str(model.requests[-1].messages)
        assert host.tool_requests(run.run_id)[0]["start_confirmation"] == "not_started"
    finally:
        host.close()


@contextmanager
def tavily_http(
    body=b'{"results":[],"usage":{"credits":0.125}}',
    status=200,
    headers=None,
    on_request=None,
):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(
                (
                    self.path,
                    dict(self.headers),
                    self.rfile.read(int(self.headers.get("Content-Length", 0))),
                )
            )
            if on_request is not None:
                on_request(self)
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@contextmanager
def search_host(tmp_path, url, script=None, clock=None, key="synthetic-tavily-key"):
    from urllib.request import ProxyHandler

    from agent_alfred.integrations_http import TavilyHTTP

    tmp_path.mkdir(parents=True, exist_ok=True)
    model = ScriptedModel(
        script
        or [
            GATE,
            calls(ToolCallBlock("search", "web_search", {"query": "原文 query"})),
            "Done",
        ]
    )
    host = build_default_host(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(model),
        clock=clock,
        credentials=CredentialOverlay({"TAVILY_API_KEY": key}, None),
    )
    host._integrations.transport = TavilyHTTP(
        base_url=url, proxy_handler=ProxyHandler({})
    )
    row = next(r for r in host.tools_catalog()["tools"] if r["name"] == "web_search")
    assert "error" not in host.save_tool_authorization(row["identity"], "allowed", 0)
    host.start()
    try:
        yield host, model
    finally:
        host.close()


def test_ce05_fractional_credits_and_empty_success(tmp_path):
    with tavily_http() as (url, requests), search_host(tmp_path, url) as (host, model):
        run = host.submit(SubmitRequest(message="搜索"))
        assert host.wait(run.run_id, timeout=5).outcome == "completed"
        assert len(requests) == 1
        sent = json.loads(requests[0][2])
        assert sent == {
            "query": "原文 query",
            "max_results": 5,
            "search_depth": "basic",
            "topic": "general",
            "include_usage": True,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "auto_parameters": False,
        }
        row = host.tool_requests(run.run_id)[0]
        assert row["result"] == "succeeded"
        assert row["cost"] == {
            "kind": "reported",
            "units": "0.125",
            "service": "tavily",
            "unit": "credits",
            "source": "reported",
        }
        assert "外部来源数据" in str(model.requests[-1].messages)
        assert (
            host.connections()["integrations"][0]["observation"]["state"] == "connected"
        )


def test_ce06_bad_200_stops_batch_and_model(tmp_path):
    script = [
        GATE,
        calls(
            ToolCallBlock("search", "web_search", {"query": "q"}),
            ToolCallBlock("later", "web_search", {"query": "q"}),
        ),
        "Must not run",
    ]
    with (
        tavily_http(b'{"wrong":[]}') as (url, requests),
        search_host(tmp_path, url, script) as (host, model),
    ):
        run = host.submit(SubmitRequest(message="搜索"))
        result = host.wait(run.run_id, timeout=5)
        assert result.outcome == "failed"
        assert result.error == "tool_result_unverified"
        assert len(model.requests) == 2 and len(requests) == 1
        assert "搜索结果无法确认，可能已执行；本次后续调用已停止，未自动重试" in str(
            result.reply
        )
        rows = host.tool_requests(run.run_id)
        assert rows[0]["result"] == "unknown"
        assert rows[1]["start_confirmation"] == "not_started"
        assert rows[0]["model_delivery"] == "not_sent"


def test_ce03_ce11_reread_is_atomic_and_preserves_authorization(tmp_path):
    from agent_alfred.gateway.web.api import DashboardApi

    path = tmp_path / ".env"
    path.write_text("TAVILY_API_KEY=key-original\n")
    host = build_default_host(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(ScriptedModel([])),
        credentials=CredentialOverlay({}, str(path)),
    )
    api = DashboardApi(facade=host)
    try:
        identity = next(
            r["identity"]
            for r in host.tools_catalog()["tools"]
            if r["name"] == "web_search"
        )
        host.save_tool_authorization(identity, "allowed", 0)
        path.write_text("TAVILY_API_KEY='unterminated\n")
        status, _ = api.reread_env()
        assert status == 400
        assert host.connections()["integrations"][0]["key"]["last4"] == "inal"
        path.write_text("TAVILY_API_KEY=key-replacement\n")
        assert api.reread_env()[0] == 200
        row = next(
            r for r in host.tools_catalog()["tools"] if r["name"] == "web_search"
        )
        assert row["exposure"] == "real" and row["authorization"] == "allowed"
        assert host.connections()["integrations"][0]["key"]["last4"] == "ment"
        path.unlink()
        assert api.reread_env()[0] == 200
        assert (
            next(r for r in host.tools_catalog()["tools"] if r["name"] == "web_search")[
                "exposure"
            ]
            == "guidance"
        )
    finally:
        host.close()


def test_ce02_probe_is_explicit_cached_and_window_bounded(tmp_path):
    from agent_alfred.clock import FakeClock
    from agent_alfred.gateway.web.api import DashboardApi

    clock = FakeClock()
    with (
        tavily_http(b"{}", 500) as (url, requests),
        search_host(tmp_path, url, clock=clock) as (host, _),
    ):
        api = DashboardApi(facade=host)
        for i in range(11):
            assert api.connections()[0] == 200
            assert api.probe_auth({"endpoint_id": "tavily"})[0] == 200
            assert api.probe_auth({"endpoint_id": "tavily"})[0] == 200
            clock.monotonic_value += 30
        assert len(requests) == 10
        assert host.connections()["integrations"][0]["retry_at"] is not None
        clock.monotonic_value = 600
        api.probe_auth({"endpoint_id": "tavily"})
        assert len(requests) == 11


@pytest.mark.parametrize(
    "configured,authorization,exposure,reason",
    [
        (False, "unset", "guidance", "configuration_required"),
        (False, "allowed", "guidance", "configuration_required"),
        (False, "denied", "hidden", "not_authorized"),
        (True, "unset", "hidden", "not_authorized"),
        (True, "allowed", "real", None),
        (True, "denied", "hidden", "not_authorized"),
    ],
)
def test_ce01_six_cells(tmp_path, configured, authorization, exposure, reason):
    from urllib.request import ProxyHandler

    from agent_alfred.integrations_http import TavilyHTTP

    args = {"query": "q"} if configured else {}
    model = ScriptedModel([GATE, calls(ToolCallBlock("s", "web_search", args)), "Done"])
    with tavily_http() as (url, requests):
        host = build_default_host(
            state_dir=tmp_path / "s",
            credentials=CredentialOverlay(
                {"TAVILY_API_KEY": "synthetic-key" if configured else ""}, None
            ),
            factory=ScriptedModelFactory(model),
        )
        host._integrations.transport = TavilyHTTP(
            base_url=url, proxy_handler=ProxyHandler({})
        )
        host.save_tool_authorization('["tavily","web_search"]', authorization, 0)
        host.start()
        try:
            row = next(
                r for r in host.tools_catalog()["tools"] if r["name"] == "web_search"
            )
            assert row["exposure"] == exposure
            run = host.submit(SubmitRequest(message="搜索"))
            assert host.wait(run.run_id, timeout=5).outcome == "completed"
            assert len(requests) == (1 if reason is None else 0)
            assert host.tool_requests(run.run_id)[0]["reason"] == reason
            exposed = next(
                (s for s in model.requests[1].tools if s.name == "web_search"), None
            )
            assert (exposed is None) == (exposure == "hidden")
        finally:
            host.close()


@pytest.mark.parametrize(
    "status,body,state,reason,result,continued",
    [
        (200, b'{"results":[]}', "connected", None, "succeeded", True),
        (200, b"[]", "error", "protocol_error", "unknown", False),
        (200, b"bad-json", "error", "protocol_error", "unknown", False),
        (400, b"{}", "configured_untested", "request_invalid", "failed", True),
        (
            422,
            b'{"detail":[{"input":"secret"}]}',
            "configured_untested",
            "request_invalid",
            "failed",
            True,
        ),
        (401, b'{"detail":{}}', "error", "authentication_failed", "failed", True),
        (403, b"bad-json", "error", "forbidden", "failed", True),
        (429, b"[]", "error", "rate_limited", "failed", True),
        (432, b"{}", "error", "plan_limit", "failed", True),
        (433, b"{}", "error", "paygo_limit", "failed", True),
        (500, b"plain-text", "error", "http_status", "failed", True),
        (302, b"", "error", "redirect_refused", "failed", True),
    ],
)
def test_ce04_entire_search_outcome_table(
    tmp_path, status, body, state, reason, result, continued
):
    with (
        tavily_http(body, status) as (url, requests),
        search_host(tmp_path, url) as (host, model),
    ):
        run = host.submit(SubmitRequest(message="搜索"))
        host.wait(run.run_id, timeout=5)
        assert len(requests) == 1
        dto = host.connections()["integrations"][0]
        assert dto["observation"]["state"] == state
        assert dto["last_attempt"]["reason"] == reason
        assert host.tool_requests(run.run_id)[0]["result"] == result
        assert len(model.requests) == (3 if continued else 2)
        assert host.read_tool_history(
            tmp_path / "state" / "traces",
            run_id=run.run_id,
            step_index=host.tool_requests(run.run_id)[0]["step_index"],
            call_id="search",
        )["text"]


@pytest.mark.parametrize(
    "credits,kind,units",
    [
        ("0", "reported", "0"),
        (
            "0.12345678901234567890123456789",
            "reported",
            "0.12345678901234567890123456789",
        ),
        ("-1", "unknown", None),
        ("true", "unknown", None),
        ('"1"', "unknown", None),
        ("NaN", "unknown", None),
        ("Infinity", "unknown", None),
        ("null", "unknown", None),
    ],
)
def test_ce05_decimal_cost_survives_restart_without_trace(
    tmp_path, credits, kind, units
):
    body = ('{"results":[],"usage":{"credits":' + credits + "}}").encode()
    with tavily_http(body) as (url, _), search_host(tmp_path, url) as (host, model):
        run = host.submit(SubmitRequest(message="搜索"))
        assert host.wait(run.run_id, timeout=5).outcome == "completed"
        cost = host.tool_requests(run.run_id)[0]["cost"]
        assert cost["kind"] == kind
        assert cost.get("units") == units
        assert len(model.requests) == 3
    import shutil

    shutil.rmtree(tmp_path / "state" / "traces")
    reopened = build_default_host(
        state_dir=tmp_path / "state",
        credentials=CredentialOverlay({}, None),
        factory=ScriptedModelFactory(ScriptedModel([])),
    )
    try:
        assert reopened.tool_requests(run.run_id)[0]["cost"] == cost
        snapshot = reopened.accounting_snapshot({"timezone": "UTC", "range": "all"})
        assert snapshot
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "args",
    [
        {"query": ""},
        {"query": "  \t"},
        {"query": "x" * 2001},
        {"query": True},
        {"query": "q", "max_results": True},
        {"query": "q", "max_results": 0},
        {"query": "q", "max_results": 11},
        {"query": "q", "max_results": 1.0},
        {"query": "q", "max_results": None},
        {"query": "q", "extra": "no"},
    ],
)
def test_ce09_invalid_inputs_send_nothing(tmp_path, args):
    script = [GATE, calls(ToolCallBlock("s", "web_search", args)), "Done"]
    with (
        tavily_http() as (url, requests),
        search_host(tmp_path, url, script) as (host, model),
    ):
        run = host.submit(SubmitRequest(message="搜索"))
        host.wait(run.run_id, timeout=5)
        assert requests == []
        assert host.tool_requests(run.run_id)[0]["reason"] == "invalid_input"
        assert host.tool_requests(run.run_id)[0]["cost"]["kind"] == "not_billable"


@pytest.mark.parametrize(
    "source",
    [
        {},
        {"title": "t", "url": "https://a.test"},
        {"title": "", "url": "https://a.test", "content": ""},
        {"title": "t", "url": "javascript:alert(1)", "content": ""},
        {"title": "t", "url": "https://user:pass@a.test", "content": ""},
        {"title": "t", "url": "https://a.test\n/", "content": ""},
        {"title": "t", "url": "https:///", "content": ""},
    ],
)
def test_ce09_invalid_source_rejects_whole_result(tmp_path, source):
    body = json.dumps(
        {
            "results": [
                {"title": "good", "url": "https://a.test", "content": ""},
                source,
            ],
            "usage": {"credits": 1},
        }
    ).encode()
    with tavily_http(body) as (url, _), search_host(tmp_path, url) as (host, model):
        run = host.submit(SubmitRequest(message="搜索"))
        assert host.wait(run.run_id, timeout=5).outcome == "failed"
        assert host.tool_requests(run.run_id)[0]["cost"]["units"] == "1"
        assert len(model.requests) == 2


def test_ce07_new_identity_searches_and_replay_never_resends(tmp_path):
    from agent_alfred.tools import ToolContext

    script = [
        GATE,
        calls(ToolCallBlock("one", "web_search", {"query": "q"})),
        calls(ToolCallBlock("two", "web_search", {"query": "q"})),
        "Done",
    ]
    with (
        tavily_http() as (url, requests),
        search_host(tmp_path, url, script) as (host, model),
    ):
        run = host.submit(SubmitRequest(message="搜索"))
        assert host.wait(run.run_id, timeout=5).outcome == "completed"
        rows = host.tool_requests(run.run_id)
        assert len(requests) == len(rows) == 2
        original = rows[0]
        context = ToolContext(
            run.run_id, original["step_index"], "one", "cli", float("inf")
        )
        receipt = host._tools.execute(
            ToolCallBlock("one", "web_search", {"query": "q"}), context
        )
        assert not receipt.block.is_error
        assert len(requests) == len(host.tool_requests(run.run_id)) == 2
        # Independently exercise the real receipt owner below the metering replay.
        tool = next(t for t in host._tools.declarations() if t.name == "web_search")
        assert (
            host._external_tools.start(tool, {"query": "q"}, context).execution
            is not None
        )
        assert len(requests) == 2


@pytest.mark.parametrize("path,limit", [("/search", 1024 * 1024), ("/usage", 65536)])
@pytest.mark.parametrize("extra", [0, 1])
def test_ce09_actual_byte_limits(tmp_path, path, limit, extra):
    from agent_alfred.gateway.web.api import DashboardApi

    prefix = b'{"results":[]}' if path == "/search" else b'{"key":{}}'
    body = prefix + b" " * (limit - len(prefix) + extra)
    with tavily_http(body) as (url, requests), search_host(tmp_path, url) as (host, _):
        if path == "/search":
            run = host.submit(SubmitRequest(message="搜索"))
            outcome = host.wait(run.run_id, timeout=5).outcome
            assert outcome == ("completed" if extra == 0 else "failed")
        else:
            assert (
                DashboardApi(facade=host).probe_auth({"endpoint_id": "tavily"})[0]
                == 200
            )
        observed = host.connections()["integrations"][0]["observation"]
        assert observed["state"] == ("connected" if extra == 0 else "error")
        if extra:
            assert observed["reason"] == "response_too_large"
        assert len(requests) == 1


@pytest.mark.parametrize("stage", ["before_fn", "before_http", "after_http"])
def test_ce10_distinguishes_start_and_send(tmp_path, stage):
    from urllib.request import ProxyHandler

    from agent_alfred.clock import FakeClock
    from agent_alfred.integrations_http import TavilyHTTP

    clock = FakeClock()
    with (
        tavily_http() as (url, requests),
        search_host(tmp_path, url, clock=clock) as (host, model),
    ):
        if stage == "before_fn":

            def exhaust():
                clock.monotonic_value = 1000
                return 0

            host._conn.create_function("exhaust", 0, exhaust)
            host._conn.execute(
                "CREATE TRIGGER expire_budget AFTER UPDATE OF start_confirmation "
                "ON tool_metering WHEN NEW.start_confirmation='unconfirmed' "
                "BEGIN SELECT exhaust(); END"
            )
        else:

            class BoundaryHTTP(TavilyHTTP):
                def request(self, *args, **kwargs):
                    if stage == "before_http":
                        clock.monotonic_value = 1000
                    else:
                        import http.client

                        original = http.client.HTTPResponse.read1

                        def expire(response, *a):
                            result = original(response, *a)
                            clock.monotonic_value = 1000
                            return result

                        with pytest.MonkeyPatch.context() as patch:
                            patch.setattr(http.client.HTTPResponse, "read1", expire)
                            return super().request(*args, **kwargs)
                    return super().request(*args, **kwargs)

            host._integrations.transport = BoundaryHTTP(
                base_url=url, proxy_handler=ProxyHandler({})
            )
        run = host.submit(SubmitRequest(message="搜索"))
        result = host.wait(run.run_id, timeout=5)
        row = host.tool_requests(run.run_id)[0]
        assert len(requests) == (1 if stage == "after_http" else 0)
        assert row["start_confirmation"] == (
            "not_started" if stage == "before_fn" else "confirmed"
        )
        assert row["cost"]["kind"] == (
            "unknown" if stage == "after_http" else "not_billable"
        )
        if stage == "before_fn":
            assert row["result"] == "not_executed"
        if stage == "after_http":
            assert row["result"] == "unknown" and result.outcome == "failed"
            assert len(model.requests) == 2


def test_ce06_control_exception_keeps_both_ledgers_unknown(tmp_path, monkeypatch):
    import http.client

    from agent_alfred.tools import ToolContext

    closed = []
    original = http.client.HTTPResponse.close

    def close(response):
        closed.append(True)
        original(response)

    def interrupt(*args):
        raise KeyboardInterrupt("synthetic-tavily-key")

    with tavily_http() as (url, requests), search_host(tmp_path, url) as (host, _):
        monkeypatch.setattr(http.client.HTTPResponse, "read1", interrupt)
        monkeypatch.setattr(http.client.HTTPResponse, "close", close)
        context = ToolContext("control-run", 1, "interrupt", "cli", float("inf"))
        with pytest.raises(KeyboardInterrupt):
            host._tools.execute(
                ToolCallBlock("interrupt", "web_search", {"query": "q"}), context
            )
        assert len(requests) == 1 and closed
        assert host.tool_requests("control-run")[0]["result"] == "unknown"
        assert (
            host._conn.execute(
                "SELECT status FROM tool_ledger WHERE run_id='control-run'"
            ).fetchone()[0]
            == "unknown"
        )


def test_ce06_metering_failure_stops_further_io(tmp_path):
    script = [
        GATE,
        calls(
            ToolCallBlock("one", "web_search", {"query": "q"}),
            ToolCallBlock("two", "web_search", {"query": "q"}),
        ),
        "Never",
    ]
    with (
        tavily_http() as (url, requests),
        search_host(tmp_path, url, script) as (host, model),
    ):
        host._conn.execute(
            "CREATE TRIGGER fail_cost BEFORE UPDATE OF finished_at ON tool_metering "
            "BEGIN SELECT RAISE(ABORT, 'storage fault'); END"
        )
        run = host.submit(SubmitRequest(message="搜索"))
        assert host.wait(run.run_id, timeout=5).outcome == "failed"
        assert len(requests) == 1 and len(model.requests) == 2
        assert host.submit(SubmitRequest(message="新搜索")).kind != "accepted"


@pytest.mark.parametrize("key", ["tiny", "sentinel-current-credential"])
def test_ce08_all_projections_redact_and_redirect_never_follows(tmp_path, key):
    body = json.dumps(
        {
            "results": [
                {"title": key, "url": "https://source.test/" + key, "content": key}
            ]
        }
    ).encode()
    with (
        tavily_http(body) as (url, _),
        search_host(tmp_path, url, key=key) as (host, model),
    ):
        run = host.submit(SubmitRequest(message="搜索"))
        host.wait(run.run_id, timeout=5)
        row = host.tool_requests(run.run_id)[0]
        history = host.read_tool_history(
            tmp_path / "state" / "traces",
            run_id=run.run_id,
            step_index=row["step_index"],
            call_id="search",
            projection="audit",
        )
        assert key not in str(history) + str(host.connections()) + str(row) + str(
            model.requests
        )
        for path in (tmp_path / "state" / "traces").rglob("*"):
            if path.is_file():
                assert key.encode() not in path.read_bytes()
    with (
        tavily_http() as (target, followed),
        tavily_http(b"{}", 302, {"Location": target}) as (url, requests),
        search_host(tmp_path / "redirect", url, key=key) as (host, _),
    ):
        run = host.submit(SubmitRequest(message="搜索"))
        host.wait(run.run_id, timeout=5)
        assert len(requests) == 1 and followed == []


@pytest.mark.parametrize("key", ["  key", "key\n", "key\rinside", "key\0inside"])
def test_ce12_invalid_key_keeps_permission_but_hides_execution(tmp_path, key):
    with (
        tavily_http() as (url, requests),
        search_host(tmp_path, url, key=key) as (host, model),
    ):
        row = next(
            r for r in host.tools_catalog()["tools"] if r["name"] == "web_search"
        )
        assert row["exposure"] == "hidden"
        assert row["reason"] == "configuration_invalid"
        assert row["authorization"] == "allowed"
        run = host.submit(SubmitRequest(message="搜索"))
        host.wait(run.run_id, timeout=5)
        assert requests == []
        assert host.tool_requests(run.run_id)[0]["reason"] == "unavailable"


def test_ce12_declarations_and_missing_extra_are_separate(tmp_path):
    from agent_alfred.clock import FakeClock
    from agent_alfred.integrations import (
        TAVILY,
        Integration,
        IntegrationField,
        Integrations,
    )
    from agent_alfred.redact import Redactor
    from agent_alfred.tools import Tool, ToolContext, ToolRegistry, ToolSuccess

    fixture = Integration(
        "fixture",
        (IntegrationField("FIXTURE_KEY"),),
        "missing-extra",
        "/probe",
        ("fixture_search",),
    )
    service = Integrations(
        {"FIXTURE_KEY": "set"},
        FakeClock(),
        Redactor(()),
        declarations=(TAVILY, fixture),
    )
    assert service.snapshot()[1]["availability"] == "dependency_missing"
    assert service.snapshot()[0]["availability"] == "unconfigured"
    invoked = []
    declaration = Tool(
        "fixture_search",
        "Fixture",
        {"type": "object", "properties": {}},
        lambda a, c: invoked.append(True) or ToolSuccess(()),
        "external",
        source_id="fixture",
    )
    registry = ToolRegistry(
        (declaration,), clock=FakeClock(), policies=service.policies()
    )
    assert registry.schemas() == ()
    result = registry.execute(
        ToolCallBlock("f", "fixture_search", {}), ToolContext("r", 1, "f", "cli", 10)
    )
    assert result.block.is_error and invoked == []
    from dataclasses import replace

    for duplicate in (
        TAVILY,
        replace(fixture, fields=TAVILY.fields),
        replace(fixture, tools=TAVILY.tools),
    ):
        with pytest.raises(ValueError, match="identity"):
            Integrations(
                {}, FakeClock(), Redactor(()), declarations=(TAVILY, duplicate)
            )


@pytest.mark.parametrize(
    "stage", ["read", "decode", "prepare", "publish", "integration_publish", "rollback"]
)
def test_ce11_injected_publication_failures_keep_consistent_view(
    tmp_path, monkeypatch, stage
):
    from pathlib import Path

    from agent_alfred.gateway.web.api import DashboardApi
    from agent_alfred.tools import ToolRegistry

    path = tmp_path / ".env"
    path.write_text("TAVILY_API_KEY=old-sentinel-key\n")
    host = build_default_host(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(ScriptedModel([])),
        credentials=CredentialOverlay({}, str(path)),
    )
    api = DashboardApi(facade=host)
    try:
        host.save_tool_authorization('["tavily","web_search"]', "allowed", 0)
        before = host.tools_catalog()
        before_connection = host.connections()
        path.write_text("TAVILY_API_KEY=new-sentinel-key\n")
        with monkeypatch.context() as patch:
            if stage == "read":
                original = Path.read_text

                def read(p, *args, **kwargs):
                    if p == path:
                        raise PermissionError("old-sentinel-key new-sentinel-key")
                    return original(p, *args, **kwargs)

                patch.setattr(Path, "read_text", read)
            elif stage == "decode":
                path.write_bytes(b"\xff")
            elif stage == "integration_publish":
                original = host._integrations.publish

                def publish(values):
                    original(values)
                    raise OSError("after integration publication")

                patch.setattr(host._integrations, "publish", publish)
            elif stage == "prepare":

                def fail(*args, **kwargs):
                    raise ValueError("prepare failed")

                patch.setattr(ToolRegistry, "with_policies", fail)
            else:
                original = host._publish_tools
                count = []

                def publish(registry):
                    original(registry)
                    count.append(True)
                    if len(count) == 1 or stage == "rollback":
                        raise OSError("publication failed")

                patch.setattr(host, "_publish_tools", publish)
            status, payload = api.reread_env()
            assert status == 400
            assert "sentinel" not in str(payload)
            row = next(
                t for t in host.tools_catalog()["tools"] if t["name"] == "web_search"
            )
            assert row["authorization"] == "allowed"
            assert row["exposure"] == ("hidden" if stage == "rollback" else "real")
            assert host._credential_env()["TAVILY_API_KEY"] == "old-sentinel-key"
            assert host.tools_catalog()["revision"] == before["revision"]
            if stage != "rollback":
                assert host.connections() == before_connection
        if stage == "rollback":
            host.reapply_tool_authorization(before["revision"])
            assert (
                next(
                    t
                    for t in host.tools_catalog()["tools"]
                    if t["name"] == "web_search"
                )["exposure"]
                == "hidden"
            )
            host.save_tool_authorization(
                '["tavily","web_search"]', "allowed", before["revision"]
            )
            assert (
                next(
                    t
                    for t in host.tools_catalog()["tools"]
                    if t["name"] == "web_search"
                )["exposure"]
                == "hidden"
            )
        path.write_text("TAVILY_API_KEY=new-sentinel-key\n")
        assert api.reread_env()[0] == 200
        assert host._credential_env()["TAVILY_API_KEY"] == "new-sentinel-key"
        assert (
            next(t for t in host.tools_catalog()["tools"] if t["name"] == "web_search")[
                "exposure"
            ]
            == "real"
        )
    finally:
        host.close()


def test_ce03_same_key_probe_cache_cannot_overwrite_search_error(tmp_path):
    from urllib.request import ProxyHandler

    from agent_alfred.gateway.web.api import DashboardApi
    from agent_alfred.integrations_http import TavilyHTTP

    with (
        tavily_http(b'{"key":{}}') as (good, probes),
        tavily_http(b"{}", 401) as (bad, searches),
        search_host(tmp_path, good) as (host, model),
    ):
        api = DashboardApi(facade=host)
        api.probe_auth({"endpoint_id": "tavily"})
        assert (
            host.connections()["integrations"][0]["observation"]["state"] == "connected"
        )
        host._integrations.transport = TavilyHTTP(
            base_url=bad, proxy_handler=ProxyHandler({})
        )
        run = host.submit(SubmitRequest(message="搜索"))
        host.wait(run.run_id, timeout=5)
        assert (
            host.connections()["integrations"][0]["observation"]["reason"]
            == "authentication_failed"
        )
        api.probe_auth({"endpoint_id": "tavily"})
        assert len(probes) == 1 and len(searches) == 2
        assert host.connections()["integrations"][0]["observation"]["state"] == "error"


@pytest.mark.parametrize("retry", ["120", "invalid", None])
def test_ce02_ce04_429_cooldowns_are_local_and_separate(tmp_path, retry):
    from agent_alfred.clock import FakeClock
    from agent_alfred.gateway.web.api import DashboardApi

    clock = FakeClock()
    script = [
        GATE,
        calls(ToolCallBlock("s1", "web_search", {"query": "q"})),
        calls(ToolCallBlock("s2", "web_search", {"query": "q"})),
        "Done",
    ]
    with (
        tavily_http(b"{}", 429, {"Retry-After": retry} if retry else {}) as (
            url,
            requests,
        ),
        search_host(tmp_path, url, script, clock) as (host, model),
    ):
        run = host.submit(SubmitRequest(message="搜索"))
        host.wait(run.run_id, timeout=5)
        assert len(requests) == 1
        assert host.tool_requests(run.run_id)[1]["cost"]["kind"] == "not_billable"
        assert (
            host.connections()["integrations"][0]["observation"]["reason"]
            == "rate_limited"
        )
        api = DashboardApi(facade=host)
        api.probe_auth({"endpoint_id": "tavily"})
        assert len(requests) == 2
        assert (
            host.connections()["integrations"][0]["observation"]["state"]
            == "configured_untested"
        )
        clock.monotonic_value = 60
        api.probe_auth({"endpoint_id": "tavily"})
        assert len(requests) == 2
        clock.monotonic_value = 120 if retry == "120" else 600
        api.probe_auth({"endpoint_id": "tavily"})
        assert len(requests) == 3


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Length": "999"},
        {"Content-Encoding": "gzip"},
        {"Transfer-Encoding": "chunked"},
    ],
)
def test_ce09_truncated_or_unsupported_response_is_unknown(tmp_path, headers):
    with (
        tavily_http(b'{"results":[]}', headers=headers) as (url, _),
        search_host(tmp_path, url) as (host, model),
    ):
        run = host.submit(SubmitRequest(message="搜索"))
        assert host.wait(run.run_id, timeout=5).outcome == "failed"
        assert host.tool_requests(run.run_id)[0]["result"] == "unknown"
        assert len(model.requests) == 2


def test_ce05_ops_sum_preserves_reported_fraction(tmp_path):
    fraction = "0.12345678901234567890123456789"
    body = ('{"results":[],"usage":{"credits":' + fraction + "}}").encode()
    with tavily_http(body) as (url, _), search_host(tmp_path, url) as (host, model):
        run = host.submit(SubmitRequest(message="搜索"))
        host.wait(run.run_id, timeout=5)
        snapshot = host.accounting_snapshot({"timezone": "UTC", "range": "all"})
        assert fraction in str(snapshot)


@pytest.mark.parametrize("shutdown", [False, True])
def test_ce06_ce09_io_ownership_blocks_mutation_until_late_response_exits(
    tmp_path, shutdown
):
    from agent_alfred.clock import FakeClock
    from agent_alfred.gateway.web.api import DashboardApi

    clock = FakeClock()
    entered, release = threading.Event(), threading.Event()

    def block(handler):
        entered.set()
        assert release.wait(5)

    script = [
        GATE,
        calls(ToolCallBlock("first", "web_search", {"query": "same"})),
        GATE,
        calls(ToolCallBlock("second", "web_search", {"query": "same"})),
        "Done",
    ]
    with (
        tavily_http(on_request=block) as (url, requests),
        search_host(tmp_path, url, script, clock) as (host, model),
    ):
        run = host.submit(SubmitRequest(message="搜索"))
        try:
            assert entered.wait(5)
            clock.monotonic_value = 11
            assert DashboardApi(facade=host).reread_env()[0] == 409
            assert host.submit(SubmitRequest(message="新搜索")).kind != "accepted"
            if shutdown:
                assert host.close(timeout=0.01) is False
            assert len(requests) == 1 and len(model.requests) == 2
        finally:
            release.set()
        assert host.wait(run.run_id, timeout=5).outcome == "failed"
        assert host.tool_requests(run.run_id)[0]["result"] == "unknown"
        assert len(requests) == 1
        if not shutdown:
            newer = host.submit(SubmitRequest(message="明确再次搜索"))
            assert host.wait(newer.run_id, timeout=5).outcome == "completed"
            assert len(requests) == 2
        else:
            assert host.close(timeout=2)


def test_ce08_real_cli_failure_does_not_print_provider_secrets(tmp_path):
    from io import StringIO

    from agent_alfred.gateway.cli import _send

    key = "tiny"
    with (
        tavily_http(b'{"detail":{"input":"tiny"}}', 401) as (url, requests),
        search_host(tmp_path, url, key=key) as (host, _),
    ):
        output = StringIO()
        _send(
            host,
            "搜索",
            host.create_session(),
            output,
            renderer=lambda t, out: out.write(t),
        )
        assert key not in output.getvalue()
        assert len(requests) == 1


def test_ce08_proxy_errors_never_fall_back_to_direct(tmp_path):
    from urllib.request import ProxyHandler

    from agent_alfred.integrations_http import TavilyHTTP

    with tavily_http() as (url, requests), search_host(tmp_path, url) as (host, _):
        host._integrations.transport = TavilyHTTP(
            base_url=url,
            proxy_handler=ProxyHandler({"https": "socks5://localhost:1080"}),
        )
        run = host.submit(SubmitRequest(message="搜索"))
        host.wait(run.run_id, timeout=5)
        assert requests == []
        assert host.tool_requests(run.run_id)[0]["cost"]["kind"] == "not_billable"
        assert (
            host.connections()["integrations"][0]["observation"]["reason"]
            == "proxy_invalid"
        )


@pytest.mark.parametrize("length,maximum", [(1, 1), (2000, 10)])
def test_ce09_valid_unicode_boundaries_and_optional_fields(tmp_path, length, maximum):
    body = json.dumps(
        {
            "results": [
                {
                    "title": "t",
                    "url": "https://source.test",
                    "content": "",
                    "score": True,
                }
            ],
            "request_id": 5,
            "answer": "DROP",
            "images": ["DROP"],
            "raw_content": "DROP",
        }
    ).encode()
    script = [
        GATE,
        calls(
            ToolCallBlock(
                "s", "web_search", {"query": "😀" * length, "max_results": maximum}
            )
        ),
        "Done",
    ]
    with (
        tavily_http(body) as (url, requests),
        search_host(tmp_path, url, script) as (host, model),
    ):
        run = host.submit(SubmitRequest(message="搜索"))
        assert host.wait(run.run_id, timeout=5).outcome == "completed"
        assert json.loads(requests[0][2])["query"] == "😀" * length
        result = json.loads(model.requests[-1].messages[-1].blocks[0].content[0].text)
        assert result["results"] == [
            {"title": "t", "url": "https://source.test", "content": ""}
        ]
        assert "request_id" not in result and "DROP" not in str(result)


def test_ce09_over_requested_count_is_not_silently_truncated(tmp_path):
    body = json.dumps(
        {"results": [{"title": "t", "url": "https://source.test", "content": ""}] * 2}
    ).encode()
    script = [
        GATE,
        calls(ToolCallBlock("s", "web_search", {"query": "q", "max_results": 1})),
        "Never",
    ]
    with (
        tavily_http(body) as (url, _),
        search_host(tmp_path, url, script) as (host, model),
    ):
        run = host.submit(SubmitRequest(message="搜索"))
        assert host.wait(run.run_id, timeout=5).outcome == "failed"
        assert len(model.requests) == 2


def test_ce08_ce09_audit_is_selected_redacted_fields_and_model_is_bounded(tmp_path):
    body = json.dumps(
        {
            "results": [
                {
                    "title": "t",
                    "url": "https://source.test",
                    "content": "safe " * 2000 + " synthetic-tavily-key END",
                }
            ],
            "answer": "UNSELECTED_SECRET",
            "request_id": "r-id",
            "usage": {"credits": 1},
        }
    ).encode()
    with tavily_http(body) as (url, _), search_host(tmp_path, url) as (host, model):
        run = host.submit(SubmitRequest(message="搜索"))
        host.wait(run.run_id, timeout=5)
        assert "[truncated original_bytes=" in str(model.requests[-1].messages)
        row = host.tool_requests(run.run_id)[0]
        history = host.read_tool_history(
            tmp_path / "state" / "traces",
            run_id=run.run_id,
            step_index=row["step_index"],
            call_id="search",
            projection="audit",
        )
        assert "END" in history["text"] and "r-id" in history["text"]
        assert "synthetic-tavily-key" not in str(history)
        assert "UNSELECTED_SECRET" not in str(history)


def test_ce02_ce03_noop_and_process_env_reread_keep_observation_and_host_window(
    tmp_path,
):
    from urllib.request import ProxyHandler

    from agent_alfred.clock import FakeClock
    from agent_alfred.gateway.web.api import DashboardApi
    from agent_alfred.integrations_http import TavilyHTTP

    path = tmp_path / ".env"
    path.write_text("TAVILY_API_KEY=file-key\n")
    clock = FakeClock()
    with tavily_http(b"{}", 500) as (url, requests):
        host = build_default_host(
            state_dir=tmp_path / "state",
            clock=clock,
            credentials=CredentialOverlay({}, str(path)),
            factory=ScriptedModelFactory(ScriptedModel([])),
        )
        host._integrations.transport = TavilyHTTP(
            base_url=url, proxy_handler=ProxyHandler({})
        )
        api = DashboardApi(facade=host)
        try:
            for i in range(10):
                api.probe_auth({"endpoint_id": "tavily"})
                before = host.connections()["integrations"][0]
                api.reread_env()
                assert host.connections()["integrations"][0] == before
                clock.monotonic_value += 30
            path.write_text("TAVILY_API_KEY=replacement\n")
            api.reread_env()
            api.probe_auth({"endpoint_id": "tavily"})
            assert len(requests) == 10
            assert (
                host.connections()["integrations"][0]["observation"]["state"]
                == "configured_untested"
            )
        finally:
            host.close()
        restarted = build_default_host(
            state_dir=tmp_path / "state",
            credentials=CredentialOverlay({"TAVILY_API_KEY": "process-key"}, str(path)),
            factory=ScriptedModelFactory(ScriptedModel([])),
        )
        try:
            assert (
                restarted.connections()["integrations"][0]["observation"]["state"]
                == "configured_untested"
            )
            path.unlink()
            assert DashboardApi(facade=restarted).reread_env()[0] == 200
            assert restarted._credential_env()["TAVILY_API_KEY"] == "process-key"
        finally:
            restarted.close()


@pytest.mark.parametrize(
    "body,connected",
    [
        (b'{"account":{}}', True),
        (b'{"key":{}}', True),
        (b'{"key":[]}', False),
        (b"{}", False),
        (b"[]", False),
        (b"bad", False),
    ],
)
def test_ce04_usage_requires_real_structure_and_does_not_invent_balances(
    tmp_path, body, connected
):
    from agent_alfred.gateway.web.api import DashboardApi

    with tavily_http(body) as (url, requests), search_host(tmp_path, url) as (host, _):
        DashboardApi(facade=host).probe_auth({"endpoint_id": "tavily"})
        observation = host.connections()["integrations"][0]["observation"]
        assert observation["state"] == ("connected" if connected else "error")
        assert "plan_usage" not in str(observation) and "plan_limit" not in str(
            observation
        )
        assert (
            host.accounting_snapshot({"timezone": "UTC", "range": "all"})["summary"][
                "run_count"
            ]
            == 0
        )


def test_ce05_unrepresentable_optional_decimal_is_unknown_cost_not_lost_result(
    tmp_path,
):
    body = b'{"results":[],"usage":{"credits":1e9999999999999999999}}'
    script = [
        GATE,
        calls(ToolCallBlock("one", "web_search", {"query": "q"})),
        calls(ToolCallBlock("two", "web_search", {"query": "q"})),
        "Done",
    ]
    with (
        tavily_http(body) as (url, requests),
        search_host(tmp_path, url, script) as (host, model),
    ):
        run = host.submit(SubmitRequest(message="搜索"))
        assert host.wait(run.run_id, timeout=5).outcome == "completed"
        rows = host.tool_requests(run.run_id)
        assert len(requests) == len(rows) == 2
        assert all(
            r["result"] == "succeeded" and r["cost"]["kind"] == "unknown" for r in rows
        )
        assert (
            host.connections()["integrations"][0]["observation"]["state"] == "connected"
        )


def test_ce06_unexpected_transport_exception_never_continues_unknown_run(tmp_path):
    from agent_alfred.integrations_http import TavilyHTTP

    class BrokenHTTP(TavilyHTTP):
        def request(self, *args, **kwargs):
            raise ArithmeticError("synthetic-tavily-key")

    script = [
        GATE,
        calls(
            ToolCallBlock("one", "web_search", {"query": "q"}),
            ToolCallBlock("two", "web_search", {"query": "q"}),
        ),
        "Never",
    ]
    with (
        tavily_http() as (url, requests),
        search_host(tmp_path, url, script) as (host, model),
    ):
        host._integrations.transport = BrokenHTTP()
        run = host.submit(SubmitRequest(message="搜索"))
        result = host.wait(run.run_id, timeout=5)
        assert result.outcome == "failed" and result.error == "tool_result_unverified"
        assert len(model.requests) == 2
        assert host.tool_requests(run.run_id)[0]["result"] == "unknown"
        assert host.tool_requests(run.run_id)[1]["result"] == "not_executed"


def test_ce06_host_preserves_interrupted_control_and_closes_http(tmp_path, monkeypatch):
    import http.client

    closed = []
    close = http.client.HTTPResponse.close

    def close_response(response):
        closed.append(True)
        return close(response)

    def control(response, *args):
        raise KeyboardInterrupt("synthetic-tavily-key")

    with tavily_http() as (url, requests), search_host(tmp_path, url) as (host, model):
        monkeypatch.setattr(http.client.HTTPResponse, "read1", control)
        monkeypatch.setattr(http.client.HTTPResponse, "close", close_response)
        run = host.submit(SubmitRequest(message="搜索"))
        result = host.wait(run.run_id, timeout=5)
        assert result.outcome == "interrupted"
        assert len(requests) == 1 and len(model.requests) == 2 and closed
        assert host.tool_requests(run.run_id)[0]["result"] == "unknown"
        assert (
            host._conn.execute(
                "SELECT status FROM tool_ledger WHERE run_id=?", (run.run_id,)
            ).fetchone()[0]
            == "unknown"
        )


def test_ce03_ce08_rotation_sends_only_new_key_and_redacts_both_in_history(tmp_path):
    from urllib.request import ProxyHandler

    from agent_alfred.gateway.web.api import DashboardApi
    from agent_alfred.integrations_http import TavilyHTTP

    old, new = "old-sentinel-credential", "new-sentinel-credential"
    path = tmp_path / ".env"
    path.write_text("TAVILY_API_KEY=" + old + "\n")
    model = ScriptedModel(
        [GATE, calls(ToolCallBlock("s", "web_search", {"query": "q"})), "Done"]
    )
    body = json.dumps(
        {"results": [{"title": old, "url": "https://source.test", "content": new}]}
    ).encode()
    with tavily_http(body) as (url, requests):
        host = build_default_host(
            state_dir=tmp_path / "state",
            credentials=CredentialOverlay({}, str(path)),
            factory=ScriptedModelFactory(model),
        )
        host._integrations.transport = TavilyHTTP(
            base_url=url, proxy_handler=ProxyHandler({})
        )
        host.save_tool_authorization('["tavily","web_search"]', "allowed", 0)
        path.write_text("TAVILY_API_KEY=" + new + "\n")
        assert DashboardApi(facade=host).reread_env()[0] == 200
        host.start()
        try:
            run = host.submit(SubmitRequest(message="搜索"))
            host.wait(run.run_id, timeout=5)
            assert requests[0][1]["Authorization"] == "Bearer " + new
            row = host.tool_requests(run.run_id)[0]
            history = host.read_tool_history(
                tmp_path / "state" / "traces",
                run_id=run.run_id,
                step_index=row["step_index"],
                call_id="s",
                projection="audit",
            )
            for secret in (old, new):
                assert secret not in str(history) + str(model.requests) + str(
                    host.connections()
                ) + str(row)
                for file in (tmp_path / "state" / "traces").rglob("*"):
                    if file.is_file():
                        assert secret.encode() not in file.read_bytes()
        finally:
            host.close()


@pytest.mark.parametrize("recovery", ["save", "reapply"])
def test_ce11_authorization_write_failure_can_recover(tmp_path, monkeypatch, recovery):
    from agent_alfred.tools import authorization

    with tavily_http() as (url, requests), search_host(tmp_path, url) as (host, _):
        identity = '["tavily","web_search"]'
        revision = host.tools_catalog()["revision"]
        with monkeypatch.context() as patch:

            def fail_write(*args, **kwargs):
                raise OSError("synthetic authorization IO failure")

            patch.setattr(authorization, "write_atomic", fail_write)
            failed = host.save_tool_authorization(identity, "denied", revision)
            assert failed["error"]["code"] == "authorization_write_unconfirmed"
        row = next(
            t for t in host.tools_catalog()["tools"] if t["name"] == "web_search"
        )
        assert row["exposure"] == "hidden"
        assert len(requests) == 0
        if recovery == "save":
            restored = host.save_tool_authorization(identity, "allowed", revision)
        else:
            restored = host.reapply_tool_authorization(revision)
        assert "error" not in restored
        row = next(
            t for t in host.tools_catalog()["tools"] if t["name"] == "web_search"
        )
        assert row["authorization"] == "allowed"
        assert row["exposure"] == "real"
        run = host.submit(SubmitRequest(message="搜索"))
        assert host.wait(run.run_id, timeout=5).outcome == "completed"
        assert len(requests) == 1


@pytest.mark.parametrize(
    "number,reported",
    [
        ("1e4095", True),
        ("1e4096", False),
        ("1e-4095", True),
        ("1e-4096", False),
        ("9" * 4096, True),
        ("9" * 4097, False),
        ("1e1000000", False),
        ("1e-1000000", False),
        ("0e1000000", True),
        ("0e-4095", True),
        ("0e-4096", False),
    ],
    ids=[
        "integer-exact",
        "integer-plus-one",
        "fraction-exact",
        "fraction-plus-one",
        "coefficient-exact",
        "coefficient-plus-one",
        "huge-positive-exponent",
        "huge-negative-exponent",
        "zero-positive-exponent",
        "zero-exact",
        "zero-plus-one",
    ],
)
@pytest.mark.parametrize("endpoint", ["search", "usage"])
def test_ce04_ce05_reported_number_expansion_is_bounded(
    tmp_path, number, reported, endpoint
):
    from decimal import Decimal, localcontext

    from agent_alfred.gateway.web.api import DashboardApi

    body = (
        '{"results":[],"usage":{"credits":' + number + "}}"
        if endpoint == "search"
        else '{"key":{"plan_usage":' + number + ',"plan_limit":0.125}}'
    ).encode()
    script = [
        GATE,
        calls(ToolCallBlock("one", "web_search", {"query": "q"})),
        calls(ToolCallBlock("two", "web_search", {"query": "q"})),
        "Done",
    ]
    with (
        tavily_http(body) as (url, requests),
        search_host(tmp_path, url, script) as (host, model),
    ):
        if endpoint == "usage":
            assert (
                DashboardApi(facade=host).probe_auth({"endpoint_id": "tavily"})[0]
                == 200
            )
            observation = host.connections()["integrations"][0]["observation"]
            assert observation["state"] == "connected"
            balances = observation["balances"]["key"]
            assert balances["plan_limit"] == "0.125"
            assert ("plan_usage" in balances) is reported
            if reported:
                assert Decimal(balances["plan_usage"]) == Decimal(number)
            assert len(requests) == 1
            assert len(json.dumps(observation)) < 5000
            assert (
                host.accounting_snapshot({"timezone": "UTC", "range": "all"})[
                    "summary"
                ]["run_count"]
                == 0
            )
            return
        run = host.submit(SubmitRequest(message="搜索"))
        assert host.wait(run.run_id, timeout=5).outcome == "completed"
        rows = host.tool_requests(run.run_id)
        assert len(rows) == len(requests) == 2
        assert len(model.requests) == 4
        for row in rows:
            assert row["result"] == "succeeded"
            assert row["cost"]["kind"] == ("reported" if reported else "unknown")
            if reported:
                assert Decimal(row["cost"]["units"]) == Decimal(number)
                assert len(row["cost"]["units"].replace(".", "")) <= 4096
        summary = host.accounting_snapshot({"timezone": "UTC", "range": "all"})[
            "summary"
        ]
        assert summary["tool_cost_states"]["reported" if reported else "unknown"] == 2
        if reported:
            with localcontext() as context:
                context.prec = 8200
                assert Decimal(summary["tool_costs"][0]["units"]) == Decimal(number) * 2
        else:
            assert summary["tool_costs"] == []
