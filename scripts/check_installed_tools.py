import json
import tempfile
from pathlib import Path

from agent_alfred.messages import ToolCallBlock
from agent_alfred.model import (
    AttemptRecord,
    ModelRef,
    ModelResponse,
    ModelResult,
    ScriptedModel,
    ScriptedModelFactory,
    Usage,
)
from agent_alfred.runtime.host import SubmitRequest
from agent_alfred.tools.files import FileTools
from agent_alfred.wiring import build_default_host


def call(name, args):
    return ModelResult(
        (AttemptRecord(name, False, "committed", Usage()),),
        ModelResponse(
            (ToolCallBlock(name, name, args),), "tool_use", ModelRef("test", "test")
        ),
        None,
    )


def gate():
    return '{"retrieve":false,"query":null,"reason_code":"greeting"}'


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    builtin = root / "builtin" / "demo"
    builtin.mkdir(parents=True)
    (builtin / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo\n---\nOriginal"
    )
    model = ScriptedModel(
        [
            gate(),
            call("draft_message", {"body": "installed draft"}),
            "ok",
            gate(),
            call(
                "create_skill",
                {"name": "demo", "description": "New", "body": "New body"},
            ),
            "confirm",
        ]
    )
    host = build_default_host(
        state_dir=root / "state",
        skill_builtin=builtin.parent,
        factory=ScriptedModelFactory(model),
    )
    host.start()
    try:
        result = host.wait(host.submit(SubmitRequest(message="draft")).run_id)
        assert result.outcome == "completed"
        receipt = json.loads(model.requests[2].messages[-1].blocks[0].content[0].text)
        assert Path(receipt["path"]).read_text() == "installed draft\n"
        result = host.wait(host.submit(SubmitRequest(message="create skill")).run_id)
        candidate = json.loads(
            model.requests[-1].messages[-1].blocks[0].content[0].text
        )["candidate_id"]
        result = host.wait(
            host.submit(SubmitRequest(message="确认创建 " + candidate)).run_id
        )
        assert result.outcome == "completed"
    finally:
        host.close()
    original = FileTools.publish

    def uncertain(self, target, content):
        original(self, target, content)
        raise OSError("after publication")

    FileTools.publish = uncertain
    model = ScriptedModel([gate(), call("draft_message", {"body": "recover me"})])
    host = build_default_host(
        state_dir=root / "state", factory=ScriptedModelFactory(model)
    )
    host.start()
    try:
        result = host.wait(host.submit(SubmitRequest(message="draft")).run_id)
        assert result.outcome == "failed"
        op = result.memory_telemetry["file_operation_id"]
    finally:
        host.close()
    FileTools.publish = original
    host = build_default_host(
        state_dir=root / "state", factory=ScriptedModelFactory(ScriptedModel([]))
    )
    host.start()
    try:
        result = host.wait(host.submit(SubmitRequest(message="恢复操作 " + op)).run_id)
        assert json.loads(result.reply.blocks[0].text)["state"] == "complete"
    finally:
        host.close()
print("PASS: installed artifact draft, confirmation, restart recovery")
