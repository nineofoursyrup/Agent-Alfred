"""ADR-0028: durable admission, not HTTP receipt, owns chat visibility."""

import sqlite3
import threading

import pytest

from agent_alfred import schema
from agent_alfred.evals.deterministic._admission_test_helpers import v3_database
from agent_alfred.evals.deterministic._web_runtime_test_helpers import (
    build_runtime_host,
    dashboard_api,
)
from agent_alfred.runtime.host import SubmitRequest


def test_v4_preserves_v3_rows_and_classifies_only_positive_admission_evidence():
    conn = v3_database()
    try:
        for index, (run_id, phase, started_at, telemetry) in enumerate(
            (
                ("unknown", "finished", None, None),
                ("queued", "accepted", None, None),
                ("started", "running", "start", None),
                ("recorded", "finished", None, "{}"),
                ("message", "finished", None, None),
            ),
            1,
        ):
            conn.execute(
                """INSERT INTO runs VALUES (
                    ?, 'chat', NULL, 'web', NULL, ?, ?, ?, 'accept', ?, ?, ?, ?
                )""",
                (
                    run_id,
                    run_id,
                    phase,
                    "interrupted" if phase == "finished" else None,
                    started_at,
                    "finish" if phase == "finished" else None,
                    index,
                    telemetry,
                ),
            )
        conn.execute(
            """INSERT INTO agent_log
               (session_id, role, content, source, created_at, run_id)
               VALUES ('historic', 'user', '[]', 'web', 'original time', 'message')"""
        )
        conn.commit()
        original_runs = conn.execute("SELECT * FROM runs ORDER BY run_id").fetchall()
        original_messages = conn.execute("SELECT * FROM agent_log").fetchall()
        original_versions = conn.execute("SELECT * FROM schema_migrations").fetchall()

        schema.migrate(conn)

        assert conn.execute(
            "SELECT run_id, admission_state FROM runs ORDER BY run_id"
        ).fetchall() == [
            ("message", "admitted"),
            ("queued", "unconfirmed"),
            ("recorded", "admitted"),
            ("started", "admitted"),
            ("unknown", "unconfirmed"),
        ]
        upgraded = conn.execute("SELECT * FROM runs ORDER BY run_id").fetchall()
        assert [row[:-1] for row in upgraded] == original_runs
        assert conn.execute("SELECT * FROM agent_log").fetchall() == original_messages
        assert (
            conn.execute(
                "SELECT * FROM schema_migrations WHERE version <= 3"
            ).fetchall()
            == original_versions
        )
        schema.migrate(conn)
        assert conn.execute("SELECT * FROM runs ORDER BY run_id").fetchall() == upgraded
    finally:
        conn.close()


def test_refused_handoff_stays_out_of_chat_and_title_after_restart():
    def refuse(_item):
        raise RuntimeError("controlled handoff refusal")

    host, conn = build_runtime_host(["must not execute"], publish_work=refuse)
    restored = sqlite3.connect(":memory:", check_same_thread=False)
    recovered_host = None
    try:
        schema.insert_session(conn, session_id="s1", created_at="creation")
        conn.commit()
        api = dashboard_api(host)
        outcome = api.submit({"message": "NOT ADMITTED", "session_id": "s1"})
        assert (outcome.status, outcome.code) == (503, "admission_failed")
        conn.backup(restored)
        recovered_host, _ = build_runtime_host(["must not execute"], conn=restored)
        recovered_host.recover()
        for facade in (api, dashboard_api(recovered_host)):
            status, page = facade.session_runs({"session_id": "s1"})
            assert status == 200
            assert page["runs"] == []
            status, inbox = facade.session_inbox({})
            assert status == 200
            assert inbox["sessions"][0]["title"] == "新会话 · creation"
            status, runs = facade.runs_page({"filter": "all"})
            assert status == 200
            assert len(runs["runs"]) == 1
            assert runs["runs"][0]["outcome"] == "interrupted"
    finally:
        assert host.close(timeout=2)
        if recovered_host is not None:
            assert recovered_host.close(timeout=2)
        conn.close()
        restored.close()


def test_unconfirmed_run_is_labelled_in_runs_without_hiding_its_session():
    host, conn = build_runtime_host(["unused"])
    try:
        schema.insert_session(conn, session_id="legacy", created_at="original creation")
        conn.execute(
            """INSERT INTO runs (
                run_id, purpose, session_id, gateway, prompt_preview, phase,
                outcome, accepted_at, finished_at, activity_revision
            ) VALUES ('unknown', 'chat', 'legacy', 'web', 'UNCONFIRMED',
                      'finished', 'interrupted', 'accepted', 'finished', 2)"""
        )
        conn.execute(
            """INSERT INTO agent_log (session_id, role, content, source, created_at)
               VALUES ('legacy', 'user', '[{"type":"text","text":"original"}]',
                       'cli', 'original time')"""
        )
        conn.commit()
        api = dashboard_api(host)
        status, page = api.runs_page({"filter": "all"})
        assert status == 200
        assert len(page["runs"]) == 1
        assert page["runs"][0]["admission_state"] == "unconfirmed"
        assert page["runs"][0]["admission_label"] == "准入未确认"
        status, chat = api.session_runs({"session_id": "legacy"})
        assert status == 200
        assert chat["runs"] == []
        status, inbox = api.session_inbox({})
        assert status == 200
        assert inbox["sessions"][0]["title"] == "新会话 · original creation"
        status, messages = api.session_messages("legacy", {})
        assert status == 200
        assert messages["messages"][0]["run_id"] is None
        assert "original" in repr(messages["messages"])
    finally:
        assert host.close(timeout=2)
        conn.close()


