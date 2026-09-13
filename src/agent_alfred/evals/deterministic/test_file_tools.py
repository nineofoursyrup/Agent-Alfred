"""File tools through the public RuntimeHost Run boundary."""

from pathlib import Path

from agent_alfred.evals.deterministic.test_runtime_tools import calls
from agent_alfred.messages import ToolCallBlock
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.host import SubmitRequest
from agent_alfred.wiring import build_default_host


def test_draft_creates_independent_utf8_files_and_durable_receipt(tmp_path):
    import json

    model = ScriptedModel(
        [
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(
                ToolCallBlock(
                    "one", "draft_message", {"subject": "Hello", "body": "你好"}
                ),
                ToolCallBlock("two", "draft_message", {"body": "你好"}),
            ),
            "Drafted.",
        ]
    )
    host = build_default_host(
        state_dir=tmp_path / "state", factory=ScriptedModelFactory(model)
    )
    host.start()
    try:
        result = host.wait(host.submit(SubmitRequest(message="草拟两条消息")).run_id)
        assert result.outcome == "completed"
        results = model.requests[-1].messages[-1].blocks
        receipts = [json.loads(block.content[0].text) for block in results]
        paths = [Path(receipt["path"]) for receipt in receipts]
        assert paths[0] != paths[1]
        assert all(path.parent == tmp_path / "state" / "outbox" for path in paths)
        assert all("你好" in path.read_text() for path in paths)
        assert all(receipt["state"] == "complete" for receipt in receipts)
    finally:
        host.close()


def test_published_file_failure_stops_run_and_restart_recovers_same_operation(
    tmp_path, monkeypatch
):
    import json

    from agent_alfred.tools.files import FileTools

    model = ScriptedModel(
        [
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(
                ToolCallBlock("draft", "draft_message", {"body": "body"}),
                ToolCallBlock("later", "draft_message", {"body": "not executed"}),
            ),
            "must not request",
        ]
    )
    original_publish = FileTools.publish

    def publish_then_fail(self, target, content):
        original_publish(self, target, content)
        raise OSError("injected publication receipt loss")

    monkeypatch.setattr(FileTools, "publish", publish_then_fail)
    host = build_default_host(
        state_dir=tmp_path / "state", factory=ScriptedModelFactory(model)
    )
    host.start()
    try:
        result = host.wait(host.submit(SubmitRequest(message="草拟消息")).run_id)
        assert result.outcome == "failed"
        assert result.error == "file_result_unverified"
        assert len(model.requests) == 2
        assert len(list((tmp_path / "state" / "outbox").glob("*.md"))) == 1
        op = result.memory_telemetry["file_operation_id"]
    finally:
        host.close()
    monkeypatch.undo()
    no_requests = ScriptedModel([])
    restarted = build_default_host(
        state_dir=tmp_path / "state", factory=ScriptedModelFactory(no_requests)
    )
    restarted.start()
    try:
        recovered = restarted.wait(
            restarted.submit(SubmitRequest(message="恢复操作 " + op)).run_id
        )
        assert recovered.outcome == "completed"
        assert no_requests.requests == []
        receipt = json.loads(recovered.reply.blocks[0].text)
        assert receipt["operation_id"] == op
        assert receipt["state"] == "complete"
        assert len(list((tmp_path / "state" / "outbox").glob("*.md"))) == 1
    finally:
        restarted.close()


def test_managed_persona_changes_only_next_run_and_rejects_stale_version(tmp_path):
    import hashlib

    from agent_alfred.settings import DEFAULT_PERSONA

    version = hashlib.sha256(DEFAULT_PERSONA.encode()).hexdigest()
    content = DEFAULT_PERSONA + "\nUse short sentences."
    model = ScriptedModel(
        [
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(
                ToolCallBlock(
                    "update",
                    "update_persona",
                    {"content": content, "expected_version": version},
                )
            ),
            "Updated.",
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            "Hello.",
        ]
    )
    host = build_default_host(
        state_dir=tmp_path / "state", factory=ScriptedModelFactory(model)
    )
    host.start()
    try:
        first = host.wait(host.submit(SubmitRequest(message="修改人格")).run_id)
        assert first.outcome == "completed"
        assert model.requests[2].system[0].text == DEFAULT_PERSONA
        second = host.wait(host.submit(SubmitRequest(message="hello")).run_id)
        assert second.outcome == "completed"
        assert model.requests[-1].system[0].text == content
    finally:
        host.close()


