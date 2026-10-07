"""Offline contract separation; this fixture authorizes no real execution."""

import hashlib
import json
import os
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agent_alfred.evals.acceptance.controlled.contract import (
    ADVISORY_BUDGET,
    ADVISORY_RISKS,
    CAP_UNITS,
    COVERAGE,
    advisory_run_disclosure,
    execution_plan,
    initial_budget,
    validate_plan,
)
from agent_alfred.evals.acceptance.controlled.local_files import OwnerDirectory
from agent_alfred.evals.acceptance.controlled.local_installation import (
    ADVISORY_BILLING,
    EVIDENCE_CHECKS,
    final_charge_path,
    read_advisory_budget,
    read_billing,
    read_final_charge,
)
from agent_alfred.evals.acceptance.controlled.local_persistence import (
    _LOCAL_STORAGE,
    LocalExecutionStore,
    LocalExecutionWitness,
)
from agent_alfred.evals.acceptance.controlled.local_runtime import LocalInstalledRuntime
from agent_alfred.evals.acceptance.controlled.store import MemoryExecutionStore
from agent_alfred.evals.acceptance.controlled_execution import ControlledAuthority
from agent_alfred.evals.acceptance.controlled_persistence import (
    PersistentControlledAuthority,
)
from agent_alfred.evals.acceptance.execution_decisions import (
    material_binding,
    run_request,
    validate_request,
)
from agent_alfred.evals.acceptance.schema import digest, encode
from agent_alfred.evals.acceptance.store import EvidenceStore

from .test_controlled_execution import fixture


def _terms(model, *, advisory):
    value = {
        "version": 2 if advisory else 1,
        "model": model,
        "currency": "USD",
        "input_per_million": 1_000_000_000,
        "output_per_million": 1_000_000_000,
        "request_fee": 0,
        "other_fee": 0,
        "tax_numerator": 0,
        "tax_denominator": 1,
        "fx_numerator": 1,
        "fx_denominator": 1,
        "quantum": 1,
        "coverage": COVERAGE,
        "valid_from": "2026-09-28T00:00:00+00:00",
        "valid_until": "2026-10-01T00:00:00+00:00",
        "evidence": "offline:unverified-price",
    }
    if advisory:
        value.update(
            planning_only=True,
            source_currency="CNY",
            source_price_ref="a" * 64,
            risks=ADVISORY_RISKS,
        )
    return value


def _plan(*, advisory, judge_profile=None):
    pricing = {
        "flash": _terms("deepseek-flash", advisory=advisory),
        "pro": _terms("deepseek-v4-pro", advisory=advisory),
    }
    if judge_profile is not None:
        pricing.pop("pro")
    return execution_plan(
        binding={"candidate_id": "b" * 64},
        material_ref={"offline": "fixture"},
        worker="offline-worker",
        controller="offline-controller",
        operations=[
            {
                "id": "offline-judge",
                "kind": "judge_test",
                "object_id": "offline-object",
                "case_id": None,
                "instance_id": "offline-instance",
                "data_scope_sha256": "c" * 64,
                "max_calls": 1,
            }
        ],
        pricing=pricing,
        execution_mode="authorized" if advisory else "synthetic_capacity",
        runtime_candidate={"files": {}},
        budget_policy=ADVISORY_BUDGET if advisory else None,
        **({"flash_judge_profile": judge_profile} if judge_profile else {}),
    )


def test_flash_plan_is_explicit_and_preserves_judge_role_limits():
    from agent_alfred.evals.acceptance.controlled.contract import (
        effective_judge_profile,
        validate_payload,
        wire_payload,
    )
    from agent_alfred.evals.acceptance.controlled_diagnostics import _request
    from agent_alfred.evals.acceptance.examples_v4 import supplement_batch

    batch = supplement_batch(phase="calibration")
    old = batch["judge_profile"]
    old["model"]["model_id"] = "deepseek-v4-pro"
    _resign(old)
    plan = _plan(advisory=True, judge_profile=old)
    assert plan["version"] == 3
    assert plan["limits"] == {"flash": 576, "pro": 0, "total": 576, "per_case": 16}
    assert initial_budget(plan)["final_bill_ceiling_proven"] is False
    batch["judge_profile"] = effective_judge_profile(plan, old)
    assert batch["judge_profile"]["model"]["model_id"] == "deepseek-flash"
    op = dict(plan["operations"][0], object_id=batch["judge_tests"][0]["id"])
    payload = wire_payload(_request(batch, op))
    assert payload["model"] == "deepseek-flash"
    assert payload["max_tokens"] == 8192
    assert payload["response_format"] == {"type": "json_object"}
    validate_payload(payload, op, plan=plan)
    with pytest.raises(ValueError, match="controlled_payload_policy_mismatch"):
        validate_payload(dict(payload, model="deepseek-v4-pro"), op, plan=plan)
    with pytest.raises(ValueError, match="controlled_integer_invalid"):
        changed = deepcopy(plan)
        changed["operations"][0]["max_calls"] = 2
        validate_plan(_resign(changed))
    with pytest.raises(ValueError, match="runtime_judge_profile_mismatch"):
        effective_judge_profile(plan, dict(old, protocol={}))
    with pytest.raises(ValueError):
        changed = deepcopy(plan)
        changed["judge_profile_change"]["after"]["protocol"] = {}
        _resign(changed["judge_profile_change"]["after"])
        validate_plan(_resign(changed))
    with pytest.raises(ValueError, match="controlled_budget_policy_invalid"):
        validate_plan(_resign(dict(plan, execution_mode="synthetic_capacity")))


