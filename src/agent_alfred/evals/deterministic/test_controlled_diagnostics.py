"""Public same-job diagnostic/checkpoint evidence, with no real model traffic."""

import json
from copy import deepcopy

import httpx2 as httpx

from agent_alfred.evals.acceptance.controlled_diagnostics import (
    DiagnosticDriver,
    diagnostic_operations,
)
from agent_alfred.evals.acceptance.examples_v4 import supplement_batch
from agent_alfred.evals.acceptance.schema import digest
from agent_alfred.evals.acceptance.score_evidence import resolve_reference
from agent_alfred.evals.acceptance.supplement_schema import signed
from agent_alfred.evals.deterministic.test_controlled_execution import fixture


def full_fixture(tmp_path, *, output_for=None, with_product=False):
    batch = supplement_batch(phase="calibration")
    batch["judge_profile"] = deepcopy(batch["judge_profile"])
    profile = batch["judge_profile"]
    profile["model"]["model_id"] = "deepseek-v4-pro"
    profile["id"] = digest({k: v for k, v in profile.items() if k != "id"})
    originals = deepcopy(batch["judge_tests"])
    batch["judge_tests"] = [
        signed(
            {
                **{k: v for k, v in row.items() if k != "id"},
                "task": row["task"] + " family " + str(i),
                "source_family_id": row["source_family_id"] + str(i),
            }
        )
        for i in range(6)
        for row in originals
    ]
    outputs = [t["expected"] for t in batch["judge_tests"]]
    sent = []

    def handler(request):
        index = len(sent)
        payload = json.loads(request.content)
        sent.append(payload)
        label = outputs[index % 18]
        output = (
            {
                "label": label,
                "reason": "fixture opinion",
                "evidence": ["judge-material#/answer"],
            }
            if index < 18
            else {
                "judgment": {
                    "status": label,
                    "support": "supported",
                    "reason": "fixture independent opinion",
                    "evidence": ["judge-material#/answer"],
                }
            }
        )
        raw = json.dumps(output) if output_for is None else output_for(index, output)
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": raw},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    from agent_alfred.evals.acceptance.controlled.contract import operation_for

    operations = diagnostic_operations(batch)
    if with_product:
        operations.append(
            operation_for(
                batch,
                operation_id="product-first",
                kind="product",
                object_id=batch["cases"][0]["id"],
                instance_id="product-first-instance",
                max_calls=16,
            )
        )
    f = fixture(
        tmp_path,
        handler=handler,
        batch_override=batch,
        operations_override=operations,
        mode="authorized",
    )
    f["wire"] = sent
    f["authority"].submit_job(f["submission"])
    f["driver"] = DiagnosticDriver(
        f["authority"], "controlled-job", principal="simulation:controller"
    )
    return f


def test_complete_diagnostics_stop_at_checkpoint_without_product_or_extra_review(
    tmp_path,
):
    f = full_fixture(tmp_path)
    report = f["driver"].run()
    assert len(f["wire"]) == 36
    assert report["complete"] is True
    assert len(report["slots"]) == 18
    assert report["product_sample_count"] == 0
    assert report["admission_threshold"] is None
    state = f["authority"].status("controlled-job")["state"]
    assert state["phase"] == "checkpoint"
    assert state["counts"] == {"flash": 0, "pro": 36, "total": 36}
    assert all(
        row["comparison"]["review"]["comparison_output"] for row in report["slots"]
    )
    assert report["diagnostic_disputes"]
    assert f["driver"].run() == report
    for index, slot in enumerate(report["slots"]):
        sent_original = json.loads(f["wire"][index]["messages"][-1]["content"])
        sent_blind = json.loads(f["wire"][18 + index]["messages"][-1]["content"])
        assert sent_original == sent_blind
        for payload in (f["wire"][index], f["wire"][18 + index]):
            instructions = payload["messages"][0]["content"]
            # The actual wire must teach the source-scoped grammar accepted
            # by the unchanged parser, not only ask for "JSON pointers".
            for field in sent_original["sources"]["judge-material"]:
                ref = "judge-material#/" + field
                assert ref in instructions
                assert resolve_reference(ref, sent_original["sources"])
        blind_instructions = f["wire"][18 + index]["messages"][0]["content"]
        assert "response_schema" in blind_instructions
        assert "original test" in blind_instructions
        assert "current response" in blind_instructions
        assert set(sent_original["sources"]["judge-material"]) == {
            "task",
            "gold",
            "answer",
            "prohibition",
        }
        assert "fixture opinion" not in json.dumps(sent_blind)
        assert slot["result"]["instance_id"] != slot["blind"]["instance_id"]
        assert (
            slot["comparison"]["compared_revision"] > slot["blind"]["sealed_revision"]
        )
        assert slot["comparison"]["semantic_support"] == "unknown"
    # The original time window includes human waiting, even after all 36 calls.
    from datetime import timedelta

    import pytest

    event, refs = approve_checkpoint(f)
    f["clock"].value += timedelta(seconds=10800)
    with pytest.raises(ValueError, match="checkpoint_decision_expired_or_premature"):
        f["driver"].continue_with(event["source_ref"], adjudication_refs=refs)
    assert len(f["wire"]) == 36


