"""Real SQLite and file acceptance combinations for issue 19."""

import json

import pytest

from agent_alfred.evals.deterministic.test_runtime_tools import calls
from agent_alfred.evals.deterministic.test_tool_recovery_edges import GATE, run
from agent_alfred.messages import ToolCallBlock
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.tools import ToolContext, ToolFailure, ToolRegistry
from agent_alfred.tools.calendar import CalendarTools
from agent_alfred.tools.files import FileTools
from agent_alfred.tools.skills import SkillTools
from agent_alfred.wiring import build_default_host


def context(call="call"):
    return ToolContext("run", 1, call, "cli", float("inf"))


@pytest.fixture
def host(tmp_path):
    value = build_default_host(
        state_dir=tmp_path / "state", factory=ScriptedModelFactory(ScriptedModel([]))
    )
    try:
        yield value
    finally:
        value.close()


@pytest.mark.parametrize("field", ["calendar_entries", "tool_ledger"])
def test_calendar_transaction_rolls_back_on_each_write_failure(host, field):
    host._conn.execute(
        f"CREATE TRIGGER deny_write BEFORE INSERT ON {field} "
        "BEGIN SELECT RAISE(ABORT,'injected'); END"
    )
    service = CalendarTools(host._file_tools.recording_store, host._file_tools._clock)
    registry = ToolRegistry(service.declarations(), clock=host._file_tools._clock)
    result = registry.execute(
        ToolCallBlock(
            "call",
            "create_event",
            {"title": "test", "starts_at": "2026-09-10T10:00:00Z"},
        ),
        context(),
    )
    assert result.block.is_error
    assert (
        host._conn.execute("SELECT count(*) FROM calendar_entries").fetchone()[0] == 0
    )
    assert host._conn.execute("SELECT count(*) FROM tool_ledger").fetchone()[0] == 0


@pytest.mark.parametrize("instant", ["2026-09-10T10:00:00", "not-a-date"])
def test_naive_calendar_rejected_without_writes(host, instant):
    service = CalendarTools(host._file_tools.recording_store, host._file_tools._clock)
    result = service.create({"title": "test", "starts_at": instant}, context())
    assert isinstance(result, ToolFailure) and result.code == "invalid_input"
    assert (
        host._conn.execute("SELECT count(*) FROM calendar_entries").fetchone()[0] == 0
    )


def test_calendar_uses_instants_and_read_does_not_write_ledger(host):
    service = CalendarTools(host._file_tools.recording_store, host._file_tools._clock)
    service.create(
        {"title": "same instant", "starts_at": "2026-09-10T18:00:00+08:00"}, context()
    )
    result = service.query(
        {"since": "2026-09-10T10:00:00Z", "until": "2026-09-10T10:00:00Z"}, context()
    )
    assert json.loads(result.content[0].text)[0]["title"] == "same instant"
    assert host._conn.execute("SELECT count(*) FROM tool_ledger").fetchone()[0] == 1


def skill_service(host, tmp_path):
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    path = builtin / "SKILL.md"
    path.write_text("---\nname: example\ndescription: Builtin\n---\nOriginal")
    return SkillTools(host._file_tools, builtin=builtin), path


def candidate(service):
    result = service.create(
        {"name": "example", "description": "Replacement", "body": "New"}, context()
    )
    return json.loads(result.content[0].text)["candidate_id"]


@pytest.mark.parametrize(
    "first,second,expected",
    [("取消", "确认创建", "cancelled"), ("确认创建", "取消", "complete")],
)
def test_confirmation_cancel_order_has_only_legal_durable_result(
    host, tmp_path, first, second, expected
):
    service, _ = skill_service(host, tmp_path)
    identity = candidate(service)
    service.candidate_action(identity, first)
    service.candidate_action(identity, second)
    path = host._file_tools.state_path / "skills" / "example" / "SKILL.md"
    assert path.exists() == (expected == "complete")
    if expected == "complete":
        receipt = json.loads(
            service.candidate_action(identity, "确认创建").content[0].text
        )
        assert receipt["state"] == "complete"
        assert "重启" in receipt["activation"]
        assert (
            host._conn.execute(
                "SELECT count(*) FROM tool_ledger WHERE tool_name='create_skill'"
            ).fetchone()[0]
            == 1
        )
    else:
        assert isinstance(service.candidate_action(identity, "确认创建"), ToolFailure)