def _resign(plan):
    plan["id"] = digest({key: value for key, value in plan.items() if key != "id"})
    return plan


def test_advisory_plan_is_distinct_from_strict_hard_cap():
    strict = _plan(advisory=False)
    advisory = _plan(advisory=True)
    assert strict["version"] == 1
    assert "budget_policy" not in strict
    assert initial_budget(strict) == {
        "cap_units": CAP_UNITS,
        "limits": strict["limits"],
        "total_seconds": 10800,
        "currency": "USD",
        "scale": 10**12,
    }
    assert advisory["version"] == 2
    assert advisory["contract"] == "V2-LOCAL-ADVISORY-CALIBRATION"
    assert advisory["budget_policy"] == ADVISORY_BUDGET
    assert initial_budget(advisory)["final_bill_ceiling_proven"] is False
    assert initial_budget(advisory)["budget_policy"] == ADVISORY_BUDGET
    assert advisory["limits"] == strict["limits"]
    assert advisory["cap_units"] == strict["cap_units"]

    forged = deepcopy(strict)
    forged["budget_policy"] = ADVISORY_BUDGET
    with pytest.raises(ValueError):
        validate_plan(_resign(forged))
    forged = deepcopy(advisory)
    forged.pop("budget_policy")
    with pytest.raises(ValueError):
        validate_plan(_resign(forged))
    forged = deepcopy(advisory)
    forged["pricing"]["flash"].pop("planning_only")
    with pytest.raises(ValueError):
        validate_plan(_resign(forged))


def test_advisory_plan_cannot_be_synthetic_or_claim_a_final_bill_bound():
    advisory = _plan(advisory=True)
    forged = deepcopy(advisory)
    forged["execution_mode"] = "synthetic_capacity"
    with pytest.raises(ValueError, match="controlled_budget_policy_invalid"):
        validate_plan(_resign(forged))
    forged = deepcopy(advisory)
    forged["pricing"]["pro"]["planning_only"] = False
    with pytest.raises(ValueError, match="billing_unverifiable"):
        validate_plan(_resign(forged))
    forged = deepcopy(advisory)
    forged["pricing"]["pro"]["input_per_million"] = 0
    forged["pricing"]["pro"]["output_per_million"] = 0
    with pytest.raises(ValueError, match="billing_unverifiable"):
        validate_plan(_resign(forged))


def test_advisory_run_request_discloses_both_changes_to_owner_helper(tmp_path):
    offline = fixture(tmp_path)
    material = offline["submission"]["material_ref"]
    evidence = EvidenceStore(tmp_path / "source-materials" / "evidence")
    binding = material_binding(
        evidence,
        offline["batch"]["batch_id"],
        package_manifest_sha256=material["manifest_sha256"],
    )
    disclosure = advisory_run_disclosure(CAP_UNITS)
    request = run_request(
        binding,
        job_id="offline-v2-job",
        plan_sha256="a" * 64,
        budget_sha256="b" * 64,
        disclosure=disclosure,
    )
    assert request["version"] == 2
    assert request["contract"] == "V2-LOCAL-ADVISORY-RUN-DECISION"
    assert "最终账单可能超过USD25" in request["disclosure"]["notice_zh"]
    assert "实际计费输入token可能超出" in request["disclosure"]["notice_zh"]
    forged = deepcopy(request)
    forged["disclosure"]["notice_zh"] = ""
    with pytest.raises(ValueError, match="advisory_run_disclosure_invalid"):
        validate_request(_resign(forged))
    old_request = run_request(
        binding,
        job_id="offline-v1-job",
        plan_sha256="a" * 64,
        budget_sha256="b" * 64,
    )
    assert old_request["version"] == 1
    assert "disclosure" not in old_request


