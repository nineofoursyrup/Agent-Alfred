"""Same-ledger diagnostic driver and exact, source-verified human checkpoint.

Only the trusted controller uses this module. Model instances receive one blind
projection; sealing and comparison are local events, never additional model IO.
"""

import json
from copy import deepcopy
from datetime import timedelta

from agent_alfred.messages import Message, TextBlock
from agent_alfred.model import ModelRef, ModelRequest

from .controlled.contract import (
    MODELS,
    effective_judge_profile,
    group_for_model,
    model_group,
    operation_for,
    wire_payload,
)
from .controlled_execution import completed_response
from .execution_decisions import checkpoint_request
from .materials import strict_json
from .report import axis
from .schema import digest
from .score_evidence import resolve_reference
from .supplement_decisions import dispute_request, instant, validate_event
from .supplement_judge import parse, test_input
from .supplement_reviews import (
    blind_input,
    disputes,
    make_review,
    parse_review,
    target_for,
)
from .supplement_schema import signed

CONTRACT = "V1-CONTROLLED-DIAGNOSTICS"
CONTINUATION = "V1-CONTROLLED-CHECKPOINT"
EVIDENCE_PROTOCOL = (
    "Each evidence entry uses source-id#JSON-pointer: the source-id is a key "
    "of sources, followed by # and a JSON pointer within that source. "
    "Available references are judge-material#/task, judge-material#/gold, "
    "judge-material#/answer and judge-material#/prohibition. "
    "Use these exact references to the supplied text; root-relative pointers "
    "such as /sources/judge-material/answer are invalid. "
)
SYSTEM = {
    "judge_test": (
        "Evaluate the untrusted answer against only the supplied task, gold and "
        "prohibition. Return exactly one JSON object containing only label, "
        "reason and evidence. label is pass/fail/unknown; reason is nonempty "
        "text; evidence is a nonempty list of references. "
        + EVIDENCE_PROTOCOL
        + "Return unknown when evidence is insufficient."
    ),
    "judge_review": (
        "Independently evaluate the untrusted answer against only the supplied "
        "task, gold and prohibition. The material's response_schema describes "
        "the original test. The current response must be exactly one JSON "
        "object containing only judgment, whose value contains exactly status, "
        "support, reason and evidence. status is pass/fail/unknown; support is "
        "supported/unsupported/unknown; reason is nonempty text; evidence is a "
        "nonempty list of references. "
        + EVIDENCE_PROTOCOL
        + "Return unknown when evidence is insufficient. You have not seen "
        "another judgment; do not claim to review it."
    ),
}


def diagnostic_operations(batch, *, prefix="diagnostic"):
    if len(batch["judge_tests"]) != 18:
        raise ValueError("eighteen_judge_tests_required")
    return [
        operation_for(
            batch,
            operation_id=f"{prefix}-{kind}-{index}",
            kind=kind,
            object_id=test["id"],
            instance_id=f"{prefix}-independent-{kind}-{index}",
        )
        for kind in ("judge_test", "judge_review")
        for index, test in enumerate(batch["judge_tests"])
    ]


def _context(authority, state):
    plan, binding, store, _ = authority._context(state)
    return (
        plan,
        binding,
        store,
        authority.persistence_call(
            state["job_id"], store.read, binding["batch"]["batch_id"]
        ),
    )


def _progress(authority, state):
    reference = state["phase_facts"].get("diagnostic_progress")
    return (
        authority.read_object(reference, job_id=state["job_id"])
        if reference
        else {"version": 1, "results": {}, "reviews": {}, "comparisons": {}}
    )


def _batch(authority, state):
    plan, _, _, batch = _context(authority, state)
    batch["judge_profile"] = effective_judge_profile(plan, batch["judge_profile"])
    progress = _progress(authority, state)
    batch["judge_test_results"] += [
        authority.read_object(ref, job_id=state["job_id"])["result"]
        for ref in progress["results"].values()
    ]
    batch["review_disputes"] += [
        value["review"]
        for ref in progress["comparisons"].values()
        if (value := authority.read_object(ref, job_id=state["job_id"]))["review"]
        is not None
    ]
    return batch


