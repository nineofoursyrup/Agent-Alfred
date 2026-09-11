"""Stable public retry actions retain their own admitted execution evidence."""

import json

import pytest

from agent_alfred.evals.deterministic.test_memory_consolidation_runtime import _host
from agent_alfred.evals.deterministic.test_memory_consolidation_service import (
    CONTEXT,
    PLAN,
    _complete_chat,
)


def _failed(host, conn):
    memory = host.memory_service
    _complete_chat(memory, conn, "s", "r1", "I like coriander", "Noted.")
    _complete_chat(memory, conn, "s", "r2", "Still coriander", "Noted.")
    first = host.generate_consolidation("s")
    assert host.wait(first.run_id, timeout=5).outcome == "failed"
    return memory.consolidation.session_status("s")["batch"]["batch_id"]


@pytest.mark.parametrize("boundary", ["running", "begun"])
def test_interrupted_retry_keeps_its_admitted_run(tmp_path, monkeypatch, boundary):
    host, model, conn = _host(tmp_path, script=["not-json", PLAN])
    try:
        bid = _failed(host, conn)
        service = host.memory_service.consolidation
        target = host if boundary == "running" else service
        method = (
            "execution_mark_running" if boundary == "running" else "begin_generation"
        )
        original = getattr(target, method)

        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt("controlled committed return interruption")

        with monkeypatch.context() as patch:
            patch.setattr(target, method, interrupted)
            retried = host.retry_consolidation(bid, 1, operation_id="interrupted")
            assert retried.kind == "accepted"
            assert host.wait(retried.run_id, timeout=5).outcome == "interrupted"
        replay = host.retry_consolidation(bid, 1, operation_id="interrupted")
        assert replay["generation_run_id"] == retried.run_id
        assert replay["status"] == "failed"
        assert replay["error_code"] != "invalid_plan_output"
        assert service.get_batch(bid)["status"] == "failed"
        assert len(model.requests) == 1
    finally:
        host.close()
        conn.close()


@pytest.mark.parametrize("pending", [False, True], ids=["success", "pending"])
def test_failed_retry_does_not_read_successor_result(tmp_path, pending):
    host, model, conn = _host(tmp_path, script=["not-json", "not-json"])
    try:
        memory = host.memory_service
        target = None
        if pending:
            target = memory.execute(
                {
                    "operation_id": "seed",
                    "kind": "semantic",
                    "action": "save",
                    "payload": {"subject": "food", "fact": "likes basil"},
                },
                CONTEXT,
            )
        bid = _failed(host, conn)
        first_retry = host.retry_consolidation(bid, 1, operation_id="retry-A")
        assert host.wait(first_retry.run_id, timeout=5).outcome == "failed"
        before = host.retry_consolidation(bid, 1, operation_id="retry-A")
        before_begin = memory.consolidation.get_action("begin:" + first_retry.run_id)
        assert before["revision"] == 2 and before["status"] == "failed"
        output = (
            PLAN
            if not pending
            else json.dumps(
                {
                    "semantic": [
                        {
                            "action": "update",
                            "id": target["memory_id"],
                            "subject": "food",
                            "fact": "SUCCESSOR-PRIVATE-CANDIDATE",
                        }
                    ],
                    "episode_summary": "Successor-only proposal",
                }
            )
        )
        model._script.append(output)
        second_retry = host.retry_consolidation(bid, 2, operation_id="retry-B")
        assert host.wait(second_retry.run_id, timeout=5).outcome == "completed"
        after = host.retry_consolidation(bid, 1, operation_id="retry-A")
        assert after == before
        assert (
            memory.consolidation.get_action("begin:" + first_retry.run_id)
            == before_begin
        )
        assert after["generation_run_id"] == first_retry.run_id
        assert "plan" not in after and "SUCCESSOR-PRIVATE" not in json.dumps(after)
        assert len(model.requests) == 3
        # Persistence keeps only safe metadata; no historical plan snapshot.
        receipts = conn.execute(
            "SELECT receipt FROM memory_consolidation_actions"
        ).fetchall()
        assert "SUCCESSOR-PRIVATE" not in json.dumps(receipts)
    finally:
        host.close()
        conn.close()


