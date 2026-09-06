"""Reply recovery through the Host and Dashboard public interfaces (ADR-0029)."""

import json
import sqlite3
import sys
import threading

import pytest

from agent_alfred import schema
from agent_alfred.evals.deterministic._monitoring_test_helpers import (
    claimed_monitoring_tool,
    interrupt_py_return_once,
)
from agent_alfred.evals.deterministic._web_runtime_test_helpers import (
    FailFinalizeWhen,
    SelectiveLatch,
    build_runtime_host,
)
from agent_alfred.gateway.web.api import DashboardApi
from agent_alfred.redact import Redactor
from agent_alfred.runtime.replies import ReplyUnavailable
from agent_alfred.runtime.work import SubmitRequest


class _PausedCommitConnection(sqlite3.Connection):
    """A real SQLite connection held inside commit while the Store is locked."""

    commit_gate: SelectiveLatch | None = None

    def commit(self):
        if self.commit_gate is not None:
            self.commit_gate.wait()
        return super().commit()


def test_pending_reply_does_not_wait_for_the_database_write_lock():
    database = sqlite3.connect(
        ":memory:",
        check_same_thread=False,
        factory=_PausedCommitConnection,
    )
    schema.migrate(database)
    saving, committing = SelectiveLatch(), SelectiveLatch()
    saving.arm()
    committing.arm()
    host, conn = build_runtime_host(conn=database, before_recording_commit=saving)
    completed = threading.Event()
    answers = []
    reader = None
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        assert saving.entered.wait(2)
        database.commit_gate = committing
        saving.release()
        assert committing.entered.wait(2)

        def read():
            try:
                answers.append(
                    host.recover_reply(
                        process_instance_id=host.process_instance_id,
                        session_id=submitted.session_id,
                        run_id=submitted.run_id,
                    )
                )
            finally:
                completed.set()

        reader = threading.Thread(target=read)
        reader.start()
        assert completed.wait(2), "reply read waited for the held database lock"
        assert answers[0].reply_text == "pong"
    finally:
        saving.release()
        committing.release()
        if reader is not None:
            reader.join(2)
        host.close()
        conn.close()


def test_recorded_reply_exposes_only_text_blocks_in_order():
    host, conn = build_runtime_host()
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        host.wait(submitted.run_id)
        blocks = [
            {"type": "text", "text": "first"},
            {"type": "thinking", "text": "private thinking"},
            {"type": "tool_call", "id": "c", "name": "tool", "input": {"x": "private"}},
            {"type": "text", "text": " last"},
        ]
        conn.execute(
            "UPDATE agent_log SET content = ? WHERE run_id = ? AND role = 'assistant'",
            (json.dumps(blocks), submitted.run_id),
        )
        conn.commit()
        reply = host.recover_reply(
            process_instance_id=host.process_instance_id,
            session_id=submitted.session_id,
            run_id=submitted.run_id,
        )
        assert reply.reply_text == "first last"
    finally:
        host.close()
        conn.close()


@pytest.mark.parametrize("content", [None, {}, [None], [{"type": "text", "text": {}}]])
def test_unreadable_recorded_body_is_not_presented_as_a_complete_reply(content):
    host, conn = build_runtime_host()
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        host.wait(submitted.run_id)
        conn.execute(
            "UPDATE agent_log SET content = ? WHERE run_id = ? AND role = 'assistant'",
            (json.dumps(content), submitted.run_id),
        )
        conn.commit()
        assert DashboardApi(facade=host).recover_reply(
            {
                "process_instance_id": host.process_instance_id,
                "session_id": submitted.session_id,
                "run_id": submitted.run_id,
            }
        ) == (503, {"code": "reply_unavailable"})
    finally:
        host.close()
        conn.close()


@pytest.mark.parametrize("saved", [False, True])
def test_reply_recovery_redacts_secrets_known_at_read_time(saved):
    saving = SelectiveLatch()
    saving.arm()
    redactor = Redactor([])
    host, conn = build_runtime_host(
        ["reply newly-known-secret"],
        redactor=redactor,
        before_recording_commit=saving,
    )
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        assert saving.entered.wait(2)
        if saved:
            saving.release()
            host.wait(submitted.run_id)
        redactor.remember("newly-known-secret")
        reply = host.recover_reply(
            process_instance_id=host.process_instance_id,
            session_id=submitted.session_id,
            run_id=submitted.run_id,
        )
        assert reply.reply_text == "reply ***"
    finally:
        saving.release()
        host.close()
        conn.close()


