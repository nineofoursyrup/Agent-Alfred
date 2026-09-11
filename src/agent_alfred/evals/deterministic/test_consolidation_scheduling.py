"""Saved-chat scheduling through the real Host, SQLite and offline transport."""

import threading

import pytest

from agent_alfred.evals.deterministic.test_memory_consolidation_runtime import (
    _traced_host,
)
from agent_alfred.evals.deterministic.test_memory_consolidation_service import PLAN
from agent_alfred.runtime.host import SubmitRequest

SKIP = '{"retrieve":false,"query":null,"reason_code":"greeting"}'


def test_saved_chat_schedules_one_async_run_and_releases_original_result(
    tmp_path, monkeypatch
):
    host, model, conn = _traced_host(tmp_path, [SKIP, "Noted.", SKIP, "Noted.", PLAN])
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = model.respond
    notify = host.notify_run_done

    def respond(request, **kwargs):
        if request.system[0].text.startswith("You consolidate"):
            entered.set()
            assert release.wait(5)
        return original(request, **kwargs)

    def notified(run_id):
        notify(run_id)
        row = conn.execute(
            "SELECT purpose FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row == ("consolidation",):
            finished.set()

    monkeypatch.setattr(model, "respond", respond)
    monkeypatch.setattr(host, "notify_run_done", notified)
    try:
        first = host.submit(SubmitRequest("I like coriander"))
        assert host.wait(first.run_id, timeout=5).outcome == "completed"
        assert not entered.is_set()
        second = host.submit(
            SubmitRequest("Still coriander", session_id=first.session_id)
        )
        assert host.wait(second.run_id, timeout=5).outcome == "completed"
        assert entered.wait(5), "threshold saved chat did not schedule consolidation"
        assert host.submit(SubmitRequest("busy")).kind == "run_in_progress"
        release.set()
        assert finished.wait(5)
        assert conn.execute(
            "SELECT count(*) FROM runs WHERE purpose='consolidation'"
        ).fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM agent_log").fetchone() == (4,)
        assert conn.execute(
            "SELECT count(*) FROM memory_consolidation_source_results"
        ).fetchone() == (2,)
        assert len(model.requests) == 5
    finally:
        release.set()
        host.close()
        conn.close()


def _observe_settlement(host, monkeypatch, conn):
    import queue

    settled = queue.Queue()
    callback = host._recorder._after_recorded

    def observed(run_id):
        try:
            return callback(run_id)
        finally:
            settled.put(run_id)

    monkeypatch.setattr(host._recorder, "_after_recorded", observed)
    notify = host.notify_run_done

    def notified(run_id):
        notify(run_id)
        row = conn.execute(
            "SELECT purpose FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row == ("consolidation",):
            settled.put(run_id)

    monkeypatch.setattr(host, "notify_run_done", notified)
    return settled


def _chat(host, settled, session_id=None, message="A normal chat"):
    result = host.submit(SubmitRequest(message, session_id=session_id))
    assert result.kind == "accepted", result
    assert host.wait(result.run_id, timeout=5).outcome == "completed"
    assert settled.get(timeout=5) == result.run_id
    return result


def _system(conn, settled):
    run_id = settled.get(timeout=5)
    row = conn.execute(
        "SELECT purpose,phase FROM runs WHERE run_id=?", (run_id,)
    ).fetchone()
    assert row == ("consolidation", "finished")
    return run_id


def test_one_opportunity_one_batch_and_later_chat_advances_backlog(
    tmp_path, monkeypatch
):
    from agent_alfred.evals.deterministic.test_memory_consolidation_service import (
        _complete_chat,
    )

    host, model, conn = _traced_host(
        tmp_path, [SKIP, "Noted", PLAN, SKIP, "Noted", PLAN]
    )
    settled = _observe_settlement(host, monkeypatch, conn)
    try:
        for n in range(5):
            _complete_chat(
                host.memory_service,
                conn,
                "backlog",
                f"source-{n}",
                "Coriander",
                "Noted",
            )
        first = _chat(host, settled)
        first_system = _system(conn, settled)
        assert conn.execute(
            "SELECT count(*) FROM memory_consolidation_source_results"
        ).fetchone() == (2,)
        assert len(model.requests) == 3
        assert conn.execute(
            "SELECT count(*) FROM runs WHERE purpose='consolidation'"
        ).fetchone() == (1,)
        second = _chat(host, settled)
        second_system = _system(conn, settled)
        assert second_system != first_system
        assert conn.execute(
            "SELECT count(*) FROM memory_consolidation_source_results"
        ).fetchone() == (4,)
        assert (
            host.memory_service.consolidation.session_status("backlog")[
                "unprocessed_count"
            ]
            == 1
        )
        assert conn.execute("SELECT count(*) FROM agent_log").fetchone() == (14,)
        assert conn.execute(
            "SELECT count(*) FROM memory_consolidation_triggers"
        ).fetchone() == (2,)
        assert first.session_id != second.session_id
    finally:
        host.close()
        conn.close()


def test_oldest_ready_opaque_order_survives_restart_and_blocker(tmp_path, monkeypatch):
    from agent_alfred.evals.deterministic.test_memory_consolidation_service import (
        _complete_chat,
    )

    host, model, conn = _traced_host(tmp_path, [])
    try:
        for session, text in [
            ("0-blocked", "x" * 70000),
            ("z-older", "Coriander"),
            ("a-newer", "Coriander"),
        ]:
            for n in range(2):
                _complete_chat(
                    host.memory_service,
                    conn,
                    session,
                    f"{session}-{2 - n}",
                    text,
                    "Noted",
                )
        host.close()
        conn.close()
        host, model, conn = _traced_host(
            tmp_path, [SKIP, "Noted", PLAN, SKIP, "Noted", PLAN]
        )
        assert model.requests == []
        ready = conn.execute(
            "SELECT session_id,ready_order,ready_at FROM "
            "memory_consolidation_ready ORDER BY ready_order"
        ).fetchall()
        assert [x[0] for x in ready] == ["0-blocked", "z-older", "a-newer"]
        host.close()
        conn.close()
        host, model, conn = _traced_host(
            tmp_path, [SKIP, "Noted", PLAN, SKIP, "Noted", PLAN]
        )
        assert (
            conn.execute(
                "SELECT session_id,ready_order,ready_at FROM "
                "memory_consolidation_ready ORDER BY ready_order"
            ).fetchall()
            == ready
        )
        settled = _observe_settlement(host, monkeypatch, conn)
        _chat(host, settled)
        _system(conn, settled)
        assert conn.execute(
            "SELECT session_id FROM memory_consolidation_batches"
        ).fetchall() == [("z-older",)]
        _chat(host, settled)
        _system(conn, settled)
        assert set(
            conn.execute(
                "SELECT session_id FROM memory_consolidation_batches"
            ).fetchall()
        ) == {("z-older",), ("a-newer",)}
        assert len(model.requests) == 6
        assert (
            host.memory_service.consolidation.session_status("0-blocked")["block"][
                "run_id"
            ]
            == "0-blocked-2"
        )
    finally:
        host.close()
        conn.close()


def test_below_threshold_sessions_do_not_combine(tmp_path, monkeypatch):
    host, model, conn = _traced_host(tmp_path, [SKIP, "Noted"] * 3)
    settled = _observe_settlement(host, monkeypatch, conn)
    try:
        for _ in range(3):
            session = _chat(host, settled).session_id
            assert (
                host.memory_service.consolidation.session_status(session)[
                    "unprocessed_count"
                ]
                == 1
            )
        assert len(model.requests) == 6
        assert conn.execute(
            "SELECT count(*) FROM runs WHERE purpose='consolidation'"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM memory_consolidation_ready"
        ).fetchone() == (0,)
    finally:
        host.close()
        conn.close()


def test_explicit_oversize_skip_preserves_history_and_rechecks_identity(
    tmp_path, monkeypatch
):
    from agent_alfred.evals.deterministic.test_memory_consolidation_service import (
        CONTEXT,
        _complete_chat,
    )

    host, model, conn = _traced_host(tmp_path, [SKIP, "Noted"])
    settled = _observe_settlement(host, monkeypatch, conn)
    try:
        for run, text in [
            ("oversize-A", "x" * 70000),
            ("oversize-B", "y" * 70000),
            ("small", "Coriander"),
        ]:
            _complete_chat(host.memory_service, conn, "blocked", run, text, "Noted")
        _chat(host, settled)
        service = host.memory_service.consolidation
        block = service.session_status("blocked")["block"]
        assert block["run_id"] == "oversize-A" and block["characters"] > block["limit"]
        assert conn.execute(
            "SELECT count(*) FROM runs WHERE purpose='consolidation'"
        ).fetchone() == (0,)
        first = service.skip_oversized(
            "blocked", "oversize-A", context=CONTEXT, operation_id="skip-A"
        )
        assert first["status"] == "skipped" and first["unprocessed_count"] == 2
        assert service.session_status("blocked")["block"]["run_id"] == "oversize-B"
        assert (
            service.skip_oversized(
                "blocked", "oversize-A", context=CONTEXT, operation_id="stale-A"
            )["error"]["code"]
            == "source_mismatch"
        )
        second = service.skip_oversized(
            "blocked", "oversize-B", context=CONTEXT, operation_id="skip-B"
        )
        assert second["unprocessed_count"] == 1
        assert service.session_status("blocked")["block"] is None
        assert (
            service.skip_oversized(
                "blocked", "small", context=CONTEXT, operation_id="not-oversize"
            )["error"]["code"]
            == "source_mismatch"
        )
        assert len(model.requests) == 2
        assert conn.execute("SELECT count(*) FROM agent_log").fetchone() == (8,)
        assert conn.execute("SELECT count(*) FROM facts").fetchone() == (0,)
    finally:
        host.close()
        conn.close()


@pytest.mark.parametrize("method", ["search", "list_recent"])
def test_candidate_read_failure_is_durable_and_does_not_auto_retry(
    tmp_path, monkeypatch, method
):
    import sqlite3

    from agent_alfred.evals.deterministic.test_memory_consolidation_service import (
        _complete_chat,
    )
    from agent_alfred.memory.semantic import SQLiteSemanticStore

    host, model, conn = _traced_host(tmp_path, [SKIP, "Noted", SKIP, "Noted"])
    settled = _observe_settlement(host, monkeypatch, conn)
    try:
        for n in range(2):
            _complete_chat(
                host.memory_service, conn, "ready", f"s-{n}", "Coriander", "Noted"
            )
        original = getattr(SQLiteSemanticStore, method)

        def failed(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite3.OperationalError("controlled partial read failure")

        with monkeypatch.context() as patch:
            patch.setattr(SQLiteSemanticStore, method, failed)
            _chat(host, settled)
            run = _system(conn, settled)
        batch = host.memory_service.consolidation.session_status("ready")["batch"]
        assert (
            batch["status"] == "failed" and batch["error_code"] == "storage_read_failed"
        )
        assert conn.execute(
            "SELECT generation_run_id FROM memory_consolidation_batches"
        ).fetchone() == (run,)
        assert conn.execute(
            "SELECT count(*) FROM memory_consolidation_sources"
        ).fetchone() == (2,)
        assert len(model.requests) == 2
        _chat(host, settled)
        assert len(model.requests) == 4
        assert conn.execute(
            "SELECT count(*) FROM runs WHERE purpose='consolidation'"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM memory_consolidation_source_results"
        ).fetchone() == (0,)
    finally:
        host.close()
        conn.close()


def test_source_inspection_error_is_visible_and_not_an_empty_queue(
    tmp_path, monkeypatch
):
    from agent_alfred.evals.deterministic.test_memory_consolidation_service import (
        _complete_chat,
    )

    host, model, conn = _traced_host(tmp_path, [SKIP, "Noted", SKIP, "Noted"])
    settled = _observe_settlement(host, monkeypatch, conn)
    try:
        for n in range(2):
            _complete_chat(
                host.memory_service, conn, "ready", f"s-{n}", "Coriander", "Noted"
            )
        forgetting = host.memory_service.forgetting
        original = forgetting.evaluate_history

        def fail(*args, **kwargs):
            if kwargs.get("purpose") == "consolidation_source":
                return {"error": {"code": "storage_read_failed"}}
            return original(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(forgetting, "evaluate_history", fail)
            _chat(host, settled)
        batch = host.memory_service.consolidation.session_status("ready")["batch"]
        assert (
            batch["status"] == "failed" and batch["error_code"] == "storage_read_failed"
        )
        _chat(host, settled)
        assert conn.execute(
            "SELECT count(*) FROM runs WHERE purpose='consolidation'"
        ).fetchone() == (0,)
        assert (
            host.memory_service.consolidation.session_status("ready")[
                "unprocessed_count"
            ]
            == 2
        )
        assert len(model.requests) == 4
    finally:
        host.close()
        conn.close()


@pytest.mark.parametrize("closing", [False, True])
def test_callback_return_loss_cannot_double_schedule_or_release_successor(
    tmp_path, monkeypatch, closing
):
    from agent_alfred.evals.deterministic.test_memory_consolidation_service import (
        _complete_chat,
    )

    host, model, conn = _traced_host(tmp_path, [SKIP, "Noted", PLAN])
    settled = _observe_settlement(host, monkeypatch, conn)
    entered, release = threading.Event(), threading.Event()
    submit, respond = host.submit, model.respond

    def submitted(request):
        result = submit(request)
        if request.purpose == "consolidation":
            raise KeyboardInterrupt("return lost after accepted handoff")
        return result

    def responding(request, **kwargs):
        if request.system[0].text.startswith("You consolidate"):
            entered.set()
            assert release.wait(5)
        return respond(request, **kwargs)

    monkeypatch.setattr(host, "submit", submitted)
    monkeypatch.setattr(model, "respond", responding)
    try:
        for n in range(5):
            _complete_chat(
                host.memory_service, conn, "backlog", f"s-{n}", "Coriander", "Noted"
            )
        chat = _chat(host, settled)
        # Retry of the same scheduling settlement is observable but consumes
        # no second opportunity. It is not a second user chat.
        assert settled.get(timeout=5) == chat.run_id
        assert entered.wait(5)
        assert submit(SubmitRequest("busy")).kind == "run_in_progress"
        assert conn.execute(
            "SELECT count(*) FROM runs WHERE purpose='consolidation'"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT status FROM memory_consolidation_triggers"
        ).fetchone() == ("accepted",)
        if closing:
            assert host.close(timeout=0) is False
        release.set()
        _system(conn, settled)
        if closing:
            assert host.close(timeout=5)
        assert len(model.requests) == 3
        assert host.snapshot().coordinator_state == "idle"
    finally:
        release.set()
        host.close()
        conn.close()


def test_default_threshold_is_ten_and_applies_per_session(tmp_path, monkeypatch):
    from agent_alfred.settings import Settings

    threshold = Settings().consolidation_source_threshold
    assert threshold == 10
    host, model, conn = _traced_host(
        tmp_path, [SKIP, "Noted"] * threshold + [PLAN], threshold=threshold
    )
    settled = _observe_settlement(host, monkeypatch, conn)
    try:
        session_id = None
        for n in range(threshold):
            session_id = _chat(host, settled, session_id).session_id
            if n + 1 < threshold:
                assert conn.execute(
                    "SELECT count(*) FROM runs WHERE purpose='consolidation'"
                ).fetchone() == (0,)
                assert (
                    host.memory_service.consolidation.session_status(session_id)[
                        "unprocessed_count"
                    ]
                    == n + 1
                )
        _system(conn, settled)
        assert len(model.requests) == 21
        assert conn.execute(
            "SELECT count(*) FROM memory_consolidation_source_results"
        ).fetchone() == (10,)
        assert conn.execute("SELECT count(*) FROM agent_log").fetchone() == (20,)
    finally:
        host.close()
        conn.close()


@pytest.mark.parametrize("terminal", ["failed", "unrecorded"])
def test_failed_or_unrecorded_chat_does_not_spend_an_opportunity(
    tmp_path, monkeypatch, terminal
):
    from agent_alfred.evals.deterministic.test_memory_consolidation_service import (
        _complete_chat,
    )

    script = (
        [SKIP, RuntimeError("offline chat failed")]
        if terminal == "failed"
        else [SKIP, "Noted"]
    )
    host, model, conn = _traced_host(tmp_path, script)
    try:
        for n in range(2):
            _complete_chat(
                host.memory_service, conn, "ready", f"s-{n}", "Coriander", "Noted"
            )
        if terminal == "unrecorded":
            conn.execute(
                "CREATE TRIGGER fail_record BEFORE INSERT ON agent_log BEGIN "
                "SELECT RAISE(ABORT,'recording fault'); END"
            )
            conn.commit()
        chat = host.submit(SubmitRequest("chat"))
        result = host.wait(chat.run_id, timeout=5)
        assert result.outcome == ("failed" if terminal == "failed" else "completed")
        assert conn.execute(
            "SELECT count(*) FROM memory_consolidation_triggers"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM runs WHERE purpose='consolidation'"
        ).fetchone() == (0,)
        assert host.snapshot().coordinator_state == (
            "recording_failed" if terminal == "unrecorded" else "idle"
        )
    finally:
        host.close()
        conn.close()


def test_close_before_automatic_admission_preserves_chat_and_readiness(
    tmp_path, monkeypatch
):
    from agent_alfred.evals.deterministic.test_memory_consolidation_service import (
        _complete_chat,
    )

    host, model, conn = _traced_host(tmp_path, [SKIP, "Noted"])
    entered, release = threading.Event(), threading.Event()
    submit = host.submit

    def submitting(request):
        if request.purpose == "consolidation":
            entered.set()
            assert release.wait(5)
        return submit(request)

    monkeypatch.setattr(host, "submit", submitting)
    try:
        for n in range(2):
            _complete_chat(
                host.memory_service, conn, "ready", f"s-{n}", "Coriander", "Noted"
            )
        chat = host.submit(SubmitRequest("chat"))
        assert entered.wait(5)
        assert host.close(timeout=0) is False
        release.set()
        assert host.wait(chat.run_id, timeout=5).outcome == "completed"
        assert host.close(timeout=5)
        assert conn.execute(
            "SELECT status,error_code FROM memory_consolidation_triggers"
        ).fetchone() == ("refused", "admission_failed")
        assert conn.execute(
            "SELECT count(*) FROM memory_consolidation_ready"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM runs WHERE purpose='consolidation'"
        ).fetchone() == (0,)
        assert len(model.requests) == 2
    finally:
        release.set()
        host.close()
        conn.close()


def test_claim_return_loss_is_not_permission_to_resubmit(tmp_path, monkeypatch):
    from agent_alfred.evals.deterministic.test_memory_consolidation_service import (
        _complete_chat,
    )

    host, model, conn = _traced_host(tmp_path, [SKIP, "Noted"])
    try:
        for n in range(2):
            _complete_chat(
                host.memory_service, conn, "ready", f"s-{n}", "Coriander", "Noted"
            )
        scheduling = host.memory_service.consolidation.scheduling
        claim = scheduling.claim

        def claimed(run_id):
            result = claim(run_id)
            if result is not None:
                raise KeyboardInterrupt("lost committed claim result")
            return result

        monkeypatch.setattr(scheduling, "claim", claimed)
        chat = host.submit(SubmitRequest("chat"))
        assert host.wait(chat.run_id, timeout=5).outcome == "completed"
        assert conn.execute(
            "SELECT status,generation_run_id FROM memory_consolidation_triggers"
        ).fetchone() == ("attempted", None)
        assert conn.execute(
            "SELECT count(*) FROM runs WHERE purpose='consolidation'"
        ).fetchone() == (0,)
        host.close()
        conn.close()
        host, model, conn = _traced_host(tmp_path, [SKIP, "Noted", PLAN])
        assert not model.requests
        assert conn.execute(
            "SELECT status FROM memory_consolidation_triggers"
        ).fetchone() == ("expired",)
        settled = _observe_settlement(host, monkeypatch, conn)
        _chat(host, settled)
        _system(conn, settled)
        assert len(model.requests) == 3
    finally:
        host.close()
        conn.close()


@pytest.mark.parametrize("pending", [False, True], ids=["failed", "awaiting"])
def test_unresolved_session_does_not_starve_other_ready_work(
    tmp_path, monkeypatch, pending
):
    import json

    from agent_alfred.evals.deterministic.test_memory_consolidation_service import (
        CONTEXT,
        _complete_chat,
    )

    host, model, conn = _traced_host(
        tmp_path, [SKIP, "Noted", "not-json", SKIP, "Noted", PLAN]
    )
    settled = _observe_settlement(host, monkeypatch, conn)
    try:
        memory = host.memory_service
        target = memory.execute(
            {
                "operation_id": "seed",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "upstream", "fact": "protected"},
            },
            CONTEXT,
        )["memory_id"]
        if pending:
            model._script[2] = json.dumps(
                {
                    "semantic": [
                        {
                            "action": "update",
                            "id": target,
                            "subject": "upstream",
                            "fact": "new fact",
                        }
                    ],
                    "episode_summary": "Proposed",
                }
            )
        for session in ["older", "newer"]:
            for n in range(2):
                _complete_chat(
                    memory, conn, session, f"{session}-{n}", "Coriander", "Noted"
                )
        _chat(host, settled)
        _system(conn, settled)
        batch = memory.consolidation.session_status("older")["batch"]
        assert batch["status"] == ("awaiting_approval" if pending else "failed")
        assert host.snapshot().coordinator_state == "idle"
        _chat(host, settled)
        _system(conn, settled)
        assert conn.execute(
            "SELECT session_id,status FROM memory_consolidation_batches ORDER BY rowid"
        ).fetchall() == [
            ("older", "awaiting_approval" if pending else "failed"),
            ("newer", "succeeded"),
        ]
        assert len(model.requests) == 6
        if pending:
            approved = memory.consolidation.approve(
                batch["batch_id"], 1, context=CONTEXT, operation_id="approve"
            )
            assert approved["status"] == "succeeded"
            assert len(model.requests) == 6
            assert conn.execute(
                "SELECT count(*) FROM runs WHERE purpose='consolidation'"
            ).fetchone() == (2,)
    finally:
        host.close()
        conn.close()


def test_fitting_complete_prefix_marks_only_selected_sources(tmp_path, monkeypatch):
    from agent_alfred.evals.deterministic.test_memory_consolidation_service import (
        _complete_chat,
    )

    host, model, conn = _traced_host(tmp_path, [SKIP, "Noted", PLAN])
    settled = _observe_settlement(host, monkeypatch, conn)
    try:
        for n in range(2):
            _complete_chat(
                host.memory_service, conn, "ready", f"s-{n}", "x" * 40000, "Noted"
            )
        _chat(host, settled)
        _system(conn, settled)
        assert conn.execute(
            "SELECT run_id FROM memory_consolidation_source_results"
        ).fetchall() == [("s-0",)]
        assert (
            host.memory_service.consolidation.session_status("ready")[
                "unprocessed_count"
            ]
            == 1
        )
        assert len(model.requests) == 3
        assert conn.execute("SELECT count(*) FROM agent_log").fetchone() == (6,)
    finally:
        host.close()
        conn.close()


def test_mixed_history_only_counts_recorded_permitted_complete_chats(
    tmp_path, monkeypatch
):
    from agent_alfred.evals.deterministic.test_memory_consolidation_service import (
        CONTEXT,
        _complete_chat,
    )

    host, model, conn = _traced_host(tmp_path, [SKIP, "Noted", PLAN])
    settled = _observe_settlement(host, monkeypatch, conn)
    try:
        memory = host.memory_service
        names = [
            "failed",
            "max_steps",
            "interrupted",
            "system",
            "missing",
            "unrecorded",
            "unknown",
            "isolated",
            "good-A",
            "good-B",
        ]
        for name in names:
            _complete_chat(memory, conn, "mixed", name, "Coriander", "Noted")
        for outcome in ["failed", "max_steps", "interrupted"]:
            conn.execute("UPDATE runs SET outcome=? WHERE run_id=?", (outcome, outcome))
        conn.execute(
            "UPDATE runs SET purpose='consolidation',session_id=NULL WHERE "
            "run_id='system'"
        )
        conn.execute(
            "DELETE FROM agent_log WHERE run_id='missing' AND role='assistant'"
        )
        conn.execute(
            "UPDATE runs SET phase='running',outcome=NULL,finished_at=NULL "
            "WHERE run_id='unrecorded'"
        )
        conn.commit()
        assert "error" not in memory.forgetting.register_group(
            "unknown",
            kind="run",
            container_id="mixed",
            evidence="unknown",
            context=CONTEXT,
        )
        target = memory.execute(
            {
                "operation_id": "seed",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "upstream", "fact": "source"},
            },
            CONTEXT,
        )["memory_id"]
        assert "error" not in memory.forgetting.register_sources(
            "semantic",
            target,
            1,
            groups=("isolated",),
            evidence_id="source",
            context=CONTEXT,
        )
        assert "error" not in memory.execute(
            {
                "operation_id": "delete",
                "kind": "semantic",
                "action": "delete",
                "expected_version": 1,
                "payload": {"id": target},
            },
            CONTEXT,
        )
        assert memory.consolidation.session_status("mixed")["unprocessed_count"] == 2
        before = conn.execute("SELECT id,content FROM agent_log ORDER BY id").fetchall()
        _chat(host, settled)
        _system(conn, settled)
        assert conn.execute(
            "SELECT run_id FROM memory_consolidation_source_results ORDER BY run_id"
        ).fetchall() == [("good-A",), ("good-B",)]
        assert (
            conn.execute(
                "SELECT id,content FROM agent_log WHERE id<=? ORDER BY id",
                (before[-1][0],),
            ).fetchall()
            == before
        )
        assert len(model.requests) == 3
    finally:
        host.close()
        conn.close()


def test_busy_automatic_admission_is_spent_without_background_retry(
    tmp_path, monkeypatch
):
    from agent_alfred.evals.deterministic.test_memory_consolidation_service import (
        _complete_chat,
    )

    host, model, conn = _traced_host(tmp_path, [SKIP, "Noted", SKIP, "Noted", PLAN])
    settled = _observe_settlement(host, monkeypatch, conn)
    entered, release = threading.Event(), threading.Event()
    failures, threads = [], []
    submit = host.submit

    def mutate():
        try:

            def held():
                entered.set()
                assert release.wait(5)

            host.execute_mutation(held)
        except BaseException as error:
            failures.append(error)

    def collide(request):
        if request.purpose != "consolidation":
            return submit(request)
        worker = threading.Thread(target=mutate)
        threads.append(worker)
        worker.start()
        assert entered.wait(5)
        return submit(request)

    try:
        for n in range(2):
            _complete_chat(
                host.memory_service, conn, "ready", f"s-{n}", "Coriander", "Noted"
            )
        with monkeypatch.context() as patch:
            patch.setattr(host, "submit", collide)
            _chat(host, settled)
        assert conn.execute(
            "SELECT status,error_code FROM memory_consolidation_triggers"
        ).fetchone() == ("refused", "mutation_in_flight")
        assert conn.execute(
            "SELECT count(*) FROM runs WHERE purpose='consolidation'"
        ).fetchone() == (0,)
        assert len(model.requests) == 2
        release.set()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
        assert not failures
        _chat(host, settled)
        _system(conn, settled)
        assert len(model.requests) == 5
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
        host.close()
        conn.close()


def test_readiness_database_error_preserves_saved_chat_and_observable_failure(
    tmp_path, monkeypatch
):
    import sqlite3

    from agent_alfred.evals.deterministic.test_memory_consolidation_service import (
        _complete_chat,
    )

    host, model, conn = _traced_host(tmp_path, [SKIP, "Noted"])
    try:
        for n in range(2):
            _complete_chat(
                host.memory_service, conn, "ready", f"s-{n}", "Coriander", "Noted"
            )
        scheduling = host.memory_service.consolidation.scheduling
        original = scheduling.claim

        def denied(run_id):
            def authorize(action, table, *rest):
                return (
                    sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_READ and table == "runs"
                    else sqlite3.SQLITE_OK
                )

            conn.set_authorizer(authorize)
            try:
                return original(run_id)
            finally:
                conn.set_authorizer(None)

        monkeypatch.setattr(scheduling, "claim", denied)
        chat = host.submit(SubmitRequest("chat"))
        assert host.wait(chat.run_id, timeout=5).outcome == "completed"
        assert conn.execute(
            "SELECT status,error_code FROM memory_consolidation_triggers"
        ).fetchone() == ("error", "storage_read_failed")
        assert conn.execute(
            "SELECT count(*) FROM runs WHERE purpose='consolidation'"
        ).fetchone() == (0,)
        assert (
            host.memory_service.consolidation.session_status("ready")[
                "unprocessed_count"
            ]
            == 2
        )
        assert len(model.requests) == 2
    finally:
        host.close()
        conn.close()