@pytest.mark.parametrize("stage", ["client", "unpublished", "published"])
def test_retry_admission_failure_preserves_execution_truth(
    tmp_path, monkeypatch, stage
):
    import threading

    host, model, conn = _host(tmp_path, script=["not-json", PLAN])
    try:
        bid = _failed(host, conn)
        done = threading.Event()
        notify = host.notify_run_done

        def notified(run_id):
            notify(run_id)
            done.set()

        monkeypatch.setattr(host, "notify_run_done", notified)
        admitted = []
        if stage == "client":
            target, method = host._admission._factory, "create"
        else:
            target, method = host, "admission_publish_handoff"
        original = getattr(target, method)

        def fail(*args, **kwargs):
            if stage == "published":
                original(*args, **kwargs)
                admitted.append(args[0].run_id)
            raise OSError("controlled admission fault")

        with monkeypatch.context() as patch:
            patch.setattr(target, method, fail)
            if stage == "published":
                with pytest.raises(OSError):
                    host.retry_consolidation(bid, 1, operation_id="admission-fault")
                assert done.wait(5)
            else:
                host.retry_consolidation(bid, 1, operation_id="admission-fault")
        replay = host.retry_consolidation(bid, 1, operation_id="admission-fault")
        if stage == "client":
            assert replay.kind == "accepted"
            assert host.wait(replay.run_id, timeout=5).outcome == "completed"
        elif stage == "unpublished":
            assert replay["status"] == "failed"
            assert replay["admission_state"] == "rejected"
            assert replay["run_outcome"] == "interrupted"
            assert len(model.requests) == 1
        else:
            assert replay["status"] == "succeeded"
            assert len(model.requests) == 2
        assert host.snapshot().coordinator_state == "idle"
    finally:
        host.close()
        conn.close()


@pytest.mark.parametrize("fault", ["finish-return", "recording", "reply-return"])
def test_committed_retry_survives_lost_return_and_recording(
    tmp_path, monkeypatch, fault
):
    import sqlite3

    host, model, conn = _host(tmp_path, script=["not-json", PLAN])
    try:
        bid = _failed(host, conn)
        service = host.memory_service.consolidation
        original_finish = service.finish_generation
        admitted = []

        def finish(*args, **kwargs):
            result = original_finish(*args, **kwargs)
            assert result["status"] == "succeeded"
            if fault == "finish-return":
                raise KeyboardInterrupt("committed generation response lost")
            if fault == "recording":

                def deny(action, table, column, database, trigger):
                    if action == sqlite3.SQLITE_UPDATE and table == "runs":
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                conn.set_authorizer(deny)
            return result

        monkeypatch.setattr(service, "finish_generation", finish)
        original_submit = host.submit

        def lost_reply(request):
            result = original_submit(request)
            admitted.append(result.run_id)
            raise OSError("caller lost accepted response")

        if fault == "reply-return":
            with monkeypatch.context() as patch:
                patch.setattr(host, "submit", lost_reply)
                with pytest.raises(OSError):
                    host.retry_consolidation(bid, 1, operation_id="lost-reply")
            run_id = admitted[0]
        else:
            run_id = host.retry_consolidation(bid, 1, operation_id="lost-reply").run_id
        result = host.wait(run_id, timeout=5)
        conn.set_authorizer(None)
        assert result.outcome == (
            "interrupted" if fault == "finish-return" else "completed"
        )
        readback = service.get_action("lost-reply")
        assert readback["status"] == "succeeded"
        assert len(readback["products"]) == 2
        assert conn.execute("SELECT count(*) FROM episodes").fetchone() == (1,)
        assert len(model.requests) == 2
        if fault == "recording":
            assert host.snapshot().coordinator_state == "recording_failed"
            assert host.generate_consolidation("s").kind == "recording_unavailable"
        else:
            assert (
                host.retry_consolidation(bid, 1, operation_id="lost-reply") == readback
            )
    finally:
        conn.set_authorizer(None)
        host.close()
        conn.close()


