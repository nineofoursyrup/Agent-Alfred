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
        assert located["aggregation"] is None
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
        assert record["aggregation"] == row["aggregation"]
        assert model.requests == []


@pytest.mark.parametrize("produce_reply", [False, True])
def test_exact_aggregation_retains_graph_facts_before_and_after_recording(
    tmp_path, produce_reply
):
    from agent_alfred.evals.deterministic._web_runtime_test_helpers import (
        SelectiveLatch,
    )
    from agent_alfred.evals.deterministic.test_aggregation import save_fact
    from agent_alfred.evals.deterministic.test_runtime_skills import runtime

    saving = SelectiveLatch()
    saving.arm()
    expected = {
        "graph_result": "Completed" if produce_reply else "NoAction",
        "reply_disposition": "reply" if produce_reply else "no_reply",
        "reason_code": None if produce_reply else "sources_not_selected",
        "evidence_state": "known",
    }
    with runtime(
        tmp_path,
        ["草稿 [[S1]]"] if produce_reply else [],
        before_recording_commit=saving,
    ) as (host, model, _):
        try:
            if produce_reply:
                save_fact(host)
            session = host.create_session()
            accepted = host.aggregate(
                session_id=session,
                goal="整理 coffee",
                keywords="coffee",
                sources=("semantic",) if produce_reply else (),
            )
            assert saving.entered.wait(2)
            api = DashboardApi(facade=host)
            params = {
                "process_instance_id": host.process_instance_id,
                "session_id": session,
                "run_id": accepted.run_id,
            }
            for saved in (False, True):
                if saved:
                    saving.release()
                    assert host.wait(accepted.run_id).outcome == "completed"
                status, located = api.shared_read("/api/mainbar/locate", params)
                assert status == 200
                assert located["aggregation"] == expected
                assert located["reply_text"] == (
                    "草稿 [[S1]]" if produce_reply else None
                )
                assert located["recording_state"] == (
                    "recorded" if saved else "pending"
                )
                assert located["source"] == (
                    "recorded_pair" if saved else "unrecorded_projection"
                )
            assert len(model.requests) == int(produce_reply)
        finally:
            saving.release()


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


@pytest.mark.parametrize("same_revision", [True, False])
def test_session_run_location_preserves_composite_boundary_and_cursor(
    tmp_path, same_revision
):
    with ops_host(tmp_path, [GATE, "answer"]) as (host, model, conn, _):
        original, _ = run(host)
        session = host.list_runs().runs[0].session_id
        keys = ["tie-a", "tie-b", "tie-c"]
        for index, identity in enumerate(keys):
            conn.execute(
                "INSERT INTO runs (run_id,purpose,session_id,gateway,prompt_preview,"
                "phase,outcome,accepted_at,finished_at,activity_revision,telemetry,"
                "admission_state) SELECT ?,purpose,session_id,gateway,prompt_preview,"
                "phase,outcome,accepted_at,finished_at,?,telemetry,admission_state "
                "FROM runs WHERE run_id=?",
                (identity, 100 if same_revision else 100 + index, original),
            )
        conn.commit()
        before = (host.snapshot(), len(model.requests))
        api = DashboardApi(facade=host)
        for index, target in enumerate(keys):
            for limit in (1, 2):
                status, located = api.shared_read(
                    "/api/sessions/runs/locate",
                    {
                        "process_instance_id": host.process_instance_id,
                        "session_id": session,
                        "run_id": target,
                        "limit": str(limit),
                    },
                )
                assert status == 200
                assert located["target"]["run_id"] == target
                assert located["target"]["placement"] == "page"
                assert [r["run_id"] for r in located["runs"]] == list(
                    reversed(keys[index : index + limit])
                )
                # The normal descending reader continues strictly below the
                # located target, even when adjacent records share a revision.
                cursor, tail = located["next_cursor"], []
                while cursor is not None:
                    status, page = api.session_runs(
                        {
                            "session_id": session,
                            "cursor": cursor,
                            "limit": "2",
                        }
                    )
                    assert status == 200
                    tail.extend(r["run_id"] for r in page["runs"])
                    assert len(tail) <= len(keys)
                    cursor = page["next_cursor"]
                assert tail == [*reversed(keys[:index]), original]
        assert (host.snapshot(), len(model.requests)) == before


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


def test_source_waiting_and_pinned_targets_never_claim_a_history_cursor(tmp_path):
    with ops_host(tmp_path, [GATE, "first", GATE, "second"]) as (host, _, conn, _):
        first, _ = run(host)
        session = host.list_runs().runs[0].session_id
        second, _ = run(host)
        # A second admitted row makes accidental historical neighbors observable.
        conn.execute("UPDATE runs SET session_id=? WHERE run_id=?", (session, second))
        conn.execute(
            "INSERT INTO agent_log(session_id,role,content,source,created_at) "
            "VALUES (?, 'user', '[]', 'web', 'historic')",
            (session,),
        )
        conn.commit()
        api = DashboardApi(facade=host)
        anchor = next(
            m["message_anchor"]
            for m in api.session_messages(session, {})[1]["messages"]
            if m["run_id"] is None
        )
        conn.execute(
            "UPDATE runs SET phase='running',outcome=NULL,finished_at=NULL "
            "WHERE run_id=?",
            (first,),
        )
        conn.commit()
        params = {
            "process_instance_id": host.process_instance_id,
            "session_id": session,
        }
        status, messages = api.shared_read(
            "/api/sessions/messages/locate", {**params, "anchor": anchor}
        )
        assert status == 200
        assert messages["target"]["placement"] == "waiting"
        assert messages["runs_pending"] is True
        assert messages["messages"] == []
        assert messages["next_cursor"] is not None
        status, located = api.shared_read(
            "/api/sessions/runs/locate", {**params, "run_id": first}
        )
        assert status == 200
        assert located["target"]["placement"] == "pinned"
        assert [r["run_id"] for r in located["runs"]] == [first]
        assert located["next_cursor"] is None
        status, inbox = api.shared_read("/api/sessions/locate", params)
        assert status == 200
        assert inbox["target"]["placement"] == "pinned"
        assert inbox["sessions"] == []
        assert inbox["next_cursor"] is None
        conn.execute("DELETE FROM agent_log WHERE run_id IS NULL")
        conn.commit()
        assert api.shared_read(
            "/api/sessions/messages/locate", {**params, "anchor": anchor}
        ) == (404, {"code": "source_target_unavailable"})


