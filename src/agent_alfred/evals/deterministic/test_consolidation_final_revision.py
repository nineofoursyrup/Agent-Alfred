"""Actual Host/broker terminal readback must advance memory invalidation."""

import json
import threading

import pytest

from agent_alfred.evals.deterministic.test_memory_consolidation_service import PLAN
from agent_alfred.evals.deterministic.test_memory_http import (
    prepared_dashboard,
    read,
    seed_sources,
)
from agent_alfred.gateway.web.connection import FakeConnection
from agent_alfred.runtime.recording import RunRecorder


class Wire(FakeConnection):
    def __init__(self):
        super().__init__()
        self.condition = threading.Condition()
        self.memory = []
        self.lifecycle = []

    def write(self, data):
        with self.condition:
            super().write(data)
            for frame in data.split(b"\n\n"):
                if b"data: " not in frame:
                    continue
                if b"event: memory_patch" in frame:
                    self.memory.append(json.loads(frame.split(b"data: ")[1]))
                if b"event: state_patch" in frame:
                    self.lifecycle.append(json.loads(frame.split(b"data: ")[1]))
            self.condition.notify_all()

    def through(self, lane, revision, key):
        with self.condition:
            assert self.condition.wait_for(
                lambda: any(
                    item.get(key, -1) >= revision for item in getattr(self, lane)
                ),
                timeout=10,
            ), (lane, revision, getattr(self, lane))


@pytest.mark.parametrize("batch_status", ["succeeded", "awaiting_approval", "failed"])
def test_final_run_usage_has_a_new_persisted_revision_and_wire_patch(
    tmp_path, monkeypatch, batch_status
):
    dashboard, model = prepared_dashboard(tmp_path, monkeypatch, [PLAN])
    entered = threading.Event()
    release = threading.Event()
    original = RunRecorder._reconcile_finalize
    observed = {}
    try:
        host = dashboard.host
        if batch_status == "failed":
            model._script = ["invalid JSON"]
        elif batch_status == "awaiting_approval":
            from agent_alfred.evals.deterministic.test_memory_mirrors import save

            saved = save(host.memory_service)
            model._script = [
                json.dumps(
                    {
                        "semantic": [
                            {
                                "action": "update",
                                "id": saved["memory_id"],
                                "subject": "food",
                                "fact": "new preference",
                            }
                        ],
                        "episode_summary": "Preference changed.",
                    }
                )
            ]
        outcome = "failed" if batch_status == "failed" else "completed"
        seed_sources(dashboard)
        wire = Wire()
        dashboard.broker.connect(connection=wire)

        def paused(recorder, item, **kwargs):
            # Immediately before _finalize's transaction/lock, so real HTTP
            # can read the already committed batch while recording is paused.
            if item.request.purpose == "consolidation":
                entered.set()
                assert release.wait(10)
            return original(recorder, item, **kwargs)

        monkeypatch.setattr(RunRecorder, "_reconcile_finalize", paused)
        accepted = host.generate_consolidation("s")
        assert accepted.kind == "accepted"
        assert entered.wait(10)
        status, before = read(dashboard, "/api/memory/consolidation")
        assert status == 200
        batch = before["batches"][0]
        assert batch["status"] == batch_status
        assert batch["run"]["outcome"] is None and batch["model_usage"] is None
        revision = before["memory_revision"]
        wire.through("memory", revision, "memory_revision")
        before_patch = wire.memory[-1]["memory_revision"]
        release.set()
        assert host.wait(accepted.run_id, timeout=10).outcome == outcome
        status, after = read(dashboard, "/api/memory/consolidation")
        assert status == 200
        final_batch = after["batches"][0]
        assert final_batch["run"]["outcome"] == outcome
        assert final_batch["run"]["finished_at"] is not None
        assert len(final_batch["model_usage"]) == 1
        # A real later lifecycle wire frame fences dispatcher/writer progress;
        # no timeout/sleep is used to infer the absence of a memory patch.
        wire.through("lifecycle", host.snapshot().state_revision, "state_revision")
        observed.update(
            before_revision=revision,
            after_revision=after["memory_revision"],
            before_wire_revision=before_patch,
            after_wire_revision=wire.memory[-1]["memory_revision"],
            before_run=batch["run"],
            after_run=final_batch["run"],
            before_usage=batch["model_usage"],
            after_usage=final_batch["model_usage"],
            model_requests=len(model.requests),
        )
    finally:
        release.set()
        monkeypatch.setattr(RunRecorder, "_reconcile_finalize", original)
        observed["dashboard_closed"] = dashboard.close()
        print(json.dumps(observed, sort_keys=True), flush=True)
    assert observed["dashboard_closed"]
    assert after["memory_revision"] > revision, (
        "Run outcome/usage changed without a new revision"
    )
    assert observed["after_wire_revision"] > before_patch, (
        "No terminal memory_patch on the actual wire"
    )