def _retry_crash_child(folder, stage):
    import threading
    from pathlib import Path

    from agent_alfred.evals.deterministic.test_memory_consolidation_runtime import (
        _traced_host,
    )

    host, model, conn = _traced_host(Path(folder), ["not-json"])
    memory = host.memory_service
    target = None
    if stage == "pending":
        target = memory.execute(
            {
                "operation_id": "seed",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "food", "fact": "likes basil"},
            },
            CONTEXT,
        )
    bid = _failed(host, conn)
    output = "not-json" if stage == "failed" else PLAN
    if target:
        output = json.dumps(
            {
                "semantic": [
                    {
                        "action": "update",
                        "id": target["memory_id"],
                        "subject": "food",
                        "fact": "PENDING-CANDIDATE",
                    }
                ],
                "episode_summary": "Changed preference",
            }
        )
    model._script.append(output)

    def stop():
        print("READY", flush=True)
        threading.Event().wait()

    if stage == "intent":
        host.submit = lambda request: stop()
    elif stage == "admitted":
        original = host.execution_mark_running

        def running(*args, **kwargs):
            original(*args, **kwargs)
            stop()

        host.execution_mark_running = running
    elif stage == "begun":
        original = memory.consolidation.begin_generation

        def begun(*args, **kwargs):
            original(*args, **kwargs)
            stop()

        memory.consolidation.begin_generation = begun
    result = host.retry_consolidation(bid, 1, operation_id="crash-intent")
    host.wait(result.run_id, timeout=5)
    stop()


@pytest.mark.parametrize(
    "stage", ["intent", "admitted", "begun", "failed", "success", "pending"]
)
def test_retry_restart_reads_own_durable_result_without_startup_calls(tmp_path, stage):
    import select
    import subprocess
    import sys

    from agent_alfred.evals.deterministic.test_memory_consolidation_runtime import (
        _traced_host,
    )

    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from agent_alfred.evals.deterministic.test_consolidation_retry_ownership "
            "import _retry_crash_child; _retry_crash_child(%r, %r)"
            % (str(tmp_path), stage),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready, _, _ = select.select([child.stdout], [], [], 15)
        assert ready, "child did not reach the durable boundary"
        assert child.stdout.readline().strip() == "READY"
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
    host, model, conn = _traced_host(tmp_path, [PLAN])
    try:
        assert model.requests == []
        service = host.memory_service.consolidation
        fact = service.get_action("crash-intent")
        bid = fact["batch_id"]
        replay = host.retry_consolidation(bid, 1, operation_id="crash-intent")
        if stage == "intent":
            assert replay.kind == "accepted"
            assert host.wait(replay.run_id, timeout=5).outcome == "completed"
            assert len(model.requests) == 1
        else:
            assert replay == fact
            assert model.requests == []
            assert fact["status"] == (
                "succeeded"
                if stage == "success"
                else "awaiting_approval"
                if stage == "pending"
                else "failed"
            )
            assert service.get_batch(bid)["status"] not in ("running", "queued")
            if stage == "pending":
                assert "PENDING-CANDIDATE" in json.dumps(service.get_batch(bid))
                approved = service.approve(
                    bid, 2, context=CONTEXT, operation_id="approve"
                )
                assert approved["status"] == "succeeded"
                assert model.requests == []
        host.close()
        host.close()
    finally:
        host.close()
        conn.close()