@pytest.mark.parametrize("mutation", ["builtin", "candidate", "user"])
def test_late_approval_rechecks_candidate_and_target(host, tmp_path, mutation):
    service, builtin = skill_service(host, tmp_path)
    identity = candidate(service)
    target = service.user / "example" / "SKILL.md"
    if mutation == "builtin":
        builtin.write_text(builtin.read_text() + " changed")
    elif mutation == "candidate":
        host._conn.execute("UPDATE skill_candidates SET content=content||' changed'")
        host._conn.commit()
    else:
        target.parent.mkdir(parents=True)
        target.write_text("---\nname: example\ndescription: Human\n---\nHuman")
    result = service.candidate_action(identity, "确认创建")
    assert isinstance(result, ToolFailure)
    assert target.exists() == (mutation == "user")
    if target.exists():
        assert target.read_text().endswith("Human")


@pytest.mark.parametrize("command", ["create", "cancel", "confirm"])
def test_skill_commands_do_not_commit_caller_transaction(host, tmp_path, command):
    service, _ = skill_service(host, tmp_path)
    identity = candidate(service)
    host._conn.execute("CREATE TABLE marker(x TEXT)")
    host._conn.execute("INSERT INTO marker VALUES ('uncommitted')")
    result = (
        service.create(
            {"name": "other", "description": "Other", "body": "Body"}, context("other")
        )
        if command == "create"
        else service.candidate_action(
            identity, "取消" if command == "cancel" else "确认创建"
        )
    )
    assert isinstance(result, ToolFailure)
    assert host._conn.in_transaction
    host._conn.rollback()
    assert host._conn.execute("SELECT * FROM marker").fetchall() == []


@pytest.mark.parametrize("stage", ["publish_before", "publish_after", "ledger"])
def test_file_failure_recovery_commits_exactly_once(host, monkeypatch, stage):
    files = host._file_tools
    original = FileTools.publish

    def fault(self, *args, **kwargs):
        if stage == "publish_after":
            original(self, *args, **kwargs)
        raise OSError("publication failure")

    if stage == "ledger":
        host._conn.execute(
            "CREATE TRIGGER deny_ledger BEFORE INSERT ON tool_ledger "
            "BEGIN SELECT RAISE(ABORT,'ledger'); END"
        )
    else:
        monkeypatch.setattr(FileTools, "publish", fault)
    result = files.draft({"body": "Draft"}, context())
    assert isinstance(result, ToolFailure)
    assert result.stop_reason == "file_result_unverified"
    if stage == "ledger":
        host._conn.execute("DROP TRIGGER deny_ledger")
    monkeypatch.undo()
    for _ in range(2):
        recovered = files.recover(result.operation_id)
        assert json.loads(recovered.content[0].text)["state"] == "complete"
    assert host._conn.execute("SELECT count(*) FROM tool_ledger").fetchone()[0] == 1
    assert len(list((files.state_path / "outbox").glob("*.md"))) == 1


def test_memory_second_fact_failure_keeps_first_and_per_item_receipts(tmp_path):
    model = ScriptedModel(
        [
            GATE,
            calls(
                ToolCallBlock("one", "save_fact", {"subject": "one", "fact": "first"}),
                ToolCallBlock("two", "save_fact", {"subject": "two", "fact": "second"}),
            ),
            "Done",
        ]
    )
    host = build_default_host(
        state_dir=tmp_path / "state", factory=ScriptedModelFactory(model)
    )
    host._conn.execute(
        "CREATE TRIGGER deny_second BEFORE INSERT ON facts "
        "WHEN NEW.subject='two' BEGIN SELECT RAISE(ABORT,'injected'); END"
    )
    host.start()
    try:
        run(host, "保存两条事实")
        result = model.requests[-1].messages[-1].blocks
        assert not result[0].is_error
        assert result[1].is_error
        assert host._conn.execute("SELECT count(*) FROM facts").fetchone()[0] == 1
        assert host._conn.execute("SELECT count(*) FROM tool_ledger").fetchone()[0] == 1
    finally:
        host.close()


def test_same_content_human_file_does_not_prove_original_publication(host, monkeypatch):
    files = host._file_tools

    def before(self, *args, **kwargs):
        raise OSError("before publish")

    monkeypatch.setattr(FileTools, "publish", before)
    result = files.draft({"body": "Draft"}, context())
    monkeypatch.undo()
    path = files.state_path / "outbox" / f"{result.operation_id}.md"
    path.write_text("Draft\n")
    path.chmod(0o600)
    recovered = files.recover(result.operation_id)
    assert isinstance(recovered, ToolFailure)
    assert host._conn.execute("SELECT count(*) FROM tool_ledger").fetchone()[0] == 0


