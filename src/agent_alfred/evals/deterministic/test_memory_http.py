"""Issue 18 services over the real guarded dashboard HTTP boundary."""

import json

import pytest

from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
    free_loopback_port,
)
from agent_alfred.evals.deterministic.test_memory_mirrors import save
from agent_alfred.evals.deterministic.test_web_http import _get, _request
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.wiring import build_dashboard


def test_real_dashboard_reads_queue_and_verified_mirror(tmp_path):
    model = ScriptedModel(["unused"])
    dashboard = build_dashboard(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(model),
        port=free_loopback_port(),
    )
    try:
        dashboard.start()
        save(dashboard.host.memory_service)
        head, body = _request(
            dashboard.port,
            _get(dashboard.port, "/api/memory/mirrors?name=facts&preview=1"),
        )
        assert head.startswith(b"HTTP/1.1 200")
        assert json.loads(body)["mirrors"][0]["ready"]
        assert "I prefer coriander" in json.loads(body)["mirrors"][0]["text"]
        head, body = _request(
            dashboard.port, _get(dashboard.port, "/api/memory/consolidation")
        )
        assert head.startswith(b"HTTP/1.1 200")
        assert json.loads(body)["sessions"] == []
        assert model.requests == []
    finally:
        assert dashboard.close()


def post(dashboard, body, path="/api/memory/consolidation/actions"):
    raw = json.dumps(body).encode()
    request = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: localhost:{dashboard.port}\r\n"
        f"Origin: http://localhost:{dashboard.port}\r\n"
        f"x-agent-alfred-csrf: {dashboard.csrf_token}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(raw)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + raw
    head, content = _request(dashboard.port, request)
    return int(head.split()[1]), json.loads(content)


def test_http_mirror_confirmation_busy_identity_and_durable_query(tmp_path):
    dashboard = build_dashboard(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
        port=free_loopback_port(),
    )
    try:
        dashboard.start()
        host = dashboard.host
        save(host.memory_service)
        path = tmp_path / "state/memory/facts.md"
        path.write_text("External edit")
        observation = host.memory_service.mirrors.status("facts")["confirmation"]
        action = {
            "schema_version": 1,
            "action": "mirror_confirm",
            "name": "facts",
            "observation": observation,
            "operation_id": "confirm",
        }
        assert host.try_begin_mutation() is None
        try:
            status, response = post(dashboard, action)
            assert status == 409 and response["result"]["error"]["code"] == "busy"
        finally:
            host.end_mutation()
        assert post(dashboard, {**action, "origin": "manual"})[0] == 400
        assert post(dashboard, {**action, "observation": "bad"})[0] == 400
        status, receipt = post(dashboard, action)
        assert status == 200 and receipt["result"]["status"] == "regenerated"
        path.write_text("External successor")
        status, replay = post(dashboard, action)
        assert status == 200 and replay["result"] == receipt["result"]
        head, body = _request(
            dashboard.port,
            _get(dashboard.port, "/api/memory/mirrors?operation_id=confirm"),
        )
        assert head.startswith(b"HTTP/1.1 200")
        assert json.loads(body)["result"] == receipt["result"]
        assert path.read_text() == "External successor"
        assert (
            post(dashboard, {**action, "observation": {}, "operation_id": "stale"})[0]
            == 409
        )
        retry = {
            "schema_version": 1,
            "action": "mirror_retry",
            "name": "facts",
            "operation_id": "retry",
        }
        assert post(dashboard, retry)[0] == 409
        assert post(dashboard, {**retry, "name": "episodes"})[0] == 409
    finally:
        assert dashboard.close()


def read(dashboard, query):
    head, body = _request(dashboard.port, _get(dashboard.port, query))
    return int(head.split()[1]), json.loads(body)


def prepared_dashboard(tmp_path, monkeypatch, script):
    from agent_alfred.settings import OPENCODE_API_KEY_ENV, Settings

    monkeypatch.setenv(OPENCODE_API_KEY_ENV, "offline-fixture")
    model = ScriptedModel(script)
    dashboard = build_dashboard(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(model),
        settings=Settings(consolidation_source_threshold=2),
        port=free_loopback_port(),
    )
    dashboard.start()
    return dashboard, model