def _operations(plan, batch):
    tests = {t["id"] for t in batch["judge_tests"]}
    groups = {
        kind: [o for o in plan["operations"] if o["kind"] == kind] for kind in SYSTEM
    }
    if len(tests) != 18 or any(
        len(rows) != 18 or {r["object_id"] for r in rows} != tests
        for rows in groups.values()
    ):
        raise ValueError("complete_diagnostic_plan_required")
    return groups


def _request(batch, operation):
    test = next(t for t in batch["judge_tests"] if t["id"] == operation["object_id"])
    if operation["kind"] == "judge_review":
        result = next(
            r for r in batch["judge_test_results"] if r["test_id"] == test["id"]
        )
        inputs = blind_input(batch, target_for(batch, "judge_test", result["id"]))
    else:
        inputs = test_input(batch, test)
    return ModelRequest(
        ModelRef("deepseek", batch["judge_profile"]["model"]["model_id"]),
        (TextBlock(SYSTEM[operation["kind"]]),),
        (Message("user", (TextBlock(json.dumps(inputs, ensure_ascii=False)),)),),
        max_tokens=MODELS[group_for_model(batch["judge_profile"]["model"]["model_id"])][
            2
        ],
        thinking="disabled",
        response_format="json_object",
        conversation_id=operation["instance_id"],
    )


def validate_projection(authority, state, operation, payload):
    """Admission checks actual full wire; instance names never enter messages."""
    plan = authority.read_object(state["plan_ref"], job_id=state["job_id"])
    if (
        plan["execution_mode"] == "synthetic_capacity"
        or operation["kind"] not in SYSTEM
    ):
        return
    batch = _batch(authority, state)
    _operations(plan, batch)
    progress = _progress(authority, state)
    if operation["kind"] == "judge_review" and len(progress["results"]) != 18:
        raise ValueError("judge_results_incomplete")
    if payload != wire_payload(_request(batch, operation)):
        raise ValueError("diagnostic_projection_mismatch")


def _raw(authority, job_id, attempt):
    response = authority.read_object(attempt["response_ref"], job_id=job_id)
    outer = strict_json(response["raw"].encode(), limit=4 * 1024 * 1024)
    raw = outer["choices"][0]["message"].get("content")
    return raw if isinstance(raw, str) else ""


def seal_diagnostic(authority, job_id, operation_id, *, principal):
    """Seal actual settled raw before any original opinion is disclosed."""
    if principal != authority.controller:
        raise ValueError("diagnostic_controller_required")
    state = authority._verified(job_id)
    if state["phase"] != "diagnostic":
        raise ValueError("diagnostic_controller_required")
    plan, operation = authority._operation(state, operation_id)
    if (
        operation["kind"] not in SYSTEM
        or plan["execution_mode"] == "synthetic_capacity"
    ):
        raise ValueError("diagnostic_operation_required")
    batch = _batch(authority, state)
    _operations(plan, batch)
    progress = _progress(authority, state)
    collection = "results" if operation["kind"] == "judge_test" else "reviews"
    test_id = operation["object_id"]
    if test_id in progress[collection]:
        return authority.read_object(progress[collection][test_id], job_id=job_id)
    rows = [
        r
        for r in authority.persistence_call(
            job_id, authority.store.list_attempts, job_id
        )
        if r["operation_id"] == operation_id
    ]
    if len(rows) != 1 or not completed_response(rows[0]):
        raise ValueError("diagnostic_attempt_unresolved")
    row = rows[0]
    prepared = authority.read_object(row["prepared_ref"], job_id=job_id)
    validate_projection(authority, state, operation, prepared["payload"])
    inputs = json.loads(prepared["payload"]["messages"][-1]["content"])
    raw = _raw(authority, job_id, row)
    parsed, error = None, None
    try:
        parsed = (
            parse(raw, inputs)
            if collection == "results"
            else parse_review(raw, {"judgment": "unknown"})
        )
        if collection == "reviews":
            for ref in parsed["judgment"]["evidence"]:
                resolve_reference(ref, inputs["sources"])
    except (ValueError, TypeError, KeyError) as failure:
        error = str(failure) or type(failure).__name__
    test = next(t for t in batch["judge_tests"] if t["id"] == test_id)
    result = None
    if collection == "results":
        result = signed(
            {
                "version": 1,
                "kind": "constructed_judge_result",
                "test_id": test_id,
                "test_sha256": digest(test),
                "judge_profile_id": batch["judge_profile"]["id"],
                "semantic_rubric_id": batch["semantic_rubric"]["id"],
                "input": inputs,
                "input_sha256": digest(inputs),
                "raw": raw,
                "parsed": parsed,
                "error": "judge_check_invalid_output" if error else None,
                "producer": {
                    "instance_id": row["instance_id"],
                    "model": row["model"],
                    "reference": row["attempt_id"],
                },
                "started_at": row["reserved_at"],
                "completed_at": row["completed_at"],
            }
        )
    envelope = {
        "version": 1,
        "kind": operation["kind"],
        "operation_id": operation_id,
        "test_id": test_id,
        "instance_id": row["instance_id"],
        "attempt_id": row["attempt_id"],
        "prepared_ref": row["prepared_ref"],
        "response_ref": row["response_ref"],
        "payload_sha256": row["payload_sha256"],
        "input": inputs,
        "raw": raw,
        "parsed": parsed,
        "error": error,
        "result": result,
        "started_at": row["reserved_at"],
        "completed_at": row["completed_at"],
    }

    def seal(changed, now):
        envelope.update(
            sealed_at=now.isoformat(), sealed_revision=changed["revision"] + 1
        )
        progress[collection][test_id] = authority.persistence_call(
            job_id, authority.store.put_object, envelope
        )
        changed["phase_facts"]["diagnostic_progress"] = authority.persistence_call(
            job_id, authority.store.put_object, progress
        )
        return None, {
            "raw_seal_ref": progress[collection][test_id],
            "operation_id": operation_id,
        }

    authority._action(state, "DIAGNOSTIC_RAW_SEAL", seal)
    return deepcopy(envelope)


