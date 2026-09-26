"""Closed setup plans are applied through disposable, public product services."""

import json
from copy import deepcopy

import pytest

from agent_alfred.evals.acceptance.examples_v3 import controlled_batch
from agent_alfred.evals.acceptance.runner import run_offline
from agent_alfred.evals.acceptance.schema import digest, validate


def one_case(group="conversation"):
    batch = controlled_batch()
    batch["cases"] = [next(c for c in batch["cases"] if c["group"] == group)]
    return batch


def test_unknown_setup_is_refused_before_state_or_model(tmp_path):
    batch = one_case()
    batch["cases"][0]["setup"]["python"] = "arbitrary code"
    with pytest.raises(ValueError, match="unknown_setup_field"):
        run_offline(batch, tmp_path)
    assert not list(tmp_path.iterdir())


def test_memory_sources_are_publicly_saved_deleted_and_reopened(tmp_path):
    batch = one_case("memory")
    case = batch["cases"][0]
    case["setup"] = {
        "version": 1,
        "local_tool_allowlist": [],
        "memory": [
            {
                "ref": "A",
                "kind": "semantic",
                "payload": {"subject": "handover", "fact": "handover requires a photo"},
            },
            {
                "ref": "B",
                "kind": "episodic",
                "payload": {
                    "summary": "handover included the keys",
                    "occurred_at": "2026-09-20T10:00:00+08:00",
                    "occurred_until": None,
                },
            },
            {
                "ref": "deleted",
                "kind": "semantic",
                "payload": {"subject": "private", "fact": "forgotten room 708"},
            },
        ],
        "delete_memory": ["deleted"],
    }
    case["script"] = [
        '{"retrieve":false,"query":null,"reason_code":"greeting"}',
        "unknown",
    ]
    result = run_offline(batch, tmp_path)["results"][0]
    setup = result["evidence"]["setup"]
    assert setup["source"] == "simulation"
    assert setup["status"] == "verified"
    assert setup["memory"][0]["record"]["fact"] == "handover requires a photo"
    assert setup["memory"][1]["record"]["summary"] == "handover included the keys"
    deleted = setup["memory"][2]
    assert deleted["present"] is False
    assert deleted["forgetting"]["state"] == "complete"
    assert "forgotten room 708" not in str(result)
    assert result["recorded"] is True


def test_calendar_setup_and_two_projections_preserve_truncation(tmp_path):
    batch = one_case("tools")
    profile = batch["profiles"][0]
    profile["local_tool_allowlist"] = ["query_events"]
    profile["id"] = digest({k: v for k, v in profile.items() if k != "id"})
    case = batch["cases"][0]
    case["setup"] = {
        "version": 1,
        "local_tool_allowlist": ["query_events"],
        "calendar": [
            {
                "title": f"Event{i:02}",
                "starts_at": "2026-10-12T10:00:00+08:00",
                "notes": "待核对" * 200,
            }
            for i in range(1, 41)
        ],
    }
    case["script"] = [
        '{"retrieve":false,"query":null,"reason_code":"greeting"}',
        {"tool": "query_events", "arguments": {}},
        "partial calendar",
    ]
    result = run_offline(batch, tmp_path)["results"][0]
    evidence = result["evidence"]
    assert len(evidence["setup"]["business"]["calendar"]["entries"]) == 40
    assert len(evidence["local_business"]["calendar"]["entries"]) == 40
    tool = evidence["tool_projections"]["requests"][0]
    assert tool["tool_name"] == "query_events"
    assert tool["effect"] == "local_read"
    assert tool["model"]["status"] == "available"
    assert "truncated" in tool["model"]["text"].lower()
    visible_json, _ = tool["model"]["text"].split("\n[truncated ", 1)
    visible_events = json.loads(visible_json)
    assert 0 < len(visible_events) < 40
    assert all(event["notes"] == "待核对" * 200 for event in visible_events)
    assert "Event40" in tool["audit"]["text"]
    assert tool["parameters"]["text"].strip() == "{}"
    assert result["tools"][0]["result"] == "succeeded"