def approve_checkpoint(f, *, decision="approved", refs=None, request_change=None):
    from agent_alfred.evals.acceptance.supplement_decisions import dispute_request

    source, driver = f["source"], f["driver"]
    if refs is None:
        refs = [
            source.issue_decision(
                dispute_request(f["batch"], dispute),
                subject="simulation:user",
                decision="dismissed",
                reason="Fixture protocol adjudication; semantic record stays unknown",
                evidence=["judge-material#/answer"],
            )["source_ref"]
            for dispute in driver.report()["diagnostic_disputes"]
        ]
    context = driver.checkpoint(adjudication_refs=refs)
    request = context["request"]
    if request_change:
        request = signed(
            {**{k: v for k, v in request.items() if k != "id"}, **request_change}
        )
    event = source.issue_execution_decision(
        request,
        subject="simulation:user",
        decision=decision,
        reason="Fixture checkpoint permission only; no product results or new budget",
        evidence=["synthetic:checkpoint"],
    )
    return event, refs


def test_checkpoint_scope_rejection_and_live_withdrawal_keep_zero_product(tmp_path):
    from dataclasses import replace
    from datetime import timedelta

    import pytest

    from agent_alfred.evals.deterministic.test_controlled_execution import prepare
    from agent_alfred.model import ModelRef

    # A product operation is in the original signed plan, never added at checkpoint.
    f = full_fixture(tmp_path, with_product=True)
    auth, driver = f["authority"], f["driver"]
    driver.run()
    before = auth.status("controlled-job")["state"]
    with pytest.raises(ValueError, match="checkpoint_diagnostic_blocked"):
        driver.continue_with("unknown-source")
    rejected, refs = approve_checkpoint(f, decision="rejected")
    with pytest.raises(ValueError, match="execution_decision_not_effective"):
        driver.continue_with(rejected["source_ref"], adjudication_refs=refs)
    wrong, _ = approve_checkpoint(
        f, refs=refs, request_change={"judge_summary_sha256": "a" * 64}
    )
    with pytest.raises(ValueError, match="decision_scope_mismatch"):
        driver.continue_with(wrong["source_ref"], adjudication_refs=refs)
    valid, _ = approve_checkpoint(f, refs=refs)
    f["clock"].value += timedelta(seconds=37)
    after = driver.continue_with(valid["source_ref"], adjudication_refs=refs)["state"]
    for key in (
        "started_at",
        "cap_units",
        "counts",
        "spent_units",
        "pending_units",
        "finished_operations",
    ):
        assert after[key] == before[key]
    assert after["phase"] == "product"
    assert len(f["wire"]) == 36
    product = next(
        o for o in f["submission"]["plan"]["operations"] if o["kind"] == "product"
    )
    request = replace(
        f["request"],
        model=ModelRef("deepseek", "deepseek-flash"),
        max_tokens=8192,
        response_format=None,
    )
    client, _ = prepare(f, operation=product, request=request)
    f["source"].revoke_decision(valid["source_ref"])
    with pytest.raises(
        ValueError, match="decision_source_unverifiable|approval_source_unverifiable"
    ):
        client.respond(request)
    assert auth.status("controlled-job")["state"]["counts"] == {
        "flash": 0,
        "pro": 36,
        "total": 36,
    }
    assert len(f["wire"]) == f["credentials"].reads == 36
    with pytest.raises(ValueError):
        auth.phase_event(
            "controlled-job",
            principal="simulation:controller",
            expected_phase="product",
            next_phase="diagnostic",
            evidence={"redo": True},
        )