def seed_sources(dashboard):
    from agent_alfred.evals.deterministic.test_memory_consolidation_service import (
        _complete_chat,
    )

    host = dashboard.host
    for i in (1, 2):
        _complete_chat(
            host.memory_service, host._conn, "s", f"r{i}", "I like coriander", "Noted."
        )


def generate(dashboard):
    accepted = dashboard.host.generate_consolidation("s")
    assert accepted.kind == "accepted"
    assert dashboard.host.wait(accepted.run_id, timeout=5) is not None
    return read(dashboard, "/api/memory/consolidation")[1]["batches"][0]


def test_http_commit_retry_and_regeneration_have_distinct_model_effects(
    tmp_path,
    monkeypatch,
):
    from agent_alfred.evals.deterministic.test_memory_consolidation_service import PLAN

    dashboard, model = prepared_dashboard(
        tmp_path, monkeypatch, [PLAN, "invalid", PLAN]
    )
    try:
        seed_sources(dashboard)
        conn = dashboard.host._conn
        conn.execute(
            "CREATE TRIGGER injected AFTER INSERT ON episodes BEGIN "
            "SELECT RAISE(ABORT, 'test-only'); END"
        )
        batch = generate(dashboard)
        assert batch["status"] == "failed" and len(model.requests) == 1
        conn.execute("DROP TRIGGER injected")
        conn.commit()
        action = {
            "schema_version": 1,
            "action": "retry",
            "operation_id": "commit",
            "batch_id": batch["batch_id"],
            "expected_revision": batch["revision"],
        }
        status, receipt = post(dashboard, action)
        assert status == 200 and receipt["result"]["status"] == "succeeded"
        assert len(model.requests) == 1
        assert post(dashboard, action)[1]["result"] == receipt["result"]
        assert (
            read(dashboard, "/api/memory/consolidation?operation_id=commit")[1][
                "result"
            ]
            == receipt["result"]
        )
        assert post(dashboard, {**action, "expected_revision": 99})[0] == 409
        # A separate complete source pair reaches a failed generation.
        from agent_alfred.evals.deterministic.test_memory_consolidation_service import (
            _complete_chat,
        )

        for i in (3, 4):
            _complete_chat(
                dashboard.host.memory_service,
                conn,
                "s",
                f"r{i}",
                "I like coriander",
                "Noted.",
            )
        failed = generate(dashboard)
        assert failed["status"] == "failed" and len(model.requests) == 2
        action = {
            **action,
            "batch_id": failed["batch_id"],
            "expected_revision": failed["revision"],
            "operation_id": "regenerate",
        }
        status, receipt = post(dashboard, action)
        assert status == 202
        run_id = receipt["result"]["generation_run_id"]
        assert run_id != failed["generation_run_id"]
        assert dashboard.host.wait(run_id, timeout=5) is not None
        assert len(model.requests) == 3
        current = read(dashboard, "/api/memory/consolidation?operation_id=regenerate")[
            1
        ]
        assert current["result"]["generation_run_id"] == run_id
        assert post(dashboard, {**action, "model_output": PLAN})[0] == 400
    finally:
        assert dashboard.close()