def test_aggregation_consumes_explicit_sources_and_separate_session_seed(tmp_path):
    batch = one_case("aggregation")
    case = batch["cases"][0]
    case["setup"] = {
        "version": 1,
        "local_tool_allowlist": [],
        "memory": [
            {
                "ref": "A",
                "kind": "semantic",
                "payload": {"subject": "handover", "fact": "handover needs keys"},
            },
            {
                "ref": "B",
                "kind": "episodic",
                "payload": {
                    "summary": "handover found broken latch",
                    "occurred_at": "2026-09-20T10:00:00Z",
                    "occurred_until": None,
                },
            },
        ],
        "session_seed": [{"input": "handover needs photo", "output": "noted"}],
        "aggregate": {
            "keywords": "handover",
            "sources": ["semantic", "episodic", "history"],
        },
    }
    result = run_offline(batch, tmp_path)["results"][0]
    setup = result["evidence"]["setup"]
    assert setup["session_seed"][0]["source"] == "simulation"
    assert setup["session_seed"][0]["run_id"] != result["run_id"]
    assert setup["session_seed"][0]["recorded"] is True
    sources = result["evidence"]["aggregation_inputs"]["attempts"][0][
        "provided_sources"
    ]
    assert "handover needs keys" in str(sources)
    assert "handover found broken latch" in str(sources)
    assert "handover needs photo" in str(sources)
    assert setup["aggregate"]["keywords"] == "handover"
    assert result["recorded"] is True


def test_named_skill_and_case_limits_are_applied_and_do_not_leak(tmp_path):
    batch = one_case("skills")
    case = batch["cases"][0]
    case["input"] = "/skills Checklist\nReview this list"
    case["material_id"] = digest({"input": case["input"], "gold": case["gold"]})
    case["setup"] = {
        "version": 1,
        "local_tool_allowlist": [],
        "skills": [
            {
                "name": "Checklist",
                "description": "Review lists",
                "body": "Keep unknowns.",
            },
            {"name": "Invoice", "description": "Review taxes", "body": "Check taxes."},
        ],
        "limits": {"max_steps": 1},
    }
    other = deepcopy(case)
    other.update(id="isolated", input="No skills here", gold="nothing")
    other["material_id"] = digest({"input": other["input"], "gold": other["gold"]})
    other["setup"] = {"version": 1, "local_tool_allowlist": []}
    batch["cases"].append(other)
    results = run_offline(batch, tmp_path)["results"]
    setup = results[0]["evidence"]["setup"]
    assert [s["name"] for s in setup["skills"]] == ["Checklist", "Invoice"]
    assert setup["limits"]["max_steps"] == 1
    assert any("Keep unknowns." in str(e) for e in results[0]["evidence"]["events"])
    assert results[1]["evidence"]["setup"]["skills"] == []
    assert "Keep unknowns." not in str(results[1])


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("local_tool_allowlist", ["create_event"], "case_tool_mask_exceeds_profile"),
        ("limits", {"max_steps": 999}, "case_limit_exceeds_profile"),
        ("limits", {"python": 1}, "invalid_setup_limit"),
        ("skills", [{"name": "../outside", "description": "x", "body": "x"}], "skill"),
        ("fault_fixture", "arbitrary_python", "unknown_fault_fixture"),
    ],
)
def test_setup_cannot_expand_authority(field, value, error):
    batch = one_case()
    batch["cases"][0]["setup"][field] = value
    with pytest.raises(ValueError, match=error):
        validate(batch)


def test_existing_projection_seam_causes_real_graph_recovery(tmp_path):
    batch = one_case("routing")
    case = batch["cases"][0]
    case["setup"].update(
        fault_fixture="prepared_context_projection_error_v1",
        session_seed=[{"input": "Power off; grounding unknown", "output": "noted"}],
    )
    result = run_offline(batch, tmp_path)["results"][0]
    fixture = result["evidence"]["setup"]["fault_fixture"]
    assert fixture["id"] == "prepared_context_projection_error_v1"
    assert fixture["triggered"] is True
    assert len(fixture["observations"]) == 1
    assert any(
        e["envelope"].get("node_id") == "recover_context"
        for e in result["evidence"]["events"]
    )
    assert result["recorded"] is True


def test_persona_seed_uses_public_versioned_update_and_reopen(tmp_path):
    batch = one_case()
    batch["cases"][0]["setup"]["persona"] = "回答使用中文"
    result = run_offline(batch, tmp_path)["results"][0]
    assert (
        result["evidence"]["setup"]["business"]["persona"]["content"] == "回答使用中文"
    )
    assert result["evidence"]["setup"]["persona_receipt"]["source"] == "simulation"
    assert result["evidence"]["local_business"]["persona"]["content"] == "回答使用中文"