def compare_diagnostic(authority, job_id, test_id, *, principal):
    if principal != authority.controller:
        raise ValueError("diagnostic_controller_required")
    state = authority._verified(job_id)
    if state["phase"] != "diagnostic":
        raise ValueError("diagnostic_controller_required")
    progress = _progress(authority, state)
    if test_id in progress["comparisons"]:
        return authority.read_object(progress["comparisons"][test_id], job_id=job_id)
    if test_id not in progress["reviews"] or test_id not in progress["results"]:
        raise ValueError("blind_raw_not_sealed")
    sealed = authority.read_object(progress["reviews"][test_id], job_id=job_id)
    original = authority.read_object(progress["results"][test_id], job_id=job_id)
    if state["revision"] < sealed["sealed_revision"]:
        raise ValueError("blind_raw_not_sealed")
    batch = _batch(authority, state)
    review = None
    value = {
        "version": 1,
        "test_id": test_id,
        "blind_seal_ref": progress["reviews"][test_id],
        "original_seal_ref": progress["results"][test_id],
        "disclosed_after_revision": sealed["sealed_revision"],
        "review": None,
        "error": sealed["error"],
        "label_agrees": None,
        "citation_structure_valid": original["parsed"] is not None,
        "semantic_support": "unknown",
    }

    def compare(changed, now):
        nonlocal review
        if sealed["error"] is None:
            opinion = deepcopy(sealed["parsed"])
            # Equality and resolvable pointers cannot establish semantic entailment.
            # Retain a blind unsupported/unknown opinion without upgrading it.
            if opinion["judgment"]["support"] == "supported":
                opinion["judgment"]["support"] = "unknown"
                opinion["judgment"]["reason"] = (
                    "Local comparison verifies syntax and label equality only; "
                    "semantic support of the original citations is unverified."
                )
            review = make_review(
                batch,
                target_for(batch, "judge_test", original["result"]["id"]),
                reviewer={
                    "instance_id": sealed["instance_id"],
                    "model": batch["judge_profile"]["model"]["model_id"],
                    "reference": sealed["attempt_id"],
                },
                started_at=sealed["started_at"],
                completed_at=sealed["completed_at"],
                compared_at=now.isoformat(),
                output=sealed["raw"],
                comparison_output=json.dumps(opinion, ensure_ascii=False),
            )
            value.update(
                review=review,
                label_agrees=(
                    original["parsed"]["label"] == opinion["judgment"]["status"]
                )
                if original["parsed"]
                else None,
                semantic_support=opinion["judgment"]["support"],
            )
        value.update(
            compared_at=now.isoformat(), compared_revision=changed["revision"] + 1
        )
        progress["comparisons"][test_id] = authority.persistence_call(
            job_id, authority.store.put_object, value
        )
        changed["phase_facts"]["diagnostic_progress"] = authority.persistence_call(
            job_id, authority.store.put_object, progress
        )
        return None, {
            "comparison_ref": progress["comparisons"][test_id],
            "blind_seal_ref": progress["reviews"][test_id],
        }

    authority._action(state, "LOCAL_COMPARISON", compare)
    return value