def test_create_skill_writes_valid_file_without_hot_loading(tmp_path):
    import json

    model = ScriptedModel(
        [
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(
                ToolCallBlock(
                    "skill",
                    "create_skill",
                    {
                        "name": "test-skill",
                        "description": "Test skill",
                        "body": "Do a test.",
                    },
                )
            ),
            "Created.",
        ]
    )
    host = build_default_host(
        state_dir=tmp_path / "state", factory=ScriptedModelFactory(model)
    )
    host.start()
    try:
        result = host.wait(host.submit(SubmitRequest(message="创建测试 Skill")).run_id)
        assert result.outcome == "completed"
        receipt = json.loads(model.requests[-1].messages[-1].blocks[0].content[0].text)
        assert receipt["state"] == "complete"
        assert "重启" in receipt["activation"]
        assert "Do a test." in Path(receipt["path"]).read_text()
    finally:
        host.close()


def test_builtin_skill_candidate_requires_direct_user_confirmation_after_restart(
    tmp_path,
):
    import json

    builtin = tmp_path / "builtin"
    (builtin / "example").mkdir(parents=True)
    (builtin / "example" / "SKILL.md").write_text(
        "---\nname: example\ndescription: Builtin\n---\nOriginal body."
    )
    model = ScriptedModel(
        [
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(
                ToolCallBlock(
                    "candidate",
                    "create_skill",
                    {
                        "name": "example",
                        "description": "Replacement",
                        "body": "New body.",
                    },
                )
            ),
            "Please confirm.",
        ]
    )
    host = build_default_host(
        state_dir=tmp_path / "state",
        skill_builtin=builtin,
        factory=ScriptedModelFactory(model),
    )
    host.start()
    try:
        result = host.wait(
            host.submit(SubmitRequest(message="/skills off\n创建同名 Skill")).run_id
        )
        assert result.outcome == "completed"
        receipt = json.loads(model.requests[-1].messages[-1].blocks[0].content[0].text)
        assert receipt["state"] == "awaiting_confirmation"
        candidate = receipt["candidate_id"]
        assert not (tmp_path / "state" / "skills" / "example" / "SKILL.md").exists()
    finally:
        host.close()
    model = ScriptedModel([])
    host = build_default_host(
        state_dir=tmp_path / "state",
        skill_builtin=builtin,
        factory=ScriptedModelFactory(model),
    )
    host.start()
    try:
        viewed = host.wait(
            host.submit(SubmitRequest(message="查看候选 " + candidate)).run_id
        )
        assert "New body." in viewed.reply.blocks[0].text
        confirmed = host.wait(
            host.submit(SubmitRequest(message="确认创建 " + candidate)).run_id
        )
        assert confirmed.outcome == "completed"
        receipt = json.loads(confirmed.reply.blocks[0].text)
        assert receipt["state"] == "complete"
        assert "New body." in Path(receipt["path"]).read_text()
        again = host.wait(
            host.submit(SubmitRequest(message="确认创建 " + candidate)).run_id
        )
        assert again.reply == confirmed.reply
        assert model.requests == []
    finally:
        host.close()


