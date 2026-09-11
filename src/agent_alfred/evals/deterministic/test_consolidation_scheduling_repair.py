"""Public regression for saved-response isolation and current-prefix fairness."""

import json
import sqlite3
import threading

import pytest

from agent_alfred.evals.deterministic.test_consolidation_scheduling import (
    SKIP,
    _chat,
    _observe_settlement,
    _system,
)
from agent_alfred.evals.deterministic.test_memory_consolidation_runtime import (
    _traced_host,
)
from agent_alfred.evals.deterministic.test_memory_consolidation_service import (
    CONTEXT,
    PLAN,
    _complete_chat,
)
from agent_alfred.runtime.host import SubmitRequest


@pytest.mark.parametrize("notification_loss", ["before", "after"])
@pytest.mark.parametrize("recovery", ["close", "successor"])
def test_saved_chat_notification_survives_failed_scheduling_receipt(
    tmp_path, monkeypatch, notification_loss, recovery
):
    host, model, conn = _traced_host(tmp_path, [SKIP, "Saved", SKIP, "Second"])
    seen, entered, release = threading.Event(), threading.Event(), threading.Event()
    publish, finish, notify, respond = (
        host.recording_publish_recorded_then_release,
        host.memory_service.consolidation.scheduling.finish_admission,
        host.notify_run_done,
        model.respond,
    )
    lost = False

    def authorize(action, table, *rest):
        if (
            action == sqlite3.SQLITE_READ and table == "memory_consolidation_ready"
        ) or (
            action == sqlite3.SQLITE_UPDATE and table == "memory_consolidation_triggers"
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def published(run_id):
        publish(run_id)
        conn.set_authorizer(authorize)

    def failed(*args, **kwargs):
        try:
            return finish(*args, **kwargs)
        except sqlite3.Error:
            seen.set()
            raise

    def notified(run_id):
        nonlocal lost
        if not lost:
            lost = True
            if notification_loss == "after":
                notify(run_id)
            raise KeyboardInterrupt("notification return edge")
        return notify(run_id)

    def responding(request, **kwargs):
        if len(model.requests) >= 2:
            entered.set()
            assert release.wait(5)
        return respond(request, **kwargs)

    monkeypatch.setattr(host, "recording_publish_recorded_then_release", published)
    monkeypatch.setattr(
        host.memory_service.consolidation.scheduling, "finish_admission", failed
    )
    monkeypatch.setattr(host, "notify_run_done", notified)
    monkeypatch.setattr(model, "respond", responding)
    try:
        first = host.submit(SubmitRequest("first"))
        assert seen.wait(5)
        assert host.wait(first.run_id, timeout=1).outcome == "completed"
        assert conn.execute(
            "SELECT phase,outcome FROM runs WHERE run_id=?", (first.run_id,)
        ).fetchone() == ("finished", "completed")
        assert conn.execute(
            "SELECT status FROM memory_consolidation_triggers WHERE chat_run_id=?",
            (first.run_id,),
        ).fetchone() == ("pending",)
        if recovery == "close":
            assert host.close(timeout=0) is False
        conn.set_authorizer(None)
        monkeypatch.setattr(host, "recording_publish_recorded_then_release", publish)
        monkeypatch.setattr(
            host.memory_service.consolidation.scheduling, "finish_admission", finish
        )
        if recovery == "successor":
            second = host.submit(SubmitRequest("second"))
            assert second.kind == "accepted"
            assert entered.wait(5)
            assert host.submit(SubmitRequest("busy")).kind == "run_in_progress"
            assert host.close(timeout=0) is False
            release.set()
            assert host.wait(second.run_id, timeout=5).outcome == "completed"
        assert host.close(timeout=5)
        assert conn.execute(
            "SELECT status FROM memory_consolidation_triggers WHERE chat_run_id=?",
            (first.run_id,),
        ).fetchone() == ("refused",)
        assert conn.execute(
            "SELECT count(*) FROM runs WHERE purpose='consolidation'"
        ).fetchone() == (0,)
    finally:
        release.set()
        monkeypatch.setattr(host, "notify_run_done", notify)
        conn.set_authorizer(None)
        monkeypatch.setattr(host, "recording_publish_recorded_then_release", publish)
        monkeypatch.setattr(
            host.memory_service.consolidation.scheduling, "finish_admission", finish
        )
        host.close()
        conn.close()


@pytest.mark.parametrize("role", ["user", "assistant"])
@pytest.mark.parametrize("text", ["", " \n\t", "  real content  "])
def test_blank_source_is_excluded_without_trimming_history(
    tmp_path, monkeypatch, role, text
):
    host, model, conn = _traced_host(
        tmp_path, [SKIP, text if role == "assistant" else "Answer"]
    )
    try:
        settled = _observe_settlement(host, monkeypatch, conn)
        first = _chat(host, settled, message=text if role == "user" else "Question")
        expected = int(bool(text.strip()))
        assert (
            host.memory_service.consolidation.session_status(first.session_id)[
                "unprocessed_count"
            ]
            == expected
        )
        raw = conn.execute(
            "SELECT content FROM agent_log WHERE run_id=? AND role=?",
            (first.run_id, role),
        ).fetchone()[0]
        assert json.loads(raw)[0]["text"] == text
        host.close()
        conn.close()
        host, model, conn = _traced_host(tmp_path, [])
        assert (
            host.memory_service.consolidation.session_status(first.session_id)[
                "unprocessed_count"
            ]
            == expected
        )
        assert not model.requests
    finally:
        host.close()
        conn.close()


def _prefixes(memory, conn, first="A1"):
    for session, names in [
        ("z-A", (first, "A2")),
        ("a-B", ("B1", "B2")),
        ("z-A", ("A3", "A4")),
    ]:
        for name in names:
            _complete_chat(memory, conn, session, name, name, "Noted")


@pytest.mark.parametrize("consume", ["success", "approve", "reject"])
def test_consumed_prefix_updates_durable_ready_order(tmp_path, monkeypatch, consume):
    host, model, conn = _traced_host(tmp_path, [SKIP, "Noted", PLAN])
    try:
        memory = host.memory_service
        target = memory.execute(
            {
                "operation_id": "seed",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "target", "fact": "protected"},
            },
            CONTEXT,
        )["memory_id"]
        _prefixes(memory, conn)
        if consume != "success":
            model._script[2] = json.dumps(
                {
                    "semantic": [
                        {
                            "action": "update",
                            "id": target,
                            "subject": "target",
                            "fact": "new",
                        }
                    ],
                    "episode_summary": "Proposed",
                }
            )
        settled = _observe_settlement(host, monkeypatch, conn)
        _chat(host, settled)
        run_id = _system(conn, settled)
        old_order = conn.execute(
            "SELECT ready_order FROM memory_consolidation_ready WHERE session_id='z-A'"
        ).fetchone()[0]
        if consume != "success":
            batch = memory.consolidation.session_status("z-A")["batch"]
            assert batch["status"] == "awaiting_approval"
        host.close()
        conn.close()
        host, model, conn = _traced_host(tmp_path, [SKIP, "Noted", PLAN])
        memory = host.memory_service
        if consume != "success":
            assert conn.execute(
                "SELECT ready_order FROM memory_consolidation_ready WHERE "
                "session_id='z-A'"
            ).fetchone() == (old_order,)
            action = getattr(memory.consolidation, consume)
            assert action(batch["batch_id"], 1, context=CONTEXT, operation_id=consume)[
                "status"
            ] == ("succeeded" if consume == "approve" else "rejected")
        assert not model.requests
        before = dict(
            conn.execute(
                "SELECT session_id,ready_order FROM memory_consolidation_ready"
            )
        )
        assert before["z-A"] > before["a-B"]
        settled = _observe_settlement(host, monkeypatch, conn)
        _chat(host, settled)
        successor = _system(conn, settled)
        assert successor != run_id
        assert conn.execute(
            "SELECT session_id FROM memory_consolidation_batches WHERE "
            "generation_run_id=?",
            (successor,),
        ).fetchone() == ("a-B",)
        orders = dict(
            conn.execute(
                "SELECT session_id,ready_order FROM memory_consolidation_ready"
            )
        )
        assert orders["z-A"] > before["a-B"]
        assert "a-B" not in orders
    finally:
        host.close()
        conn.close()