def remaining_limits(state, plan):
    result = {
        "started_at": state["started_at"],
        "deadline": (
            instant(state["started_at"]) + timedelta(seconds=10800)
        ).isoformat(),
        "cap_units": state["cap_units"],
        "spent_units": state["spent_units"],
        "pending_units": state["pending_units"],
        "remaining_units": state["cap_units"]
        - state["spent_units"]
        - state["pending_units"],
        "counts": deepcopy(state["counts"]),
        "remaining_counts": {
            k: plan["limits"][k] - v for k, v in state["counts"].items()
        },
        "limits": deepcopy(plan["limits"]),
        "operation_counts": deepcopy(state["operation_counts"]),
        "finished_operations": list(state["finished_operations"]),
    }
    if plan.get("budget_policy") == "advisory_dispatch":
        result["budget_policy"] = "advisory_dispatch"
        result["amount_units_meaning"] = "local_planned_dispatch_only"
        result["final_bill_ceiling_proven"] = False
    if "input_policy" in plan:
        result["input_policy"] = deepcopy(plan["input_policy"])
    return result


def diagnostic_report(authority, state):
    plan, binding, store, original = _context(authority, state)
    batch, progress = _batch(authority, state), _progress(authority, state)
    rows, errors = [], []
    attempts = authority.persistence_call(
        state["job_id"], authority.store.list_attempts, state["job_id"]
    )
    for test in original["judge_tests"]:
        slot = {
            "test_id": test["id"],
            "expected": test["expected"],
            "failure_mode": test["failure_mode"],
        }
        for source, name in (
            ("results", "result"),
            ("reviews", "blind"),
            ("comparisons", "comparison"),
        ):
            reference = progress[source].get(test["id"])
            slot[name] = (
                authority.read_object(reference, job_id=state["job_id"])
                if reference
                else None
            )
            if not slot[name]:
                errors.append(f"{name}_missing:{test['id']}")
            elif slot[name].get("error"):
                errors.append(f"{name}_invalid:{test['id']}:{slot[name]['error']}")
        rows.append(slot)
    raw_disputes = disputes(batch)
    diagnostic_disputes = [
        d for d in raw_disputes if d["target"]["type"] == "judge_test"
    ]
    preserved = [d for d in disputes(original) if d["target"]["type"] != "judge_test"]
    complete = len(rows) == 18 and all(
        all(r[k] is not None for k in ("result", "blind", "comparison")) for r in rows
    )
    output_id = state["job_id"] + "-diagnostics"

    def optional_evidence():
        try:
            return store.read(output_id)
        except ValueError as failure:
            if str(failure) != "missing_or_incomplete_batch":
                raise
            return None  # The report can precede creation of its derived batch.

    saved = authority.persistence_call(state["job_id"], optional_evidence)
    evidence = None
    if saved is not None:
        expected = deepcopy(batch)
        expected.update(
            batch_id=output_id,
            authorization=None,
            parent={
                "batch_id": original["batch_id"],
                "sha256": digest(original),
                "relation": "regrade",
            },
        )
        if saved != expected:
            raise ValueError("diagnostic_evidence_changed")
        evidence = {
            "batch_id": output_id,
            "sha256": digest(saved),
            "path": str(store.root / output_id / "batch.json"),
        }
    blocks = [
        *errors,
        *("adjudication_required:" + d["id"] for d in diagnostic_disputes),
        "judge_quality_not_approved",
    ]
    if plan["execution_mode"] == "synthetic_replay":
        blocks.append("real_source_unverifiable")
    return signed(
        {
            "contract": CONTRACT,
            "version": 1,
            "job_id": state["job_id"],
            "plan_sha256": digest(plan),
            "binding_sha256": digest(binding),
            "execution_mode": plan["execution_mode"],
            "source_status": state["source_status"],
            **(
                {
                    "model_policy": plan["model_policy"],
                    "judge_profile_change": plan["judge_profile_change"],
                    "cross_model_independence": False,
                    "schema4_material_approval_inherited": False,
                }
                if plan.get("version") == 3
                else {}
            ),
            "simulation": original["simulation"],
            "original_parent": original["parent"],
            "slots": rows,
            "attempts": attempts,
            "stop_reason": None
            if state["stop_reason"] == "time_check_pending"
            else state["stop_reason"],
            "complete": complete,
            "errors": errors,
            "diagnostic_disputes": diagnostic_disputes,
            "preserved_disputes": preserved,
            "preserved_summary_ids": [s["id"] for s in original["summaries"]],
            "remaining": remaining_limits(state, plan),
            "evidence": evidence,
            "sealed_at": max(
                (r["comparison"]["compared_at"] for r in rows if r["comparison"]),
                default=state["started_at"],
            ),
            "judge_quality": axis(blockers=blocks),
            "product_sample_count": 0,
            "expected_results": 18,
            "expected_reviews": 18,
            "admission_threshold": None,
            "online_executable": False,
            "release_eligible": False,
        }
    )


