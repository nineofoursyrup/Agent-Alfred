"""Approved fault fixtures exercise real public business and evidence paths."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from agent_alfred.evals.acceptance.examples import SKIP
from agent_alfred.evals.acceptance.examples_v3 import controlled_batch
from agent_alfred.evals.acceptance.runner import run_offline
from agent_alfred.evals.acceptance.schema import digest
from agent_alfred.evals.acceptance.store import EvidenceStore


def fault_batch(fixture, tools, script):
    batch = controlled_batch()
    batch["cases"] = [next(c for c in batch["cases"] if c["group"] == "tools")]
    profile = batch["profiles"][0]
    profile["local_tool_allowlist"] = tools
    profile["id"] = digest({k: v for k, v in profile.items() if k != "id"})
    case = batch["cases"][0]
    case["setup"] = {
        "version": 1,
        "local_tool_allowlist": tools,
        "fault_fixture": fixture,
    }
    case["script"] = [SKIP, *script]
    return batch


def test_persona_conflict_preserves_original_read_and_separate_fixture_receipt(
    tmp_path,
):
    version = hashlib.sha256("Original persona".encode()).hexdigest()
    batch = fault_batch(
        "persona_version_conflict_v1",
        ["read_persona", "update_persona"],
        [
            {"tool": "read_persona", "arguments": {}},
            {
                "tool": "update_persona",
                "arguments": {
                    "content": "Model replacement",
                    "expected_version": version,
                },
            },
            "The version changed; I stopped.",
        ],
    )
    batch["cases"][0]["setup"]["persona"] = "Original persona"
    sampled = run_offline(batch, tmp_path / "runtime")
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(sampled)
    result = store.read(batch["batch_id"])["results"][0]
    evidence = result["evidence"]
    fixture = evidence["setup"]["fault_fixture"]
    assert fixture["status"] == "triggered"
    (observation,) = fixture["observations"]
    assert observation["source"] == "simulation"
    assert observation["admission_scope"] == "active_run_exclusive_command"
    assert observation["read"]["content"] == "Original persona"
    assert observation["read"]["version"] == version
    assert observation["receipt"]["state"] == "complete"
    assert observation["readback"]["version"] != version
    assert observation["readback"]["content"].startswith("Original persona\n")
    read, update = evidence["tool_projections"]["requests"]
    assert json.loads(read["audit"]["text"])["version"] == version
    assert update["result"] == "failed"
    assert "Persona version conflict" in update["audit"]["text"]
    assert observation["receipt"]["operation_id"] != update.get("operation_id")
    assert (
        evidence["local_business"]["persona"]["content"]
        == observation["readback"]["content"]
    )
    assert [row["tool_name"] for row in result["tools"]] == [
        "read_persona",
        "update_persona",
    ]
    assert result["recorded"] is True


def test_publication_fault_keeps_original_unknown_and_later_recovery_separate(tmp_path):
    batch = fault_batch(
        "file_publication_unknown_v1",
        ["draft_message"],
        [
            {"tool": "draft_message", "arguments": {"body": "Local draft body"}},
            "This answer must never be requested after an unknown action.",
        ],
    )
    sampled = run_offline(batch, tmp_path / "runtime")
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(sampled)
    result = store.read(batch["batch_id"])["results"][0]
    evidence = result["evidence"]
    fixture = evidence["setup"]["fault_fixture"]
    assert fixture["status"] == "triggered"
    (observation,) = fixture["observations"]
    (request,) = evidence["tool_projections"]["requests"]
    assert observation["operation_id"] == request["operation_id"]
    assert observation["boundary"] == "durable_publication_intent_before_create"
    assert "body" not in observation
    assert request["result"] == "unknown"
    assert request["verification"]["state"] == "unverified"
    assert json.loads(request["audit"]["text"].splitlines()[-1])["state"] == "prepared"
    assert evidence["local_artifacts_before_reopen"]["files"] == []
    assert evidence["local_artifacts_before_reopen"]["status"] == "available"
    assert evidence["local_artifacts"]["files"] == []
    (recovered,) = evidence["tool_verifications_after_reopen"]
    assert recovered["state"] == "unverified"
    assert recovered["collected_at"] > evidence["tool_projections"]["observed_at"]
    assert evidence["local_business"]["file_operations"][0]["state"] == "conflict"
    assert result["error"] == "file_result_unverified"
    assert len(evidence["attempts"]) == 2
    assert "must never be requested" not in (result["output"] or "")
    recovery = fixture["recovery"]
    assert recovery["run_id"] != result["run_id"]
    assert recovery["before_recovery"]["receipt"]["state"] == "prepared"
    assert recovery["before_recovery"]["evidence"]["attempts"] == []
    assert recovery["before_recovery"]["run_id"] != recovery["run_id"]
    assert recovery["before_recovery"]["observed_at"] < recovery["observed_at"]
    assert recovery["receipt"]["state"] == "conflict"
    assert recovery["evidence"]["attempts"] == []
    original = json.loads(
        (
            tmp_path
            / "runtime"
            / batch["batch_id"]
            / "tools"
            / "original-observation.json"
        ).read_text()
    )
    assert "recovery" not in original["evidence"]["setup"]["fault_fixture"]
    assert original["tools"][0]["result"] == "unknown"
    assert "tool_verifications_after_reopen" not in original["evidence"]
    sealed = fixture["original_observation"]
    assert sealed["path"] == "original-observation.json"
    assert sealed["record"] == original
    assert sealed["sha256"] == digest(original)


@pytest.mark.parametrize(
    "fixture,tools,script,status,missing",
    [
        (
            "persona_version_conflict_v1",
            ["read_persona", "update_persona"],
            ["No tool used."],
            "not_triggered",
            ["read_persona", "update_persona"],
        ),
        (
            "persona_version_conflict_v1",
            ["read_persona", "update_persona"],
            [{"tool": "read_persona", "arguments": {}}, "Stopped after reading."],
            "triggered",
            ["update_persona"],
        ),
        (
            "file_publication_unknown_v1",
            ["draft_message"],
            ["No draft made."],
            "not_triggered",
            ["draft_message"],
        ),
    ],
)
def test_missing_model_action_stays_uncovered_without_an_extra_attempt(
    tmp_path,
    fixture,
    tools,
    script,
    status,
    missing,
):
    batch = fault_batch(fixture, tools, script)
    result = run_offline(batch, tmp_path)["results"][0]
    fault = result["evidence"]["setup"]["fault_fixture"]
    assert fault["status"] == status
    assert fault["coverage"] == "not_covered"
    assert fault["missing_model_tools"] == missing
    assert len(result["evidence"]["attempts"]) == len(script) + 1
    assert "recovery" not in fault


def test_unknown_fixture_is_refused_before_workspace_creation(tmp_path):
    batch = fault_batch("untrusted.module:run", [], ["unused"])
    with pytest.raises(ValueError, match="unknown_fault_fixture"):
        run_offline(batch, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_repeat_read_and_model_retry_do_not_repeat_or_hide_fixture_actions(tmp_path):
    version = hashlib.sha256("Original persona".encode()).hexdigest()
    stale_update = {
        "tool": "update_persona",
        "arguments": {
            "content": "Replacement",
            "expected_version": version,
        },
    }
    batch = fault_batch(
        "persona_version_conflict_v1",
        ["read_persona", "update_persona"],
        [
            {"tool": "read_persona", "arguments": {}},
            stale_update,
            {"tool": "read_persona", "arguments": {}},
            stale_update,
            "Stopped.",
        ],
    )
    batch["cases"][0]["setup"]["persona"] = "Original persona"
    result = run_offline(batch, tmp_path)["results"][0]
    evidence = result["evidence"]
    (observation,) = evidence["setup"]["fault_fixture"]["observations"]
    read, update, reread, retry = evidence["tool_projections"]["requests"]
    assert json.loads(read["audit"]["text"])["version"] == version
    assert (
        json.loads(reread["audit"]["text"])["version"]
        == observation["readback"]["version"]
    )
    assert update["result"] == retry["result"] == "failed"
    assert len(result["tools"]) == 4
    assert len(evidence["attempts"]) == 6


@pytest.mark.parametrize("failure", ["write", "fsync", "close"])
def test_original_seal_failure_closes_descriptor_and_never_recovers(
    tmp_path,
    monkeypatch,
    failure,
):
    from agent_alfred.evals.acceptance.artifacts import local_business
    from agent_alfred.settings import Settings

    batch = fault_batch(
        "file_publication_unknown_v1",
        ["draft_message"],
        [
            {"tool": "draft_message", "arguments": {"body": "Unpublished body"}},
        ],
    )
    original_open, original_close, original_fsync = os.open, os.close, os.fsync
    original_write = os.write
    held = []
    failure_calls = []
    attempted = []

    def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if os.fspath(path) == "original-observation.json":
            held.append(fd)
        return fd

    def failed_fsync(fd):
        if failure == "fsync" and fd in held:
            failure_calls.append(fd)
            raise OSError("synthetic original seal fsync failure")
        return original_fsync(fd)

    def failed_write(fd, content):
        if failure == "write" and fd in held:
            attempted.append(json.loads(bytes(content)))
            failure_calls.append(fd)
            raise OSError("synthetic original seal write failure")
        return original_write(fd, content)

    def failed_close(fd):
        result = original_close(fd)
        if failure == "close" and fd in held and not failure_calls:
            failure_calls.append(fd)
            raise OSError("synthetic original seal close return failure")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", tracked_open)
        patch.setattr(os, "fsync", failed_fsync)
        patch.setattr(os, "write", failed_write)
        patch.setattr(os, "close", failed_close)
        with pytest.raises(OSError, match="synthetic original seal"):
            run_offline(batch, tmp_path)
        assert len(held) == len(failure_calls) == 1
        with pytest.raises(OSError):
            os.fstat(held[0])
    state = tmp_path / batch["batch_id"] / "tools"
    original = (
        attempted[0]
        if attempted
        else json.loads((state / "original-observation.json").read_text())
    )
    operation = original["tools"][0]["operation_id"]
    business = local_business(state, Settings(), operation_ids=[operation])
    assert business["file_operations"][0]["state"] == "prepared"
    assert original["tools"][0]["result"] == "unknown"
    assert not list((state / "outbox").iterdir())


@pytest.mark.parametrize(
    "fixture", ["persona_version_conflict_v1", "file_publication_unknown_v1"]
)
def test_execute_uses_actual_sdk_requests_and_never_dispatches_recovery_to_model(
    tmp_path,
    fixture,
):
    import httpx2 as httpx

    from agent_alfred.evals.acceptance.candidate import capture
    from agent_alfred.evals.acceptance.runner import execute
    from agent_alfred.evals.deterministic.test_acceptance_trial import authorize

    persona_fixture = fixture == "persona_version_conflict_v1"
    tools = ["read_persona", "update_persona"] if persona_fixture else ["draft_message"]
    batch = fault_batch(fixture, tools, [])
    batch["cases"][0]["setup"]["persona"] = "Original persona"
    root = Path(__file__).resolve().parents[4]
    batch["candidate"] = capture(root)
    batch["candidate_id"] = digest(batch["candidate"])
    authorize(batch)
    sent = []
    transport_checks = []
    old_version = hashlib.sha256("Original persona".encode()).hexdigest()

    def call(name, arguments, identity):
        return {
            "id": identity,
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(arguments),
            },
        }

    def send(request):
        body = json.loads(request.content)
        sent.append(body)
        transport_checks.append(
            (
                body.get("stream", False) is False,
                request.headers["authorization"]
                == "Bearer offline-synthetic-credential-admission",
            )
        )
        if len(sent) == 1:
            message = {"content": SKIP}
        elif persona_fixture and len(sent) == 2:
            message = {"tool_calls": [call("read_persona", {}, "read")]}
        elif persona_fixture and len(sent) == 3:
            assert any(old_version in str(m) for m in body["messages"])
            message = {
                "tool_calls": [
                    call(
                        "update_persona",
                        {
                            "content": "Model replacement",
                            "expected_version": old_version,
                        },
                        "update",
                    )
                ]
            }
        elif persona_fixture and len(sent) == 4:
            assert any("Persona version conflict" in str(m) for m in body["messages"])
            message = {"content": "Stopped after version conflict."}
        elif not persona_fixture and len(sent) == 2:
            message = {
                "tool_calls": [
                    call("draft_message", {"body": "First draft"}, "draft1"),
                    call("draft_message", {"body": "Must not start"}, "draft2"),
                ]
            }
        else:
            pytest.fail("extra model dispatch after fixed script")
        return httpx.Response(
            200,
            json={
                "id": f"response-{len(sent)}",
                "model": body["model"],
                "choices": [
                    {
                        "message": message,
                        "finish_reason": (
                            "tool_calls" if "tool_calls" in message else "stop"
                        ),
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
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
    store.import_batch(sampled)
    actual = store.read(batch["batch_id"])
    result = actual["results"][0]
    fault = result["evidence"]["setup"]["fault_fixture"]
    assert all(all(check) for check in transport_checks), transport_checks
    assert len(sent) == len(actual["requests"]) == (4 if persona_fixture else 2)
    assert len({r["attempt_id"] for r in actual["requests"]}) == len(sent)
    assert len(fault["observations"]) == 1
    assert fault["coverage"] == "requires_review"
    if persona_fixture:
        assert [r["result"] for r in result["tools"]] == ["succeeded", "failed"]
    else:
        assert [r["result"] for r in result["tools"]] == ["unknown", "not_executed"]
        assert fault["recovery"]["evidence"]["attempts"] == []
        assert fault["recovery"]["receipt"]["state"] == "conflict"


@pytest.mark.parametrize("observer_behavior", ["return_other_result", "raise"])
def test_persona_observer_cannot_replace_real_read_and_exceptions_close_cleanly(
    tmp_path,
    observer_behavior,
):
    from agent_alfred.connections import CredentialOverlay
    from agent_alfred.evals.acceptance.artifacts import local_business, tool_projections
    from agent_alfred.evals.acceptance.case_setup import SimulatedModel
    from agent_alfred.evals.acceptance.runner import scripted
    from agent_alfred.messages import TextBlock
    from agent_alfred.model import ScriptedModelFactory
    from agent_alfred.runtime.host import SubmitRequest
    from agent_alfred.settings import Settings
    from agent_alfred.tools import ToolSuccess
    from agent_alfred.wiring import build_default_host

    def observer(persona, original, context):
        assert json.loads(original.content[0].text)["content"] == "Actual persona"
        if observer_behavior == "raise":
            raise OSError("synthetic observer failure")
        return ToolSuccess((TextBlock("Forged observer replacement"),))

    state = tmp_path / "state"
    model = SimulatedModel(
        list(
            scripted(
                [
                    SKIP,
                    {"tool": "read_persona", "arguments": {}},
                    "Done.",
                ]
            )
        )
    )
    settings = Settings(persona="Actual persona", stream=False, stream_fallback=False)
    host = build_default_host(
        state_dir=state,
        settings=settings,
        skill_builtin=tmp_path / "skills",
        factory=ScriptedModelFactory(model),
        credentials=CredentialOverlay({}, None),
        local_tool_allowlist=["read_persona"],
        persona_read_observer=observer,
    )
    try:
        host.start()
        accepted = host.submit(
            SubmitRequest("Read my persona", session_id=host.create_session())
        )
        host.wait(accepted.run_id)
        (request,) = tool_projections(host, state, accepted.run_id)["requests"]
        if observer_behavior == "raise":
            assert request["result"] != "succeeded"
            assert "Tool execution failed" in request["audit"]["text"]
        else:
            assert json.loads(request["audit"]["text"])["content"] == "Actual persona"
            assert "Forged observer replacement" not in request["model"]["text"]
    finally:
        assert host.close() is True
    assert local_business(state, settings)["persona"]["content"] == "Actual persona"


@pytest.mark.parametrize("triggered", [True, False])
def test_sealed_recording_fact_is_publicly_confirmed_before_recovery(
    tmp_path, triggered
):
    script = (
        [{"tool": "draft_message", "arguments": {"body": "Synthetic draft"}}]
        if triggered
        else ["No draft requested."]
    )
    batch = fault_batch("file_publication_unknown_v1", ["draft_message"], script)
    sampled = run_offline(batch, tmp_path / "runtime")
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(sampled)
    (result,) = store.read(batch["batch_id"])["results"]
    fault = result["evidence"]["setup"]["fault_fixture"]
    original = fault["original_observation"]["record"]
    sealed_bytes = (
        tmp_path / "runtime" / batch["batch_id"] / "tools" / "original-observation.json"
    ).read_bytes()
    assert json.loads(sealed_bytes) == original
    assert (
        hashlib.sha256(sealed_bytes).hexdigest()
        == fault["original_observation"]["sha256"]
    )
    assert original["recorded"] is True
    assert original["evidence"]["recording_state"] == "recorded"
    assert result["recorded"] is True
    assert original["run_id"] == result["run_id"]
    assert original["output"] == result["output"]
    assert digest(original) == fault["original_observation"]["sha256"]
    assert ("recovery" in fault) is triggered
    assert "recovery" not in original["evidence"]["setup"]["fault_fixture"]


def test_failed_public_recording_readback_never_seals_placeholder(
    tmp_path, monkeypatch
):
    from agent_alfred.evals.acceptance.artifacts import local_business
    from agent_alfred.runtime.host import RuntimeHost
    from agent_alfred.settings import Settings

    batch = fault_batch(
        "file_publication_unknown_v1",
        ["draft_message"],
        [
            {"tool": "draft_message", "arguments": {"body": "Synthetic draft"}},
        ],
    )
    read_session = RuntimeHost.open_session
    observed = []

    def interrupted_readback(host, *args, **kwargs):
        page = read_session(host, *args, **kwargs)
        if page.messages:
            run = next(m.run_id for m in page.messages if m.role == "assistant")
            observed.extend(host.tool_requests(run))
            raise OSError("synthetic public recording readback return failure")
        return page

    with monkeypatch.context() as patch:
        # Only interrupt the approved public readback return boundary, after
        # obtaining its real result; never supply a substituted business state.
        patch.setattr(RuntimeHost, "open_session", interrupted_readback)
        with pytest.raises(OSError, match="synthetic public recording readback"):
            run_offline(batch, tmp_path / "runtime")
    state = tmp_path / "runtime" / batch["batch_id"] / "tools"
    assert not (state / "original-observation.json").exists()
    (request,) = observed
    assert request["result"] == "unknown"
    operation = request["operation_id"]
    readback = local_business(state, Settings(), operation_ids=[operation])
    assert readback["file_operations"][0]["state"] == "prepared"