def test_retry_prepare_failure_keeps_each_generation_revision(tmp_path, monkeypatch):
    from agent_alfred.memory.semantic import SQLiteSemanticStore

    host, model, conn = _host(tmp_path, script=["not-json", "not-json", PLAN])
    try:
        bid = _failed(host, conn)
        first = host.retry_consolidation(bid, 1, operation_id="retry-A")
        assert host.wait(first.run_id, timeout=5).outcome == "failed"
        a_before = host.memory_service.consolidation.get_action("retry-A")

        def unavailable(*args, **kwargs):
            import sqlite3

            raise sqlite3.OperationalError("controlled FTS failure")

        with monkeypatch.context() as patch:
            patch.setattr(SQLiteSemanticStore, "search", unavailable)
            failed = host.retry_consolidation(bid, 2, operation_id="retry-B")
            assert host.wait(failed.run_id, timeout=5).outcome == "failed"
        b = host.memory_service.consolidation.get_action("retry-B")
        assert b["revision"] == 3 and b["generation_run_id"] == failed.run_id
        assert host.memory_service.consolidation.get_action("retry-A") == a_before
        assert len(model.requests) == 2
        successor = host.retry_consolidation(bid, 3, operation_id="retry-C")
        assert host.wait(successor.run_id, timeout=5).outcome == "completed"
        assert host.memory_service.consolidation.get_action("retry-B") == b
    finally:
        host.close()
        conn.close()


def test_retry_rechecks_threshold_and_durably_reports_no_generation(
    tmp_path, monkeypatch
):
    from agent_alfred.memory.consolidation import ConsolidationLimits

    host, model, conn = _host(tmp_path, script=["not-json", PLAN])
    try:
        bid = _failed(host, conn)
        # Controlled startup configuration fixture: fresh explicit retry must
        # obey its generation limits rather than force the old source batch.
        monkeypatch.setattr(
            host.memory_service.consolidation,
            "_limits",
            ConsolidationLimits(source_threshold=3),
        )
        retried = host.retry_consolidation(bid, 1, operation_id="threshold")
        result = host.wait(retried.run_id, timeout=5)
        assert result.error == "below_threshold"
        replay = host.retry_consolidation(bid, 1, operation_id="threshold")
        assert replay["status"] == "below_threshold", replay
        assert replay["generation_run_id"] == retried.run_id
        assert len(model.requests) == 1
    finally:
        host.close()
        conn.close()


def test_retry_step_budget_failure_releases_owner_without_regeneration(
    tmp_path, monkeypatch
):
    from agent_alfred.loop.budget import RunBudget, StepBudgetExceeded

    host, model, conn = _host(tmp_path, script=["not-json", PLAN])
    try:
        bid = _failed(host, conn)

        def exhausted(*args, **kwargs):
            raise StepBudgetExceeded()

        with monkeypatch.context() as patch:
            patch.setattr(RunBudget, "reserve_step", exhausted)
            retried = host.retry_consolidation(bid, 1, operation_id="budget")
            assert host.wait(retried.run_id, timeout=5).outcome == "max_steps"
        replay = host.retry_consolidation(bid, 1, operation_id="budget")
        assert replay["generation_run_id"] == retried.run_id
        assert replay["status"] == "failed"
        assert len(model.requests) == 1
        assert host.snapshot().coordinator_state == "idle"
    finally:
        host.close()
        conn.close()


def test_retry_binding_rolls_back_with_accepted_run(tmp_path):
    host, model, conn = _host(tmp_path, script=["not-json", PLAN])
    try:
        bid = _failed(host, conn)
        conn.execute(
            "CREATE TRIGGER reject_binding AFTER UPDATE "
            "ON memory_consolidation_actions "
            "WHEN json_extract(NEW.receipt,'$.generation_run_id') IS NOT NULL "
            "BEGIN SELECT RAISE(ABORT,'controlled binding failure'); END"
        )
        refused = host.retry_consolidation(bid, 1, operation_id="atomic-binding")
        assert refused.kind == "admission_failed"
        assert conn.execute(
            "SELECT count(*) FROM runs WHERE purpose='consolidation'"
        ).fetchone() == (1,)
        assert (
            host.memory_service.consolidation.get_action("atomic-binding")["status"]
            == "generation_required"
        )
        conn.execute("DROP TRIGGER reject_binding")
        retried = host.retry_consolidation(bid, 1, operation_id="atomic-binding")
        assert host.wait(retried.run_id, timeout=5).outcome == "completed"
        assert len(model.requests) == 2
    finally:
        host.close()
        conn.close()