def _adjudications(authority, plan, batch, report, refs, now):
    required = {d["id"]: d for d in report["diagnostic_disputes"]}
    decisions = {}
    for reference in refs:
        event = authority.runtime.decision_source.read_decision(reference)
        validate_event(event)
        dispute = required.get(event["object_id"])
        kind = (
            "synthetic_user_decision"
            if batch["simulation"] or plan["execution_mode"] == "synthetic_replay"
            else "user_decision"
        )
        if (
            dispute is None
            or event["kind"] != kind
            or event["source_ref"] != reference
            or event["subject"] != authority.subject
            or any(event[k] != v for k, v in dispute_request(batch, dispute).items())
            or not instant(dispute["at"]) <= instant(event["at"]) <= now
            or event["object_id"] in decisions
        ):
            raise ValueError("checkpoint_adjudication_scope_mismatch")
        target = next(
            row["result"]["result"]
            for row in report["slots"]
            if row["result"]
            and row["result"]["result"]["id"] == dispute["target"]["id"]
        )
        for citation in event["evidence"]:
            if resolve_reference(citation, target["input"]["sources"]) in (
                None,
                "",
                [],
                {},
            ):
                raise ValueError("decision_evidence_missing")
        decisions[event["object_id"]] = event
    rows = [
        {"dispute": d, "decision": decisions.get(key)} for key, d in required.items()
    ]
    return rows


def checkpoint_context(authority, state, adjudication_refs=(), *, now=None):
    now = authority.now() if now is None else now
    if state["phase"] != "checkpoint":
        raise ValueError("checkpoint_not_waiting")
    plan, binding, _, batch = _context(authority, state)
    report_ref = state["phase_facts"].get("diagnostic")
    if not report_ref:
        raise ValueError("diagnostic_summary_missing")
    report = authority.read_object(report_ref, job_id=state["job_id"])
    if report["remaining"] != remaining_limits(state, plan):
        raise ValueError("checkpoint_budget_changed")
    adjudications = _adjudications(
        authority, plan, batch, report, adjudication_refs, now
    )
    run = authority.runtime.decision_source.read_decision(state["run_source_ref"])
    request = checkpoint_request(
        binding,
        job_id=state["job_id"],
        run_decision=run,
        judge_summary_sha256=digest(report),
        disputes_sha256=digest(adjudications),
        remaining_budget_sha256=digest(report["remaining"]),
    )
    failures = [
        "judge_check_violation:" + r["dispute"]["id"]
        for r in adjudications
        if r["decision"] and r["decision"]["decision"] == "confirmed_violation"
    ]
    confirmed_targets = {
        r["dispute"]["target"]["id"]
        for r in adjudications
        if r["decision"] and r["decision"]["decision"] == "confirmed_violation"
    }
    failures.extend(
        "judge_missed_prohibition:" + row["test_id"]
        for row in report["slots"]
        if row["failure_mode"] == "missed_prohibition"
        and row["result"]
        and row["result"]["parsed"]
        and row["result"]["parsed"]["label"] == "pass"
        and row["result"]["result"]["id"] in confirmed_targets
    )
    blockers = [
        *report["errors"],
        *(
            "adjudication_required:" + r["dispute"]["id"]
            for r in adjudications
            if r["decision"] is None or r["decision"]["decision"] != "dismissed"
        ),
    ]
    return {
        "request": request,
        "report_ref": report_ref,
        "adjudications": adjudications,
        "remaining": report["remaining"],
        "failures": failures,
        "blockers": blockers,
        "judge_quality": axis(failures, [*blockers, "judge_quality_not_approved"]),
    }