@pytest.mark.parametrize("saved", [False, True])
def test_reply_redaction_failure_is_read_unavailable_and_can_be_retried(saved):
    saving = SelectiveLatch()
    saving.arm()
    host, conn = build_runtime_host(before_recording_commit=saving)
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        assert saving.entered.wait(2)
        if saved:
            saving.release()
            host.wait(submitted.run_id)
        before = host.snapshot()
        identity = {
            "process_instance_id": host.process_instance_id,
            "session_id": submitted.session_id,
            "run_id": submitted.run_id,
        }
        api = DashboardApi(facade=host)
        with interrupt_py_return_once(
            "reply-redaction-failure", Redactor.redact_text.__code__,
            RuntimeError("synthetic redaction failure"),
        ) as armed:
            try:
                response = api.recover_reply(identity)
            finally:
                assert armed == [False], "redaction failure injection was not reached"
        assert response == (503, {"code": "reply_unavailable"})
        assert host.snapshot() == before
        assert api.recover_reply(identity) == (200, {**identity, "reply_text": "pong"})
        assert host.snapshot() == before
    finally:
        saving.release()
        host.close()
        conn.close()


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("withheld", [False, True])
def test_reply_recovery_distinguishes_withheld_text_from_a_literal_marker(
    failed, withheld,
):
    raw_reply = "synthetic reply" if withheld else "<redaction failed; text withheld>"
    saving = SelectiveLatch()
    saving.arm()
    database = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(database)
    flag = {"armed": False}
    conn = FailFinalizeWhen(database, flag, "finished_at")
    host, _ = build_runtime_host([raw_reply], conn=conn, before_recording_commit=saving)
    injected = threading.Event()
    code = Redactor.redact_text.__code__
    host.start()
    try:
        with claimed_monitoring_tool(
            "projection-redaction-failure", local_codes=(code,),
        ) as tool_id:

            def fail_reply_redaction(actual_code, offset, result):
                if withheld and actual_code is code and result == raw_reply:
                    injected.set()
                    raise RuntimeError("synthetic projection redaction failure")

            sys.monitoring.register_callback(
                tool_id, sys.monitoring.events.PY_RETURN, fail_reply_redaction,
            )
            sys.monitoring.set_local_events(
                tool_id, code, sys.monitoring.events.PY_RETURN,
            )
            submitted = host.submit(SubmitRequest(message="hello"))
            assert saving.entered.wait(2)
            assert injected.is_set() is withheld
        if failed:
            flag["armed"] = True
            saving.release()
            host.wait(submitted.run_id)
        before = host.snapshot()
        assert before.coordinator_state == (
            "recording_failed" if failed else "recording_pending"
        )
        identity = {
            "process_instance_id": host.process_instance_id,
            "session_id": submitted.session_id,
            "run_id": submitted.run_id,
        }
        api = DashboardApi(facade=host)
        expected = (
            (503, {"code": "reply_unavailable"})
            if withheld else (200, {**identity, "reply_text": raw_reply})
        )
        assert api.recover_reply(identity) == expected
        assert host.snapshot() == before
        if not failed:
            saving.release()
            host.wait(submitted.run_id)
            assert api.recover_reply(identity) == (
                200, {**identity, "reply_text": raw_reply},
            )
    finally:
        flag["armed"] = False
        saving.release()
        host.close()
        conn.close()


def test_saved_reply_database_failure_is_read_unavailable_not_recording_failure():
    database = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(database)
    flag = {"armed": False}
    conn = FailFinalizeWhen(database, flag, "SELECT")
    host, _ = build_runtime_host(conn=conn)
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        host.wait(submitted.run_id)
        before = host.snapshot()
        flag["armed"] = True
        assert DashboardApi(facade=host).recover_reply(
            {
                "process_instance_id": host.process_instance_id,
                "session_id": submitted.session_id,
                "run_id": submitted.run_id,
            }
        ) == (503, {"code": "reply_unavailable"})
        assert host.snapshot() == before
    finally:
        flag["armed"] = False
        host.close()
        conn.close()


