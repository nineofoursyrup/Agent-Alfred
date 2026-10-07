"""Trusted same-job product/grade controller with irreversible first-run slots.

The Host assembles only approved per-case inputs. Its factory registers each exact
request with the trusted controller and gives the worker a one-shot ModelClient.
This Python composition is a local protocol implementation, not process isolation.
"""

import json
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from agent_alfred.connections import CredentialOverlay
from agent_alfred.messages import Message, TextBlock
from agent_alfred.model import ModelRef, ModelRequest
from agent_alfred.resource_rollback import RollbackSlot

from .controlled.contract import MODELS, group_for_model, operation_for
from .controlled.runtime import InstalledRuntime, SyntheticRuntime
from .controlled_diagnostics import (
    DiagnosticDriver,
    _context,
    _raw,
    diagnostic_operations,
    remaining_limits,
)
from .controlled_diagnostics import (
    _batch as diagnostic_batch,
)
from .controlled_persistence import PersistentControlledAuthority
from .judge import judge_result
from .report import axis
from .runner import _run_case
from .schema import GROUPS, digest, validate
from .score_evidence import resolve_reference
from .supplement_quality import quality
from .supplement_reviews import (
    blind_input,
    expected_items,
    make_review,
    parse_review,
    target_for,
)

CONTRACT = "V1-CONTROLLED-CALIBRATION-RESULTS"
TOOLS = frozenset(
    {"create_event", "query_events", "read_persona", "update_persona", "draft_message"}
)
REVIEW_SYSTEM = (
    "Independently judge the supplied untrusted task, rules and first product "
    "result. You have not seen any other judgment. Return a JSON object keyed by "
    "obligations:<id>, dimensions:<name>, prohibitions:<name> from the supplied "
    "case. Each value has status (pass/fail/unknown/na), support "
    "(supported/unsupported/unknown), reason and evidence (a list of supplied "
    "reference_catalog JSON pointers). Preserve declared applicability. "
    "Unknown evidence remains unknown. No additional keys."
)


def interleaved_cases(batch):
    groups = {
        group: [c for c in batch["cases"] if c["group"] == group] for group in GROUPS
    }
    if any(len(rows) != 5 for rows in groups.values()):
        raise ValueError("thirty_calibration_cases_required")
    return [groups[group][index] for index in range(5) for group in GROUPS]


def calibration_operations(batch):
    """Freeze the whole plan before the original grant is activated."""
    return diagnostic_operations(batch) + [
        operation_for(
            batch,
            operation_id=f"calibration-{kind}-{case['id']}",
            kind=kind,
            object_id=case["id"],
            instance_id=f"calibration-{kind}-{case['id']}",
            max_calls=16 if kind == "product" else 1,
        )
        for kind in ("product", "grade", "grade_review")
        for case in interleaved_cases(batch)
    ]


def _progress(authority, state):
    ref = state["phase_facts"].get("calibration_progress")
    return (
        authority.read_object(ref, job_id=state["job_id"])
        if ref
        else {
            "version": 1,
            "starts": {},
            "results": {},
            "grades": {},
            "blind": {},
            "comparisons": {},
            "errors": {},
        }
    )


def _operation(plan, kind, case_id):
    rows = [
        o for o in plan["operations"] if o["kind"] == kind and o["case_id"] == case_id
    ]
    if len(rows) != 1:
        raise ValueError("complete_calibration_plan_required")
    return rows[0]