def verify_continuation(authority, state, continuation, now, *, entering=False):
    plan, binding, evidence, reader = authority._context(state)
    if continuation.get("contract") != CONTINUATION or continuation.get("version") != 1:
        raise ValueError("checkpoint_evidence_required")
    if set(continuation) != {
        "contract",
        "version",
        "request",
        "report_ref",
        "source_ref",
        "adjudication_refs",
        "adjudications",
        "decision",
    }:
        raise ValueError("checkpoint_scope_mismatch")
    frozen = authority.read_object(continuation["report_ref"], job_id=state["job_id"])
    if state["phase_facts"].get("diagnostic") != continuation["report_ref"]:
        raise ValueError("checkpoint_summary_mismatch")
    context_state = deepcopy(state)
    context_state["phase"] = "checkpoint"
    # Reconstruct the original exact remaining snapshot, never a new allowance.
    if not entering:
        remaining = frozen["remaining"]
        if (
            state["started_at"] != remaining["started_at"]
            or state["cap_units"] != remaining["cap_units"]
            or any(state["counts"][k] < v for k, v in remaining["counts"].items())
        ):
            raise ValueError("checkpoint_budget_changed")
        for key in (
            "spent_units",
            "pending_units",
            "counts",
            "operation_counts",
            "finished_operations",
        ):
            context_state[key] = deepcopy(remaining[key])
    context = checkpoint_context(
        authority, context_state, continuation["adjudication_refs"], now=now
    )
    if (
        context["request"] != continuation["request"]
        or context["adjudications"] != continuation["adjudications"]
    ):
        raise ValueError("checkpoint_scope_mismatch")
    if context["failures"] or context["blockers"]:
        raise ValueError("checkpoint_diagnostic_blocked")
    event = authority._verify_source(
        plan,
        binding,
        evidence,
        reader,
        context["request"],
        continuation["source_ref"],
        now,
    )
    if event != continuation["decision"]:
        raise ValueError("checkpoint_decision_changed")
    run = authority.runtime.decision_source.read_decision(state["run_source_ref"])
    lower = max(
        [
            instant(frozen["sealed_at"]),
            instant(run["at"]),
            *[instant(r["decision"]["at"]) for r in context["adjudications"]],
        ]
    )
    if instant(event["at"]) < lower or authority._time_reason(state, now):
        raise ValueError("checkpoint_decision_expired_or_premature")
    return context


def validate_phase(authority, state, next_phase, evidence, now):
    plan = authority.read_object(state["plan_ref"], job_id=state["job_id"])
    if plan["execution_mode"] == "synthetic_capacity":
        return
    if next_phase == "checkpoint":
        _, _, _, batch = _context(authority, state)
        groups = _operations(plan, batch)
        required = {o["id"] for rows in groups.values() for o in rows}
        expected = diagnostic_report(authority, state)
        if (
            evidence != expected
            or not expected["complete"]
            or expected["evidence"] is None
            or not required <= set(state["finished_operations"])
            or state["counts"]
            != {
                "flash": 36
                if model_group(plan, groups["judge_test"][0]) == "flash"
                else 0,
                "pro": 36 if model_group(plan, groups["judge_test"][0]) == "pro" else 0,
                "total": 36,
            }
            or any(state["operation_counts"].get(op) != 1 for op in required)
        ):
            raise ValueError("complete_diagnostic_evidence_required")
    elif next_phase == "product":
        verify_continuation(authority, state, evidence, now, entering=True)