def test_advisory_status_and_stop_report_never_label_plan_as_liability_ceiling():
    plan = _plan(advisory=True)
    state = {
        "job_id": "offline-job",
        "state": "SUSPENDED",
        "plan_ref": digest(plan),
        "spent_units": 12,
        "pending_units": 34,
        "stop_reason": "offline-stop",
    }
    rows = []
    authority = object.__new__(PersistentControlledAuthority)
    authority.runtime = object.__new__(LocalInstalledRuntime)
    authority.store = SimpleNamespace(
        get_object=lambda reference: plan,
        list_attempts=lambda job_id: rows,
        faults=lambda job_id: [],
        read_events=lambda job_id: [],
    )
    authority.anchor = SimpleNamespace(faults=lambda job_id: [])
    authority._verified = lambda job_id: state
    authority.persistence_call = lambda job_id, operation, *args, **kwargs: operation(
        *args, **kwargs
    )
    status = authority.status("offline-job")
    assert status["contract"] == "V2-LOCAL-CONTROLLED-STATUS"
    assert status["billing"]["unsettled_planned_units"] == 34
    assert status["billing"]["final_bill_ceiling_proven"] is False
    assert "unsettled_maximum_units" not in status["billing"]
    stopped = authority.stop_report("offline-job")
    assert stopped["contract"] == "V2-LOCAL-CONTROLLED-STOP-REPORT"
    assert stopped["unsettled_planned_units"] == 34
    assert stopped["final_bill_ceiling_proven"] is False
    assert "final_bill_verified" not in stopped
    state["pending_units"] = 0
    rows.append({"send_state": "RESPONSE_VERIFIED", "error": None})
    assert (
        authority.status("offline-job")["billing"]["all_attempts_reconciled"] is False
    )
    assert authority.stop_report("offline-job")["all_attempts_reconciled"] is False
    authority.status = lambda job_id: (_ for _ in ()).throw(ValueError("ledger_lost"))
    authority.store.get = lambda job_id: state
    unknown = authority.stop_report("offline-job")
    assert unknown["contract"] == "LOCAL-CONTROLLED-STOP-REPORT-UNVERIFIED"
    assert unknown["version"] is None
    assert unknown["budget_policy"] == "UNVERIFIED"


@pytest.mark.parametrize("flash_only", [False, True])
def test_advisory_quote_and_response_keep_estimate_distinct_from_usage(
    monkeypatch, flash_only
):
    from agent_alfred.evals.acceptance.controlled import local_runtime
    from agent_alfred.evals.acceptance.examples_v4 import supplement_batch

    profile = supplement_batch()["judge_profile"]
    profile["model"]["model_id"] = "deepseek-v4-pro"
    _resign(profile)
    plan = _plan(advisory=True, judge_profile=profile if flash_only else None)
    model = "deepseek-flash" if flash_only else "deepseek-v4-pro"
    budget = {
        "pricing": plan["pricing"],
        "input_measurement": {
            "method": "complete_wire_utf8_planning_estimate",
            "numerator": 1,
            "denominator": 1,
            "overhead": 0,
        },
        "account_scope": "offline:account",
        "provider_origin": "https://platform.deepseek.com/",
        "provider_evidence": {"path": "offline", "sha256": "f" * 64},
        "approval_ref": "offline:source",
    }
    runtime = object.__new__(LocalInstalledRuntime)
    runtime.store = MemoryExecutionStore()
    runtime._config = {
        "installation_id": "offline:installation",
        "candidate_id": "b" * 64,
    }
    runtime._directory = None
    runtime._decision_source = SimpleNamespace(
        verify_owner_record=lambda source_ref, request: None
    )
    runtime.verify_installation = lambda: None
    runtime._billing = lambda now, **kwargs: budget
    if flash_only:
        budget["input_policy"] = plan["input_policy"]
    monkeypatch.setattr(
        local_runtime,
        "read_readiness",
        lambda *args, **kwargs: {
            "identities": {"evidence": "offline:identity"},
            "evidence": {"advisory_budget": budget["provider_evidence"]},
        },
    )
    monkeypatch.setattr(
        local_runtime, "read_final_charge", lambda *args, **kwargs: None
    )
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "x"}],
    }
    quote = runtime.quote(plan, plan["operations"][0], payload, datetime.now(UTC))
    assert quote["budget_policy"] == ADVISORY_BUDGET
    assert "input_estimate" in quote and "input_tokens" not in quote
    if flash_only:
        assert quote["input_estimate"] == 276
    else:
        assert quote["input_estimate"] == len(encode(payload))
    assert quote["terms"]["model"] == model
    original = runtime.store.get_object(quote["token_evidence"])
    assert original["complete_wire_bytes"] == len(encode(payload))
    assert "input_upper_bound" not in original
    row = {
        "payload_sha256": quote["payload_sha256"],
        "model": model,
        "kind": "judge_test",
        "output_bound": 8192 if flash_only else 16384,
    }
    response = {
        "status_code": 200,
        "raw": json.dumps(
            {
                "model": row["model"],
                "usage": {
                    "prompt_tokens": quote["input_estimate"] + 1,
                    "completion_tokens": 1,
                },
            }
        ),
    }
    settlement = runtime.settlement(row, response, quote)
    assert settlement["contract"] == "V2-LOCAL-PLAN-READBACK"
    assert settlement["actual_units"] is None
    assert settlement["liability_state"] == "RESERVED"


