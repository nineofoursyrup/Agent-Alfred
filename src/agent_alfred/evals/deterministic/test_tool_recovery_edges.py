"""Issue 19 review regressions through the real host and durable IO seams."""

import json
import os

import pytest

from agent_alfred.evals.deterministic.test_runtime_tools import calls
from agent_alfred.managed_state import ManagedFileLease
from agent_alfred.messages import ToolCallBlock
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.host import SubmitRequest
from agent_alfred.tools import ToolContext, ToolFailure
from agent_alfred.tools.files import FileTools, digest
from agent_alfred.wiring import build_default_host

GATE = '{"retrieve":false,"query":null,"reason_code":"greeting"}'


def run(host, message="act"):
    return host.wait(host.submit(SubmitRequest(message=message)).run_id)


def test_existing_persona_update_preserves_old_inode(tmp_path):
    state = tmp_path / "state"
    model = ScriptedModel(
        [
            GATE,
            calls(
                ToolCallBlock(
                    "p",
                    "update_persona",
                    {
                        "content": "New persona",
                        "expected_version": digest("Old persona"),
                    },
                )
            ),
            "Done",
        ]
    )
    host = build_default_host(state_dir=state, factory=ScriptedModelFactory(model))
    path = state / "persona" / "persona.md"
    path.parent.mkdir()
    path.write_text("Old persona")
    path.chmod(0o600)
    host.start()
    try:
        result = run(host)
        assert result.outcome == "completed"
        assert path.read_text() == "New persona"
        assert next(path.parent.glob(".original-*.md")).read_text() == "Old persona"
    finally:
        host.close()


@pytest.mark.parametrize("late_stage", ["preserve", "publish"])
def test_persona_race_retains_human_and_unrelated_tools_work(
    tmp_path, monkeypatch, late_stage
):
    state = tmp_path / "state"
    model = ScriptedModel(
        [
            GATE,
            calls(
                ToolCallBlock(
                    "p",
                    "update_persona",
                    {"content": "Candidate", "expected_version": digest("Old persona")},
                )
            ),
            GATE,
            calls(ToolCallBlock("d", "draft_message", {"body": "Unrelated"})),
            "Done",
        ]
    )
    host = build_default_host(state_dir=state, factory=ScriptedModelFactory(model))
    path = state / "persona" / "persona.md"
    path.parent.mkdir()
    path.write_text("Old persona")
    path.chmod(0o600)
    original = getattr(FileTools, late_stage)

    def human(self, *args, **kwargs):
        if args[0] == "persona/persona.md":
            path.write_text("Human edit")
            path.chmod(0o600)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(FileTools, late_stage, human)
    host.start()
    try:
        result = run(host)
        assert result.outcome == "failed"
        assert result.error == "file_result_unverified"
        assert any(p.read_text() == "Human edit" for p in path.parent.iterdir())
        monkeypatch.undo()
        result = run(host, "draft unrelated")
        assert result.outcome == "completed"
        assert "Candidate" not in model.requests[-1].system[0].text
        assert "上次确认" in model.requests[-1].system[0].text
    finally:
        host.close()


def test_recovery_never_commits_caller_transaction(tmp_path, monkeypatch):
    host = build_default_host(
        state_dir=tmp_path / "state", factory=ScriptedModelFactory(ScriptedModel([]))
    )
    files = host._file_tools
    monkeypatch.setattr(
        FileTools, "publish", lambda *a, **k: (_ for _ in ()).throw(OSError("IO"))
    )
    result = files.draft(
        {"body": "draft"}, ToolContext("run", 1, "call", "cli", float("inf"))
    )
    monkeypatch.undo()
    try:
        host._conn.execute("CREATE TABLE caller_marker(value TEXT)")
        host._conn.execute("INSERT INTO caller_marker VALUES ('pending')")
        recovered = files.recover(result.operation_id)
        assert isinstance(recovered, ToolFailure)
        assert host._conn.in_transaction
        host._conn.rollback()
        assert host._conn.execute("SELECT * FROM caller_marker").fetchall() == []
    finally:
        host.close()


def test_staging_close_failure_remains_owned_until_retry(tmp_path, monkeypatch):
    host = build_default_host(
        state_dir=tmp_path / "state", factory=ScriptedModelFactory(ScriptedModel([]))
    )
    original = ManagedFileLease.close
    retained = []
    fault = [True]

    def blocked(self):
        if fault[0] and self.path.suffix == ".md":
            retained.append(self)
            raise OSError("before close effect")
        return original(self)

    monkeypatch.setattr(ManagedFileLease, "close", blocked)
    host._file_tools.draft(
        {"body": "draft"}, ToolContext("run", 1, "call", "cli", float("inf"))
    )
    assert retained
    assert not host.close()
    retained_fd = retained[0].fd
    os.fstat(retained_fd)
    fault[0] = False
    assert host.close()
    with pytest.raises(OSError):
        os.fstat(retained_fd)


def test_invalid_skill_metadata_returns_error_without_poisoning_restart(tmp_path):
    state = tmp_path / "state"
    model = ScriptedModel(
        [
            GATE,
            calls(
                ToolCallBlock(
                    "s",
                    "create_skill",
                    {"name": "valid", "description": " ", "body": "body"},
                )
            ),
            "Done",
        ]
    )
    host = build_default_host(state_dir=state, factory=ScriptedModelFactory(model))
    host.start()
    try:
        run(host)
        result = model.requests[-1].messages[-1].blocks[0]
        assert result.is_error
        assert (
            json.loads(result.content[0].text.splitlines()[0])["code"]
            == "invalid_input"
        )
        assert not (state / "skills" / "valid" / "SKILL.md").exists()
    finally:
        host.close()
    restarted = build_default_host(
        state_dir=state, factory=ScriptedModelFactory(ScriptedModel([]))
    )
    restarted.close()


def test_file_deadline_after_prepare_stops_before_publication(tmp_path, monkeypatch):
    from agent_alfred.clock import FakeClock

    clock = FakeClock()
    host = build_default_host(
        state_dir=tmp_path / "state", factory=ScriptedModelFactory(ScriptedModel([]))
    )
    host._file_tools._clock = clock
    original = FileTools.read_file
    reads = [0]

    def expired(self, target):
        value = original(self, target)
        reads[0] += 1
        if reads[0] == 2:
            clock.monotonic_value = 10
        return value

    monkeypatch.setattr(FileTools, "read_file", expired)
    try:
        result = host._file_tools.draft(
            {"body": "candidate"},
            ToolContext("run", 1, "call", "cli", 5, monotonic=clock.monotonic),
        )
        assert isinstance(result, ToolFailure)
        assert result.stop_reason == "file_result_unverified"
        assert not list((tmp_path / "state" / "outbox").glob("*.md"))
    finally:
        host.close()
