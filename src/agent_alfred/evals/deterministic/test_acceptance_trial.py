"""Trial public paths use offline transports; never real provider credentials."""

from copy import deepcopy

import pytest

from agent_alfred.evals.acceptance.examples import offline_batch
from agent_alfred.evals.acceptance.examples_v3 import controlled_batch
from agent_alfred.evals.acceptance.report import report
from agent_alfred.evals.acceptance.schema import validate
from agent_alfred.evals.acceptance.store import EvidenceStore


def trial_batch():
    batch = offline_batch()
    batch.update(schema_version=2, phase="trial", simulation=False)
    from agent_alfred.evals.acceptance.schema import digest

    profile = batch["profiles"][0]
    profile["local_tool_allowlist"] = ["draft_message"]
    profile["id"] = digest({k: v for k, v in profile.items() if k != "id"})
    return batch


def test_trial_import_preserves_six_cases_without_relaxing_release_sets(tmp_path):
    batch = trial_batch()
    store = EvidenceStore(tmp_path)
    store.import_batch(batch)
    saved = store.read(batch["batch_id"])
    assert len(saved["cases"]) == 6
    result = report(saved, store=store)
    assert result["v1_release"]["verdict"] == "BLOCKED"
    assert "trial_not_release_evidence" in result["v1_release"]["blockers"]
    for changes, reason in [
        ({"schema_version": 1}, "invalid_phase"),
        ({"schema_version": 3}, "invalid_phase"),
        ({"schema_version": 99}, "unknown_schema"),
        ({"simulation": True}, "trial_requires_real_execution"),
        ({"schema_version": 1, "phase": "calibration"}, "group_size"),
        ({"schema_version": 1, "phase": "formal"}, "group_size"),
        ({"cases": batch["cases"][:-1]}, "trial_requires_six_groups"),
    ]:
        changed = deepcopy(batch)
        changed.update(changes)
        with pytest.raises(ValueError, match=reason):
            validate(changed)


def authorize(batch):
    from agent_alfred.evals.acceptance.budget import binding

    batch["authorization"] = {
        "binding": binding(batch),
        "by": "offline-test",
        "at": "2026-09-20T00:00:00Z",
        "max_requests": 32,
        "max_output_tokens": 4096,
        "total_seconds": 1200,
        "cost": {"amount": None, "source": "offline transport"},
        "accept_unknown_cost": True,
    }
    return batch