@pytest.mark.parametrize("decision", ["approve", "reject", "delete"])
def test_http_protected_approval_and_delete_never_reveal_candidate(
    tmp_path, monkeypatch, decision
):
    from agent_alfred.evals.deterministic.test_memory_consolidation_service import (
        CONTEXT,
    )

    dashboard, model = prepared_dashboard(tmp_path, monkeypatch, [])
    try:
        memory = dashboard.host.memory_service
        saved = save(memory)
        # The injected transport remains the real RuntimeHost's sole model dependency.
        model._script = [
            json.dumps(
                {
                    "semantic": [
                        {
                            "action": "update",
                            "id": saved["memory_id"],
                            "subject": "food",
                            "fact": "PRIVATE successor",
                        }
                    ],
                    "episode_summary": "PRIVATE summary",
                }
            )
        ]
        seed_sources(dashboard)
        batch = generate(dashboard)
        assert batch["status"] == "awaiting_approval"
        detail = read(
            dashboard, "/api/memory/consolidation?batch_id=" + batch["batch_id"]
        )[1]
        assert detail["batch"]["candidate_available"]
        assert detail["batch"]["model"]["model_id"]
        assert detail["batch"]["model_usage"] is not None
        action = {
            "schema_version": 1,
            "action": "approve",
            "operation_id": "approve",
            "batch_id": batch["batch_id"],
            "expected_revision": batch["revision"],
        }
        assert post(dashboard, {**action, "expected_revision": 99})[0] == 409
        if decision != "delete":
            action = {**action, "action": decision}
            status, receipt = post(dashboard, action)
            assert status == 200
            assert receipt["result"]["status"] == (
                "succeeded" if decision == "approve" else "rejected"
            )
            assert post(dashboard, action)[1]["result"] == receipt["result"]
            assert (
                read(dashboard, "/api/memory/consolidation?operation_id=approve")[1][
                    "result"
                ]
                == receipt["result"]
            )
            assert len(model.requests) == 1
            return
        deleted = memory.execute(
            {
                "operation_id": "delete",
                "kind": "semantic",
                "action": "delete",
                "expected_version": 1,
                "payload": {"id": saved["memory_id"]},
            },
            CONTEXT,
        )
        assert deleted["status"] == "deleted"
        assert post(dashboard, action)[0] == 409
        detail = read(
            dashboard, "/api/memory/consolidation?batch_id=" + batch["batch_id"]
        )[1]
        assert not detail["batch"]["candidate_available"]
        assert "PRIVATE" not in json.dumps(detail)
        assert "PRIVATE" not in json.dumps(
            read(dashboard, "/api/memory/consolidation")[1]
        )
    finally:
        assert dashboard.close()


def test_http_skip_limits_safe_errors_and_generic_run_rejection(tmp_path, monkeypatch):
    import sqlite3

    from agent_alfred.evals.deterministic.test_memory_consolidation_service import (
        _complete_chat,
    )

    dashboard, model = prepared_dashboard(tmp_path, monkeypatch, [])
    try:
        host = dashboard.host
        _complete_chat(
            host.memory_service, host._conn, "s", "huge", "PRIVATE" * 12000, "ok"
        )
        _complete_chat(host.memory_service, host._conn, "s", "small", "next", "ok")
        accepted = host.generate_consolidation("s")
        assert accepted.kind == "accepted"
        host.wait(accepted.run_id, timeout=5)
        queue = read(dashboard, "/api/memory/consolidation")[1]
        assert queue["sessions"][0]["block"]["run_id"] == "huge"
        assert "PRIVATE" not in json.dumps(queue)
        action = {
            "schema_version": 1,
            "action": "skip_oversized",
            "session_id": "s",
            "run_id": "huge",
            "operation_id": "skip",
        }
        status, receipt = post(dashboard, action)
        assert status == 200 and receipt["result"]["status"] == "skipped"
        assert post(dashboard, action)[1]["result"] == receipt["result"]
        assert post(dashboard, {**action, "operation_id": "late"})[0] == 409
        assert len(model.requests) == 0
        assert host._conn.execute("SELECT count(*) FROM agent_log").fetchone()[0] == 4
        assert read(dashboard, "/api/memory/consolidation?limit=0")[0] == 400
        assert read(dashboard, "/api/memory/consolidation?offset=-1")[0] == 400
        assert (
            read(dashboard, "/api/memory/consolidation?limit=1&offset=1")[1]["sessions"]
            == []
        )
        oversized = (
            f"POST /api/memory/consolidation/actions HTTP/1.1\r\n"
            f"Host: localhost:{dashboard.port}\r\n"
            f"x-agent-alfred-csrf: {dashboard.csrf_token}\r\n"
            "Content-Type: application/json\r\nContent-Length: 99999999\r\n\r\n"
        ).encode()
        assert _request(dashboard.port, oversized)[0].split()[1] == b"413"
        assert (
            post(
                dashboard,
                {"purpose": "consolidation", "prompt": "bypass"},
                path="/api/runs",
            )[0]
            == 400
        )
        assert post(dashboard, {**action, "action": "generate"})[0] == 400
        host._conn.set_authorizer(
            lambda operation, table, *_: (
                sqlite3.SQLITE_DENY
                if operation == sqlite3.SQLITE_READ and table == "sessions"
                else sqlite3.SQLITE_OK
            )
        )
        try:
            assert read(dashboard, "/api/memory/consolidation") == (
                503,
                {"code": "storage_unavailable"},
            )
        finally:
            host._conn.set_authorizer(None)
    finally:
        assert dashboard.close()