def validate_phase(authority, state, next_phase, evidence):
    """Authority-side completeness gate for the final two irreversible phases."""
    plan = authority.read_object(state["plan_ref"], job_id=state["job_id"])
    if plan["execution_mode"] == "synthetic_capacity" or next_phase not in (
        "grading",
        "closed",
    ):
        return
    _, _, _, original = _context(authority, state)
    cases = interleaved_cases(original)
    progress = _progress(authority, state)
    key = "first_results_ref" if next_phase == "grading" else "results_ref"
    if evidence != {key: state["phase_facts"].get("calibration_progress")}:
        raise ValueError("calibration_phase_evidence_mismatch")
    if set(progress["results"]) != {case["id"] for case in cases}:
        raise ValueError("first_product_results_incomplete")
    required = []
    for case in cases:
        cid = case["id"]
        required.append(_operation(plan, "product", cid)["id"])
        if next_phase == "closed":
            required += [
                _operation(plan, kind, cid)["id"] for kind in ("grade", "grade_review")
            ]
            result = authority.read_object(
                progress["results"][cid], job_id=state["job_id"]
            )["result"]
            if result["output"] is not None and cid not in progress["grades"]:
                raise ValueError("original_grade_slot_incomplete")
            if cid in progress["grades"]:
                grade = authority.read_object(
                    progress["grades"][cid], job_id=state["job_id"]
                )["grade"]
                if grade["status"] == "scored" and (
                    cid not in progress["blind"] or cid not in progress["comparisons"]
                ):
                    raise ValueError("blind_grade_slot_incomplete")
    if not set(required) <= set(state["finished_operations"]):
        raise ValueError("calibration_operations_incomplete")


def _validate_product_profile(batch):
    if (
        batch["phase"] != "calibration"
        or len(batch["profiles"]) != 1
        or batch["results"]
    ):
        raise ValueError("fresh_calibration_materials_required")
    profile = batch["profiles"][0]
    expected = {
        "max_steps": 12,
        "input_character_limit": 64000,
        "gate_input_character_limit": 64000,
        "working_memory_rounds": 8,
        "per_store_limit": 8,
        "per_store_character_budget": 8000,
        "max_tokens": 8192,
        "per_attempt_timeout_s": 120.0,
        "overall_deadline_s": 900.0,
        "gate_model_budget_s": 120.0,
        "stream": False,
        "stream_fallback": False,
    }
    if any(profile["parameters"].get(k) != v for k, v in expected.items()):
        raise ValueError("approved_product_limits_required")
    if not set(profile["local_tool_allowlist"]) <= TOOLS:
        raise ValueError("approved_local_tools_required")
    for model in profile["product_models"]:
        if any(
            model.get(k) != v
            for k, v in {
                "endpoint_id": "deepseek",
                "model_id": "deepseek-flash",
                "wire_style": "openai",
                "thinking": "disabled",
            }.items()
        ):
            raise ValueError("controlled_flash_assignment_required")
    for case in interleaved_cases(batch):
        if case["operation"] not in ("chat", "aggregate") or not set(
            case["setup"]["local_tool_allowlist"]
        ) <= set(profile["local_tool_allowlist"]):
            raise ValueError("approved_local_tools_required")


class _ControllerFactory:
    """Trusted projection adapter. Every Host request keeps its callbacks intact."""

    def __init__(self, driver, operation):
        self.driver, self.operation = driver, operation

    def create(self, snapshot):
        if snapshot.endpoint_id != "deepseek" or snapshot.model_id != "deepseek-flash":
            raise ValueError("controlled_flash_assignment_required")
        return self

    def respond(self, request, *, events=None, deadline=None):
        # Host helper requests do not themselves choose wire thinking mode. Bind
        # the approved mode while retaining every actual message/tool/callback.
        request = replace(request, thinking="disabled")
        auth, op = self.driver.authority, self.operation
        prepared = auth.prepare_request(
            self.driver.job_id,
            op["id"],
            request,
            principal=self.driver.principal,
            data_scope_sha256=op["data_scope_sha256"],
        )
        return auth.client(self.driver.job_id, op["id"], prepared).respond(
            request, events=events, deadline=deadline
        )

    def close(self):
        pass  # Each controlled Attempt owns and closes its transport.