@pytest.mark.parametrize("action", ["确认创建", "取消"])
def test_cli_candidate_and_operation_commands_use_real_host(host, tmp_path, action):
    from io import StringIO

    from agent_alfred.gateway.cli import _send

    service, _ = skill_service(host, tmp_path)
    identity = candidate(service)
    # Use the service assembled for this builtin fixture at the host command seam.
    host._executor._skill_tools = service
    host.start()
    session = host.create_session()
    commands = ["查看候选 " + identity, action + " " + identity]
    if action == "确认创建":
        commands += ["查看操作 " + identity, "恢复操作 " + identity]
    for command in commands:
        output = StringIO()
        assert (
            _send(
                host,
                command,
                session,
                output,
                renderer=lambda text, out: out.write(text),
            )
            == 0
        )
        assert identity in output.getvalue()
    assert ("complete" if action == "确认创建" else "cancelled") in output.getvalue()


def test_existing_persona_same_content_and_completed_replay(host):
    from agent_alfred.settings import Settings
    from agent_alfred.tools.files import digest
    from agent_alfred.tools.persona import PersonaTools

    path = host._file_tools.state_path / "persona" / "persona.md"
    path.parent.mkdir()
    path.write_text("Existing")
    path.chmod(0o600)
    service = PersonaTools(host._file_tools, Settings())
    args = {"content": "Existing", "expected_version": digest("Existing")}
    first = service.update(args, context())
    assert not isinstance(first, ToolFailure)
    assert service.update(args, context()) == first
    assert host._conn.execute("SELECT count(*) FROM tool_ledger").fetchone()[0] == 1


def test_completed_skill_creation_replay_does_not_trip_user_protection(host, tmp_path):
    service, _ = skill_service(host, tmp_path)
    args = {"name": "new-name", "description": "Test", "body": "Test body"}
    first = service.create(args, context())
    assert not isinstance(first, ToolFailure)
    assert service.create(args, context()) == first
    changed = service.create({**args, "body": "Changed"}, context())
    assert isinstance(changed, ToolFailure)
    assert host._conn.execute("SELECT count(*) FROM tool_ledger").fetchone()[0] == 1


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_partial_file_write_has_owned_public_target_and_no_success(
    host, monkeypatch, failure
):
    from agent_alfred.managed_state import ManagedFileLease

    original = ManagedFileLease.write_all

    def partial(self, payload):
        original(self, payload[:3])
        raise failure("partial IO")

    monkeypatch.setattr(ManagedFileLease, "write_all", partial)
    if failure is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            host._file_tools.draft({"body": "abcdef"}, context())
    else:
        result = host._file_tools.draft({"body": "abcdef"}, context())
        assert isinstance(result, ToolFailure)
    monkeypatch.undo()
    assert host._conn.execute("SELECT count(*) FROM tool_ledger").fetchone()[0] == 0
    assert (
        next((host._file_tools.state_path / "outbox").glob("*.md")).read_text() == "abc"
    )
    assert not list(host._file_tools.state_path.rglob(".create-*"))


def test_cleanup_control_dominates_io_error_and_owner_is_retryable(host, monkeypatch):
    from agent_alfred.managed_state import ManagedFileLease

    original = ManagedFileLease.close
    fault = [True]

    def failed_close(self):
        if fault[0] and self.path.suffix == ".md":
            raise KeyboardInterrupt("cleanup control")
        return original(self)

    monkeypatch.setattr(ManagedFileLease, "close", failed_close)
    monkeypatch.setattr(
        FileTools,
        "record_publication_identity",
        lambda *a: (_ for _ in ()).throw(OSError("before IO")),
    )
    with pytest.raises(KeyboardInterrupt):
        host._file_tools.draft({"body": "abcdef"}, context())
    fault[0] = False
    assert host.close()


