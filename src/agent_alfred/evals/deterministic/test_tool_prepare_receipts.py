"""Preparation receipt loss must not invite a second model action."""

import sqlite3
from functools import partial

import pytest

from agent_alfred.evals.deterministic.test_runtime_tools import calls
from agent_alfred.evals.deterministic.test_tool_recovery_edges import GATE, run
from agent_alfred.messages import ToolCallBlock
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.tools import ToolContext, ToolFailure
from agent_alfred.wiring import build_default_host


def faulty_host(
    tmp_path,
    monkeypatch,
    model,
    *,
    after_commit=True,
    readback=False,
    control=False,
    rollback_error=None,
):
    class FaultConnection(sqlite3.Connection):
        pending = False
        failed = False
        block_reads = readback
        fail_rollback = rollback_error

        def execute(self, sql, *args, **kwargs):
            if (
                self.failed
                and self.block_reads
                and "SELECT state,receipt,target" in sql
            ):
                if self.block_reads == "control":
                    raise SystemExit("read-back interrupted")
                raise sqlite3.OperationalError("read-back unavailable")
            result = super().execute(sql, *args, **kwargs)
            if "INSERT INTO file_operations " in sql:
                self.pending = True
            return result

        def commit(self):
            if self.pending and not self.failed:
                self.failed = True
                self.pending = False
                if after_commit:
                    super().commit()
                if control:
                    raise SystemExit("prepare interrupted")
                raise sqlite3.OperationalError("prepare receipt lost")
            return super().commit()

        def rollback(self):
            if self.failed and self.fail_rollback:
                raise self.fail_rollback
            return super().rollback()

    original = sqlite3.connect

    def connect(path, owner, **kwargs):
        owner.capture_c_result(
            partial(original, path, **kwargs, factory=FaultConnection)
        )

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        return build_default_host(
            state_dir=tmp_path / "state", factory=ScriptedModelFactory(model)
        )


@pytest.mark.parametrize(
    "after_commit,readback,stop",
    [
        (True, False, True),
        (True, True, True),
        (False, False, False),
        (False, True, True),
    ],
)
def test_host_prepare_failure_stops_when_original_operation_may_execute(
    tmp_path,
    monkeypatch,
    after_commit,
    readback,
    stop,
):
    model = ScriptedModel(
        [
            GATE,
            calls(ToolCallBlock("first", "draft_message", {"body": "synthetic"})),
            calls(ToolCallBlock("retry", "draft_message", {"body": "synthetic"})),
            "drafted",
        ]
    )
    host = faulty_host(
        tmp_path, monkeypatch, model, after_commit=after_commit, readback=readback
    )
    host.start()
    try:
        result = run(host, "draft once")
        if stop:
            assert result.outcome == "failed"
            assert result.error == "file_result_unverified"
            assert result.memory_telemetry["file_operation_id"]
            assert len(model.requests) == 2
        else:
            assert result.outcome == "completed"
            assert len(model.requests) == 4
        host._conn.block_reads = False
        count = host._conn.execute("SELECT count(*) FROM file_operations").fetchone()[0]
        assert count == (1 if after_commit or not stop else 0)
    finally:
        host.close()
    next_model = ScriptedModel([GATE, "hello", GATE, "hello again"])
    restarted = build_default_host(
        state_dir=tmp_path / "state", factory=ScriptedModelFactory(next_model)
    )
    restarted.start()
    try:
        assert run(restarted, "hello").outcome == "completed"
        assert run(restarted, "hello again").outcome == "completed"
        expected = 1 if after_commit or not stop else 0
        assert len(list((tmp_path / "state/outbox").glob("*.md"))) == expected
        assert (
            restarted._conn.execute("SELECT count(*) FROM tool_ledger").fetchone()[0]
            == expected
        )
    finally:
        restarted.close()