@pytest.mark.parametrize("flash_only", [False, True])
def test_advisory_document_cannot_pass_the_old_hard_billing_reader(
    tmp_path, flash_only
):
    # Closed structural fixture only; none of these bytes are real account proof.
    root = tmp_path.resolve() / "owner"
    root.mkdir(mode=0o700)
    directory = OwnerDirectory(root, os.geteuid())
    now = datetime.now(UTC)
    later = (now + timedelta(hours=1)).isoformat()
    config = {"installation_id": "offline:install", "candidate_id": "d" * 64}
    origin = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"

    def document(name, value):
        path = directory.file(name, create=True)
        raw = encode(value)
        path.write_bytes(raw)
        return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}

    observations = []
    for check in sorted(EVIDENCE_CHECKS["advisory_budget"]):
        artifact = document(check + ".txt", {"offline_fixture": check})
        raw = document(
            check + ".json",
            {
                "contract": "V1-LOCAL-RAW-OBSERVATION",
                "version": 1,
                "check": check,
                **config,
                "observed_at": now.isoformat(),
                "observer": "offline:tester",
                "subject": "offline:fixture",
                "method": "owner_reviewed_account_and_dispatch_evidence",
                "result": "verified",
                "artifacts": [artifact],
            },
        )
        observations.append(
            {
                "check": check,
                "expected": "verified",
                "actual": "verified",
                "raw_evidence": raw,
            }
        )
    evidence = document(
        "advisory-evidence.json",
        {
            "contract": "V1-LOCAL-VERIFICATION-EVIDENCE",
            "version": 1,
            "kind": "advisory_budget",
            **config,
            "observed_at": now.isoformat(),
            "valid_until": later,
            "synthetic": False,
            "evidence_class": "actual_environment_or_account",
            "origin": origin,
            "observations": observations,
        },
    )
    pricing = {
        group: _terms(model, advisory=True)
        for group, model in (
            ("flash", "deepseek-flash"),
            ("pro", "deepseek-v4-pro"),
        )
    }
    for value in pricing.values():
        value["source_price_ref"] = evidence["sha256"]
        value["evidence"] = evidence["sha256"]
    budget = {
        "contract": ADVISORY_BILLING,
        "version": 2,
        **config,
        "account_scope": "offline:account",
        "provider_origin": origin,
        "provider_evidence": evidence,
        "verification": {
            "method": "public_terms_and_account_readback",
            "account_attribution": "offline:account",
            "no_automatic_credit": True,
            "final_bill_ceiling_proven": False,
            "actual_input_ceiling_proven": False,
            "risks": ADVISORY_RISKS,
        },
        "pricing": pricing,
        "input_measurement": {
            "method": "complete_wire_utf8_planning_estimate",
            "numerator": 1,
            "denominator": 1,
            "overhead": 0,
        },
        "valid_from": now.isoformat(),
        "valid_until": later,
        "approval_ref": "offline:unverified-owner-record",
    }
    if flash_only:
        budget.update(contract="V3-LOCAL-FLASH-ADVISORY-BUDGET", version=3)
        budget["pricing"].pop("pro")
        from agent_alfred.evals.acceptance.controlled.contract import flash_input_policy

        budget["input_policy"] = flash_input_policy()
    options = {"model_policy": "flash_only"} if flash_only else {}
    billing = document("billing.json", budget)
    config["billing_path"] = billing["path"]
    assert read_advisory_budget(directory, config, now=now, **options) == budget
    if flash_only:
        with pytest.raises(ValueError, match="local_advisory_budget_unverifiable"):
            read_advisory_budget(directory, config, now=now)
    else:
        with pytest.raises(ValueError, match="local_advisory_budget_unverifiable"):
            read_advisory_budget(directory, config, now=now, model_policy="flash_only")
    with pytest.raises(ValueError, match="local_billing_bound_unverifiable"):
        read_billing(directory, config, now=now)
    budget["verification"]["final_bill_ceiling_proven"] = True
    directory.file(billing["path"]).write_bytes(encode(budget))
    with pytest.raises(ValueError, match="local_advisory_budget_unverifiable"):
        read_advisory_budget(directory, config, now=now, **options)


def test_visible_final_charge_blocks_next_permission_until_original_settlement(
    tmp_path,
):
    root = tmp_path.resolve() / "owner"
    root.mkdir(mode=0o700)
    directory = OwnerDirectory(root, os.geteuid())
    row = {"attempt_id": "original-attempt", "send_state": "RESPONSE_VERIFIED"}
    runtime = object.__new__(LocalInstalledRuntime)
    runtime._directory = directory
    runtime.check = lambda plan, binding, batch, now: {"identities": {}}
    authority = object.__new__(ControlledAuthority)
    authority.runtime = runtime
    authority.store = SimpleNamespace(
        get_object=lambda reference: {},
        list_attempts=lambda job_id: [row],
    )
    authority.persistence_call = lambda job_id, operation, *args, **kwargs: operation(
        *args, **kwargs
    )
    authority._context = lambda state: (
        _plan(advisory=True),
        {"batch": {"batch_id": "offline-batch"}},
        SimpleNamespace(read=lambda batch_id: {}),
        None,
    )
    authority._verify_source = lambda *args, **kwargs: None
    state = {
        "job_id": "offline-job",
        "run_request_ref": "offline-request",
        "run_source_ref": "offline-source",
        "phase": "diagnostic",
    }
    assert runtime.visible_final_charge(row) is False
    authority._current_permission(state, datetime.now(UTC))
    final_charge_path(directory, row).write_bytes(b"offline:unverified-final-charge")
    assert runtime.visible_final_charge(row) is True
    with pytest.raises(ValueError, match="final_charge_reconciliation_required"):
        authority._current_permission(state, datetime.now(UTC))