def test_retry_bound_identity_rejects_action_and_key_changes(tmp_path, monkeypatch):
    from agent_alfred.memory.audit import AuditKey
    from agent_alfred.memory.commands import CommandContext
    from agent_alfred.memory.types import ManualOrigin

    host, model, conn = _host(tmp_path, script=["not-json", PLAN])
    try:
        bid = _failed(host, conn)
        retried = host.retry_consolidation(bid, 1, operation_id="bound")
        assert host.wait(retried.run_id, timeout=5).outcome == "completed"
        service = host.memory_service.consolidation
        before = service.get_action("bound")
        wrong_action = service.approve(
            bid,
            1,
            operation_id="bound",
            context=CommandContext(ManualOrigin("cli"), "cli"),
        )
        assert wrong_action["error"]["code"] == "operation_mismatch"
        monkeypatch.setattr(host.memory_service, "_key", AuditKey("rotated", b"x" * 32))
        unverifiable = host.retry_consolidation(bid, 1, operation_id="bound")
        assert unverifiable["error"]["code"] == "operation_unverifiable"
        assert service.get_action("bound") == before
        assert len(model.requests) == 2
    finally:
        host.close()
        conn.close()


def test_same_retry_intent_racing_at_admission_executes_once(tmp_path, monkeypatch):
    import threading

    host, model, conn = _host(tmp_path, script=["not-json", PLAN])
    try:
        bid = _failed(host, conn)
        barrier = threading.Barrier(2)
        first_prepared = threading.Event()
        original = host.submit
        results, errors = [], []

        def synchronized(request):
            first_prepared.set()
            barrier.wait(timeout=5)
            return original(request)

        def send():
            try:
                results.append(host.retry_consolidation(bid, 1, operation_id="same"))
            except BaseException as error:
                errors.append(error)

        with monkeypatch.context() as patch:
            patch.setattr(host, "submit", synchronized)
            workers = [threading.Thread(target=send) for _ in range(2)]
            workers[0].start()
            assert first_prepared.wait(5)
            workers[1].start()
            for worker in workers:
                worker.join(5)
            assert all(not worker.is_alive() for worker in workers)
        assert not errors, errors
        accepted = [result for result in results if result.kind == "accepted"]
        assert len(accepted) == 1
        assert host.wait(accepted[0].run_id, timeout=5).outcome == "completed"
        replay = host.retry_consolidation(bid, 1, operation_id="same")
        assert replay["status"] == "succeeded"
        assert len(model.requests) == 2
        assert conn.execute(
            "SELECT count(*) FROM runs WHERE purpose='consolidation'"
        ).fetchone() == (2,)
    finally:
        host.close()
        conn.close()