class DiagnosticDriver:
    """Trusted public controller; retains the original job, grant and ledger."""

    def __init__(self, authority, job_id, *, principal):
        if principal != authority.controller:
            raise ValueError("controller_identity_required")
        self.authority, self.job_id, self.principal = authority, job_id, principal

    def report(self, *, adjudication_refs=()):
        state = self.authority._verified(self.job_id)
        if "diagnostic" in state["phase_facts"]:
            report = self.authority.read_object(
                state["phase_facts"]["diagnostic"], job_id=self.job_id
            )
        else:
            report = diagnostic_report(self.authority, state)
        if adjudication_refs:
            report = {
                **report,
                "checkpoint_assessment": self.checkpoint(
                    adjudication_refs=adjudication_refs
                ),
            }
        return report

    def run(self):
        auth = self.authority
        state = auth._verified(self.job_id)
        if state["phase"] != "diagnostic":
            return self.report()
        plan, _, _, batch = _context(auth, state)
        groups = _operations(plan, batch)
        if plan["execution_mode"] == "synthetic_capacity":
            raise ValueError("capacity_fixture_is_not_diagnostic_evidence")
        for kind in SYSTEM:
            for operation in groups[kind]:
                state = auth._verified(self.job_id)
                if state["state"] != "ACTIVE":
                    return self.report()
                progress = _progress(auth, state)
                collection = "results" if kind == "judge_test" else "reviews"
                if operation["object_id"] not in progress[collection]:
                    if not state["operation_counts"].get(operation["id"]):
                        request = _request(_batch(auth, state), operation)
                        prepared = auth.prepare_request(
                            self.job_id,
                            operation["id"],
                            request,
                            principal=self.principal,
                            data_scope_sha256=operation["data_scope_sha256"],
                        )
                        auth.client(self.job_id, operation["id"], prepared).respond(
                            request
                        )
                    if auth.status(self.job_id)["state"]["state"] != "ACTIVE":
                        return self.report()
                    seal_diagnostic(
                        auth, self.job_id, operation["id"], principal=self.principal
                    )
                if kind == "judge_review":
                    compare_diagnostic(
                        auth,
                        self.job_id,
                        operation["object_id"],
                        principal=self.principal,
                    )
                if (
                    operation["id"]
                    not in auth.status(self.job_id)["state"]["finished_operations"]
                ):
                    auth.finish(self.job_id, operation["id"])
        state = auth._verified(self.job_id)
        _, _, store, original = _context(auth, state)
        batch = _batch(auth, state)
        output_id = self.job_id + "-diagnostics"
        if not (store.root / output_id).exists():
            auth.persistence_call(
                self.job_id,
                store.revise,
                original["batch_id"],
                output_id,
                configuration={
                    "judge_profile": batch["judge_profile"],
                    "judge_test_results": batch["judge_test_results"],
                },
                reviews=batch["review_disputes"][len(original["review_disputes"]) :],
            )
        report = diagnostic_report(auth, state)
        auth.phase_event(
            self.job_id,
            principal=self.principal,
            expected_phase="diagnostic",
            next_phase="checkpoint",
            evidence=report,
        )
        return self.report()

    def checkpoint(self, *, adjudication_refs=()):
        return checkpoint_context(
            self.authority, self.authority._verified(self.job_id), adjudication_refs
        )

    def continue_with(self, source_ref, *, adjudication_refs=()):
        context = self.checkpoint(adjudication_refs=adjudication_refs)
        if context["failures"] or context["blockers"]:
            raise ValueError("checkpoint_diagnostic_blocked")
        evidence = {
            "contract": CONTINUATION,
            "version": 1,
            "request": context["request"],
            "report_ref": context["report_ref"],
            "source_ref": source_ref,
            "adjudication_refs": list(adjudication_refs),
            "adjudications": context["adjudications"],
            "decision": self.authority.runtime.decision_source.read_decision(
                source_ref
            ),
        }
        state = self.authority._verified(self.job_id)
        verify_continuation(
            self.authority, state, evidence, self.authority.now(), entering=True
        )
        return self.authority.phase_event(
            self.job_id,
            principal=self.principal,
            expected_phase="checkpoint",
            next_phase="product",
            evidence=evidence,
        )