def test_advisory_final_charge_requires_cny_and_original_fx_snapshot(tmp_path):
    root = tmp_path.resolve() / "owner"
    root.mkdir(mode=0o700)
    directory = OwnerDirectory(root, os.geteuid())
    now = datetime.now(UTC)
    config = {"installation_id": "offline-install", "candidate_id": "d" * 64}
    row = {"attempt_id": "original-attempt", "payload_sha256": "a" * 64}
    response = {"status_code": 200, "raw": "first-response"}
    quote = {"terms": {"fx_numerator": 2, "fx_denominator": 3}}
    billing = {
        "contract": ADVISORY_BILLING,
        "account_scope": "offline:account",
        "provider_origin": "https://platform.deepseek.com/",
    }
    record = directory.file("provider-record.txt", create=True)
    record.write_bytes(b"OFFLINE ACCOUNT STRUCTURE ONLY")
    value = {
        "contract": "V2-LOCAL-ADVISORY-FINAL-CHARGE",
        "version": 2,
        **config,
        "attempt_id": row["attempt_id"],
        "payload_sha256": row["payload_sha256"],
        "response_sha256": digest(response),
        "quote_sha256": digest(quote),
        "account_scope": billing["account_scope"],
        "source_currency": "CNY",
        "charged_cny_units": 150,
        "normalized_usd_units": 100,
        "fx_numerator": 2,
        "fx_denominator": 3,
        "provider_origin": billing["provider_origin"],
        "provider_record": {
            "path": str(record),
            "sha256": hashlib.sha256(record.read_bytes()).hexdigest(),
        },
        "observed_at": now.isoformat(),
        "valid_until": (now + timedelta(hours=1)).isoformat(),
        "approval_ref": "offline:unverified-owner-review",
    }
    path = final_charge_path(directory, row)
    directory.file(path, create=True)
    path.write_bytes(encode(value))
    assert (
        read_final_charge(directory, config, row, response, quote, billing, now=now)
        == value
    )
    value["normalized_usd_units"] = 99
    path.write_bytes(encode(value))
    with pytest.raises(ValueError, match="local_final_charge_conversion_mismatch"):
        read_final_charge(directory, config, row, response, quote, billing, now=now)
    value["normalized_usd_units"] = 100
    value["contract"] = "V1-LOCAL-FINAL-CHARGE"
    path.write_bytes(encode(value))
    with pytest.raises(ValueError, match="local_final_charge_unverifiable"):
        read_final_charge(directory, config, row, response, quote, billing, now=now)