@pytest.mark.parametrize("terminal", ["succeeded", "rejected", "invalidated"])
def test_historical_revision_bodies_retire_with_successor(
    tmp_path, monkeypatch, terminal
):
    from dataclasses import replace

    from agent_alfred.evals.deterministic.test_memory_consolidation_runtime import (
        _traced_host,
    )
    from agent_alfred.memory.types import ManualOrigin

    host, model, conn = _traced_host(tmp_path, ["not-json", PLAN])
    try:
        memory = host.memory_service
        upstream = memory.execute(
            {
                "operation_id": "seed-private",
                "action": "save",
                "kind": "semantic",
                "payload": {"subject": "upstream", "fact": "PRIVATE-SOURCE"},
            },
            CONTEXT,
        )["memory_id"]
        bid = _failed(host, conn)
        assert "error" not in memory.forgetting.register_sources(
            "semantic",
            upstream,
            1,
            groups=("r1",),
            evidence_id="source",
            context=CONTEXT,
        )
        original = memory.consolidation.begin_generation

        def delete(context):
            result = memory.execute(
                {
                    "operation_id": "delete-private",
                    "action": "delete",
                    "kind": "semantic",
                    "expected_version": 1,
                    "payload": {"id": upstream},
                },
                context,
            )
            assert "error" not in result, result

        if terminal == "rejected":
            model._script[1] = json.dumps(
                {
                    "semantic": [
                        {
                            "action": "update",
                            "id": upstream,
                            "subject": "upstream",
                            "fact": "NEW-CANDIDATE",
                        }
                    ],
                    "episode_summary": "Proposed change",
                }
            )
        if terminal == "invalidated":

            def interrupted(*args, **kwargs):
                original(*args, **kwargs)
                delete(replace(kwargs["context"], origin=ManualOrigin("web")))
                raise KeyboardInterrupt("source invalidated after begin commit")

            monkeypatch.setattr(memory.consolidation, "begin_generation", interrupted)
        retried = host.retry_consolidation(bid, 1, operation_id="retry-history")
        result = host.wait(retried.run_id, timeout=5)
        assert result.outcome == (
            "interrupted" if terminal == "invalidated" else "completed"
        )
        if terminal == "rejected":
            rejected = memory.consolidation.reject(
                bid, 2, context=CONTEXT, operation_id="reject-history"
            )
            assert rejected["status"] == "rejected"
        if terminal != "invalidated":
            delete(CONTEXT)
        assert memory.get("semantic", upstream) is None
        view = memory.consolidation.get_batch(bid)
        assert view["status"] == terminal
        bodies = conn.execute(
            "SELECT plan_json,episode_summary,candidate_text,request_json "
            "FROM memory_consolidation_plans WHERE batch_id=?",
            (bid,),
        ).fetchall()
        assert len(bodies) == 2
        assert all(value is None for row in bodies for value in row)
        assert conn.execute(
            "SELECT count(*) FROM memory_consolidation_sources"
        ).fetchone() == (4,)
        assert host.snapshot().coordinator_state == "idle"
    finally:
        host.close()
        conn.close()


def test_initial_custom_generation_keeps_its_revision(tmp_path):
    from agent_alfred.runtime.host import SubmitRequest

    host, model, conn = _host(tmp_path, script=["not-json", "not-json", PLAN])
    try:
        memory = host.memory_service
        service = memory.consolidation
        _complete_chat(memory, conn, "s", "r1", "I like coriander", "Noted.")
        _complete_chat(memory, conn, "s", "r2", "Still coriander", "Noted.")
        first = host.submit(
            SubmitRequest(
                message="s",
                purpose="consolidation",
                gateway="cli",
                operation_id="initial-custom",
                wait_for_result=True,
            )
        )
        assert host.wait(first.run_id, timeout=5).outcome == "failed"
        bid = service.session_status("s")["batch"]["batch_id"]
        before = service.get_action("initial-custom")
        finish = service.get_action("finish:" + first.run_id)
        for rev, outcome in [(1, "failed"), (2, "completed")]:
            retry = host.retry_consolidation(bid, rev, operation_id=f"retry-{rev}")
            assert host.wait(retry.run_id, timeout=5).outcome == outcome
            assert service.get_action("initial-custom") == before
            assert service.get_action("finish:" + first.run_id) == finish
        assert before["generation_run_id"] == first.run_id
        assert before["status"] == "failed" and before["revision"] == 1
        assert len(model.requests) == 3
    finally:
        host.close()
        conn.close()