@pytest.mark.parametrize("after_commit", [False, True])
def test_prepare_control_is_propagated_and_restart_has_only_original_intent(
    tmp_path,
    monkeypatch,
    after_commit,
):
    host = faulty_host(
        tmp_path,
        monkeypatch,
        ScriptedModel([]),
        after_commit=after_commit,
        control=True,
    )
    try:
        with pytest.raises(SystemExit, match="prepare interrupted"):
            host._file_tools.draft(
                {"body": "synthetic"},
                ToolContext("run", 1, "call", "cli", float("inf")),
            )
    finally:
        host.close()
    restarted = build_default_host(
        state_dir=tmp_path / "state", factory=ScriptedModelFactory(ScriptedModel([]))
    )
    try:
        restarted._file_tools.resume_pending()
        assert len(list((tmp_path / "state/outbox").glob("*.md"))) == int(after_commit)
        assert restarted._conn.execute("SELECT count(*) FROM tool_ledger").fetchone()[
            0
        ] == int(after_commit)
    finally:
        restarted.close()


@pytest.mark.parametrize("error_type", [OSError, SystemExit])
def test_prepare_rollback_failure_retains_safe_boundary(
    tmp_path, monkeypatch, error_type
):
    failure = error_type("rollback failed")
    host = faulty_host(
        tmp_path,
        monkeypatch,
        ScriptedModel([]),
        after_commit=False,
        rollback_error=failure,
    )
    try:

        def draft():
            return host._file_tools.draft(
                {"body": "synthetic"},
                ToolContext("run", 1, "call", "cli", float("inf")),
            )

        if error_type is SystemExit:
            with pytest.raises(SystemExit) as raised:
                draft()
            assert raised.value is failure
            assert isinstance(failure.__context__, sqlite3.OperationalError)
        else:
            result = draft()
            assert isinstance(result, ToolFailure)
            assert result.stop_reason == "file_result_unverified"
            assert result.operation_id
        assert host._conn.in_transaction
        assert not host._file_tools.recording_store.available
    finally:
        host._conn.fail_rollback = None
        assert host.close()
    restarted = build_default_host(
        state_dir=tmp_path / "state", factory=ScriptedModelFactory(ScriptedModel([]))
    )
    try:
        assert (
            restarted._conn.execute("SELECT count(*) FROM file_operations").fetchone()[
                0
            ]
            == 0
        )
        assert not list((tmp_path / "state/outbox").glob("*.md"))
    finally:
        restarted.close()


def test_readback_control_keeps_original_durable_operation(tmp_path, monkeypatch):
    host = faulty_host(tmp_path, monkeypatch, ScriptedModel([]), readback="control")
    try:
        with pytest.raises(SystemExit, match="read-back interrupted") as raised:
            host._file_tools.draft(
                {"body": "synthetic"},
                ToolContext("run", 1, "call", "cli", float("inf")),
            )
        assert isinstance(raised.value.__context__, sqlite3.OperationalError)
        host._conn.block_reads = False
    finally:
        host.close()
    restarted = build_default_host(
        state_dir=tmp_path / "state", factory=ScriptedModelFactory(ScriptedModel([]))
    )
    try:
        restarted._file_tools.resume_pending()
        assert len(list((tmp_path / "state/outbox").glob("*.md"))) == 1
        assert (
            restarted._conn.execute("SELECT count(*) FROM tool_ledger").fetchone()[0]
            == 1
        )
    finally:
        restarted.close()


def test_write_never_observes_or_rolls_back_callers_pending_transaction(tmp_path):
    host = build_default_host(
        state_dir=tmp_path / "state", factory=ScriptedModelFactory(ScriptedModel([]))
    )
    try:
        host._conn.execute("CREATE TABLE caller(value TEXT)")
        host._conn.execute("INSERT INTO caller VALUES ('pending')")
        result = host._file_tools.draft(
            {"body": "synthetic"}, ToolContext("run", 1, "call", "cli", float("inf"))
        )
        assert isinstance(result, ToolFailure)
        assert host._conn.in_transaction
        assert host._conn.execute("SELECT value FROM caller").fetchall() == [
            ("pending",)
        ]
        host._conn.rollback()
        assert (
            host._conn.execute("SELECT count(*) FROM file_operations").fetchone()[0]
            == 0
        )
    finally:
        host.close()