@pytest.mark.parametrize("change", ["skip", "isolate"])
def test_eligibility_change_moves_current_ready_prefix(tmp_path, monkeypatch, change):
    host, model, conn = _traced_host(tmp_path, [SKIP, "Noted", PLAN])
    try:
        memory = host.memory_service
        _prefixes(memory, conn)
        if change == "skip":
            conn.execute(
                "UPDATE agent_log SET content=? WHERE run_id='A1' AND role='user'",
                (json.dumps([{"type": "text", "text": "x" * 70000}]),),
            )
            conn.commit()
        host.close()
        conn.close()
        host, model, conn = _traced_host(tmp_path, [SKIP, "Noted", PLAN])
        memory = host.memory_service
        before = dict(
            conn.execute(
                "SELECT session_id,ready_order FROM memory_consolidation_ready"
            )
        )
        assert before["z-A"] < before["a-B"]
        if change == "skip":
            assert (
                memory.consolidation.skip_oversized(
                    "z-A", "A1", context=CONTEXT, operation_id="skip"
                )["status"]
                == "skipped"
            )
        else:
            target = memory.execute(
                {
                    "operation_id": "seed",
                    "kind": "semantic",
                    "action": "save",
                    "payload": {"subject": "source", "fact": "upstream"},
                },
                CONTEXT,
            )["memory_id"]
            assert "error" not in memory.forgetting.register_sources(
                "semantic",
                target,
                1,
                groups=("A1",),
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
        host.close()
        conn.close()
        host, model, conn = _traced_host(tmp_path, [SKIP, "Noted", PLAN])
        current = conn.execute(
            "SELECT session_id,ready_order,ready_at FROM "
            "memory_consolidation_ready ORDER BY ready_order"
        ).fetchall()
        assert [row[0] for row in current] == ["a-B", "z-A"]
        host.close()
        conn.close()
        host, model, conn = _traced_host(tmp_path, [SKIP, "Noted", PLAN])
        assert (
            conn.execute(
                "SELECT session_id,ready_order,ready_at FROM "
                "memory_consolidation_ready ORDER BY ready_order"
            ).fetchall()
            == current
        )
        assert not model.requests
        settled = _observe_settlement(host, monkeypatch, conn)
        _chat(host, settled)
        run = _system(conn, settled)
        assert conn.execute(
            "SELECT session_id FROM memory_consolidation_batches WHERE "
            "generation_run_id=?",
            (run,),
        ).fetchone() == ("a-B",)
        assert conn.execute(
            "SELECT count(*) FROM agent_log WHERE run_id='A1'"
        ).fetchone() == (2,)
    finally:
        host.close()
        conn.close()


def test_below_threshold_reaccumulation_gets_its_current_ready_order(
    tmp_path, monkeypatch
):
    host, model, conn = _traced_host(tmp_path, [SKIP, "Noted", PLAN])
    try:
        for name in ("A1", "A2"):
            _complete_chat(host.memory_service, conn, "z-A", name, name, "Noted")
        settled = _observe_settlement(host, monkeypatch, conn)
        _chat(host, settled)
        _system(conn, settled)
        assert (
            conn.execute("SELECT session_id FROM memory_consolidation_ready").fetchall()
            == []
        )
        for name in ("B1", "B2"):
            _complete_chat(host.memory_service, conn, "a-B", name, name, "Noted")
        _complete_chat(host.memory_service, conn, "z-A", "A3", "A3", "Noted")
        host.close()
        conn.close()
        host, model, conn = _traced_host(tmp_path, [SKIP, "Noted", PLAN])
        assert conn.execute(
            "SELECT session_id FROM memory_consolidation_ready"
        ).fetchall() == [("a-B",)]
        _complete_chat(host.memory_service, conn, "z-A", "A4", "A4", "Noted")
        settled = _observe_settlement(host, monkeypatch, conn)
        _chat(host, settled)
        run = _system(conn, settled)
        assert conn.execute(
            "SELECT session_id FROM memory_consolidation_batches WHERE "
            "generation_run_id=?",
            (run,),
        ).fetchone() == ("a-B",)
        assert (
            host.memory_service.consolidation.session_status("z-A")["unprocessed_count"]
            == 2
        )
    finally:
        host.close()
        conn.close()


def test_source_selector_uses_nonblank_eligibility_and_keeps_valid_whitespace():
    from agent_alfred.evals.deterministic.test_memory_consolidation import (
        MODEL,
        _source,
    )
    from agent_alfred.memory.consolidation import (
        ConsolidationBelowThreshold,
        ConsolidationLimits,
        select_consolidation_sources,
    )

    good = _source("good", user="  question  ", assistant="  answer  ")
    sources = [_source("empty", user=""), _source("blank", assistant=" \t\n"), good]
    result = select_consolidation_sources(
        "session-a",
        sources,
        model=MODEL,
        limits=ConsolidationLimits(source_threshold=2),
    )
    assert (
        isinstance(result, ConsolidationBelowThreshold)
        and result.unprocessed_count == 1
    )
    sources.append(_source("next"))
    selected = select_consolidation_sources(
        "session-a",
        sources,
        model=MODEL,
        limits=ConsolidationLimits(source_threshold=2),
    )
    assert selected.sources == (good, sources[-1])
    assert selected.sources[0].user_text == "  question  "


def test_failed_generation_preserves_unconsumed_ready_prefix(tmp_path, monkeypatch):
    host, model, conn = _traced_host(tmp_path, [SKIP, "Noted", "not-json"])
    try:
        _prefixes(host.memory_service, conn)
        settled = _observe_settlement(host, monkeypatch, conn)
        _chat(host, settled)
        _system(conn, settled)
        before = conn.execute(
            "SELECT ready_order,ready_at FROM memory_consolidation_ready "
            "WHERE session_id='z-A'"
        ).fetchone()
        assert conn.execute(
            "SELECT count(*) FROM memory_consolidation_source_results"
        ).fetchone() == (0,)
        host.close()
        conn.close()
        host, model, conn = _traced_host(tmp_path, [SKIP, "Noted", PLAN])
        assert not model.requests
        assert (
            conn.execute(
                "SELECT ready_order,ready_at FROM memory_consolidation_ready "
                "WHERE session_id='z-A'"
            ).fetchone()
            == before
        )
        settled = _observe_settlement(host, monkeypatch, conn)
        _chat(host, settled)
        run = _system(conn, settled)
        assert conn.execute(
            "SELECT session_id FROM memory_consolidation_batches "
            "WHERE generation_run_id=?",
            (run,),
        ).fetchone() == ("a-B",)
        assert (
            conn.execute(
                "SELECT ready_order,ready_at FROM memory_consolidation_ready "
                "WHERE session_id='z-A'"
            ).fetchone()
            == before
        )
        assert (
            host.memory_service.consolidation.session_status("z-A")["unprocessed_count"]
            == 4
        )
    finally:
        host.close()
        conn.close()