@pytest.mark.parametrize(
    "boundary, terminal",
    [
        ("restart", "failed"),
        ("restart", "succeeded"),
        ("restart", "rejected"),
        ("delete", "failed"),
        ("delete", "succeeded"),
        ("delete", "rejected"),
        ("handoff", "failed"),
    ],
)
def test_persisted_obsolete_bodies_retire_atomically(tmp_path, boundary, terminal):
    from agent_alfred.evals.deterministic.test_memory_consolidation_runtime import (
        _traced_host,
    )

    host, model, conn = _traced_host(tmp_path, ["not-json", "not-json", PLAN])
    try:
        memory = host.memory_service
        upstream = memory.execute(
            {
                "operation_id": "seed",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "upstream", "fact": "PRIVATE-SOURCE"},
            },
            CONTEXT,
        )["memory_id"]
        bid = _failed(host, conn)
        old = conn.execute(
            "SELECT candidate_text,request_json FROM memory_consolidation_plans "
            "WHERE batch_id=? AND revision=1",
            (bid,),
        ).fetchone()
        if terminal == "succeeded":
            model._script[1] = PLAN
        elif terminal == "rejected":
            model._script[1] = json.dumps(
                {
                    "semantic": [
                        {
                            "action": "update",
                            "id": upstream,
                            "subject": "upstream",
                            "fact": "NEW-CANDIDATE",
                        }
                    ],
                    "episode_summary": "Proposed update",
                }
            )
        retry = host.retry_consolidation(bid, 1, operation_id="retry-A")
        assert host.wait(retry.run_id, timeout=5).outcome == (
            "failed" if terminal == "failed" else "completed"
        )
        if terminal == "rejected":
            assert (
                memory.consolidation.reject(
                    bid, 2, context=CONTEXT, operation_id="reject"
                )["status"]
                == "rejected"
            )
        # Restore exactly the obsolete body shape persisted by s3j. This is
        # upgrade input, not an alternate path for generating a live batch.
        conn.execute(
            "UPDATE memory_consolidation_plans SET candidate_text=?,request_json=? "
            "WHERE batch_id=? AND revision=1",
            (*old, bid),
        )
        conn.commit()
        if boundary == "restart":
            host.close()
            conn.close()
            host, model, conn = _traced_host(tmp_path, [])
            memory = host.memory_service
            assert not model.requests
            assert memory.consolidation.get_batch(bid)["status"] == terminal
            # The current failed attempt still owns its input for retry.
            assert conn.execute(
                "SELECT request_json IS NOT NULL FROM memory_consolidation_plans "
                "WHERE batch_id=? AND revision=2",
                (bid,),
            ).fetchone() == (int(terminal == "failed"),)
        else:
            conn.execute(
                "CREATE TRIGGER reject_cleanup BEFORE UPDATE "
                "ON memory_consolidation_plans "
                "WHEN OLD.revision=1 AND OLD.request_json IS NOT NULL "
                "AND NEW.request_json IS NULL "
                "BEGIN SELECT RAISE(ABORT, 'controlled cleanup failure'); END"
            )
            conn.commit()
            if boundary == "delete":
                command = {
                    "operation_id": "delete",
                    "kind": "semantic",
                    "action": "delete",
                    "expected_version": 1,
                    "payload": {"id": upstream},
                }
                result = memory.execute(command, CONTEXT)
                assert "error" in result
                assert memory.get("semantic", upstream) is not None
            else:
                attempt = host.retry_consolidation(
                    bid, 2, operation_id="blocked-handoff"
                )
                assert host.wait(attempt.run_id, timeout=5).outcome == "failed"
                assert memory.consolidation.get_batch(bid)["revision"] == 2
                assert len(model.requests) == 2
            assert (
                conn.execute(
                    "SELECT candidate_text,request_json "
                    "FROM memory_consolidation_plans "
                    "WHERE batch_id=? AND revision=1",
                    (bid,),
                ).fetchone()
                == old
            )
            conn.execute("DROP TRIGGER reject_cleanup")
            conn.commit()
            if boundary == "delete":
                assert "error" not in memory.execute(command, CONTEXT)
                assert memory.get("semantic", upstream) is None
            else:
                attempt = host.retry_consolidation(bid, 2, operation_id="good-handoff")
                assert host.wait(attempt.run_id, timeout=5).outcome == "completed"
        assert conn.execute(
            "SELECT candidate_text,request_json FROM memory_consolidation_plans "
            "WHERE batch_id=? AND revision=1",
            (bid,),
        ).fetchone() == (None, None)
        assert conn.execute(
            "SELECT count(*) FROM memory_consolidation_sources "
            "WHERE batch_id=? AND revision=1",
            (bid,),
        ).fetchone() == (2,)
    finally:
        host.close()
        conn.close()