def test_bad_raw_reviews_and_confirmed_miss_remain_fail_with_other_blockers(tmp_path):
    import pytest

    from agent_alfred.evals.acceptance.supplement_decisions import dispute_request

    originals = {
        1: (
            '{"label":"pass","label":"fail","reason":"duplicate",'
            '"evidence":["judge-material#/answer"]}'
        ),
        2: "",
        3: '{"label":"fail","reason":"no citation","evidence":[]}',
        4: (
            '{"label":"pass","reason":"wrong citation",'
            '"evidence":["judge-material#/missing"]}'
        ),
        5: (
            '{"label":"unknown","reason":"root pointer observed in real run",'
            '"evidence":["/sources/judge-material/answer"]}'
        ),
        23: "",
    }

    def output(index, value):
        if index == 0:
            value["label"] = "pass"
        return originals.get(index, json.dumps(value))

    f = full_fixture(tmp_path, output_for=output)
    report = f["driver"].run()
    assert len(f["wire"]) == 36 and report["complete"]
    assert report["errors"]
    assert report["slots"][5]["result"]["error"] == "broken_score_evidence"
    for index, raw in originals.items():
        slot = report["slots"][index % 18]
        assert slot["result" if index < 18 else "blind"]["raw"] == raw
    miss = next(
        d
        for d in report["diagnostic_disputes"]
        if d["kind"] == "judge_check_mismatch"
        and d["target"]["id"] == report["slots"][0]["result"]["result"]["id"]
    )
    event = f["source"].issue_decision(
        dispute_request(f["batch"], miss),
        subject="simulation:user",
        decision="confirmed_violation",
        reason="Confirmed synthetic missed prohibition",
        evidence=["judge-material#/answer"],
    )
    assessed = f["driver"].checkpoint(adjudication_refs=[event["source_ref"]])
    assert assessed["judge_quality"]["verdict"] == "FAIL"
    assert assessed["blockers"] and assessed["failures"]
    with pytest.raises(ValueError, match="checkpoint_diagnostic_blocked"):
        f["driver"].continue_with("unknown", adjudication_refs=[event["source_ref"]])
    assert f["authority"].status("controlled-job")["state"]["counts"]["flash"] == 0

    # Even fresh fixture approvals for all disputes and the exact checkpoint
    # cannot clear a malformed first response or permit a replacement call.
    approved, refs = approve_checkpoint(f)
    dismissed = f["driver"].checkpoint(adjudication_refs=refs)
    assert not dismissed["failures"]
    assert any("broken_score_evidence" in item for item in dismissed["blockers"])
    with pytest.raises(ValueError, match="checkpoint_diagnostic_blocked"):
        f["driver"].continue_with(approved["source_ref"], adjudication_refs=refs)
    assert len(f["wire"]) == 36
    assert f["authority"].status("controlled-job")["state"]["counts"]["flash"] == 0


def test_stage_and_projection_admission_cannot_be_replaced_by_controller_claims(
    tmp_path,
):
    import pytest

    from agent_alfred.evals.deterministic.test_controlled_execution import prepare

    f = full_fixture(tmp_path, with_product=True)
    auth = f["authority"]
    assert f["driver"].report()["complete"] is False
    with pytest.raises(ValueError, match="complete_diagnostic_evidence_required"):
        auth.phase_event(
            "controlled-job",
            principal="simulation:controller",
            expected_phase="diagnostic",
            next_phase="checkpoint",
            evidence={"all_pass": True},
        )
    first = f["submission"]["plan"]["operations"][0]
    with pytest.raises(ValueError, match="diagnostic_projection_mismatch"):
        prepare(f, operation=first)
    blind = next(
        o for o in f["submission"]["plan"]["operations"] if o["kind"] == "judge_review"
    )
    with pytest.raises(ValueError, match="judge_results_incomplete"):
        prepare(f, operation=blind)
    product = next(
        o for o in f["submission"]["plan"]["operations"] if o["kind"] == "product"
    )
    with pytest.raises(ValueError, match="operation_phase_closed"):
        prepare(f, operation=product)
    assert not f["wire"] and f["credentials"].reads == 0


def test_capacity_mode_never_admits_real_binding_or_installed_runtime(tmp_path):
    import pytest

    from agent_alfred.evals.acceptance.controlled.contract import validate_plan
    from agent_alfred.evals.acceptance.controlled.runtime import InstalledRuntime

    f = fixture(tmp_path, product=True)
    plan = f["submission"]["plan"]
    # Pure public runtime prechecks fail before the installed object could be used.
    binding, batch = {"simulation": False}, {"simulation": False}
    with pytest.raises(ValueError, match="synthetic_controlled_runtime_required"):
        f["runtime"].check(plan, binding, batch, f["clock"]())
    with pytest.raises(ValueError, match="runtime_candidate_mismatch"):
        InstalledRuntime.check(None, plan, binding, batch, f["clock"]())
    for mode in ("authorized", "synthetic_replay"):
        altered = signed(
            {**{k: v for k, v in plan.items() if k != "id"}, "execution_mode": mode}
        )
        with pytest.raises(ValueError, match="controlled_plan_invalid"):
            validate_plan(altered)
    assert f["credentials"].reads == f["runtime"].clients_created == 0
