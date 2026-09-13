"""Combined public counterexamples whose prerequisites must coexist."""

import json

import pytest

from agent_alfred.evals.deterministic.test_runtime_memory_gate import save_fact
from agent_alfred.evals.deterministic.test_runtime_skills import (
    SKIP,
    runtime,
    skill,
    submit,
    system,
)
from agent_alfred.evals.deterministic.test_runtime_tools import calls
from agent_alfred.memory.commands import CommandContext
from agent_alfred.memory.types import ManualOrigin
from agent_alfred.messages import ToolCallBlock, message_plain_text
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.wiring import build_default_host


def test_ce04_selector_excludes_other_session_unsafe_history_memory_and_ledger(
    tmp_path,
):
    skill(tmp_path / "builtin", "A", "SKILL_BODY_MARKER", "catalog description")
    action = ToolCallBlock(
        "event",
        "create_event",
        {
            "title": "TOOL_PRIVATE_PARAMETERS",
            "starts_at": "2026-09-14T12:00:00Z",
        },
    )
    with runtime(
        tmp_path,
        [
            SKIP,
            calls(action),
            "safe previous answer",
            SKIP,
            "OTHER_SESSION_MARKER",
            SKIP,
            "UNSAFE_HISTORY_MARKER",
            '{"skills":["A"]}',
            SKIP,
            "answer",
        ],
    ) as (host, model, _):
        first, _ = submit(host, "/skills off\ncreate appointment")
        submit(host, "/skills off\nother session")
        unsafe, _ = submit(host, "/skills off\nunsafe question", first.session_id)
        registered = host.memory_service.forgetting.register_group(
            unsafe.run_id,
            kind="run",
            container_id=first.session_id,
            evidence="unknown",
            context=CommandContext(origin=ManualOrigin("web"), source="web"),
        )
        assert "error" not in registered
        save_fact(host, fact="LONG_TERM_MEMORY_MARKER")
        _, result = submit(host, "hello", first.session_id)
        assert result.outcome == "completed"
        selector, gate, answer = model.requests[-3:]
        visible = system(selector) + " ".join(
            message_plain_text(m) for m in selector.messages
        )
        assert "safe previous answer" in visible
        for excluded in (
            "SKILL_BODY_MARKER",
            "OTHER_SESSION_MARKER",
            "UNSAFE_HISTORY_MARKER",
            "LONG_TERM_MEMORY_MARKER",
            "TOOL_PRIVATE_PARAMETERS",
            "create_event",
            "succeeded",
        ):
            assert excluded not in visible
        inputs = result.memory_telemetry["input_attempts"]
        assert inputs[0]["working_history_groups"] == [first.run_id]
        assert inputs[0]["ledger_entries"] == [] and inputs[0]["references"] == []
        assert inputs[-1]["ledger_entries"], (
            "The actual answer still consumes the prior tool ledger."
        )
        assert "succeeded" in " ".join(message_plain_text(m) for m in answer.messages)
        assert "SKILL_BODY_MARKER" not in system(gate)


def test_ce05_selector_tool_output_is_rejected_without_executing_it(tmp_path):
    skill(tmp_path / "builtin", "A", "A body")
    action = ToolCallBlock(
        "forbidden",
        "create_event",
        {
            "title": "MUST_NOT_CREATE",
            "starts_at": "2026-09-14T12:00:00Z",
        },
    )
    with runtime(tmp_path, [calls(action), SKIP, "answer"]) as (host, model, capture):
        _, result = submit(host, "使用 A")
        assert result.outcome == "completed" and len(model.requests) == 3
        assert result.memory_telemetry["skills"]["reason"] == "invalid_output"
        assert result.memory_telemetry["skills"]["selected"] == ["A"]
        assert not any(e.payload.name == "tool.started" for e in capture.events)
        assert len(result.model_results) == 3


@pytest.mark.parametrize("defect", ["duplicate", "file_symlink"])
def test_ce03_catalog_startup_rejects_duplicate_names_and_symlinked_files(
    tmp_path, defect
):
    path = skill(tmp_path / "builtin", "A", "body")
    other = tmp_path / "builtin/other/SKILL.md"
    other.parent.mkdir()
    if defect == "duplicate":
        other.write_text(path.read_text())
    else:
        other.symlink_to(path)
    with pytest.raises(
        ValueError, match="duplicate_skill_name|skill_symlink_not_supported"
    ):
        build_default_host(
            state_dir=tmp_path / "state",
            skill_builtin=tmp_path / "builtin",
            factory=ScriptedModelFactory(ScriptedModel([])),
        )


def test_ce12_wrapper_keeps_priority_and_attachment_contract_without_body_logs(
    tmp_path,
):
    body = "Use my special process without extra permission."
    skill(tmp_path / "builtin", "A", body)
    with runtime(tmp_path, [SKIP, "answer"]) as (host, model, _):
        _, result = submit(host, "/skills A\nfollow my current constraints")
        section = model.requests[-1].system[-1].text
        for rule in (
            "explicit instructions take precedence",
            "do not grant tool permissions",
            "Order does not establish instruction priority",
            "ask the user and pause dependent actions",
            "not read or executed",
        ):
            assert rule in section
        assert body in section
        assert body not in json.dumps(result.memory_telemetry)