@pytest.mark.parametrize("advance_before_upgrade", [False, True])
def test_legacy_generation_receipt_keeps_historical_facts(
    tmp_path, advance_before_upgrade
):
    from agent_alfred.evals.deterministic.test_memory_consolidation_runtime import (
        _traced_host,
    )

    host, model, conn = _traced_host(tmp_path, ["not-json", PLAN])
    try:
        bid = _failed(host, conn)
        service = host.memory_service.consolidation
        first_run = service.get_batch(bid)["generation_run_id"]
        operation_id = "begin:" + first_run
        before = service.get_action(operation_id)
        if advance_before_upgrade:
            retry = host.retry_consolidation(bid, 1, operation_id="successor")
            assert host.wait(retry.run_id, timeout=5).outcome == "completed"
        # Exact old receipt shape, retaining its original audit fingerprint.
        conn.execute(
            "UPDATE memory_consolidation_actions SET receipt=? WHERE operation_id=?",
            (
                json.dumps(
                    {"action": "begin_generation", "batch_id": bid, "revision": 1}
                ),
                operation_id,
            ),
        )
        conn.commit()
        host.close()
        conn.close()
        host, model, conn = _traced_host(tmp_path, [PLAN])
        service = host.memory_service.consolidation
        historical = service.get_action(operation_id)
        assert historical["status"] == "failed" and historical["revision"] == 1
        assert "products" not in historical and "plan" not in historical
        if not advance_before_upgrade:
            assert historical == before
            retry = host.retry_consolidation(bid, 1, operation_id="successor")
            assert host.wait(retry.run_id, timeout=5).outcome == "completed"
            assert service.get_action(operation_id) == before
        else:
            assert historical["error"]["code"] == "invalid_plan_output"
            assert not model.requests
    finally:
        host.close()
        conn.close()


def test_legacy_prepare_failure_keeps_confirmed_error(tmp_path, monkeypatch):
    import sqlite3

    from agent_alfred.memory.semantic import SQLiteSemanticStore

    host, model, conn = _host(tmp_path, script=["not-json", PLAN])
    try:
        bid = _failed(host, conn)
        service = host.memory_service.consolidation

        def unavailable(*args, **kwargs):
            raise sqlite3.OperationalError("controlled FTS failure")

        with monkeypatch.context() as patch:
            patch.setattr(SQLiteSemanticStore, "search", unavailable)
            failed = host.retry_consolidation(bid, 1, operation_id="retry-failure")
            assert host.wait(failed.run_id, timeout=5).outcome == "failed"
        action = "begin:" + failed.run_id
        legacy = json.loads(
            conn.execute(
                "SELECT receipt FROM memory_consolidation_actions WHERE operation_id=?",
                (action,),
            ).fetchone()[0]
        )
        legacy.pop("generation_run_id")
        successor = host.retry_consolidation(bid, 2, operation_id="successor")
        assert host.wait(successor.run_id, timeout=5).outcome == "completed"
        # s3j begin receipts kept prepare failures but no Run or archive. The
        # matching retry intent also predates s3j, so only begin evidence exists.
        conn.execute(
            "UPDATE memory_consolidation_actions SET receipt=? WHERE operation_id=?",
            (json.dumps(legacy), action),
        )
        conn.execute(
            "UPDATE memory_consolidation_actions SET receipt=? WHERE operation_id=?",
            (
                json.dumps(
                    {
                        "action": "retry",
                        "batch_id": bid,
                        "revision": 1,
                        "status": "generation_required",
                    }
                ),
                "retry-failure",
            ),
        )
        conn.commit()
        historical = service.get_action(action)
        assert historical["status"] == "failed" and historical["revision"] == 2
        assert historical["error"] == legacy["error"]
        assert "products" not in historical and "plan" not in historical
        assert len(model.requests) == 2
    finally:
        host.close()
        conn.close()
