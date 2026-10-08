"""Dashboard shared reads through real Host/SQLite and the HTTP projection."""

import json

import pytest

from agent_alfred.evals.deterministic.ops_support import GATE, ops_host, run
from agent_alfred.gateway.web.api import DashboardApi


def test_run_summaries_gate_input_and_preserve_source_filter(tmp_path):
    with ops_host(tmp_path, [GATE, "answer"]) as (host, _, conn, _):
        identity, _ = run(host, "private-input")
        api = DashboardApi(facade=host)
        params = {"process_instance_id": host.process_instance_id, "filter": "all"}
        assert api.runs_page(params)[1]["runs"][0]["recording_state"] == "recorded"
        for purpose, admission in [("chat", "rejected"), ("aggregation", "admitted")]:
            conn.execute(
                "UPDATE runs SET purpose=?,admission_state=? WHERE run_id=?",
                (purpose, admission, identity),
            )
            conn.commit()
            row = api.runs_page(params)[1]["runs"][0]
            assert row["prompt_preview"] is None
            assert row["filter"] == "chat"
            assert api.locate_run(identity, params)[1]["filter"] == "all"
        assert api.locate_run(identity, {**params, "filter": "system"}) == (
            409,
            {"code": "source_filter_mismatch"},
        )
        conn.execute("PRAGMA ignore_check_constraints=ON")
        for damaged in ["{", "[]", "true", "{}", '{"attempts":[]}']:
            conn.execute(
                "UPDATE runs SET telemetry=? WHERE run_id=?", (damaged, identity)
            )
            conn.commit()
            assert api.runs_page(params)[1]["runs"][0]["recording_state"] is None
        assert api.runs_page({**params, "process_instance_id": "old"}) == (
            409,
            {"code": "process_context_expired"},
        )


def test_exact_mainbar_and_source_location_are_bounded_and_identity_bound(tmp_path):
    with ops_host(tmp_path, [GATE, "answer-" + "long" * 10000]) as (host, _, conn, _):
        identity, _ = run(host)
        session = host.list_runs().runs[0].session_id
        api = DashboardApi(facade=host)
        params = {
            "process_instance_id": host.process_instance_id,
            "session_id": session,
            "run_id": identity,
        }
        status, located = api.shared_read("/api/mainbar/locate", params)
        assert status == 200
        assert located["run_id"] == identity
        assert len(located["reply_text"]) == 40007
        assert located["user"]["availability"] == "full"
        assert located["history_contiguous"] is False
        assert "next_cursor" not in located
        for _ in range(2):
            conn.execute(
                "INSERT INTO agent_log "
                "(session_id,role,content,source,created_at) VALUES "
                "(?, 'user', '[{\"type\":\"text\",\"text\":\"same\"}]', 'web','old')",
                (session,),
            )
        conn.commit()
        messages = api.session_messages(session, {})[1]["messages"]
        anchors = [m["message_anchor"] for m in messages if m["run_id"] is None]
        assert len(anchors) == len(set(anchors)) == 2
        status, source = api.shared_read(
            "/api/sessions/messages/locate",
            {
                "process_instance_id": host.process_instance_id,
                "session_id": session,
                "anchor": anchors[-1],
                "page_size": "1",
            },
        )
        assert status == 200
        assert source["target"]["placement"] == "page"
        assert [m["message_anchor"] for m in source["messages"]] == [anchors[-1]]
        assert api.shared_read(
            "/api/mainbar/locate", {**params, "session_id": "wrong"}
        ) == (404, {"code": "reply_target_unavailable"})