def test_memory_revision_changed_before_http_response_discards_the_count(
    tmp_path, monkeypatch
):
    from urllib.parse import urlencode

    from agent_alfred.evals.deterministic.test_memory_http import prepared_dashboard
    from agent_alfred.evals.deterministic.test_memory_mirrors import save
    from agent_alfred.evals.deterministic.test_web_http import _get, _request

    dashboard, model = prepared_dashboard(tmp_path, monkeypatch, [])
    try:
        host = dashboard.host
        before = host.memory_service.memory_revision
        observe = host.read_observation

        def change_before_envelope():
            assert save(host.memory_service, operation="race")["status"] == "saved"
            return observe()

        monkeypatch.setattr(host, "read_observation", change_before_envelope)
        head, body = _request(
            dashboard.port,
            _get(
                dashboard.port,
                "/api/overview/memory-counts?"
                + urlencode(
                    {
                        "process_instance_id": host.process_instance_id,
                        "expected_memory_revision": before,
                    }
                ),
            ),
        )
        assert head.startswith(b"HTTP/1.1 409")
        assert json.loads(body) == {"code": "memory_changed"}
        assert host.memory_service.memory_revision > before
        assert model.requests == []
    finally:
        assert dashboard.close()


def test_message_anchor_sqlite_integer_domain_is_validated_at_http(
    tmp_path, monkeypatch
):
    from urllib.parse import urlencode

    from agent_alfred.evals.deterministic.test_memory_http import prepared_dashboard
    from agent_alfred.evals.deterministic.test_web_http import _get, _request
    from agent_alfred.runtime.cursor import MAX_SQLITE_CURSOR_POSITION, encode_cursor

    dashboard, model = prepared_dashboard(tmp_path, monkeypatch, [])
    try:
        host = dashboard.host
        session = host.create_session()
        before = host.snapshot()
        for position, status, code in (
            (MAX_SQLITE_CURSOR_POSITION + 1, 400, "invalid_anchor"),
            (MAX_SQLITE_CURSOR_POSITION, 404, "source_target_unavailable"),
            (0, 400, "invalid_anchor"),
            (True, 400, "invalid_anchor"),
        ):
            anchor = encode_cursor(
                {
                    "v": 1,
                    "k": "message_anchor",
                    "s": session,
                    "seg": "historic",
                    "id": position,
                }
            )
            head, body = _request(
                dashboard.port,
                _get(
                    dashboard.port,
                    "/api/sessions/messages/locate?"
                    + urlencode(
                        {
                            "process_instance_id": host.process_instance_id,
                            "session_id": session,
                            "anchor": anchor,
                        }
                    ),
                ),
            )
            assert int(head.split()[1]) == status
            assert json.loads(body) == {"code": code}
        assert model.requests == []
        assert host.snapshot() == before
    finally:
        assert dashboard.close()


def test_http_disconnect_releases_period_transaction_and_keeps_ops_snapshots(
    tmp_path, monkeypatch
):
    import copy
    import socket
    import threading
    from urllib.parse import urlencode

    from agent_alfred.evals.deterministic.test_memory_http import prepared_dashboard
    from agent_alfred.evals.deterministic.test_web_http import _get
    from agent_alfred.runtime.accounting import AccountingError

    dashboard, _ = prepared_dashboard(tmp_path, monkeypatch, [GATE, "done"])
    entered, release, completed = (threading.Event() for _ in range(3))
    client = None
    try:
        host = dashboard.host
        run(host)
        host.accounting_snapshot({"range": "all", "timezone": "UTC"})
        before = copy.deepcopy(host._accounting.snapshots)
        period = host.overview_period
        outcomes = []

        def observe_period(filters, *, cancelled):
            def at_real_transaction():
                if host._conn.in_transaction:
                    entered.set()
                    assert release.wait(3)
                return cancelled()

            try:
                return period(filters, cancelled=at_real_transaction)
            except AccountingError as exc:
                outcomes.append(str(exc))
                raise
            finally:
                completed.set()

        monkeypatch.setattr(host, "overview_period", observe_period)
        client = socket.create_connection(("127.0.0.1", dashboard.port), timeout=3)
        client.sendall(
            _get(
                dashboard.port,
                "/api/overview/period?"
                + urlencode(
                    {
                        "process_instance_id": host.process_instance_id,
                        "range": "7d",
                        "timezone": "UTC",
                    }
                ),
            )
        )
        assert entered.wait(3), "period never entered the real read transaction"
        client.shutdown(socket.SHUT_RDWR)
        client.close()
        client = None
        release.set()
        assert completed.wait(3)
        assert outcomes == ["read_cancelled"]
        assert not host._conn.in_transaction
        assert host._accounting.snapshots == before
        assert period({"range": "7d", "timezone": "UTC"})["summary"]["run_count"] == 1
    finally:
        release.set()
        if client is not None:
            client.close()
        assert dashboard.close()