def test_real_delete_receipt_separates_cleanup_failure_and_completion(
    tmp_path, monkeypatch
):
    from agent_alfred.memory.commands import CommandContext
    from agent_alfred.memory.types import ManualOrigin

    model = ScriptedModel([])
    host = build_default_host(
        state_dir=tmp_path / "state", factory=ScriptedModelFactory(model)
    )
    context_ = CommandContext(ManualOrigin("cli"), "cli")
    memory = host.memory_service
    memory.forgetting.register_group(
        "source",
        kind="run",
        container_id="session",
        evidence="complete",
        evidence_id="proof",
        context=context_,
    )
    saved = memory.execute(
        {
            "operation_id": "seed",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "private subject", "fact": "private body"},
        },
        CommandContext(ManualOrigin("cli"), "cli", source_groups=("source",)),
    )

    class Projection:
        def invalidate(self, connection, *, memories, isolated):
            return ("fixture-file",) if memories else ()

    class FilePort:
        failed = True

        def __init__(self):
            self.attempts = []

        def verify(self, target_id, generation):
            path = tmp_path / "projection"
            return path.exists() and path.read_text() == str(generation)

        def rebuild(self, target_id, generation):
            self.attempts.append((target_id, generation))
            if self.failed:
                raise OSError("private cleanup error")
            (tmp_path / "projection").write_text(str(generation))

    port = FilePort()
    monkeypatch.setattr(memory, "_projection_participants", (Projection(),))
    monkeypatch.setattr(memory, "_cleanup_port", port)
    monkeypatch.setattr(
        memory, "_projection_inventory_evidence_id", "fixture-inventory"
    )
    # Keep the injected factory object but provide the real tool call once the
    # record identity is available.
    script = ScriptedModel(
        [
            GATE,
            calls(
                ToolCallBlock(
                    "delete",
                    "delete_memory",
                    {
                        "kind": "semantic",
                        "id": saved["memory_id"],
                        "expected_version": 1,
                    },
                )
            ),
        ]
    )
    monkeypatch.setattr(model, "respond", script.respond)
    host.start()
    try:
        result = run(host, "删除指定记忆")
        assert result.outcome == "completed"
        assert len(script.requests) == 2
        row = host._conn.execute(
            "SELECT operation_id FROM memory_operations WHERE operation_id!='seed'"
        ).fetchone()
        op = row[0]
        scopes = memory.forgetting.list_scopes(op)
        pending = [scope for scope in scopes if scope["state"] == "pending"]
        if pending:
            receipt = memory.forgetting.resolve_scope(
                {
                    "operation_id": op,
                    "action_id": "scope-confirm",
                    "scopes": [
                        {
                            "scope_id": scope["scope_id"],
                            "expected_revision": scope["revision"],
                        }
                        for scope in pending
                    ],
                },
                context_,
            )
            assert receipt["status"] == "confirmed"
        # The real mirror refresh already tried the injected failing port.
        cleanup = memory.forgetting.get_forgetting(op)
        assert port.attempts == [("fixture-file", 1)]
        assert cleanup["state"] == "failed"
        assert "cleanup_unconfirmed" in str(cleanup)
        assert memory.get_operation(op)["status"] == "deleted"
        memory.forgetting.retry_cleanup(op, context_)
        assert memory.forgetting.get_forgetting(op)["state"] == "failed"
        # Explicit retry and its admitted mirror refresh both retry honestly.
        assert port.attempts == [("fixture-file", 1)] * 3
        port.failed = False
        memory.forgetting.retry_cleanup(op, context_)
        assert memory.forgetting.get_forgetting(op)["state"] == "complete"
        assert memory.get("semantic", saved["memory_id"]) is None
        assert memory.get_operation(op)["status"] == "deleted"
        assert "private" not in str(memory.forgetting.get_forgetting(op))
    finally:
        host.close()


def test_manual_mutation_is_busy_while_run_tool_uses_its_trusted_permission(tmp_path):
    import threading

    from agent_alfred.memory.commands import CommandContext
    from agent_alfred.memory.types import ManualOrigin
    from agent_alfred.runtime.host import SubmitRequest

    entered, release = threading.Event(), threading.Event()

    class WaitingModel(ScriptedModel):
        def respond(self, *args, **kwargs):
            if not self.requests:
                entered.set()
                assert release.wait(5)
            return super().respond(*args, **kwargs)

    model = WaitingModel(
        [
            GATE,
            calls(
                ToolCallBlock(
                    "save", "save_fact", {"subject": "trusted", "fact": "fact"}
                )
            ),
            "Done",
        ]
    )
    host = build_default_host(
        state_dir=tmp_path / "state", factory=ScriptedModelFactory(model)
    )
    host.start()
    try:
        accepted = host.submit(SubmitRequest(message="明确保存事实"))
        assert entered.wait(5)
        result = host.memory_service.execute(
            {
                "operation_id": "manual",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "manual", "fact": "fact"},
            },
            CommandContext(ManualOrigin("web"), "web"),
        )
        assert result["error"]["code"] == "busy"
        release.set()
        assert host.wait(accepted.run_id).outcome == "completed"
        assert not model.requests[-1].messages[-1].blocks[0].is_error
        assert host._conn.execute("SELECT subject FROM facts").fetchall() == [
            ("trusted",)
        ]
    finally:
        release.set()
        host.close()