class CalibrationDriver:
    """Continue the exact diagnostic checkpoint; never create a grant or ledger."""

    def __init__(self, authority, job_id, *, principal, workspace):
        if not isinstance(authority, PersistentControlledAuthority):
            raise ValueError("persistent_calibration_authority_required")
        if principal != authority.controller:
            raise ValueError("controller_identity_required")
        self.authority, self.job_id, self.principal = authority, job_id, principal
        self.workspace = Path(workspace)
        self._cleanup = RollbackSlot()
        state = authority._verified(job_id)
        plan, _, _, batch = _context(authority, state)
        if plan["execution_mode"] == "synthetic_capacity":
            raise ValueError("capacity_fixture_is_not_calibration_evidence")
        _validate_product_profile(batch)
        for case in interleaved_cases(batch):
            for kind in ("product", "grade", "grade_review"):
                _operation(plan, kind, case["id"])

    def retry_cleanup(self):
        """Close retained local resources without repeating a slot or dispatch."""
        return self._cleanup.retry()

    def _io(self, function, *args, **kwargs):
        return self.authority.persistence_call(self.job_id, function, *args, **kwargs)

    def _read(self, reference):
        return self.authority.read_object(reference, job_id=self.job_id)

    def _write(self, kind, collection, key, value, *, admission=False):
        auth, state = self.authority, self.authority._verified(self.job_id)
        progress = _progress(auth, state)
        if key in progress[collection]:
            return self._read(progress[collection][key])
        sealed = deepcopy(value)

        def mutation(changed, now):
            sealed.update(
                sealed_at=now.isoformat(), sealed_revision=changed["revision"] + 1
            )
            ref = self._io(auth.store.put_object, sealed)
            progress[collection][key] = ref
            changed["phase_facts"]["calibration_progress"] = self._io(
                auth.store.put_object, progress
            )
            return None, {"record_ref": ref, "slot": key, "collection": collection}

        if admission:
            auth._action(state, kind, mutation)
        else:
            # Recording already-observed output is allowed after an infrastructure
            # stop. It changes no permission, budget, clock or stopped state.
            changed = deepcopy(state)
            _, detail = mutation(changed, auth.now())
            auth._commit(changed, kind, previous=state, detail=detail)
        return sealed

    def _start(self, kind, case_id):
        state = self.authority._verified(self.job_id)
        if kind + ":" + case_id in _progress(self.authority, state)["starts"]:
            self.authority.revoke(
                self.job_id,
                principal=self.principal,
                reason="first_slot_unrecoverable:" + kind + ":" + case_id,
            )
            return False
        self._write(
            "CALIBRATION_SLOT_START",
            "starts",
            kind + ":" + case_id,
            {"kind": kind, "case_id": case_id},
            admission=True,
        )
        return True

    def _attempts(self, operation):
        return [
            {
                "attempt_id": row["attempt_id"],
                "sha256": digest(row),
                "prepared_ref": row["prepared_ref"],
                "response_ref": row.get("response_ref"),
            }
            for row in self._io(self.authority.store.list_attempts, self.job_id)
            if row["operation_id"] == operation["id"]
        ]

    def _finish(self, operation):
        state = self.authority._verified(self.job_id)
        if (
            state["state"] == "ACTIVE"
            and operation["id"] not in state["finished_operations"]
        ):
            self.authority.finish(self.job_id, operation["id"])

    def _stage(self, function, case, plan, *args):
        try:
            function(case, plan, *args)
        except BaseException as error:
            self._cleanup.capture_failure(error)
            if not isinstance(error, Exception):
                raise
            state = self.authority._verified(self.job_id)
            self._write(
                "CALIBRATION_SLOT_ERROR",
                "errors",
                state["phase"] + ":" + case["id"],
                {"type": type(error).__name__, "error": str(error)},
            )
            self.authority.revoke(
                self.job_id,
                principal=self.principal,
                reason="calibration_infrastructure_failure",
            )

    def _batch(self):
        auth, state = self.authority, self.authority._verified(self.job_id)
        plan, _, store, original = _context(auth, state)
        batch = diagnostic_batch(auth, state)
        parent_id = self.job_id + "-diagnostics"
        parent = self._io(store.read, parent_id)
        batch.update(
            batch_id=self.job_id + "-first-results",
            parent={
                "batch_id": parent_id,
                "sha256": digest(parent),
                "relation": "retry",
            },
            authorization=None,
            budget_started_at=state["started_at"],
        )
        # 'retry' is the existing schema4 new-sampling child relation. This
        # controller admits only empty material parents and never resamples.
        progress = _progress(auth, state)
        batch["results"] = [
            self._read(ref)["result"] for ref in progress["results"].values()
        ]
        batch["grades"] = [
            self._read(ref)["grade"] for ref in progress["grades"].values()
        ]
        batch["review_disputes"] += [
            review
            for ref in progress["comparisons"].values()
            if (review := self._read(ref)["review"]) is not None
        ]
        batch["stop_reason"] = state["stop_reason"]
        return batch

    def _product(self, case, plan, original):
        auth, cid = self.authority, case["id"]
        op = _operation(plan, "product", cid)
        state = auth._verified(self.job_id)
        if cid in _progress(auth, state)["results"]:
            self._finish(op)
            return
        if not self._start("product", cid):
            return
        try:
            # The installed local adapter owns the native launch and trusted
            # projection bridge. No controller object enters its worker.
            if type(auth.runtime) not in (SyntheticRuntime, InstalledRuntime):
                from .controlled.local_runner import run_local_case
                from .controlled.local_runtime import LocalInstalledRuntime

                if type(auth.runtime) is not LocalInstalledRuntime:
                    raise ValueError("local_installed_runtime_required")
                record = run_local_case(self, case, plan, original)
            else:
                record = _run_case(
                    original,
                    case,
                    self.workspace / self.job_id,
                    factory=_ControllerFactory(self, op),
                    credentials=CredentialOverlay({}, None),
                    synthetic_replay=type(auth.runtime) is SyntheticRuntime,
                    capture_all=True,
                    clock=auth.now,
                )
            record["batch_id"] = self.job_id + "-first-results"
            record["evidence"]["controlled_execution"] = {
                "contract": CONTRACT,
                "version": 1,
                "job_id": self.job_id,
                "operation_id": op["id"],
                "attempts": self._attempts(op),
                "plan_sha256": digest(plan),
                "runtime_candidate_id": plan["candidate_id"],
            }
            self._write(
                "PRODUCT_FIRST_RESULT",
                "results",
                cid,
                {"case_id": cid, "result": record, "attempts": self._attempts(op)},
            )
        except BaseException as error:
            # Retain rollback handles before recording or revocation can fail,
            # and before _stage converts an ordinary error into report data.
            self._cleanup.capture_failure(error)
            self._write(
                "CALIBRATION_SLOT_ERROR",
                "errors",
                "product:" + cid,
                {
                    "type": type(error).__name__,
                    "error": str(error),
                    "attempts": self._attempts(op),
                },
            )
            auth.revoke(
                self.job_id,
                principal=self.principal,
                reason="product_infrastructure_failure",
            )
            raise
        self._finish(op)

    def _grade(self, case, plan):
        auth, cid = self.authority, case["id"]
        op = _operation(plan, "grade", cid)
        progress = _progress(auth, auth._verified(self.job_id))
        if cid in progress["grades"]:
            self._finish(op)
            return
        batch = self._batch()
        result = next((r for r in batch["results"] if r["case_id"] == cid), None)
        if result is None or result["output"] is None:
            return
        if not self._start("grade", cid):
            return
        factory = _ControllerFactory(self, op)
        grade = judge_result(
            batch,
            case,
            result,
            factory,
            producer={
                "instance_id": op["instance_id"],
                "model": batch["judge_profile"]["model"]["model_id"],
                "reference": op["id"],
            },
            clock=auth.now,
            max_output_tokens=MODELS[
                group_for_model(batch["judge_profile"]["model"]["model_id"])
            ][2],
            conversation_id=op["instance_id"],
        )
        self._write(
            "GRADE_FIRST_RESULT",
            "grades",
            cid,
            {"case_id": cid, "grade": grade, "attempts": self._attempts(op)},
        )
        if not self._attempts(op):
            auth.revoke(
                self.job_id,
                principal=self.principal,
                reason="grade_infrastructure_failure",
            )
        self._finish(op)

    def _review(self, case, plan):
        auth, cid = self.authority, case["id"]
        op = _operation(plan, "grade_review", cid)
        state = auth._verified(self.job_id)
        progress = _progress(auth, state)
        batch = self._batch()
        original = (
            self._read(progress["grades"][cid])["grade"]
            if cid in progress["grades"]
            else None
        )
        if original is None or original["status"] != "scored":
            return
        target = target_for(batch, "grade", original["id"])
        if cid not in progress["blind"]:
            if not self._start("grade_review", cid):
                return
            inputs = blind_input(batch, target)
            request = ModelRequest(
                ModelRef("deepseek", batch["judge_profile"]["model"]["model_id"]),
                (TextBlock(REVIEW_SYSTEM),),
                (
                    Message(
                        "user", (TextBlock(json.dumps(inputs, ensure_ascii=False)),)
                    ),
                ),
                max_tokens=MODELS[
                    group_for_model(batch["judge_profile"]["model"]["model_id"])
                ][2],
                thinking="disabled",
                response_format="json_object",
                conversation_id=op["instance_id"],
            )
            _ControllerFactory(self, op).respond(request)
            rows = [
                r
                for r in self._io(auth.store.list_attempts, self.job_id)
                if r["operation_id"] == op["id"]
            ]
            if len(rows) == 1 and rows[0]["send_state"] == "RESPONSE_VERIFIED":
                from .controlled_execution import completed_response

                complete = completed_response(rows[0])
            else:
                complete = len(rows) == 1 and rows[0]["send_state"] == "SETTLED"
            if len(rows) != 1 or not complete or rows[0]["outcome"] != "completed":
                return
            row = rows[0]
            raw, parsed, error = _raw(auth, self.job_id, row), None, None
            try:
                # Coverage is declared by material, never by the original labels.
                parsed = parse_review(raw, expected_items(batch, target))
                for item in parsed.values():
                    for reference in item["evidence"]:
                        resolve_reference(reference, inputs["sources"])
            except (ValueError, TypeError, KeyError) as failure:
                error = str(failure)
            self._write(
                "GRADE_BLIND_RAW_SEAL",
                "blind",
                cid,
                {
                    "case_id": cid,
                    "input": inputs,
                    "raw": raw,
                    "parsed": parsed,
                    "error": error,
                    "instance_id": op["instance_id"],
                    "started_at": row["reserved_at"],
                    "completed_at": row["completed_at"],
                    "attempts": self._attempts(op),
                },
            )
        self._compare(case, target)
        self._finish(op)

    def _compare(self, case, target):
        auth, cid = self.authority, case["id"]
        progress = _progress(auth, auth._verified(self.job_id))
        if cid in progress["comparisons"]:
            return
        sealed = self._read(progress["blind"][cid])
        review, agreement = None, None
        if sealed["error"] is None:
            opinion = deepcopy(sealed["parsed"])
            for item in opinion.values():
                if item["support"] == "supported":
                    item["support"] = "unknown"
                    item["reason"] = (
                        "Local syntax and label comparison cannot prove "
                        "semantic support."
                    )
            batch = self._batch()
            review = make_review(
                batch,
                target,
                reviewer={
                    "instance_id": sealed["instance_id"],
                    "model": batch["judge_profile"]["model"]["model_id"],
                    "reference": sealed["attempts"][0]["attempt_id"],
                },
                started_at=sealed["started_at"],
                completed_at=sealed["completed_at"],
                compared_at=auth.now().isoformat(),
                output=sealed["raw"],
                comparison_output=json.dumps(opinion, ensure_ascii=False),
            )
            agreement = {
                key: value["status"] == expected_items(batch, target)[key]
                for key, value in opinion.items()
            }
        self._write(
            "PRODUCT_LOCAL_COMPARISON",
            "comparisons",
            cid,
            {
                "case_id": cid,
                "blind_seal_ref": progress["blind"][cid],
                "disclosed_after_revision": sealed["sealed_revision"],
                "original_seal_ref": progress["grades"][cid],
                "review": review,
                "label_agrees": agreement,
                "semantic_support": "unknown",
                "error": sealed["error"],
            },
        )

    def run(self, *, stop_after_cases=None):
        """Run first outputs once. Optional fixture stop never renews permission."""
        auth = self.authority
        state = auth._verified(self.job_id)
        if stop_after_cases is not None and type(auth.runtime) is not SyntheticRuntime:
            raise ValueError("synthetic_stop_injection_only")
        if state["phase"] in ("diagnostic", "checkpoint"):
            raise ValueError("diagnostic_checkpoint_required")
        plan, _, _, original = _context(auth, state)
        cases = interleaved_cases(original)
        if state["state"] == "ACTIVE" and state["phase"] == "product":
            for case in cases:
                self._stage(self._product, case, plan, original)
                state = auth._verified(self.job_id)
                if (
                    stop_after_cases is not None
                    and len(_progress(auth, state)["results"]) >= stop_after_cases
                ):
                    auth.revoke(
                        self.job_id,
                        principal=self.principal,
                        reason="synthetic_partial_stop",
                    )
                if auth.status(self.job_id)["state"]["state"] != "ACTIVE":
                    break
            state = auth._verified(self.job_id)
            if state["state"] == "ACTIVE":
                auth.phase_event(
                    self.job_id,
                    principal=self.principal,
                    expected_phase="product",
                    next_phase="grading",
                    evidence={
                        "first_results_ref": state["phase_facts"][
                            "calibration_progress"
                        ]
                    },
                )
        state = auth._verified(self.job_id)
        if state["state"] == "ACTIVE" and state["phase"] == "grading":
            for kind in ("grade", "grade_review"):
                for case in cases:
                    self._stage(
                        self._grade if kind == "grade" else self._review, case, plan
                    )
                    if auth.status(self.job_id)["state"]["state"] != "ACTIVE":
                        break
                if auth.status(self.job_id)["state"]["state"] != "ACTIVE":
                    break
            state = auth._verified(self.job_id)
            if state["state"] == "ACTIVE":
                # Empty-output/invalid-grade slots are named gaps, never purchases
                # of replacement output. Close their unused signed operations too.
                for op in plan["operations"]:
                    if op["kind"] in ("grade", "grade_review"):
                        self._finish(op)
                auth.phase_event(
                    self.job_id,
                    principal=self.principal,
                    expected_phase="grading",
                    next_phase="closed",
                    evidence={
                        "results_ref": state["phase_facts"]["calibration_progress"]
                    },
                )
        self.publish()
        return self.report()

    def publish(self):
        """Write only a new, immutable derivative in the received EvidenceStore."""
        state = self.authority._verified(self.job_id)
        _, _, store, _ = _context(self.authority, state)
        batch = validate(self._batch())
        path = store.root / batch["batch_id"]
        if path.exists():
            if self._io(store.read, batch["batch_id"]) != batch:
                raise ValueError("first_evidence_already_sealed")
        else:
            self._io(store.import_batch, batch)
        return {
            "batch_id": batch["batch_id"],
            "sha256": digest(batch),
            "path": str(path / "batch.json"),
        }

    def report(self):
        auth, state = self.authority, self.authority._verified(self.job_id)
        plan, binding, _, original = _context(auth, state)
        batch, progress = self._batch(), _progress(auth, state)
        assessed = quality(batch, auth.now(), auth.runtime.decision_source)
        slots, gaps = [], []
        for case in interleaved_cases(original):
            slot = {
                "case_id": case["id"],
                "group": case["group"],
                "scene": case["scene"],
            }
            for name in ("results", "grades", "blind", "comparisons"):
                ref = progress[name].get(case["id"])
                slot[name] = self._read(ref) if ref else None
                if ref is None:
                    gaps.append(name + "_missing:" + case["id"])
                elif slot[name].get("error"):
                    gaps.append(name + "_invalid:" + case["id"])
                elif name == "grades" and slot[name]["grade"]["status"] != "scored":
                    gaps.append("grades_invalid:" + case["id"])
            slot["started"] = "product:" + case["id"] in progress["starts"]
            slot["run"] = (
                {
                    "outcome": slot["results"]["result"]["outcome"],
                    "recording_state": slot["results"]["result"]["evidence"].get(
                        "recording_state"
                    ),
                }
                if slot["results"]
                else None
            )
            slots.append(slot)
        diagnostics = DiagnosticDriver(
            auth, self.job_id, principal=self.principal
        ).report()
        blind_slots = [
            {
                "kind": "judge_test",
                "id": s["test_id"],
                "present": s["blind"] is not None,
                "valid": s["blind"] is not None and s["blind"]["error"] is None,
            }
            for s in diagnostics["slots"]
        ]
        blind_slots += [
            {
                "kind": "grade",
                "id": s["case_id"],
                "present": s["blind"] is not None,
                "valid": s["blind"] is not None and s["blind"]["error"] is None,
            }
            for s in slots
        ]
        report = {
            "contract": CONTRACT,
            "version": 1,
            "job_id": self.job_id,
            "execution_mode": plan["execution_mode"],
            "simulation": original["simulation"],
            "original_parent": original["parent"],
            "binding_sha256": digest(binding),
            "candidate_change": plan["candidate_change"],
            "manifest": original["manifest"],
            "distribution": {
                g: dict(
                    Counter(c["scene"] for c in original["cases"] if c["group"] == g)
                )
                for g in GROUPS
            },
            "product_denominator": 30,
            "judge_only_product_denominator": 0,
            "slots": slots,
            "diagnostics": diagnostics,
            "expected_blind_slots": blind_slots,
            "gaps": gaps,
            "slot_errors": {
                key: self._read(ref) for key, ref in progress["errors"].items()
            },
            "quality_observations": assessed,
            "offline_engineering": axis([], ["final_candidate_checks_not_bound"]),
            "flow_complete": state["phase"] == "closed",
            "real_readiness": axis(
                [], ["real_source_model_billing_isolation_unverified"]
            ),
            "calibration_quality": axis(
                assessed["failures"],
                [*assessed["blockers"], "real_calibration_not_performed"],
            ),
            "threshold": axis([], ["user_threshold_decision_pending"]),
            "v1_release": axis([], ["real_acceptance_pending"]),
            "aggregation_policy": original["aggregation_policy"],
            "actual_sampled_at": None
            if type(auth.runtime) is SyntheticRuntime
            else [r["sampled_at"] for r in batch["results"]],
            "seven_day_clock_started": any(
                r["source"] == "online" and r["sampled_at"] is not None
                for r in batch["results"]
            ),
            "remaining": remaining_limits(state, plan),
            "state": state,
            "settlement": auth.stop_report(self.job_id),
            "preserved_seen_families": original["seen_families"],
            "formal120": (
                "BLOCKED: future independent materials and explicit "
                "authorization required"
            ),
        }
        if type(auth.runtime) not in (SyntheticRuntime, InstalledRuntime):
            from .controlled.local_runtime import LocalInstalledRuntime

            if type(auth.runtime) is not LocalInstalledRuntime:
                raise ValueError("calibration_runtime_not_supported")
            provenance = auth.runtime.provenance
            observed = [
                r
                for r in batch["results"]
                if (
                    r["source"] == "online"
                    and r["sampled_at"] is not None
                    and r["evidence"].get("local_bridge", {}).get("real_sample") is True
                    and r["evidence"]["local_bridge"].get("mode")
                    == "LOCAL_INSTALLED_RUNNER"
                    and r["evidence"].get("independent_readback", {}).get("source")
                    == "actual_runner_files"
                )
            ]
            report["local_runtime_provenance"] = provenance
            report["real_execution_observations"] = {
                "contract": "V1-LOCAL-CALIBRATION-OBSERVATIONS",
                "version": 1,
                "trust_profile": provenance["trust_profile"],
                "fixed_denominator": 30,
                "actual_first_results": len(observed),
                "actual_result_ids": [r["id"] for r in observed],
                "all_thirty_observed": len(observed) == 30,
                "independent_administrator_anchor": False,
                "quality_or_threshold_approval_implied": False,
            }
            last_check = provenance["last_validated_readiness"]
            report["real_readiness"] = {
                **axis(
                    [],
                    ["fresh_runtime_check_required_for_next_operation"]
                    if last_check
                    else ["local_real_readiness_evidence_missing"],
                ),
                "last_validated_readiness": last_check,
                "scope": "observed_proofs_not_future_run_permission",
            }
            report["calibration_quality"] = axis(
                assessed["failures"],
                [
                    *assessed["blockers"],
                    *(
                        []
                        if len(observed) == 30
                        else [
                            "real_calibration_incomplete"
                            if observed
                            else "real_calibration_not_performed"
                        ]
                    ),
                ],
            )
        return report