def test_failure_evidence_does_not_become_success_after_reopen(tmp_path):
    batch = one_case("tools")
    profile = batch["profiles"][0]
    profile["local_tool_allowlist"] = ["update_persona"]
    profile["id"] = digest({k: v for k, v in profile.items() if k != "id"})
    case = batch["cases"][0]
    case["setup"] = {"version": 1, "local_tool_allowlist": ["update_persona"]}
    case["script"] = [
        '{"retrieve":false,"query":null,"reason_code":"greeting"}',
        {
            "tool": "update_persona",
            "arguments": {"content": "must not publish", "expected_version": "stale"},
        },
        "failed",
    ]
    result = run_offline(batch, tmp_path)["results"][0]
    evidence = result["evidence"]
    assert result["tools"][0]["result"] == "failed"
    assert (
        "version conflict"
        in evidence["tool_projections"]["requests"][0]["audit"]["text"]
    )
    assert (
        evidence["local_business"]["persona"]["content"] == profile["inputs"]["persona"]
    )
    assert evidence["tool_verifications_after_reopen"][0]["state"] == "unrecorded"
    assert [t["name"] for t in evidence["setup"]["capabilities"]] == ["update_persona"]
    assert evidence["setup"]["behaviour"]["enabled"] is False


def test_low_gate_limit_fails_before_routing_without_claiming_coverage(tmp_path):
    batch = one_case("routing")
    case = batch["cases"][0]
    case["setup"]["limits"] = {"gate_input_character_limit": 1}
    case["script"] = []
    result = run_offline(batch, tmp_path)["results"][0]
    assert result["outcome"] == "failed"
    assert result["error"] == "input_limit_exceeded"
    assert result["evidence"]["attempts"] == []
    assert result["evidence"]["setup"]["behaviour"]["enabled"] is True
    memory = result["evidence"]["memory"]
    assert memory["input_preparation"]["status"] == "failed"
    assert memory["input_preparation"]["gate_limit"] == 1
    assert memory["routing_statistics"]["graph_entered"] is False
    assert memory["routing_statistics"]["fallback"] is False


def test_missing_saved_projection_stays_unknown_and_keeps_durable_failure(tmp_path):
    from agent_alfred.connections import CredentialOverlay
    from agent_alfred.evals.acceptance.artifacts import tool_projections
    from agent_alfred.model import ScriptedModel, ScriptedModelFactory
    from agent_alfred.settings import Settings
    from agent_alfred.wiring import build_default_host

    batch = one_case("tools")
    result = run_offline(batch, tmp_path)["results"][0]
    state = tmp_path / batch["batch_id"] / "tools"
    trace = next((state / "traces").glob("*/*/trace.jsonl"))
    trace.unlink()
    host = build_default_host(
        state_dir=state,
        settings=Settings(),
        factory=ScriptedModelFactory(ScriptedModel([])),
        credentials=CredentialOverlay({}, None),
        skill_builtin=tmp_path / "empty-skills",
        local_tool_allowlist=["draft_message"],
    )
    host.start()
    try:
        actual = tool_projections(host, state, result["run_id"])
        assert actual["status"] == "partial"
        row = actual["requests"][0]
        assert row["result"] == "succeeded"
        assert all(
            row[k]["status"] == "unknown" for k in ("parameters", "model", "audit")
        )
        assert row["verification"]["state"] == "complete"
    finally:
        host.close()


def test_artifact_count_limit_does_not_return_a_selected_prefix(tmp_path):
    from agent_alfred.evals.acceptance.artifacts import drafts

    outbox = tmp_path / "outbox"
    outbox.mkdir()
    for i in range(17):
        (outbox / f"{i}.md").write_text("synthetic")
    result = drafts(tmp_path)
    assert result["status"] == "unavailable"
    assert result["files"] == []


def test_shared_gate_limit_can_fail_only_real_classifier_preparation(tmp_path):
    from agent_alfred.evals.acceptance.examples import SKIP

    batch = one_case("routing")
    case = batch["cases"][0]
    case["input"] = (
        "请把以下设备维护规则概括为一句话：先核实设备状态，再决定是否继续；不能把等待核验说成已完成。"
    )
    case["material_id"] = digest({"input": case["input"], "gold": case["gold"]})
    case["setup"]["limits"] = {"gate_input_character_limit": 800}
    case["script"] = [SKIP, "fallback answer"]
    result = run_offline(batch, tmp_path)["results"][0]
    memory = result["evidence"]["memory"]
    assert memory["input_preparation"]["gate_characters"] < 800
    assert memory["classification_input_preparation"]["characters"] > 800
    assert memory["classification_input_preparation"]["status"] == "failed"
    assert memory["routing"]["classification"]["error"] == "classification_input_limit"
    assert memory["routing"]["fallback"]["entered"] is True
    assert memory["routing"]["fallback"]["model_requests"] == 1
    assert {a["purpose"] for a in memory["input_attempts"]} == {"gate", "answer"}
    assert len(result["evidence"]["attempts"]) == 2
    assert result["output"] == "fallback answer"