def test_no_reply_aggregation_metadata_is_independent_and_body_free(tmp_path):
    import sqlite3

    with ops_host(tmp_path, []) as (host, model, conn, _):
        session = host.create_session()
        accepted = host.aggregate(
            session_id=session, goal="目标", keywords="", sources=()
        )
        host.wait(accepted.run_id)
        api = DashboardApi(facade=host)
        params = {"process_instance_id": host.process_instance_id}

        def no_bodies(action, table, column, database, source):
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table == "agent_log"
                else sqlite3.SQLITE_OK
            )

        conn.set_authorizer(no_bodies)
        try:
            status, recent = api.shared_read("/api/overview/recent-runs", params)
            assert status == 200
            [row] = recent["runs"]
            assert "prompt_preview" not in row
            assert row["filter"] == "chat"
            assert row["aggregation"] == {
                "graph_result": "NoAction",
                "reply_disposition": "no_reply",
                "reason_code": "sources_not_selected",
                "evidence_state": "known",
            }
            assert row["recording_state"] == "recorded"
        finally:
            conn.set_authorizer(None)
        status, record = api.shared_read(
            "/api/mainbar/locate",
            {**params, "session_id": session, "run_id": accepted.run_id},
        )
        assert status == 200
        assert record["reply_text"] is None
        assert record["reply_disposition"] == "no_reply"
        assert record["user"]["availability"] == "full"
        assert model.requests == []


@pytest.mark.parametrize("length", [4, 240])
def test_sources_use_bounded_keysets_and_compatible_cursors(tmp_path, length):
    from agent_alfred import schema

    with ops_host(tmp_path, [GATE, "same"]) as (host, _, conn, _):
        original, _ = run(host)
        session = host.list_runs().runs[0].session_id
        for index in range(length):
            revision = schema.allocate_activity_revision(conn)
            conn.execute(
                "INSERT INTO runs (run_id,purpose,session_id,gateway,"
                "prompt_preview,phase,"
                "outcome,accepted_at,finished_at,activity_revision,"
                "telemetry,admission_state) "
                "SELECT ?,purpose,session_id,gateway,prompt_preview,"
                "phase,outcome,accepted_at,"
                "finished_at,?,telemetry,admission_state FROM runs WHERE run_id=?",
                (f"run-{index:04}", revision, original),
            )
            conn.execute(
                "INSERT INTO agent_log(session_id,role,content,source,"
                "created_at,run_id) "
                "SELECT session_id,role,content,source,created_at,? "
                "FROM agent_log WHERE run_id=?",
                (f"run-{index:04}", original),
            )
            conn.execute(
                "INSERT INTO sessions(session_id,created_at,activity_revision) "
                "VALUES (?,?,?)",
                (
                    f"session-{index:04}",
                    "same-time",
                    schema.allocate_activity_revision(conn),
                ),
            )
        conn.commit()
        api = DashboardApi(facade=host)
        identity = {"process_instance_id": host.process_instance_id}
        observed = []
        conn.set_trace_callback(observed.append)
        status, result = api.shared_read(
            "/api/sessions/locate",
            {**identity, "session_id": "session-0000", "limit": "3"},
        )
        assert status == 200
        assert [r["session_id"] for r in result["sessions"]] == [
            "session-0002",
            "session-0001",
            "session-0000",
        ]
        assert len(observed) < 20
        tail = api.session_inbox({"cursor": result["next_cursor"], "limit": "3"})[1]
        assert [r["session_id"] for r in tail["sessions"]] == [session]
        observed.clear()
        status, result = api.shared_read(
            "/api/sessions/runs/locate",
            {**identity, "session_id": session, "run_id": "run-0001", "limit": "3"},
        )
        assert status == 200
        assert [r["run_id"] for r in result["runs"]] == [
            "run-0003",
            "run-0002",
            "run-0001",
        ]
        assert len(observed) < 15
        next_runs = api.session_runs(
            {"session_id": session, "cursor": result["next_cursor"]}
        )[1]
        assert [r["run_id"] for r in next_runs["runs"]] == ["run-0000", original]
        target_messages = api.session_messages(session, {"page_size": "3"})[1][
            "messages"
        ]
        anchor = next(
            m["message_anchor"] for m in target_messages if m["run_id"] == "run-0001"
        )
        observed.clear()
        status, result = api.shared_read(
            "/api/sessions/messages/locate",
            {**identity, "session_id": session, "anchor": anchor, "page_size": "1"},
        )
        assert status == 200
        assert {r["run_id"] for r in result["messages"]} == {"run-0001"}
        assert len(observed) < 12
        next_messages = api.session_messages(
            session, {"cursor": result["next_cursor"], "page_size": "1"}
        )[1]
        assert {r["run_id"] for r in next_messages["messages"]} == {"run-0002"}
        assert api.shared_read(
            "/api/sessions/messages/locate",
            {**identity, "session_id": "session-0000", "anchor": anchor},
        ) == (400, {"code": "invalid_anchor"})