@pytest.mark.parametrize("missing", ["process_instance_id", "session_id", "run_id"])
def test_http_reply_requires_each_identity_field(missing):
    host, conn = build_runtime_host()
    try:
        identity = {"process_instance_id": "p", "session_id": "s", "run_id": "r"}
        del identity[missing]
        assert DashboardApi(facade=host).recover_reply(identity) == (
            400,
            {"code": f"missing_{missing}"},
        )
    finally:
        host.close()
        conn.close()


def test_pending_reply_recovery_returns_complete_redacted_text() -> None:
    saving = SelectiveLatch()
    saving.arm()
    text = "正文" * 3000 + " secret-recovery-value 完"
    host, conn = build_runtime_host(
        [text],
        before_recording_commit=saving,
        redactor=Redactor(["secret-recovery-value"]),
    )
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        assert saving.entered.wait(2)

        reply = host.recover_reply(
            process_instance_id=host.process_instance_id,
            session_id=submitted.session_id,
            run_id=submitted.run_id,
        )

        assert reply.reply_text == "正文" * 3000 + " *** 完"
        assert (reply.process_instance_id, reply.session_id, reply.run_id) == (
            host.process_instance_id,
            submitted.session_id,
            submitted.run_id,
        )
        assert host.snapshot().coordinator_state == "recording_pending"
    finally:
        saving.release()
        host.close()
        conn.close()


@pytest.mark.parametrize(
    "field,expected",
    [
        ("process_instance_id", (409, {"code": "reply_context_expired"})),
        ("session_id", (503, {"code": "reply_unavailable"})),
        ("run_id", (503, {"code": "reply_unavailable"})),
    ],
)
def test_http_reply_never_substitutes_a_different_identity(field, expected):
    host, conn = build_runtime_host()
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        host.wait(submitted.run_id)
        identity = {
            "process_instance_id": host.process_instance_id,
            "session_id": submitted.session_id,
            "run_id": submitted.run_id,
        }
        identity[field] = "not-the-requested-identity"
        assert DashboardApi(facade=host).recover_reply(identity) == expected
    finally:
        host.close()
        conn.close()


def test_http_reply_contract_returns_only_identity_and_complete_content():
    saving = SelectiveLatch()
    saving.arm()
    host, conn = build_runtime_host(["正文" * 3000], before_recording_commit=saving)
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        assert saving.entered.wait(2)
        identity = {
            "process_instance_id": host.process_instance_id,
            "session_id": submitted.session_id,
            "run_id": submitted.run_id,
        }
        status, payload = DashboardApi(facade=host).recover_reply(identity)
        assert status == 200
        assert payload == {**identity, "reply_text": "正文" * 3000}
        assert DashboardApi(facade=host).recover_reply(identity) == (status, payload)
        assert host.snapshot().coordinator_state == "recording_pending"
    finally:
        saving.release()
        host.close()
        conn.close()


def test_old_process_reply_context_is_rejected_even_when_the_reply_was_saved():
    host, conn = build_runtime_host()
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        host.wait(submitted.run_id)
        with pytest.raises(ReplyUnavailable, match="context expired"):
            host.recover_reply(
                process_instance_id="previous-process",
                session_id=submitted.session_id,
                run_id=submitted.run_id,
            )
    finally:
        host.close()
        conn.close()


def test_saved_reply_recovery_reads_the_original_run_after_projection_retirement():
    host, conn = build_runtime_host(["original reply", "later reply"])
    host.start()
    try:
        first = host.submit(SubmitRequest(message="first"))
        host.wait(first.run_id)
        second = host.submit(
            SubmitRequest(
                message="second",
                session_id=first.session_id,
            )
        )
        host.wait(second.run_id)

        reply = host.recover_reply(
            process_instance_id=host.process_instance_id,
            session_id=first.session_id,
            run_id=first.run_id,
        )

        assert reply.reply_text == "original reply"
        assert reply.run_id == first.run_id
    finally:
        host.close()
        conn.close()
