"""SPEC-04: exact approved combinations, through real Run and transport seams."""

import json
import shutil
from pathlib import Path

import httpx2
import pytest
from openai import OpenAI

from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic.test_runtime_skills import (
    SKIP,
    runtime,
    skill,
    submit,
    system,
)
from agent_alfred.events import CapturingSink
from agent_alfred.memory.commands import CommandContext
from agent_alfred.memory.types import ManualOrigin
from agent_alfred.messages import message_plain_text
from agent_alfred.model import ModelAssignment, ModelRef, ScriptedModel
from agent_alfred.openai_compatible import OpenAICompatibleAdapter
from agent_alfred.retry import RetryPolicy
from agent_alfred.runtime.config import (
    MutableAssignmentProvider,
    StoreBackedSnapshotProvider,
)
from agent_alfred.runtime.model_settings import ModelSettingsStore
from agent_alfred.settings import DEFAULT_ENDPOINT_ID, OPENCODE_API_KEY_ENV, Settings
from agent_alfred.stream_fallback import StreamFallback


def test_ce13_answer_retry_and_tool_roundtrip_keep_snapshot_then_next_run_reselects(
    tmp_path,
):
    clock = FakeClock()
    original = skill(tmp_path / "builtin", "A", "A ORIGINAL PROCEDURE")
    skill(tmp_path / "builtin", "B", "B NEXT PROCEDURE")
    sent = []

    class ChangeAfterTool(CapturingSink):
        def commit(self, prepared, event):
            super().commit(prepared, event)
            if event.payload.name == "tool.finished":
                original.write_text("---\nname: A\ndescription: changed\n---\nNEW")

    def dispatch(request):
        body = json.loads(request.content)
        sent.append(body)
        if len(sent) == 2:
            return httpx2.Response(500, json={"error": {"message": "retry fixture"}})
        message = {"role": "assistant", "content": "answer"}
        if len(sent) == 1:
            message.update(
                content="A new clue suggests B",
                tool_calls=[
                    {
                        "id": "query",
                        "type": "function",
                        "function": {"name": "query_events", "arguments": "{}"},
                    }
                ],
            )
        return httpx2.Response(
            200,
            json={
                "id": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if len(sent) == 1 else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7},
            },
        )

    with httpx2.Client(transport=httpx2.MockTransport(dispatch)) as http:
        sdk = OpenAI(api_key="fixture", http_client=http, max_retries=0)
        adapter = OpenAICompatibleAdapter(client=sdk, model=ModelRef("answer", "a"))

        class NoSleep:
            def sleep(self, delay):
                raise AssertionError("zero delay must not sleep")

        answer = RetryPolicy(
            StreamFallback(adapter, clock=clock),
            clock=clock,
            sleeper=NoSleep(),
            retry_delay_s=0,
        )
        judgments = ScriptedModel(['{"skills":["A"]}', SKIP, '{"skills":["B"]}', SKIP])

        class Factory:
            def create(self, snapshot):
                return judgments if snapshot.endpoint_id == "judgment" else answer

        provider = MutableAssignmentProvider(
            endpoint_id="answer",
            model_id="a",
            wire_style="openai",
            api_key="key",
            retrieval_gate=ModelAssignment("judgment", "j", "openai"),
        )
        with runtime(
            tmp_path,
            [],
            factory=Factory(),
            clock=clock,
            snapshot_provider=provider,
            extra_sinks=(ChangeAfterTool(),),
        ) as (
            host,
            _,
            capture,
        ):
            first, result = submit(host, "initial task")
            assert result.outcome == "completed" and result.step_count == 4
            assert len(sent) == 3 and len(judgments.requests) == 2
            assert "NEW" in original.read_text()
            systems = [
                [m for m in p["messages"] if m["role"] == "system"] for p in sent
            ]
            assert systems[0] == systems[1] == systems[2]
            assert "A ORIGINAL PROCEDURE" in json.dumps(systems[0])
            assert "B NEXT PROCEDURE" not in json.dumps(systems[0])
            evidence = host.read_run_evidence(first.run_id, trace_root=tmp_path)
            inputs = evidence["memory"]["input_attempts"]
            answers = [a for a in inputs if a["purpose"] == "answer"]
            assert len(answers) == 3
            assert answers[0]["skills"] == answers[1]["skills"] == answers[2]["skills"]
            assert len({a["attempt_id"] for a in answers}) == 3
            assert len(evidence["attempts"]) == 5
            assert sum(e.payload.name == "tool.finished" for e in capture.events) == 1
            _, next_result = submit(host, "new task", first.session_id)
            assert next_result.outcome == "completed" and len(judgments.requests) == 4
            final_system = json.dumps(
                [m for m in sent[-1]["messages"] if m["role"] == "system"]
            )
            assert "B NEXT PROCEDURE" in final_system
            assert "A ORIGINAL PROCEDURE" not in final_system