@pytest.mark.parametrize(
    "session_id,run_id", [("", ""), ("session/a?b#%中文", "run/a?b#%中文")]
)
def test_new_reads_cross_real_guarded_http_without_mutating_or_leaking(
    tmp_path, monkeypatch, session_id, run_id
):
    from urllib.parse import quote, urlencode

    from agent_alfred.evals.deterministic.test_memory_http import prepared_dashboard
    from agent_alfred.evals.deterministic.test_memory_mirrors import save
    from agent_alfred.evals.deterministic.test_web_http import _get, _request

    dashboard, model = prepared_dashboard(
        tmp_path, monkeypatch, [GATE, "complete reply"]
    )
    try:
        host = dashboard.host
        identity, _ = run(host)
        conn = host._conn
        conn.execute("UPDATE sessions SET session_id=?", (session_id,))
        conn.execute(
            "UPDATE runs SET session_id=?,run_id=? WHERE run_id=?",
            (session_id, run_id, identity),
        )
        conn.execute(
            "UPDATE agent_log SET session_id=?,run_id=? WHERE run_id=?",
            (session_id, run_id, identity),
        )
        conn.commit()
        save(host.memory_service)
        before = (host.snapshot(), len(model.requests))

        def get(path, params):
            head, body = _request(
                dashboard.port, _get(dashboard.port, path + "?" + urlencode(params))
            )
            assert b"Cache-Control: no-store" in head
            assert b"X-Content-Type-Options: nosniff" in head
            assert b"Content-Security-Policy:" in head
            assert b"Access-Control-Allow-Origin" not in head
            return int(head.split()[1]), json.loads(body)

        instance = {"process_instance_id": host.process_instance_id}
        cases = {
            "/api/overview/period": {"range": "7d", "timezone": "Asia/Shanghai"},
            "/api/overview/memory-counts": {
                "expected_memory_revision": str(host.memory_service.memory_revision)
            },
            "/api/overview/recent-runs": {},
            "/api/sessions/locate": {"session_id": session_id},
            "/api/sessions/runs/locate": {"session_id": session_id, "run_id": run_id},
            "/api/mainbar/locate": {"session_id": session_id, "run_id": run_id},
        }
        for path, params in cases.items():
            status, result = get(path, {**instance, **params})
            assert status == 200, (path, result)
            assert result["process_instance_id"] == host.process_instance_id
            assert result["schema_version"] == 1
            assert "observed_at" in result
            assert get(path, params) == (400, {"code": "missing_process_instance_id"})
            assert get(path, {**instance, **params, "body": "no"}) == (
                400,
                {"code": "invalid_input"},
            )
        assert (
            get(
                "/api/runs/locate/" + quote(run_id, safe=""),
                {**instance, "filter": "all"},
            )[1]["target"]["run_id"]
            == run_id
        )
        assert get(
            "/api/overview/period", {**instance, "range": "all", "timezone": "UTC"}
        ) == (400, {"code": "invalid_range"})
        assert get(
            "/api/overview/period", {**instance, "range": "7d", "timezone": "bad"}
        ) == (400, {"code": "invalid_timezone"})
        assert get(
            "/api/overview/memory-counts", {**instance, "expected_memory_revision": "0"}
        ) == (409, {"code": "memory_changed"})
        assert get(
            "/api/overview/recent-runs", {"process_instance_id": "previous"}
        ) == (409, {"code": "process_context_expired"})
        assert get(
            "/api/mainbar/locate",
            {
                "process_instance_id": "previous",
                "session_id": session_id,
                "run_id": run_id,
            },
        ) == (409, {"code": "reply_context_expired"})
        assert (host.snapshot(), len(model.requests)) == before
    finally:
        assert dashboard.close()