def test_real_run_rejects_forged_origin_and_stale_memory_version(tmp_path):
    from agent_alfred.memory.commands import CommandContext
    from agent_alfred.memory.types import ManualOrigin

    model = ScriptedModel([])
    host = build_default_host(
        state_dir=tmp_path / "state", factory=ScriptedModelFactory(model)
    )
    saved = host.memory_service.execute(
        {
            "operation_id": "seed",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "original", "fact": "keep"},
        },
        CommandContext(ManualOrigin("cli"), "cli"),
    )
    script = ScriptedModel(
        [
            GATE,
            calls(
                ToolCallBlock(
                    "stale",
                    "update_memory",
                    {
                        "kind": "semantic",
                        "id": saved["memory_id"],
                        "expected_version": 99,
                        "fact": "wrong",
                    },
                ),
                ToolCallBlock(
                    "forged",
                    "save_fact",
                    {"subject": "fake", "fact": "fake", "origin": "manual"},
                ),
            ),
            "Done",
        ]
    )
    model.respond = script.respond
    host.start()
    try:
        assert run(host).outcome == "completed"
        blocks = script.requests[-1].messages[-1].blocks
        assert (
            json.loads(blocks[0].content[0].text.splitlines()[0])["memory_code"]
            == "version_conflict"
        )
        assert (
            json.loads(blocks[1].content[0].text.splitlines()[0])["code"]
            == "invalid_input"
        )
        assert host.memory_service.get("semantic", saved["memory_id"]).fact == "keep"
        assert host._conn.execute("SELECT count(*) FROM tool_ledger").fetchone()[0] == 1
    finally:
        host.close()


@pytest.mark.parametrize("content", ["Human valid persona", "  "])
def test_next_run_validates_manual_persona_without_silent_fallback(tmp_path, content):
    model = ScriptedModel([GATE, "Done"])
    host = build_default_host(
        state_dir=tmp_path / "state", factory=ScriptedModelFactory(model)
    )
    path = host._file_tools.state_path / "persona" / "persona.md"
    path.parent.mkdir()
    path.write_text(content)
    path.chmod(0o600)
    host.start()
    try:
        result = run(host)
        if content.strip():
            assert result.outcome == "completed"
            assert model.requests[-1].system[0].text == content
        else:
            assert result.outcome == "failed"
            assert model.requests == []
    finally:
        host.close()


def test_open_return_interruption_keeps_descriptor_owned(host):
    import os
    import sys

    from agent_alfred.managed_state import ManagedDirectoryLease

    captured = []

    def interrupt(frame, event, value):
        if (
            event == "return"
            and frame.f_code is ManagedDirectoryLease.open_regular.__code__
            and value is not None
            and value.path.suffix == ".md"
        ):
            captured.append(value.fd)
            raise KeyboardInterrupt("open return boundary")
        return interrupt

    previous = sys.gettrace()
    try:
        sys.settrace(interrupt)
        with pytest.raises(KeyboardInterrupt):
            host._file_tools.draft({"body": "never written"}, context())
    finally:
        sys.settrace(previous)
    assert captured
    assert host.close()
    with pytest.raises(OSError):
        os.fstat(captured[0])


@pytest.mark.parametrize("action", ["取消", "awaiting", "invalidated"])
def test_skill_replay_keeps_candidate_identity_when_builtin_disappears(
    host, tmp_path, action
):
    service, builtin = skill_service(host, tmp_path)
    identity = candidate(service)
    if action == "取消":
        service.candidate_action(identity, "取消")
    elif action == "invalidated":
        builtin.write_text(builtin.read_text() + " changed")
        assert isinstance(service.candidate_action(identity, "确认创建"), ToolFailure)
    builtin.unlink()
    result = service.create(
        {"name": "example", "description": "Replacement", "body": "New"}, context()
    )
    assert not (service.user / "example" / "SKILL.md").exists()
    receipt = json.loads(result.content[0].text)
    assert receipt["candidate_id"] == identity
    assert (
        receipt["state"]
        == {
            "取消": "cancelled",
            "awaiting": "awaiting_confirmation",
            "invalidated": "invalidated",
        }[action]
    )
    assert host._conn.execute("SELECT count(*) FROM tool_ledger").fetchone()[0] == 0