@pytest.mark.parametrize("retrieve", [False, True])
def test_ce18_long_skill_with_empty_or_full_escaped_retrieval_freezes_same_window(
    tmp_path, retrieve
):
    skill(tmp_path / "builtin", "A", "长" * 7900)
    # Fixed Skill + maximum escaped retrieval fits; adding the old group does not.
    settings = Settings(input_character_limit=64000, per_store_character_budget=4000)
    lookup = (
        '{"retrieve":true,"query":"coriander","reason_code":"personal_information"}'
    )
    with runtime(
        tmp_path,
        [
            SKIP,
            "old" * 2500,
            '{"skills":["A"]}',
            lookup if retrieve else SKIP,
            "answer",
        ],
        settings=settings,
    ) as (host, model, _):
        first, previous = submit(host, "/skills off\n" + "hello " * 1200)
        assert previous.outcome == "completed"
        context = CommandContext(ManualOrigin("web"), "web")
        for kind, field in (("semantic", "fact"), ("episodic", "summary")):
            payload = {field: "coriander"}
            if kind == "semantic":
                payload["subject"] = "herb"
            else:
                payload.update(
                    occurred_at="2026-09-09T12:00:00+00:00", occurred_until=None
                )
            saved = host.memory_service.execute(
                {
                    "operation_id": "seed-" + kind,
                    "kind": kind,
                    "action": "save",
                    "payload": payload,
                },
                context,
            )
            assert saved["status"] == "saved"
            item = {
                "id": saved["memory_id"],
                "version": 2,
                **payload,
                "origin": {"type": "manual", "source": "web"},
            }
            remaining = 4000 - len(
                json.dumps([item], ensure_ascii=False, separators=(",", ":"))
            )
            changed = host.memory_service.execute(
                {
                    "operation_id": "fill-" + kind,
                    "kind": kind,
                    "action": "update",
                    "expected_version": 1,
                    "payload": {
                        "id": saved["memory_id"],
                        field: "coriander"
                        + '"' * (remaining // 2)
                        + "a" * (remaining % 2),
                    },
                },
                context,
            )
            assert "error" not in changed
        accepted, result = submit(host, "coriander", first.session_id)
        assert result.outcome == "completed"
        evidence = host.read_run_evidence(accepted.run_id, trace_root=tmp_path)
        inputs = evidence["memory"]["input_attempts"]
        assert inputs[0]["working_history_groups"] == [first.run_id]
        assert (
            inputs[1]["working_history_groups"]
            == inputs[2]["working_history_groups"]
            == []
        )
        assert "长" * 7900 not in system(model.requests[-2])
        assert "长" * 7900 in system(model.requests[-1])
        assert len(inputs[-1]["references"]) == (2 if retrieve else 0)
        if retrieve:
            reference = message_plain_text(model.requests[-1].messages[0])
            lengths = [
                len(line.split("=", 1)[1])
                for line in reference.splitlines()
                if line.startswith(("semantic=", "episodic="))
            ]
            assert lengths == [4000, 4000]
        assert all(a["input_characters"] <= a["input_limit"] for a in inputs)


def test_ce29_existing_settings_change_reloads_both_judgments_for_next_run(tmp_path):
    example = (
        Path(__file__).resolve().parents[4] / "docs/examples/skills/checklist/SKILL.md"
    )
    target = tmp_path / "state/skills/checklist/SKILL.md"
    target.parent.mkdir(parents=True)
    shutil.copyfile(example, target)
    from agent_alfred.evals.deterministic.test_check_skills import _load_check_skills

    assert _load_check_skills().validate_skill_text(target.read_text()) == []
    store = ModelSettingsStore(tmp_path / "model_settings.json")
    store.load()
    settings = Settings()
    provider = StoreBackedSnapshotProvider(
        store,
        settings,
        environ={OPENCODE_API_KEY_ENV: "fixture"},
    )
    models, snapshots = {}, []

    class Factory:
        def create(self, snapshot):
            snapshots.append(snapshot)
            if snapshot.model_id not in models:
                models[snapshot.model_id] = ScriptedModel(
                    ['{"skills":["checklist"]}', SKIP, "answer", "next answer"]
                    if snapshot.model_id == "deepseek-v4-flash"
                    else ['{"skills":["checklist"]}', SKIP]
                )
            return models[snapshot.model_id]

    with runtime(
        tmp_path,
        [],
        factory=Factory(),
        snapshot_provider=provider,
        model_settings=store,
    ) as (host, _, _):
        _, first = submit(host, "organize")
        assert first.outcome == "completed"
        for op in ("pin", "assign"):
            body = dict(
                op=op,
                expected_revision=store.snapshot().revision,
                endpoint_id=DEFAULT_ENDPOINT_ID,
                model_id="qwen3.7-max",
            )
            if op == "assign":
                body["slot"] = "retrieval_gate"
            _, reason = host.execute_mutation(lambda: host.apply_settings(body))
            assert reason is None
        reloaded = ModelSettingsStore(tmp_path / "model_settings.json").load()
        assert reloaded.assignments.retrieval_gate.model_id == "qwen3.7-max"
        _, second = submit(host, "organize differently")
        assert second.outcome == "completed"
        gate_requests = models["qwen3.7-max"].requests
        assert len(gate_requests) == 2
        assert "Catalog: " in system(gate_requests[0])
        assert "Catalog: " not in system(gate_requests[1])
        assert [a["purpose"] for a in second.memory_telemetry["input_attempts"]] == [
            "skill_selector",
            "gate",
            "answer",
        ]
        assert host.skill_catalog.load("checklist") in system(
            models["deepseek-v4-flash"].requests[-1]
        )
        assert snapshots[-1].retrieval_gate.model_id == "qwen3.7-max"