def test_recovery_preserves_manual_file_edit_and_allows_unrelated_new_draft(
    tmp_path, monkeypatch
):
    from agent_alfred.tools.files import FileTools

    original = FileTools.publish

    def interrupted(self, target, content):
        original(self, target, content)
        raise OSError("after publish")

    monkeypatch.setattr(FileTools, "publish", interrupted)
    model = ScriptedModel(
        [
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(ToolCallBlock("draft", "draft_message", {"body": "original"})),
        ]
    )
    host = build_default_host(
        state_dir=tmp_path / "state", factory=ScriptedModelFactory(model)
    )
    host.start()
    try:
        result = host.wait(host.submit(SubmitRequest(message="draft")).run_id)
        op = result.memory_telemetry["file_operation_id"]
    finally:
        host.close()
    monkeypatch.undo()
    path = next((tmp_path / "state" / "outbox").glob("*.md"))
    path.write_text("Manual edit")
    model = ScriptedModel(
        [
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(ToolCallBlock("other", "draft_message", {"body": "unrelated"})),
            "Done.",
        ]
    )
    host = build_default_host(
        state_dir=tmp_path / "state", factory=ScriptedModelFactory(model)
    )
    host.start()
    try:
        result = host.wait(host.submit(SubmitRequest(message="draft unrelated")).run_id)
        assert result.outcome == "completed"
        assert path.read_text() == "Manual edit"
        status = host.wait(host.submit(SubmitRequest(message="查看操作 " + op)).run_id)
        assert '"state": "conflict"' in status.reply.blocks[0].text
    finally:
        host.close()


def test_new_file_publication_does_not_overwrite_a_late_human_file(
    tmp_path, monkeypatch
):
    from agent_alfred.tools.files import FileTools

    original = FileTools.publish

    def human_arrives(self, target, content):
        path = self.state_path / target
        path.write_text("Human content")
        path.chmod(0o600)
        original(self, target, content)

    monkeypatch.setattr(FileTools, "publish", human_arrives)
    model = ScriptedModel(
        [
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(ToolCallBlock("draft", "draft_message", {"body": "generated"})),
            "Done.",
        ]
    )
    host = build_default_host(
        state_dir=tmp_path / "state", factory=ScriptedModelFactory(model)
    )
    host.start()
    try:
        result = host.wait(host.submit(SubmitRequest(message="draft")).run_id)
        assert result.outcome == "failed"
        assert (
            next((tmp_path / "state" / "outbox").glob("*.md")).read_text()
            == "Human content"
        )
    finally:
        host.close()


def test_user_skill_created_after_startup_is_preserved(tmp_path):
    import json

    model = ScriptedModel(
        [
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(
                ToolCallBlock(
                    "skill",
                    "create_skill",
                    {"name": "mine", "description": "replacement", "body": "generated"},
                )
            ),
            "Done.",
        ]
    )
    host = build_default_host(
        state_dir=tmp_path / "state", factory=ScriptedModelFactory(model)
    )
    host.start()
    path = tmp_path / "state" / "skills" / "late" / "SKILL.md"
    path.parent.mkdir(parents=True)
    original = "---\nname: mine\ndescription: Human\n---\nOriginal"
    path.write_text(original)
    try:
        host.wait(host.submit(SubmitRequest(message="创建 Skill")).run_id)
        block = model.requests[-1].messages[-1].blocks[0]
        assert block.is_error
        assert (
            json.loads(block.content[0].text.splitlines()[0])["code"]
            == "execution_error"
        )
        assert path.read_text() == original
    finally:
        host.close()


def test_explicit_persona_override_refuses_hidden_managed_write(tmp_path):
    from agent_alfred.settings import load_settings

    explicit = tmp_path / "external.md"
    explicit.write_text("External persona")
    model = ScriptedModel(
        [
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(
                ToolCallBlock(
                    "update",
                    "update_persona",
                    {"content": "new", "expected_version": "ignored"},
                )
            ),
            "Done.",
        ]
    )
    host = build_default_host(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(model),
        settings=load_settings(environ={}, persona_file=str(explicit)),
    )
    host.start()
    try:
        host.wait(host.submit(SubmitRequest(message="修改人格")).run_id)
        assert model.requests[-1].messages[-1].blocks[0].is_error
        assert explicit.read_text() == "External persona"
        assert not (tmp_path / "state" / "persona" / "persona.md").exists()
    finally:
        host.close()
