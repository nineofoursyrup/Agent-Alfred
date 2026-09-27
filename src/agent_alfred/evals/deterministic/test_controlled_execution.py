"""The public controlled broker reserves all liability before any model IO."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event, Thread

import httpx2 as httpx
import pytest

from agent_alfred.evals.acceptance.admission import proposal
from agent_alfred.evals.acceptance.controlled.contract import (
    CAP_UNITS,
    COVERAGE,
    USD_SCALE,
    cost_units,
    execution_plan,
)
from agent_alfred.evals.acceptance.controlled.runtime import (
    SyntheticCredentials,
    SyntheticRuntime,
    install_runtime,
)
from agent_alfred.evals.acceptance.controlled.store import (
    MemoryExecutionAnchor,
    MemoryExecutionStore,
)
from agent_alfred.evals.acceptance.controlled_execution import ControlledAuthority
from agent_alfred.evals.acceptance.examples_v4 import supplement_batch
from agent_alfred.evals.acceptance.execution_decisions import (
    material_binding,
    run_request,
)
from agent_alfred.evals.acceptance.materials import (
    ProtectedMaterialStore,
    export_materials,
)
from agent_alfred.evals.acceptance.schema import digest, encode
from agent_alfred.evals.acceptance.simulation_authority import SimulationAuthority
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.acceptance.supplement_decisions import (
    decision_request,
    make_summary,
)
from agent_alfred.messages import Message, TextBlock
from agent_alfred.model import ModelRef, ModelRequest, ToolSpec


def test_uninstalled_authority_cannot_submit():
    import pytest

    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        ControlledAuthority().submit_job({})


class Clock:
    def __init__(self):
        self.value = datetime(2026, 9, 27, tzinfo=UTC)

    def wall_utc(self):
        return self.value

    def monotonic(self):
        return self.value.timestamp()

    def __call__(self):
        return self.value


def terms(model, **changes):
    result = {
        "version": 1,
        "model": model,
        "currency": "USD",
        "input_per_million": 300000000000,
        "output_per_million": 1200000000000,
        "request_fee": 0,
        "other_fee": 0,
        "tax_numerator": 0,
        "tax_denominator": 1,
        "fx_numerator": 1,
        "fx_denominator": 1,
        "quantum": 1,
        "coverage": COVERAGE,
        "valid_from": "2026-09-26T00:00:00+00:00",
        "valid_until": "2026-09-28T00:00:00+00:00",
        "evidence": "synthetic:full-price-proof",
    }
    result.update(changes)
    return result


def response(request, *, usage=True, model=None):
    payload = json.loads(request.content)
    raw = {
        "id": "synthetic-completion",
        "model": model or payload["model"],
        "choices": [
            {"message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}
        ],
    }
    if usage:
        raw["usage"] = {"prompt_tokens": 10, "completion_tokens": 2}
    return httpx.Response(200, json=raw)


def fixture(
    tmp_path,
    *,
    handler=None,
    product=False,
    cap=CAP_UNITS,
    before_intent=None,
    pricing=None,
    replay=False,
    batch_override=None,
    operations_override=None,
    mode=None,
):
    clock = Clock()
    source = SimulationAuthority(tmp_path / "source-authority", clock=clock)
    material_root = tmp_path / "source-materials"
    evidence = EvidenceStore(material_root / "evidence", decision_source=source)
    batch = batch_override or supplement_batch()
    summary = make_summary(batch)
    batch["summaries"].append(summary)
    event = source.issue_decision(
        decision_request(summary),
        subject="simulation:user",
        decision="approved",
        reason="Exact synthetic material scope",
        evidence=["synthetic:material-event"],
    )
    batch["user_decisions"].append(event)
    evidence.import_batch(batch)
    (material_root / "proposal.json").write_bytes(
        encode(
            proposal(
                batch,
                output_scope=str(tmp_path / "output"),
                operations=["product", "judge"],
            )
        )
    )
    members = [
        {
            "path": p.relative_to(material_root).as_posix(),
            "bytes": p.stat().st_size,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        }
        for p in sorted(material_root.rglob("*"))
        if p.is_file()
    ]
    manifest = encode({"version": 1, "file_count": len(members), "files": members})
    (material_root / "BUNDLE-MANIFEST.json").write_bytes(manifest)
    vault = ProtectedMaterialStore(tmp_path / "vault", scope="synthetic-full-materials")
    reference = export_materials(
        material_root,
        vault=vault,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        batch_path="evidence/" + batch["batch_id"] + "/batch.json",
        proposal_path="proposal.json",
        evidence_store_path="evidence",
    )
    binding = material_binding(
        evidence,
        batch["batch_id"],
        package_manifest_sha256=reference["manifest_sha256"],
    )
    case = batch["cases"][0]
    operation = {
        "id": "primary" if product else "diagnose",
        "kind": "product" if product else "judge_test",
        "object_id": case["id"] if product else batch["judge_tests"][0]["id"],
        "case_id": case["id"] if product else None,
        "instance_id": "synthetic:instance-1",
        "data_scope_sha256": digest(case if product else batch["judge_tests"][0]),
        "max_calls": 16 if product else 1,
    }
    operations = [operation]
    if product:
        operations.append({**operation, "id": "auxiliary", "kind": "auxiliary"})
    plan = execution_plan(
        binding=binding,
        material_ref=reference,
        worker="simulation:worker",
        controller="simulation:controller",
        operations=operations_override or operations,
        pricing=pricing
        or {"flash": terms("deepseek-flash"), "pro": terms("deepseek-v4-pro")},
        initial_phase="product" if product else "diagnostic",
        cap_units=cap,
        execution_mode=mode or ("synthetic_replay" if replay else "synthetic_capacity"),
    )
    budget = {
        "cap_units": cap,
        "limits": plan["limits"],
        "total_seconds": 10800,
        "currency": "USD",
        "scale": USD_SCALE,
    }
    run = run_request(
        binding,
        job_id="controlled-job",
        plan_sha256=digest(plan),
        budget_sha256=digest(budget),
    )
    run_event = source.issue_execution_decision(
        run,
        subject="simulation:user",
        decision="approved",
        reason="Synthetic offline invocation only",
        evidence=["synthetic:run-event"],
    )
    sends = []

    def send(request):
        sends.append(request)
        return (handler or response)(request)

    credentials = SyntheticCredentials()
    runtime = SyntheticRuntime(
        source=source, transport=httpx.MockTransport(send), credentials=credentials
    )
    ledger, anchor = MemoryExecutionStore(), MemoryExecutionAnchor()
    authority = ControlledAuthority(
        runtime=runtime,
        store=ledger,
        anchor=anchor,
        vault=vault,
        allowed_objects={reference["object_sha256"]},
        expected_manifest=reference["manifest_sha256"],
        output_root=tmp_path / "received",
        worker="simulation:worker",
        controller="simulation:controller",
        subject="simulation:user",
        now=clock,
        before_intent=before_intent,
    )
    submission = {
        "job_id": "controlled-job",
        "material_ref": reference,
        "plan": plan,
        "run_source_ref": run_event["source_ref"],
        "approved_batch_id": batch["batch_id"],
    }
    model = "deepseek-flash" if product else "deepseek-v4-pro"
    request = ModelRequest(
        ModelRef("deepseek", model),
        (TextBlock("system scope"),),
        (Message("user", (TextBlock("input scope"),)),),
        tools=(ToolSpec("query_events", "local schema", {"type": "object"}),)
        if product
        else (),
        max_tokens=8192 if product else 16384,
        thinking="disabled",
        response_format=None if product else "json_object",
    )
    return {
        "authority": authority,
        "source": source,
        "submission": submission,
        "request": request,
        "runtime": runtime,
        "ledger": ledger,
        "anchor": anchor,
        "clock": clock,
        "sends": sends,
        "credentials": credentials,
        "operation": operation,
        "material_event": event,
        "batch": batch,
    }


def prepare(f, *, operation=None, request=None):
    op = operation or f["operation"]
    req = request or f["request"]
    auth = f["authority"]
    prepared = auth.prepare_request(
        "controlled-job",
        op["id"],
        req,
        principal="simulation:controller",
        data_scope_sha256=op["data_scope_sha256"],
    )
    return auth.client("controlled-job", op["id"], prepared), prepared


def test_public_material_source_money_payload_and_model_result(tmp_path):
    f = fixture(tmp_path)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    client, _ = prepare(f)
    result = client.respond(f["request"])
    assert result.final_error is None
    assert len(result.attempts) == len(f["sends"]) == f["credentials"].reads == 1
    assert result.attempts[0].streamed is False
    wire = json.loads(f["sends"][0].content)
    assert str(f["sends"][0].url) == "https://api.deepseek.com/chat/completions"
    assert wire["messages"][0] == {"role": "system", "content": "system scope"}
    assert wire["response_format"] == {"type": "json_object"}
    assert wire["thinking"] == {"type": "disabled"} and wire["stream"] is False
    status = auth.status("controlled-job")
    assert status["state"]["spent_units"] == 5400000  # 10*0.30 + 2*1.20 USD / million
    assert status["state"]["pending_units"] == 0
    assert status["state"]["counts"] == {"flash": 0, "pro": 1, "total": 1}
    row = status["attempts"][0]
    assert row["kind"] == "judge_test" and row["case_id"] is None
    assert (
        json.loads(auth.read_object(row["response_ref"])["raw"])["id"]
        == "synthetic-completion"
    )
    assert row["payload_sha256"] == digest(wire)
    assert status["online_executable"] is False and status["release_eligible"] is False
    auth.finish("controlled-job", "diagnose")
    with pytest.raises(ValueError, match="operation_phase_closed"):
        prepare(f)


def test_full_payload_mutation_and_worker_role_forgery_never_read_credentials(tmp_path):
    f = fixture(tmp_path, product=True)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    client, descriptor = prepare(f)
    changed = replace(f["request"], system=(TextBlock("altered system"),))
    with pytest.raises(ValueError, match="prepared_request_mismatch"):
        client.respond(changed)
    changed = replace(
        f["request"],
        tools=(ToolSpec("query_events", "altered schema", {"type": "object"}),),
    )
    with pytest.raises(ValueError, match="prepared_request_mismatch"):
        client.respond(changed)
    with pytest.raises(ValueError, match="operation_not_in_plan"):
        auth.invoke(
            "controlled-job",
            "judge",
            "worker-role-change",
            {**descriptor, "timeout_seconds": 120},
        )
    with pytest.raises(ValueError, match="controller_identity_required"):
        auth.prepare_request(
            "controlled-job",
            "primary",
            f["request"],
            principal="simulation:worker",
            data_scope_sha256=f["operation"]["data_scope_sha256"],
        )
    assert (
        not f["sends"] and f["credentials"].reads == f["runtime"].clients_created == 0
    )
    assert auth.status("controlled-job")["state"]["counts"]["total"] == 0
    result = client.respond(f["request"])
    assert result.final_error is None
    assert "tools" in json.loads(f["sends"][0].content)


def test_live_source_revocation_is_permanent_and_precedes_any_privileged_io(tmp_path):
    f = fixture(tmp_path)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    client, _ = prepare(f)
    f["source"].revoke_decision(f["submission"]["run_source_ref"])
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        client.respond(f["request"])
    status = auth.status("controlled-job")
    assert status["state"]["state"] == "SUSPENDED"
    assert status["state"]["counts"]["total"] == 0
    assert (
        not f["sends"] and f["credentials"].reads == f["runtime"].clients_created == 0
    )
    assert status["state"]["stop_reason"] == "approval_source_unverifiable"


def test_revoke_wins_before_intent_and_reservation_is_not_refunded(tmp_path):
    f = fixture(tmp_path)
    auth = f["authority"]
    auth.before_intent = lambda job, attempt: auth.revoke(
        job, principal="simulation:controller"
    )
    auth.submit_job(f["submission"])
    client, _ = prepare(f)
    with pytest.raises(ValueError, match="revoked"):
        client.respond(f["request"])
    status = auth.status("controlled-job")
    row = status["attempts"][0]
    assert row["send_state"] == "CANCELLED_BEFORE_SEND"
    assert status["state"]["pending_units"] == row["worst_units"] > 0
    assert status["state"]["stop_reason"] == "revoked"
    assert (
        not f["sends"] and f["credentials"].reads == f["runtime"].clients_created == 0
    )


def test_dispatch_rechecks_revocation_before_reading_credential(tmp_path):
    f = fixture(tmp_path)
    auth = f["authority"]
    f["runtime"].before_credentials = lambda: auth.revoke(
        "controlled-job", principal="simulation:controller"
    )
    auth.submit_job(f["submission"])
    client, _ = prepare(f)
    with pytest.raises(ValueError, match="revoked"):
        client.respond(f["request"])
    state = auth.status("controlled-job")["state"]
    assert state["stop_reason"] == "revoked" and state["pending_units"] > 0
    assert (
        not f["sends"] and f["credentials"].reads == f["runtime"].clients_created == 0
    )


def test_unknown_usage_preserves_raw_full_liability_and_closes_new_dispatch(tmp_path):
    f = fixture(tmp_path, handler=lambda request: response(request, usage=False))
    auth = f["authority"]
    auth.submit_job(f["submission"])
    client, _ = prepare(f)
    result = client.respond(f["request"])
    assert result.final_error.code == "usage_unknown"
    status = auth.status("controlled-job")
    row = status["attempts"][0]
    assert row["send_state"] == "MAY_HAVE_SENT" and row["actual_units"] is None
    assert (
        status["state"]["spent_units"] == 0
        and status["state"]["pending_units"] == row["worst_units"]
    )
    assert "usage" not in json.loads(auth.read_object(row["response_ref"])["raw"])
    with pytest.raises(ValueError, match="request_state_unresolved"):
        prepare(f)
    assert len(f["sends"]) == 1


def test_transport_timeout_carries_real_attempt_without_refund_or_retry(tmp_path):
    from agent_alfred.model import ModelCallInterrupted

    def lost(request):
        raise httpx.ReadTimeout("synthetic lost response", request=request)

    f = fixture(tmp_path, handler=lost)
    f["authority"].submit_job(f["submission"])
    client, _ = prepare(f)
    with pytest.raises(ModelCallInterrupted) as interrupted:
        client.respond(f["request"])
    assert len(interrupted.value.result.attempts) == len(f["sends"]) == 1
    state = f["authority"].status("controlled-job")
    assert state["state"]["pending_units"] == state["attempts"][0]["worst_units"] > 0
    assert state["attempts"][0]["error"] == "ReadTimeout"


def test_exact_quantum_tax_fx_and_atomic_amount_rejection(tmp_path):
    # ((1 token * 1 picodollar / million + 2) * 1.2 * 1.5) -> 3.6000018,
    # round UP to the next 5-picodollar unit, never truncate to zero.
    tariff = terms(
        "deepseek-v4-pro",
        input_per_million=1,
        output_per_million=0,
        request_fee=2,
        tax_numerator=1,
        tax_denominator=5,
        fx_numerator=3,
        fx_denominator=2,
        quantum=5,
    )
    assert cost_units(tariff, 1, 0) == 5
    f = fixture(
        tmp_path, cap=4, pricing={"flash": terms("deepseek-flash"), "pro": tariff}
    )
    f["authority"].submit_job(f["submission"])
    client, _ = prepare(f)
    with pytest.raises(ValueError, match="amount_cap_exceeded"):
        client.respond(f["request"])
    status = f["authority"].status("controlled-job")
    assert status["state"]["counts"]["total"] == status["state"]["pending_units"] == 0
    assert not status["attempts"] and not f["sends"] and f["credentials"].reads == 0


@pytest.mark.parametrize(
    "problem", ["input_limit", "price_drift", "uncovered", "expired"]
)
def test_input_proof_and_price_fail_before_credentials(tmp_path, problem):
    f = fixture(tmp_path)
    f["authority"].submit_job(f["submission"])
    client, _ = prepare(f)
    if problem == "input_limit":
        f["runtime"].input_tokens = 24001
    elif problem == "price_drift":
        f["runtime"].pricing_override = terms(
            "deepseek-v4-pro", input_per_million=900000000000
        )
    elif problem == "uncovered":
        f["runtime"].pricing_override = terms(
            "deepseek-v4-pro", coverage=["input", "output"]
        )
    else:
        f["clock"].value += timedelta(days=2)
    with pytest.raises(ValueError):
        client.respond(f["request"])
    assert (
        not f["sends"] and f["credentials"].reads == f["runtime"].clients_created == 0
    )
    assert f["authority"].status("controlled-job")["state"]["counts"]["total"] == 0


def test_price_drift_after_reserve_keeps_full_liability(tmp_path):
    f = fixture(tmp_path)
    f["authority"].before_intent = lambda *args: setattr(
        f["runtime"], "pricing_override", terms("deepseek-v4-pro", other_fee=1)
    )
    f["authority"].submit_job(f["submission"])
    client, _ = prepare(f)
    with pytest.raises(ValueError, match="billing_or_token_proof_mismatch"):
        client.respond(f["request"])
    state = f["authority"].status("controlled-job")["state"]
    assert state["pending_units"] > 0 and state["counts"]["total"] == 1
    assert not f["sends"] and f["credentials"].reads == 0


def test_parallel_attempts_cannot_split_money_or_request_reservation(tmp_path):
    started, release = Event(), Event()

    def blocking(request):
        started.set()
        assert release.wait(5)
        return response(request)

    f = fixture(tmp_path, product=True, handler=blocking)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    first, prepared = prepare(f)
    second = auth.client("controlled-job", f["operation"]["id"], prepared)
    results = []
    thread = Thread(target=lambda: results.append(first.respond(f["request"])))
    thread.start()
    assert started.wait(5)
    try:
        with pytest.raises(ValueError, match="request_state_unresolved"):
            second.respond(f["request"])
        status = auth.status("controlled-job")
        assert status["state"]["counts"]["total"] == 1
        assert status["state"]["pending_units"] == status["attempts"][0]["worst_units"]
    finally:
        release.set()
        thread.join(5)
    assert len(results) == len(f["sends"]) == 1
    assert results[0].final_error is None


def test_product_and_auxiliary_share_sixteen_attempt_limit(tmp_path):
    f = fixture(tmp_path, product=True)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    auxiliary = {**f["operation"], "id": "auxiliary", "kind": "auxiliary"}
    for i in range(16):
        client, _ = prepare(f, operation=f["operation"] if i % 2 else auxiliary)
        assert client.respond(f["request"]).final_error is None
    client, _ = prepare(f)
    with pytest.raises(ValueError, match="case_quota_exhausted"):
        client.respond(f["request"])
    status = auth.status("controlled-job")
    assert len(f["sends"]) == 16 and status["state"]["counts"] == {
        "flash": 16,
        "pro": 0,
        "total": 16,
    }


def test_deadline_or_clock_recovery_cannot_reactivate_job(tmp_path):
    f = fixture(tmp_path)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    client, _ = prepare(f)
    f["clock"].value -= timedelta(seconds=1)
    with pytest.raises(ValueError, match="authority_clock_unverifiable"):
        client.respond(f["request"])
    f["clock"].value += timedelta(seconds=2)
    with pytest.raises(ValueError, match="authority_clock_unverifiable"):
        prepare(f)
    assert (
        auth.status("controlled-job")["state"]["stop_reason"]
        == "authority_clock_unverifiable"
    )
    assert not f["sends"]


def test_attempt_window_is_rechecked_after_send_intent(tmp_path):
    f = fixture(tmp_path)
    f["runtime"].before_credentials = lambda: setattr(
        f["clock"], "value", f["clock"].value + timedelta(seconds=120)
    )
    f["authority"].submit_job(f["submission"])
    client, _ = prepare(f)
    with pytest.raises(ValueError, match="attempt_deadline"):
        client.respond(f["request"])
    status = f["authority"].status("controlled-job")
    assert status["state"]["stop_reason"] == "attempt_deadline"
    assert status["state"]["pending_units"] > 0
    assert not f["sends"] and f["credentials"].reads == 0


def test_untrusted_install_file_and_arbitrary_adapters_cannot_open_transport(tmp_path):
    path = tmp_path / "installed.json"
    path.write_text("{}")
    with pytest.raises(ValueError, match="installation_not_administrator_protected"):
        install_runtime(path)
    with pytest.raises(ValueError, match="synthetic_controlled_runtime_required"):
        SyntheticRuntime(
            source=object(),
            transport=httpx.MockTransport(response),
            credentials=SyntheticCredentials(),
        )
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        ControlledAuthority(runtime=object()).submit_job({})


def test_explicit_replay_preserves_source_blocker_and_cannot_be_a_real_grant(tmp_path):
    f = fixture(tmp_path, replay=True)
    f["source"].revoke_decision(f["material_event"]["source_ref"])
    f["authority"].submit_job(f["submission"])
    with pytest.raises(ValueError, match="complete_diagnostic_plan_required"):
        prepare(f)
    assert not f["sends"]
    status = f["authority"].status("controlled-job")
    assert status["state"]["source_status"] == "SOURCE_UNVERIFIABLE_AUDIT_ONLY"
    assert status["mode"] == "SYNTHETIC_ONLY" and status["online_executable"] is False


def test_revoke_after_actual_send_preserves_first_stop_and_allows_only_settlement(
    tmp_path,
):
    f = None

    def revoke_in_flight(request):
        f["authority"].revoke(
            "controlled-job",
            principal="simulation:controller",
            reason="human_withdrawal",
        )
        return response(request)

    f = fixture(tmp_path, handler=revoke_in_flight)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    client, _ = prepare(f)
    assert client.respond(f["request"]).final_error is None
    status = auth.status("controlled-job")
    assert status["state"]["state"] == "SUSPENDED"
    assert status["state"]["stop_reason"] == "human_withdrawal"
    assert (
        status["state"]["pending_units"] == 0
        and status["state"]["spent_units"] == 5400000
    )
    assert len(f["sends"]) == 1


def test_unverifiable_settlement_preserves_received_response_and_unknown_debt(
    tmp_path, monkeypatch
):
    f = fixture(tmp_path)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    client, _ = prepare(f)

    def unavailable(*args):
        raise RuntimeError("synthetic billing reader unavailable")

    monkeypatch.setattr(f["runtime"], "settlement", unavailable)
    result = client.respond(f["request"])
    assert result.final_error is not None
    status = auth.status("controlled-job")
    row = status["attempts"][0]
    assert (
        json.loads(auth.read_object(row["response_ref"])["raw"])["usage"][
            "prompt_tokens"
        ]
        == 10
    )
    assert row["actual_units"] is None
    assert status["state"]["pending_units"] == row["worst_units"]


def test_product_run_deadline_is_shared_by_later_auxiliary(tmp_path):
    f = fixture(tmp_path, product=True)
    f["authority"].submit_job(f["submission"])
    client, _ = prepare(f)
    assert client.respond(f["request"]).final_error is None
    f["clock"].value += timedelta(seconds=900)
    auxiliary = {**f["operation"], "id": "auxiliary", "kind": "auxiliary"}
    client, _ = prepare(f, operation=auxiliary)
    with pytest.raises(ValueError, match="run_deadline"):
        client.respond(f["request"])
    assert len(f["sends"]) == 1
    assert (
        f["authority"].status("controlled-job")["state"]["stop_reason"]
        == "run_deadline"
    )


def test_changed_runtime_manifest_needs_new_exact_run_decision(tmp_path):
    from agent_alfred.evals.acceptance.controlled.contract import signed

    f = fixture(tmp_path)
    proposed = f["submission"]["plan"]
    candidate = {
        "files": {
            "src/agent_alfred/controlled_fixture.py": {
                "kind": "file",
                "sha256": "a" * 64,
            }
        }
    }
    changed = {k: v for k, v in proposed.items() if k != "id"}
    changed.update(
        runtime_candidate=candidate,
        candidate_id=digest(candidate),
        candidate_change={
            "before": proposed["material_candidate_id"],
            "after": digest(candidate),
        },
    )
    f["submission"]["plan"] = signed(changed)
    with pytest.raises(ValueError, match="decision_scope_mismatch"):
        f["authority"].submit_job(f["submission"])
    assert f["credentials"].reads == 0 and not f["sends"]


def test_anchor_rollback_and_attempt_row_tampering_do_not_reopen_dispatch(tmp_path):
    f = fixture(tmp_path, handler=lambda request: response(request, usage=False))
    auth = f["authority"]
    auth.submit_job(f["submission"])
    client, _ = prepare(f)
    client.respond(f["request"])
    row = auth.status("controlled-job")["attempts"][0]
    f["ledger"].attempts[("controlled-job", row["attempt_id"])]["worst_units"] = 0
    with pytest.raises(ValueError, match="attempt_ledger_unverifiable"):
        auth.status("controlled-job")
    f["anchor"].high["controlled-job"]["revision"] -= 1
    with pytest.raises(ValueError, match="authority_anchor_mismatch"):
        auth.status("controlled-job")
    assert len(f["sends"]) == 1


def test_invalid_response_can_settle_proven_cost_but_never_become_success(tmp_path):
    def invalid(request):
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4-pro",
                "choices": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    f = fixture(tmp_path, handler=invalid)
    f["authority"].submit_job(f["submission"])
    client, _ = prepare(f)
    result = client.respond(f["request"])
    assert result.final_error.code == "invalid_response"
    assert result.attempts[0].outcome == "aborted"
    status = f["authority"].status("controlled-job")
    assert status["state"]["stop_reason"] == "invalid_response"
    assert (
        status["state"]["spent_units"] == 5400000
        and status["state"]["pending_units"] == 0
    )


def test_billing_bound_violation_keeps_verified_actual_cost_visible(
    tmp_path, monkeypatch
):
    f = fixture(tmp_path)
    original = f["runtime"].settlement

    def inconsistent(row, response, quote):
        receipt = original(row, response, quote)
        receipt["actual_units"] = row["worst_units"] + 1
        return receipt

    monkeypatch.setattr(f["runtime"], "settlement", inconsistent)
    f["authority"].submit_job(f["submission"])
    client, _ = prepare(f)
    result = client.respond(f["request"])
    assert result.final_error.code == "billing_bound_violated"
    status = f["authority"].status("controlled-job")
    assert status["state"]["spent_units"] == status["attempts"][0]["worst_units"] + 1
    assert status["state"]["stop_reason"] == "billing_bound_violated"