def test_v16_run_evidence_revision_is_transactional_and_chat_is_not_consolidation(
    monkeypatch,
):
    import sqlite3

    from agent_alfred import schema

    conn = sqlite3.connect(":memory:")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:15])
            schema.migrate(conn)
        original = conn.execute("SELECT name,sql FROM sqlite_master").fetchall()
        schema.migrate(conn)
        for name, sql in original:
            assert conn.execute(
                "SELECT sql FROM sqlite_master WHERE name=?", (name,)
            ).fetchone() == (sql,)
        for purpose in ("chat", "consolidation"):
            schema.insert_accepted_run(
                conn,
                run_id=purpose,
                purpose=purpose,
                session_id=None,
                gateway="cli",
                accepted_at="2026-09-10T00:00:00Z",
            )
        conn.commit()

        def revision():
            return conn.execute("SELECT revision FROM memory_revision").fetchone()[0]

        before = revision()
        conn.execute("UPDATE runs SET telemetry='{}' WHERE run_id='consolidation'")
        assert revision() == before + 1
        conn.rollback()
        assert revision() == before
        conn.execute("UPDATE runs SET telemetry='{}' WHERE run_id='chat'")
        conn.commit()
        assert revision() == before
        conn.execute("UPDATE runs SET telemetry='{}' WHERE run_id='consolidation'")
        conn.commit()
        assert revision() == before + 1
        conn.execute("UPDATE runs SET telemetry='{}' WHERE run_id='consolidation'")
        conn.commit()
        assert revision() == before + 1
    finally:
        conn.close()


@pytest.mark.parametrize("batch_status", ["succeeded", "awaiting_approval", "failed"])
def test_restart_publishes_recovered_run_evidence_without_model_calls(
    tmp_path,
    monkeypatch,
    batch_status,
):
    import sqlite3

    from agent_alfred.evals.deterministic.test_memory_http import generate
    from agent_alfred.evals.deterministic.test_memory_mirrors import save

    dashboard, model = prepared_dashboard(tmp_path, monkeypatch, [PLAN])
    try:
        if batch_status == "failed":
            model._script = ["invalid JSON"]
        elif batch_status == "awaiting_approval":
            saved = save(dashboard.host.memory_service)
            model._script = [
                json.dumps(
                    {
                        "semantic": [
                            {
                                "action": "update",
                                "id": saved["memory_id"],
                                "subject": "food",
                                "fact": "new preference",
                            }
                        ],
                        "episode_summary": "Preference changed.",
                    }
                )
            ]
        seed_sources(dashboard)
        batch = generate(dashboard)
        assert batch["status"] == batch_status
        db_path = dashboard.host._conn.execute("PRAGMA database_list").fetchone()[2]
    finally:
        assert dashboard.close()
    # Persist precisely the crash window: batch committed, Run final recording absent.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE runs SET phase='running',outcome=NULL,finished_at=NULL,"
            "telemetry=NULL WHERE run_id=?",
            (batch["generation_run_id"],),
        )
        conn.commit()
        before = conn.execute("SELECT revision FROM memory_revision").fetchone()[0]
    finally:
        conn.close()
    dashboard, model = prepared_dashboard(tmp_path, monkeypatch, [])
    try:
        wire = Wire()
        dashboard.broker.connect(connection=wire)
        status, body = read(dashboard, "/api/memory/consolidation")
        assert status == 200
        assert body["memory_revision"] > before
        recovered = body["batches"][0]
        assert recovered["status"] == batch_status
        assert recovered["run"]["outcome"] == "interrupted"
        assert recovered["run"]["finished_at"] is not None
        assert recovered["model_usage"] is None
        wire.through("memory", body["memory_revision"], "memory_revision")
        assert model.requests == []
    finally:
        assert dashboard.close()