class _AdmissionCommitFault:
    """Real SQLite with one controlled lost commit return / read failure."""

    def __init__(self, inner, *, after_commit, fail_read=False):
        self.inner = inner
        self.after_commit = after_commit
        self.fail_read = fail_read
        self.armed = False
        self.injected = False
        self.failure = RuntimeError("admission commit interrupted")

    def execute(self, sql, parameters=()):
        if "UPDATE runs SET admission_state = 'admitted'" in sql:
            self.armed = True
        if (
            self.injected
            and self.fail_read
            and sql.startswith("SELECT admission_state")
        ):
            self.fail_read = False
            raise sqlite3.OperationalError("one unavailable admission read")
        return self.inner.execute(sql, parameters)

    def commit(self):
        if self.armed and not self.injected:
            self.injected = True
            if self.after_commit:
                self.inner.commit()
            raise self.failure
        return self.inner.commit()

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_failed_admission_commit_cancels_work_and_persists_rejection():
    inner = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(inner)
    fault = _AdmissionCommitFault(inner, after_commit=False)
    host, _ = build_runtime_host(["must not execute"], conn=fault)
    host.start()
    try:
        api = dashboard_api(host)
        session_id = api.create_session().session_id
        outcome = api.submit({"message": "refused", "session_id": session_id})
        assert (outcome.status, outcome.code) == (503, "admission_failed")
        assert host.close(timeout=2), "cancelled queue cell did not release the worker"
        status, page = api.runs_page({})
        assert status == 200
        assert len(page["runs"]) == 1
        assert page["runs"][0]["admission_state"] == "rejected"
        assert page["runs"][0]["outcome"] == "interrupted"
        assert page["runs"][0]["started_at"] is None
        status, messages = api.session_messages(session_id, {})
        assert status == 200
        assert messages["messages"] == []
    finally:
        assert host.close(timeout=2)
        inner.close()


def test_restart_rejects_unpublished_pending_but_does_not_reclassify_old_unknown():
    host, conn = build_runtime_host(["must not execute"])
    try:
        for run_id, admission_state in (("pending", "pending"), ("old", "unconfirmed")):
            conn.execute(
                """INSERT INTO runs (run_id, purpose, gateway, phase,
                   accepted_at, activity_revision, admission_state)
                   VALUES (?, 'chat', 'web', 'accepted', 'accepted', ?, ?)""",
                (run_id, 2 if run_id == "old" else 1, admission_state),
            )
        conn.execute("UPDATE activity_clock SET next_revision = 3")
        conn.commit()
        host.start()
        assert host.close(timeout=2)
        status, page = dashboard_api(host).runs_page({})
        assert status == 200
        by_id = {run["run_id"]: run for run in page["runs"]}
        assert by_id["pending"]["admission_state"] == "rejected"
        assert by_id["old"]["admission_state"] == "unconfirmed"
        assert all(run["outcome"] == "interrupted" for run in by_id.values())
        assert all(run["started_at"] is None for run in by_id.values())
    finally:
        assert host.close(timeout=2)
        conn.close()


@pytest.mark.parametrize(
    "failure_type", (RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit)
)
@pytest.mark.parametrize("fail_read", (False, True))
def test_lost_admission_commit_still_releases_execution(failure_type, fail_read):
    inner = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(inner)
    fault = _AdmissionCommitFault(inner, after_commit=True, fail_read=fail_read)
    fault.failure = failure_type("admission commit return lost")
    completed = threading.Event()
    host, _ = build_runtime_host(
        ["one reply"],
        conn=fault,
        snapshot_listener=lambda state: (
            completed.set() if state.coordinator_state == "idle" else None
        ),
    )
    host.start()
    try:
        with pytest.raises(BaseException) as caught:
            host.submit(SubmitRequest(message="one request"))
        assert caught.value is fault.failure or caught.value.__cause__ is fault.failure
        assert completed.wait(2), "durably admitted execution was never released"
        status, inbox = dashboard_api(host).session_inbox({})
        assert status == 200
        session_id = inbox["sessions"][0]["session_id"]
        status, page = dashboard_api(host).session_runs({"session_id": session_id})
        assert status == 200
        assert len(page["runs"]) == 1
        assert page["runs"][0]["outcome"] == "completed"
    finally:
        assert host.close(timeout=2)
        inner.close()


def test_admitted_unstarted_run_remains_chat_after_restart_without_execution():
    handed_off = []
    host, conn = build_runtime_host(
        ["must not execute"], publish_work=handed_off.append
    )
    restored = sqlite3.connect(":memory:", check_same_thread=False)
    recovered_host = None
    try:
        schema.insert_session(conn, session_id="s1", created_at="creation")
        conn.commit()
        outcome = dashboard_api(host).submit(
            {"message": "ADMITTED", "session_id": "s1"}
        )
        assert outcome.status == 202
        assert len(handed_off) == 1
        conn.backup(restored)
        recovered_host, _ = build_runtime_host(["must not execute"], conn=restored)
        recovered_host.recover()
        status, page = dashboard_api(recovered_host).session_runs(
            {"session_id": outcome.session_id}
        )
        assert status == 200
        assert [run["run_id"] for run in page["runs"]] == [outcome.run_id]
        assert page["runs"][0]["started_at"] is None
        assert page["runs"][0]["outcome"] == "interrupted"
        status, inbox = dashboard_api(recovered_host).session_inbox({})
        assert status == 200
        assert inbox["sessions"][0]["title"] == "ADMITTED"
        status, messages = dashboard_api(recovered_host).session_messages(
            outcome.session_id, {}
        )
        assert status == 200
        assert messages["messages"] == []
    finally:
        assert host.close(timeout=2)
        if recovered_host is not None:
            assert recovered_host.close(timeout=2)
        conn.close()
        restored.close()