def test_actual_usage_over_preflight_preserves_response_and_stops_new_dispatch():
    now = datetime.now(UTC)
    raw = {
        "model": "deepseek-v4-pro",
        "choices": [
            {"message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 6, "completion_tokens": 1},
    }
    response = {"status_code": 200, "raw": json.dumps(raw)}
    quote = {"budget_policy": ADVISORY_BUDGET, "terms": {"valid_until": "future"}}
    objects = {digest(quote): quote}
    row = {
        "attempt_id": "attempt-1",
        "response_ref": None,
        "send_state": "SENDING",
        "budget_basis": ADVISORY_BUDGET,
        "payload_sha256": "a" * 64,
        "input_estimate": 5,
        "output_bound": 16384,
        "model": "deepseek-v4-pro",
        "kind": "judge_test",
        "planned_units": 100,
        "quote_ref": digest(quote),
        "deadline": (now + timedelta(seconds=30)).isoformat(),
        "error": None,
    }
    state = {
        "job_id": "offline-job",
        "active_attempt": row["attempt_id"],
        "pending_units": 100,
        "spent_units": 0,
        "state": "ACTIVE",
        "stop_reason": None,
        "started_at": (now - timedelta(seconds=1)).isoformat(),
        "last_at": now.isoformat(),
    }
    events = []

    def put_object(value):
        reference = digest(value)
        objects[reference] = value
        return reference

    runtime = object.__new__(LocalInstalledRuntime)
    runtime.settlement = lambda attempt, result, original: {
        "contract": "V2-LOCAL-PLAN-READBACK",
        "version": 2,
        "payload_sha256": attempt["payload_sha256"],
        "response_sha256": digest(result),
        "liability_state": "RESERVED",
        "actual_units": None,
        "evidence": "offline:usage",
    }
    authority = object.__new__(ControlledAuthority)
    authority.runtime = runtime
    authority.store = SimpleNamespace(
        get_attempt=lambda job_id, attempt_id: row,
        put_object=put_object,
        get_object=lambda reference: objects[reference],
    )
    authority.now = lambda: now
    authority.persistence_call = lambda job_id, operation, *args, **kwargs: operation(
        *args, **kwargs
    )
    authority._verified = lambda job_id: state

    def commit(changed, kind, **kwargs):
        events.append((kind, deepcopy(changed), deepcopy(kwargs.get("attempt"))))
        return changed

    authority._commit = commit
    result = authority._settle("offline-job", row["attempt_id"], response)
    assert result["attempt"]["response_ref"] == digest(response)
    assert result["attempt"]["usage"]["input_tokens"] == 6
    assert result["attempt"]["error"] == "input_preflight_exceeded"
    assert result["attempt"]["outcome"] == "aborted"
    assert result["attempt"]["liability_state"] == "RESERVED"
    assert events[0][0] == "RESPONSE_RECEIVED"
    assert events[1][0] == "STOP"
    assert events[1][1]["stop_reason"] == "input_preflight_exceeded"
    assert events[2][0] == "VERIFY_RESPONSE_RETAIN_LIABILITY"
    assert events[2][1]["pending_units"] == 100


def test_late_final_charge_above_plan_is_not_clipped_to_usd25():
    now = datetime.now(UTC)
    response = {"status_code": 200, "raw": "original-first-response"}
    quote = {"budget_policy": ADVISORY_BUDGET, "input_estimate": 5}
    objects = {digest(quote): quote}
    row = {
        "attempt_id": "original-attempt",
        "payload_sha256": "b" * 64,
        "response_ref": digest(response),
        "liability_state": "RESERVED",
        "actual_units": None,
        "budget_basis": ADVISORY_BUDGET,
        "planned_units": 100,
        "quote_ref": digest(quote),
    }
    state = {
        "job_id": "offline-job",
        "active_attempt": None,
        "pending_units": 100,
        "spent_units": 0,
        "state": "ACTIVE",
        "stop_reason": None,
    }
    runtime = object.__new__(LocalInstalledRuntime)
    runtime.settlement = lambda attempt, result, original: {
        "payload_sha256": attempt["payload_sha256"],
        "response_sha256": digest(result),
        "actual_units": 120,
        "evidence": "offline:final-charge-structure",
    }

    def put_object(value):
        reference = digest(value)
        objects[reference] = value
        return reference

    authority = object.__new__(ControlledAuthority)
    authority.runtime = runtime
    authority.store = SimpleNamespace(
        get_object=lambda reference: objects[reference], put_object=put_object
    )
    authority.now = lambda: now
    authority.persistence_call = lambda job_id, operation, *args, **kwargs: operation(
        *args, **kwargs
    )
    events = []

    def commit(changed, kind, **kwargs):
        events.append((kind, deepcopy(changed), deepcopy(kwargs.get("attempt"))))
        return changed

    authority._commit = commit
    result = authority._settle_reserved_liability(state, row, response)
    assert result["attempt"]["actual_units"] == 120
    assert result["attempt"]["response_ref"] == digest(response)
    assert result["response"] == response
    assert events[0][0] == "STOP"
    assert events[0][1]["stop_reason"] == "budget_plan_exceeded"
    assert events[1][0] == "SETTLE_RETAINED_LIABILITY"
    assert events[1][1]["spent_units"] == 120
    assert events[1][1]["pending_units"] == 0


def test_public_local_settlement_persists_overrun_and_first_response_across_restart(
    tmp_path,
):
    """An offline provider-proof seam; actual owner/provider facts are absent."""
    root = tmp_path.resolve() / "owner"
    root.mkdir(mode=0o700)
    directory = OwnerDirectory(root, os.geteuid())
    plan = _plan(advisory=True)
    response = {"status_code": 200, "raw": "first-provider-response"}

    def open_authority():
        store = LocalExecutionStore(directory, "ledger", token=_LOCAL_STORAGE)
        witness = LocalExecutionWitness(directory, "witness", token=_LOCAL_STORAGE)
        runtime = object.__new__(LocalInstalledRuntime)
        runtime.store, runtime.anchor = store, witness
        runtime._config = {
            "worker": "offline-worker",
            "controller": "offline-controller",
            "subject": "offline-owner",
        }
        runtime.settlement = lambda attempt, result, quote: {
            "payload_sha256": attempt["payload_sha256"],
            "response_sha256": digest(result),
            "actual_units": 120,
            "evidence": "offline:final-charge-fixture",
        }
        authority = PersistentControlledAuthority(
            runtime=runtime,
            store=store,
            anchor=witness,
            worker="offline-worker",
            controller="offline-controller",
            subject="offline-owner",
        )
        return authority, store, witness

    authority, store, witness = open_authority()
    try:
        plan_ref = store.put_object(plan)
        response_ref = store.put_object(response)
        quote_ref = store.put_object({"budget_policy": ADVISORY_BUDGET})
        row = {
            "attempt_id": "original-attempt",
            "prepared_ref": "c" * 64,
            "payload_sha256": "b" * 64,
            "response_ref": response_ref,
            "send_state": "RESPONSE_VERIFIED",
            "liability_state": "RESERVED",
            "actual_units": None,
            "budget_basis": ADVISORY_BUDGET,
            "planned_units": 100,
            "quote_ref": quote_ref,
            "outcome": "completed",
            "error": None,
        }
        original = authority._commit(
            {
                "job_id": "offline-job",
                "state": "ACTIVE",
                "stop_reason": None,
                "plan_ref": plan_ref,
                "active_attempt": row["attempt_id"],
                "active_attempt_sha256": None,
                "pending_units": 100,
                "spent_units": 0,
            },
            "ACTIVATE",
            attempt=row,
        )
        idle = deepcopy(original)
        idle["active_attempt"] = None
        authority._commit(
            idle, "VERIFY_RESPONSE_RETAIN_LIABILITY", previous=original, attempt=row
        )
    finally:
        store.close()
        witness.close()

    authority, store, witness = open_authority()
    try:
        result = authority.settle_attempt(
            "offline-job",
            row["attempt_id"],
            response_ref,
            principal="offline-controller",
        )
        assert result["attempt"]["response_ref"] == response_ref
        assert result["attempt"]["actual_units"] == 120
        stopped = authority.status("offline-job")
        assert stopped["state"]["stop_reason"] == "budget_plan_exceeded"
        assert stopped["state"]["spent_units"] == 120
        assert stopped["state"]["pending_units"] == 0
        assert stopped["billing"]["final_bill_ceiling_proven"] is False
        with pytest.raises(ValueError, match="budget_plan_exceeded"):
            authority.finish("offline-job", "offline-judge")
        other_ref = store.put_object({"status_code": 200, "raw": "changed-response"})
        with pytest.raises(ValueError, match="original_response_immutable"):
            authority.settle_attempt(
                "offline-job",
                row["attempt_id"],
                other_ref,
                principal="offline-controller",
            )
    finally:
        store.close()
        witness.close()

    authority, store, witness = open_authority()
    try:
        after = authority.status("offline-job")
        assert after["state"]["stop_reason"] == "budget_plan_exceeded"
        assert after["attempts"][0]["response_ref"] == response_ref
        assert store.get_object(response_ref) == response
        assert after["state"]["spent_units"] == 120
    finally:
        store.close()
        witness.close()


@pytest.mark.parametrize("valid_final_charge", [True, False])
def test_visible_final_charge_reconciles_before_next_action_without_resetting_job(
    tmp_path, valid_final_charge
):
    """A deposited bill either reconciles or durably stops the same job."""
    root = tmp_path.resolve() / "owner"
    root.mkdir(mode=0o700)
    directory = OwnerDirectory(root, os.geteuid())
    store = LocalExecutionStore(directory, "ledger", token=_LOCAL_STORAGE)
    witness = LocalExecutionWitness(directory, "witness", token=_LOCAL_STORAGE)
    try:
        runtime = object.__new__(LocalInstalledRuntime)
        runtime.store, runtime.anchor = store, witness
        runtime._config = {
            "worker": "offline-worker",
            "controller": "offline-controller",
            "subject": "offline-owner",
        }
        runtime.visible_final_charge = lambda row: True

        def settlement(attempt, result, quote):
            if not valid_final_charge:
                raise ValueError("final_charge_invalid")
            return {
                "payload_sha256": attempt["payload_sha256"],
                "response_sha256": digest(result),
                "actual_units": 80,
                "evidence": "offline:deposited-final-charge-fixture",
            }

        runtime.settlement = settlement
        authority = PersistentControlledAuthority(
            runtime=runtime,
            store=store,
            anchor=witness,
            worker="offline-worker",
            controller="offline-controller",
            subject="offline-owner",
        )
        plan_ref = store.put_object(_plan(advisory=True))
        response = {"status_code": 200, "raw": "original-first-response"}
        response_ref = store.put_object(response)
        quote_ref = store.put_object({"budget_policy": ADVISORY_BUDGET})
        row = {
            "attempt_id": "original-attempt",
            "prepared_ref": "c" * 64,
            "payload_sha256": "b" * 64,
            "response_ref": response_ref,
            "send_state": "RESPONSE_VERIFIED",
            "liability_state": "RESERVED",
            "actual_units": None,
            "budget_basis": ADVISORY_BUDGET,
            "planned_units": 100,
            "quote_ref": quote_ref,
            "outcome": "completed",
            "error": None,
        }
        started = datetime.now(UTC) - timedelta(seconds=60)
        original = authority._commit(
            {
                "job_id": "offline-job",
                "state": "ACTIVE",
                "stop_reason": None,
                "plan_ref": plan_ref,
                "active_attempt": row["attempt_id"],
                "active_attempt_sha256": None,
                "pending_units": 100,
                "spent_units": 0,
                "started_at": started.isoformat(),
                "last_at": started.isoformat(),
            },
            "ACTIVATE",
            attempt=row,
        )
        idle = deepcopy(original)
        idle["active_attempt"] = None
        authority._commit(
            idle, "VERIFY_RESPONSE_RETAIN_LIABILITY", previous=original, attempt=row
        )

        before = authority._verified("offline-job")

        def action():
            return authority._action(
                before, "OFFLINE_NEXT", lambda state, now: (None, {}), permission=False
            )

        if valid_final_charge:
            action()
        else:
            with pytest.raises(ValueError, match="final_charge_invalid"):
                action()
        after = authority.status("offline-job")
        assert after["state"]["started_at"] == before["started_at"]
        assert after["attempts"][0]["response_ref"] == response_ref
        assert store.get_object(response_ref) == response
        if valid_final_charge:
            assert after["state"]["state"] == "ACTIVE"
            assert after["state"]["spent_units"] == 80
            assert after["state"]["pending_units"] == 0
            assert after["attempts"][0]["send_state"] == "SETTLED"
            assert [event["kind"] for event in store.read_events("offline-job")][
                -3:
            ] == ["SETTLE_RETAINED_LIABILITY", "TIME_CHECK", "OFFLINE_NEXT"]
        else:
            assert after["state"]["state"] == "SUSPENDED"
            assert after["state"]["stop_reason"] == "final_charge_invalid"
            assert after["state"]["spent_units"] == 0
            assert after["state"]["pending_units"] == 100
            assert after["attempts"][0]["send_state"] == "RESPONSE_VERIFIED"
            assert store.read_events("offline-job")[-1]["kind"] == "STOP"
    finally:
        store.close()
        witness.close()

    store = LocalExecutionStore(directory, "ledger", token=_LOCAL_STORAGE)
    witness = LocalExecutionWitness(directory, "witness", token=_LOCAL_STORAGE)
    try:
        runtime = object.__new__(LocalInstalledRuntime)
        runtime.store, runtime.anchor = store, witness
        runtime._config = {
            "worker": "offline-worker",
            "controller": "offline-controller",
            "subject": "offline-owner",
        }
        reopened = PersistentControlledAuthority(
            runtime=runtime,
            store=store,
            anchor=witness,
            worker="offline-worker",
            controller="offline-controller",
            subject="offline-owner",
        ).status("offline-job")
        assert reopened["state"]["state"] == (
            "ACTIVE" if valid_final_charge else "SUSPENDED"
        )
        assert reopened["attempts"][0]["response_ref"] == response_ref
    finally:
        store.close()
        witness.close()


def test_local_handoff_continuity_failure_survives_runtime_and_storage_restart(
    tmp_path,
):
    """Offline clock seam and test state: no installation, owner event or grant."""
    from agent_alfred.evals.acceptance.controlled.local_clock import ContinuityGuard

    root = tmp_path.resolve() / "offline-continuity"
    root.mkdir(mode=0o700)
    directory = OwnerDirectory(root, os.geteuid())
    checkpoint = {"wall": 1000, "awake": 20, "sleep_interval": [100, 110]}
    handoff = {
        "version": 2,
        "job_id": "offline-continuity-job",
        "previous_handler": "offline-prior-handler",
        "revision": 2,
        "nonce": "offline-handoff",
        "trust_profile": "owner_trusted_local",
        "local_continuity": checkpoint,
    }

    def open_authority(sample):
        store = LocalExecutionStore(directory, "ledger", token=_LOCAL_STORAGE)
        witness = LocalExecutionWitness(directory, "witness", token=_LOCAL_STORAGE)
        runtime = object.__new__(LocalInstalledRuntime)
        runtime.store, runtime.anchor = store, witness
        runtime._config = {
            "worker": "offline-worker",
            "controller": "offline-controller",
            "subject": "offline-owner",
        }
        runtime._clock = SimpleNamespace(sample=lambda: deepcopy(sample))
        runtime._continuity = ContinuityGuard()
        authority = PersistentControlledAuthority(
            runtime=runtime,
            store=store,
            anchor=witness,
            worker="offline-worker",
            controller="offline-controller",
            subject="offline-owner",
        )
        return authority, store, witness

    authority, store, witness = open_authority(
        {"wall": 1070, "awake": 31, "sleep_interval": [1000, 1010]}
    )
    try:
        plan_ref = store.put_object(_plan(advisory=True))
        original = authority._commit(
            {
                "job_id": handoff["job_id"],
                "state": "ACTIVE",
                "stop_reason": None,
                "plan_ref": plan_ref,
                "active_attempt": None,
                "active_attempt_sha256": None,
                "prepared_ref": None,
                "pending_units": 0,
                "spent_units": 7,
                "started_at": "2026-09-27T00:00:00+00:00",
                "counts": {"total": 1, "flash": 0, "pro": 1},
                "cap_units": CAP_UNITS,
            },
            "ACTIVATE",
        )
        authority._commit(
            {**original, "handler_epoch": None, "clean_handoff": digest(handoff)},
            "HANDLER_RELEASED",
            previous=original,
            detail={"handoff_sha256": digest(handoff)},
        )
        with pytest.raises(ValueError, match="local_clock_continuity_lost"):
            authority.recover(
                handoff["job_id"], principal="offline-controller", handoff=handoff
            )
        # Close immediately: no status call may create the missing fault for us.
    finally:
        store.close()
        witness.close()

    authority, store, witness = open_authority(
        {"wall": 1001, "awake": 21, "sleep_interval": [100, 110]}
    )
    try:
        with pytest.raises(ValueError, match="clean_handoff_unverifiable"):
            authority.recover(
                handoff["job_id"], principal="offline-controller", handoff=handoff
            )
        stopped = authority.status(handoff["job_id"])["state"]
        assert stopped["state"] == "SUSPENDED"
        assert stopped["stop_reason"] == "local_clock_continuity_lost"
        for field in (
            "started_at",
            "spent_units",
            "pending_units",
            "counts",
            "cap_units",
        ):
            assert stopped[field] == original[field]
        assert all(
            event["kind"] != "HANDLER_ACCEPTED"
            for event in store.read_events(handoff["job_id"])
        )
    finally:
        store.close()
        witness.close()