def test_approved_thinking_mode_reaches_product_and_judge_http_body(tmp_path):
    import json
    from dataclasses import replace

    import httpx2 as httpx

    from agent_alfred.evals.acceptance.budget import AuthorizedBatch
    from agent_alfred.evals.acceptance.schema import digest
    from agent_alfred.evals.deterministic.test_endpoint_factory import snapshot
    from agent_alfred.model import ModelRef, ModelRequest

    batch = controlled_batch()
    profile = batch["profiles"][0]
    for model, name in [
        (profile["product_models"][0], "deepseek-flash"),
        (profile["judge_model"], "deepseek-v4-pro"),
    ]:
        model.update(endpoint_id="deepseek", model_id=name, thinking="disabled")
    profile["id"] = digest({k: v for k, v in profile.items() if k != "id"})
    from agent_alfred.evals.acceptance.judge_protocol import current_profile

    batch["judge_profile"] = current_profile(profile["judge_model"])
    from agent_alfred.evals.acceptance.store import EvidenceStore
    from agent_alfred.evals.deterministic._simulation_test_helpers import session_for

    authorize(batch)
    store = EvidenceStore(tmp_path / "evidence")
    session = session_for(batch, store)
    for role in ("product", "judge"):
        session.bind(batch, operation=role, output_scope=store.root)
    seen = []

    def send(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "test",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    budget = AuthorizedBatch(
        batch, session=session, mock_transport=httpx.MockTransport(send)
    )
    try:
        for role, name in [("product", "deepseek-flash"), ("judge", "deepseek-v4-pro")]:
            config = replace(
                snapshot(endpoint="deepseek", model=name),
                stream=False,
                stream_fallback=False,
            )
            client = budget.client(config, role=role)
            result = client.respond(
                ModelRequest(
                    ModelRef("deepseek", name),
                    None,
                    (),
                    max_tokens=(
                        profile["parameters"]["max_tokens"]
                        if role == "product"
                        else batch["authorization"]["max_output_tokens"]
                    ),
                )
            )
            assert result.response is not None
    finally:
        budget.close()
    assert len(seen) == len(budget.requests) == 2
    assert all(body.get("thinking") == {"type": "disabled"} for body in seen)
    assert [body["max_tokens"] for body in seen] == [
        profile["parameters"]["max_tokens"],
        batch["authorization"]["max_output_tokens"],
    ]


def test_trial_host_whitelist_blocks_unlisted_tool_calls_before_execution(tmp_path):
    from agent_alfred.connections import CredentialOverlay
    from agent_alfred.evals.acceptance.runner import scripted
    from agent_alfred.model import ScriptedModel, ScriptedModelFactory
    from agent_alfred.runtime.host import SubmitRequest
    from agent_alfred.settings import Settings
    from agent_alfred.wiring import build_default_host

    model = ScriptedModel(
        list(
            scripted(
                [
                    '{"retrieve":false,"query":null,"reason_code":"greeting"}',
                    {"tool": "read_persona", "arguments": {}},
                    {
                        "tool": "draft_message",
                        "arguments": {"body": "Local trial draft"},
                    },
                    "Done",
                ]
            )
        )
    )
    host = build_default_host(
        state_dir=tmp_path / "state",
        settings=Settings(max_tokens=256),
        factory=ScriptedModelFactory(model),
        credentials=CredentialOverlay({}, None),
        local_tool_allowlist=("draft_message",),
    )
    host.start()
    try:
        session = host.create_session()
        run = host.submit(SubmitRequest("Save a draft", session_id=session))
        settled = host.wait(run.run_id)
        assert settled.outcome == "completed"
        assert {tool.name for request in model.requests for tool in request.tools} == {
            "draft_message"
        }
        from agent_alfred.messages import TextBlock, ToolResultBlock

        replies = [
            block
            for request in model.requests
            for message in request.messages
            for block in message.blocks
            if isinstance(block, ToolResultBlock)
        ]
        assert any(
            block.is_error
            and any(
                "Unknown tool" in part.text
                for part in block.content
                if isinstance(part, TextBlock)
            )
            for block in replies
        )
        assert len(list((tmp_path / "state" / "outbox").glob("*.md"))) == 1
    finally:
        host.close()


@pytest.mark.parametrize("judge_correctness", ["pass", "fail", "unknown"])
def test_simulation_execute_and_judge_share_mock_wire_budget_and_isolated_host(
    tmp_path, judge_correctness
):
    import json
    from pathlib import Path

    import httpx2 as httpx

    from agent_alfred.evals.acceptance.candidate import capture
    from agent_alfred.evals.acceptance.online_judge import grade_batch
    from agent_alfred.evals.acceptance.runner import execute
    from agent_alfred.evals.acceptance.schema import digest

    root = Path(__file__).resolve().parents[4]
    batch = controlled_batch()
    batch["candidate"] = capture(root)
    batch["candidate_id"] = digest(batch["candidate"])
    profile = batch["profiles"][0]
    profile["local_tool_allowlist"] = ["draft_message"]
    for model, name in [
        (profile["product_models"][0], "deepseek-flash"),
        (profile["judge_model"], "deepseek-v4-pro"),
    ]:
        model.update(endpoint_id="deepseek", model_id=name, thinking="disabled")
    profile["id"] = digest({k: v for k, v in profile.items() if k != "id"})
    from agent_alfred.evals.acceptance.judge_protocol import current_profile

    batch["judge_profile"] = current_profile(batch["profiles"][0]["judge_model"])
    authorize(batch)
    responses = [item for case in batch["cases"] for item in case["script"]]
    seen = []

    def send(request):
        body = json.loads(request.content)
        seen.append(body)
        assert request.url.host == "api.deepseek.com"
        assert body["thinking"] == {"type": "disabled"}
        assert {t["function"]["name"] for t in body.get("tools", [])} <= {
            "draft_message"
        }
        if body["model"] == "deepseek-v4-pro":
            assert not body.get("tools")
            data = json.loads(body["messages"][-1]["content"])
            result_id = next(k for k in data["sources"] if k.startswith("result:"))
            evidence = result_id + "#/output"
            case_data = next(
                v for k, v in data["sources"].items() if k.startswith("case:")
            )
            item = {"status": "pass", "reason": "offline test", "evidence": evidence}
            content = json.dumps(
                {
                    "dimensions": {
                        k: {
                            **item,
                            "status": (
                                judge_correctness
                                if k == "correctness"
                                and case_data["id"] == "conversation"
                                else "pass"
                            )
                            if v
                            else "na",
                        }
                        for k, v in case_data["applicability"].items()
                    },
                    "prohibitions": {k: item for k in case_data["forbidden"]},
                    "disputed": False,
                    "suspected_safety": False,
                }
            )
        else:
            content = responses.pop(0)
        message, stop = {"content": content}, "stop"
        if isinstance(content, dict):
            message = {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-test",
                        "type": "function",
                        "function": {
                            "name": content["tool"],
                            "arguments": json.dumps(content["arguments"]),
                        },
                    }
                ],
            }
            stop = "tool_calls"
        return httpx.Response(
            200,
            json={
                "id": "offline-test",
                "model": body["model"],
                "choices": [{"message": message, "finish_reason": stop}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )

    store = EvidenceStore(tmp_path / "evidence")
    from agent_alfred.evals.deterministic._simulation_test_helpers import session_for

    session = session_for(batch, store)
    transport = httpx.MockTransport(send)
    sampled = execute(
        batch,
        tmp_path / "runtime",
        simulation_session=session,
        mock_transport=transport,
        candidate_root=root,
        store=store,
    )
    assert len(sampled["results"]) == 6
    assert all(
        r["outcome"] == "completed" and r["recorded"] for r in sampled["results"]
    )
    artifacts = sampled["results"][2]["evidence"]["local_artifacts"]
    assert artifacts["status"] == "available"
    assert artifacts["files"][0]["content"] == "Synthetic draft\n"
    routes = sampled["results"][4]["evidence"]["events"]
    assert any(e["payload"].get("route_label") == "full" for e in routes)
    store.import_batch(sampled)
    graded = grade_batch(
        sampled,
        sampled["authorization"],
        simulation_session=session,
        mock_transport=transport,
        candidate_root=root,
        store=store,
        new_batch="trial-graded",
    )
    saved = store.revise(
        sampled["batch_id"],
        "trial-graded",
        grades=graded["grades"],
        execution=graded,
    )
    assert len(saved["grades"]) == 6 and all(
        g["status"] == "scored" for g in saved["grades"]
    )
    assert len(saved["requests"]) == len(seen) <= 32
    assert saved["budget_started_at"] == sampled["budget_started_at"]
    result = report(saved, candidate_root=root, store=store)
    actual_label = saved["grades"][0]["dimensions"]["correctness"]["status"]
    assert actual_label == judge_correctness
    # Schema3 keeps the individual label; no aggregate threshold was approved.
    assert result["v1_release"]["verdict"] in ("FAIL", "BLOCKED")
    assert "aggregation_policy_missing" in result["v1_release"]["blockers"]
    assert "simulation_not_release_evidence" in result["v1_release"]["blockers"]


def test_trial_rejects_missing_isolation_and_stops_after_auth_failure(tmp_path):
    import httpx2 as httpx

    from agent_alfred.evals.deterministic._simulation_test_helpers import budget_client
    from agent_alfred.model import ModelRef, ModelRequest

    batch = trial_batch()
    batch["profiles"][0].pop("local_tool_allowlist", None)
    with pytest.raises(ValueError, match="trial_local_tool_allowlist_required"):
        validate(batch)
    seen = []

    def send(request):
        seen.append(request.content)
        return httpx.Response(
            401, json={"error": {"message": "synthetic authentication failure"}}
        )

    budget, client = budget_client(
        authorize(controlled_batch()), tmp_path, httpx.MockTransport(send)
    )
    try:
        request = ModelRequest(
            ModelRef("deepseek", "deepseek-v4-flash"), None, (), max_tokens=256
        )
        assert client.respond(request).final_error.status_code == 401
        with pytest.raises(ValueError, match="execution_infrastructure_failure"):
            client.respond(request)
        assert len(seen) == len(budget.requests) == 1
    finally:
        budget.close()


def test_trial_preserves_provider_identity_and_refuses_changed_model(tmp_path):
    import httpx2 as httpx

    from agent_alfred.evals.deterministic._simulation_test_helpers import budget_client
    from agent_alfred.model import ModelRef, ModelRequest

    def send(request):
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "model": "different-model",
                "choices": [
                    {
                        "message": {"content": "untrusted result"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    budget, client = budget_client(
        authorize(controlled_batch()), tmp_path, httpx.MockTransport(send)
    )
    try:
        ref = ModelRef("deepseek", "deepseek-v4-flash")
        result = client.respond(ModelRequest(ref, None, (), max_tokens=256))
        assert result.response is None
        assert result.final_error.code == "model_identity_mismatch"
        assert budget.requests[0]["provider_model_id"] == "different-model"
        with pytest.raises(ValueError, match="execution_infrastructure_failure"):
            budget.check()
    finally:
        budget.close()


@pytest.mark.parametrize("defect", ["symlink", "oversize"])
def test_local_artifact_snapshot_refuses_unsafe_or_incomplete_content(tmp_path, defect):
    from agent_alfred.evals.acceptance.artifacts import drafts

    state = tmp_path / "state"
    box = state / "outbox"
    box.mkdir(parents=True)
    outside = tmp_path / "private.txt"
    outside.write_text("must not be included")
    if defect == "symlink":
        (box / "draft.md").symlink_to(outside)
    else:
        (box / "draft.md").write_bytes(b"x" * 65537)
    result = drafts(state)
    assert result["status"] == "unavailable"
    assert result["files"] == []
