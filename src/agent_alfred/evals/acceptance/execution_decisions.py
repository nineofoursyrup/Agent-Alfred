"""Live decision readback and exact evidence binding; never a model capability.

This separate versioned contract does not extend schema4. A source-verifiable
material choice, a run decision and a checkpoint decision have distinct scopes.
Ordinary construction has no production adapter. The explicitly installed source
uses authenticated fresh readbacks; SimulationAuthority remains fixture-only.
"""

from copy import deepcopy
from datetime import UTC, datetime

from .authorization_history import HISTORY_SHA256, assess, history
from .schema import digest, hash_value
from .supplement_decisions import (
    approval,
    decision_request,
    dispute_request,
    instant,
    make_summary,
    validate_event,
)
from .supplement_reviews import assess_disputes
from .supplement_schema import closed, identity, signed, text

CONTRACT = "V1-REAL-CALIBRATION-DECISIONS-r1"
APPROVED_SUMMARY_ID = "9db180c708fda2d8e95fac974a9aa1b42364fcbf18a130726e0a16140220c355"
APPROVED_SUMMARY_SHA256 = (
    "d994472fa16b68b87d35ccb1176fef040c7e96b3406418f108f3b443e2254273"
)
APPROVED_PACKAGE_MANIFEST_SHA256 = (
    "76b364052aabec12dbdf1717a33c1fa56f7edd6ab1f1c556298a178942fe0e4f"
)
APPROVED_SOURCE_DISPUTES = (
    (
        "874a3d80ebeb0a06adc13c15b53438cb56829718c43b1d149b3940fb97fc24bc",
        "e664c94c7c5642e41cfbeae5ef457c663d17da4d306af5ef01830d0a97b6c839",
    ),
    (
        "1e8a28abf52db402dfead8daeeb4669d68c3d7835aba3aaaff56ca630b8c346f",
        "1c48d9d35c240aedd9149663ffd920fadc97506bc63bbafc3a019e008d82d7f0",
    ),
    (
        "4bbfe25b51f08e344e50e4ecd8c7d477974191e730923a79a767ca195282fee0",
        "3b62f3c43d30fa0fe1c33088ae61d1ca94baa0271a368e260e4241d1d50f6df3",
    ),
)


def approved_materials():
    """The approved object identity, not an authenticated decision or grant."""
    return {
        "summary_id": APPROVED_SUMMARY_ID,
        "summary_sha256": APPROVED_SUMMARY_SHA256,
        "package_manifest_sha256": APPROVED_PACKAGE_MANIFEST_SHA256,
        "source_disputes": [
            {
                "id": key,
                "sha256": value,
                "accepted_use": "judge_diagnostic_only",
                "evidence_conclusion": "unknown",
            }
            for key, value in APPROVED_SOURCE_DISPUTES
        ],
        "choices_preserved": ["Q1-Q25", "D1", "D2", "D4", "D5", "D6"],
        "evidence_gaps": [
            "D3_bounded_historical_inventory",
            "three_registered_families_original_cases_missing",
            "formal_120_material_slots_empty",
        ],
        "human_material_approval_missing": False,
        "trusted_source_status": "approval_source_unverifiable",
    }


def _lineage(store, batch_id):
    """Recheck every package and deny fact on every call, without a cache."""
    history()  # Verify the bundled deny-only registry before traversing anything.
    chain, seen = [], set()
    while batch_id is not None:
        if batch_id in seen:
            raise ValueError("authorization_ancestry_unverifiable")
        seen.add(batch_id)
        try:
            batch = store.read(batch_id)
        except OSError, ValueError:
            raise ValueError("authorization_ancestry_unverifiable") from None
        if batch["schema_version"] != 4:
            raise ValueError("execution_binding_requires_schema4")
        if assess(batch)["validity"] == "INVALID":
            raise ValueError("execution_authorization_invalid")
        if chain:
            child = chain[-1]
            if digest(batch) != child["parent"]["sha256"]:
                raise ValueError("authorization_ancestry_unverifiable")
            if batch["simulation"] != child["simulation"]:
                raise ValueError("execution_lineage_mode_changed")
            prior = batch.get("request_history", []) + batch.get("requests", [])
            current = child.get("request_history", []) + child.get("requests", [])
            if current[: len(prior)] != prior:
                raise ValueError("execution_request_history_changed")
            if batch.get("budget_started_at") is not None and any(
                batch.get(key) != child.get(key)
                for key in ("budget_started_at", "budget_scope")
            ):
                raise ValueError("execution_budget_continuity_lost")
        chain.append(batch)
        batch_id = batch["parent"]["batch_id"] if batch["parent"] else None
    return list(reversed(chain))


def _reference(batch):
    return {"batch_id": batch["batch_id"], "sha256": digest(batch)}


def material_binding(
    store, batch_id, *, package_manifest_sha256, approved_batch_id=None
):
    """Describe a candidate difference without resigning an old approval.

    The package manifest digest is supplied by the protected material intake.
    Full package byte verification belongs to that intake, not DecisionSource.
    The approved material package must be the leaf or an actual ancestor.
    """
    hash_value(package_manifest_sha256)
    chain = _lineage(store, batch_id)
    batch = chain[-1]
    approved_batch_id = approved_batch_id or batch_id
    original = next((b for b in chain if b["batch_id"] == approved_batch_id), None)
    if original is None:
        raise ValueError("approved_material_ancestor_missing")
    original_summary = make_summary(original)
    current_summary = make_summary(batch)
    if not batch["simulation"] and (
        original_summary["id"] != APPROVED_SUMMARY_ID
        or digest(original_summary) != APPROVED_SUMMARY_SHA256
        or package_manifest_sha256 != APPROVED_PACKAGE_MANIFEST_SHA256
    ):
        raise ValueError("approved_material_identity_mismatch")
    changes = {
        key: {"before": digest(value), "after": digest(current_summary["objects"][key])}
        for key, value in original_summary["objects"].items()
        if value != current_summary["objects"][key]
    }
    return signed(
        {
            "version": 1,
            "contract": CONTRACT,
            "simulation": batch["simulation"],
            "batch": _reference(batch),
            "ancestors": [_reference(b) for b in chain[:-1]],
            "approved_batch": _reference(original),
            "approved_summary": decision_request(original_summary),
            "current_summary": decision_request(current_summary),
            "package_manifest_sha256": package_manifest_sha256,
            "candidate_id": batch["candidate_id"],
            "changes": changes,
            "authorization_history_sha256": HISTORY_SHA256,
            "budget": {
                "scope": batch.get("budget_scope"),
                "started_at": batch.get("budget_started_at"),
                "request_history_sha256": digest(
                    batch.get("request_history", []) + batch.get("requests", [])
                ),
                "authorization_chain_sha256": digest(
                    [b.get("authorization") for b in chain]
                ),
            },
        }
    )


def _validate_binding(value):
    closed(
        value,
        "id version contract simulation batch ancestors approved_batch "
        "approved_summary current_summary package_manifest_sha256 candidate_id "
        "changes authorization_history_sha256 budget",
        "invalid_execution_binding",
    )
    identity(value)
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("unknown_execution_binding_version")
    if value["contract"] != CONTRACT or type(value["simulation"]) is not bool:
        raise ValueError("invalid_execution_binding")


def source_preflight(
    store,
    batch_id,
    *,
    package_manifest_sha256,
    approved_batch_id=None,
    source=None,
    subject=None,
    dispute_scope_refs=(),
):
    """Pure public report; no store writes, credential reads or client factories."""
    from .report import axis

    result = {
        "version": 1,
        "scope": "decision_source_readback_only",
        "approved_materials": approved_materials(),
        "source_status": "approval_source_unverifiable",
        "online_executable": False,
        "release_eligible": False,
    }
    blockers = []
    try:
        binding = material_binding(
            store,
            batch_id,
            approved_batch_id=approved_batch_id,
            package_manifest_sha256=package_manifest_sha256,
        )
        result["binding"] = binding
        result.update(
            DecisionAdmission(
                store,
                source=source,
                subject=subject,
                dispute_scope_refs=dispute_scope_refs,
            ).verify_materials(binding)
        )
    except ValueError as error:
        blockers.append(str(error))
    result["blockers"] = blockers
    result["offline_engineering"] = axis(blockers=blockers)
    result["v1_release"] = axis(blockers=["real_acceptance_not_performed", *blockers])
    return result


def run_request(binding, *, job_id, plan_sha256, budget_sha256):
    """An exact initial run decision request, distinct from material approval."""
    _validate_binding(binding)
    request = signed(
        {
            "version": 1,
            "contract": CONTRACT,
            "scope": "run",
            "binding_sha256": digest(binding),
            "job_id": job_id,
            "plan_sha256": plan_sha256,
            "budget_sha256": budget_sha256,
        }
    )
    validate_request(request)
    return request


def checkpoint_request(
    binding,
    *,
    job_id,
    run_decision,
    judge_summary_sha256,
    disputes_sha256,
    remaining_budget_sha256,
):
    """Bind the judge-only checkpoint to the original run and remaining budget.

    The stage executor validates the complete 18+18 summary and actual remaining
    ledger before constructing this request. Neither this request nor its event
    can refresh that ledger or approve product results, calibration or thresholds.
    """
    _validate_binding(binding)
    validate_execution_event(run_decision)
    if (
        run_decision["request"]["scope"] != "run"
        or run_decision["request"]["job_id"] != job_id
        or run_decision["request"]["binding_sha256"] != digest(binding)
        or run_decision["decision"] != "approved"
    ):
        raise ValueError("checkpoint_run_mismatch")
    request = signed(
        {
            "version": 1,
            "contract": CONTRACT,
            "scope": "checkpoint",
            "binding_sha256": digest(binding),
            "job_id": job_id,
            "run_decision": {
                "id": run_decision["id"],
                "source_ref": run_decision["source_ref"],
                "request_sha256": digest(run_decision["request"]),
            },
            "judge_summary_sha256": judge_summary_sha256,
            "disputes_sha256": disputes_sha256,
            "remaining_budget_sha256": remaining_budget_sha256,
        }
    )
    validate_request(request)
    return request


def dispute_scope_request(binding, dispute, adjudication):
    """Bind limited diagnostic use to one original opinion and schema4 ruling.

    This supplements, never fabricates or replaces, the existing per-dispute user
    adjudication. Source authentication of both projections may refer to the same
    original human message; no new product choice or blanket dismissal is implied.
    """
    _validate_binding(binding)
    validate_event(adjudication)
    if (
        dispute.get("item") != "source_independence"
        or adjudication["object_type"] != "dispute"
        or adjudication["object_id"] != dispute["id"]
        or adjudication["object_sha256"] != digest(dispute)
        or adjudication["manifest"] != binding["current_summary"]["manifest"]
        or adjudication["decision"] != "dismissed"
    ):
        raise ValueError("dispute_scope_mismatch")
    request = signed(
        {
            "version": 1,
            "contract": CONTRACT,
            "scope": "material_dispute",
            "binding_sha256": digest(binding),
            "dispute_sha256": digest(dispute),
            "adjudication_sha256": digest(adjudication),
            "accepted_use": "judge_diagnostic_only",
            "retained_conclusion": dispute["opinion"]["status"],
        }
    )
    validate_request(request)
    return request


def validate_request(request):
    if not isinstance(request, dict) or request.get("scope") not in (
        "run",
        "checkpoint",
        "material_dispute",
    ):
        raise ValueError("invalid_execution_decision_request")
    fields = "id version contract scope binding_sha256 "
    fields += {
        "run": "job_id plan_sha256 budget_sha256",
        "checkpoint": "job_id run_decision judge_summary_sha256 disputes_sha256 "
        "remaining_budget_sha256",
        "material_dispute": "dispute_sha256 adjudication_sha256 accepted_use "
        "retained_conclusion",
    }[request["scope"]]
    closed(request, fields, "invalid_execution_decision_request")
    identity(request)
    if type(request["version"]) is not int or request["version"] != 1:
        raise ValueError("unknown_execution_decision_version")
    if request["contract"] != CONTRACT:
        raise ValueError("invalid_execution_decision_request")
    if request["scope"] != "material_dispute":
        text(request["job_id"])
    elif request["accepted_use"] != "judge_diagnostic_only" or request[
        "retained_conclusion"
    ] not in ("unknown", "fail", "pass"):
        raise ValueError("dispute_scope_mismatch")
    for key, value in request.items():
        if key.endswith("_sha256"):
            hash_value(value)
    if request["scope"] == "checkpoint":
        prior = request["run_decision"]
        closed(
            prior, "id source_ref request_sha256", "invalid_execution_decision_request"
        )
        text(prior["source_ref"])
        hash_value(prior["id"])
        hash_value(prior["request_sha256"])


def validate_execution_event(event):
    closed(
        event,
        "id version kind subject source_ref at decision request reason evidence",
        "invalid_execution_decision_event",
    )
    identity(event)
    if type(event["version"]) is not int or event["version"] != 1:
        raise ValueError("unknown_execution_decision_version")
    if event["kind"] not in ("synthetic_execution_decision", "execution_decision"):
        raise ValueError("invalid_execution_decision_event")
    if event["decision"] not in ("approved", "rejected"):
        raise ValueError("invalid_execution_decision_event")
    for key in ("subject", "source_ref", "reason"):
        text(event[key])
    instant(event["at"])
    if not isinstance(event["evidence"], list) or not event["evidence"]:
        raise ValueError("decision_evidence_required")
    for reference in event["evidence"]:
        text(reference)
    validate_request(event["request"])


class DecisionAdmission:
    """Read-only admission for a source protocol; no credentials or factories.

    Recreate this reader freely after restart. Its results are observations, never
    reusable authority: consumers must call it again before every admission.
    """

    def __init__(self, store, *, source=None, subject=None, dispute_scope_refs=()):
        self.store = store
        self.source = source if source is not None else store.decision_source
        self.subject = subject
        self.dispute_scope_refs = tuple(dispute_scope_refs)

    def _source(self, binding):
        from .controlled.runtime import InstalledDecisionSource
        from .simulation_authority import SimulationAuthority

        if type(self.source) is InstalledDecisionSource:
            self.source.authorize(binding, self.subject)
            return self.source
        if (
            not binding["simulation"]
            or type(self.source) is not SimulationAuthority
            or self.subject is None
        ):
            raise ValueError("approval_source_unverifiable")
        return self.source

    def _current(self, binding):
        _validate_binding(binding)
        observed = material_binding(
            self.store,
            binding["batch"]["batch_id"],
            package_manifest_sha256=binding["package_manifest_sha256"],
            approved_batch_id=binding["approved_batch"]["batch_id"],
        )
        if observed != binding:
            raise ValueError("execution_binding_changed")
        return self.store.read(binding["batch"]["batch_id"])

    def _installed_scope_event(self, source, request, now):
        from .controlled.runtime import InstalledDecisionSource

        if type(source) is not InstalledDecisionSource:
            return None
        event = source.read_scoped_decision(request)
        validate_event(event)
        if (
            event["kind"] != "user_decision"
            or event["subject"] != self.subject
            or any(event.get(key) != value for key, value in request.items())
            or instant(event["at"]) > now
            or source.read_decision(event["source_ref"]) != event
        ):
            raise ValueError("approval_source_unverifiable")
        return event

    def verify_materials(self, binding, *, now=None):
        _validate_binding(binding)
        source = self._source(binding)
        batch = self._current(binding)
        original = self.store.read(binding["approved_batch"]["batch_id"])
        now = datetime.now(UTC) if now is None else now
        # A new candidate changes the summary identity without changing approved
        # material choices. The eventual run decision must bind the explicit diff.
        # Any other material change requires a current material summary decision.
        target = batch if set(binding["changes"]) - {"candidate_id"} else original
        event = approval(target, "materials", source, now)
        installed = self._installed_scope_event(
            source, decision_request(make_summary(target)), now
        )
        if installed is not None:
            event = installed if installed["decision"] == "approved" else None
        if event is None:
            raise ValueError("approval_source_unverifiable")
        if event["subject"] != self.subject:
            raise ValueError("decision_subject_mismatch")
        if not event["evidence"]:
            raise ValueError("decision_evidence_required")
        disputes = [
            d
            for d in assess_disputes(batch, source, now)
            if d["target"]["type"] == "materials"
        ]
        for dispute in disputes:
            original_dispute = {
                key: value
                for key, value in dispute.items()
                if key not in ("status", "decision")
            }
            live = self._installed_scope_event(
                source, dispute_request(batch, original_dispute), now
            )
            if live is not None:
                if instant(live["at"]) < instant(dispute["at"]):
                    raise ValueError("decision_before_dispute")
                dispute.update(status=live["decision"], decision=live)
            if dispute["status"] != "dismissed":
                raise ValueError("material_dispute_unresolved")
            if dispute["decision"]["subject"] != self.subject:
                raise ValueError("decision_subject_mismatch")
            if dispute["item"] == "source_independence":
                expected = dispute_scope_request(
                    binding, original_dispute, dispute["decision"]
                )
                scoped = [
                    self._read_execution(ref, binding, now)
                    for ref in self.dispute_scope_refs
                ]
                matches = [
                    e
                    for e in scoped
                    if e["request"] == expected
                    and instant(e["at"]) >= instant(dispute["decision"]["at"])
                ]
                if not matches:
                    raise ValueError("material_dispute_scope_unverifiable")
                dispute["accepted_use"] = "judge_diagnostic_only"
                dispute["scope_decision"] = matches[-1]
        return {
            "version": 1,
            "source_status": "VERIFIED_SYNTHETIC_ONLY"
            if binding["simulation"]
            else "VERIFIED_INSTALLED_SOURCE",
            "binding": deepcopy(binding),
            "material_decision": deepcopy(event),
            "disputes": deepcopy(disputes),
            "source_independence": "unknown"
            if any(d["item"] == "source_independence" for d in disputes)
            else "not_assessed",
            "online_executable": False,
            "release_eligible": False,
            "run_decision_required": True,
        }

    def _read_execution(self, source_ref, binding, now):
        try:
            event = self._source(binding).read_decision(source_ref)
        except OSError, ValueError, KeyError:
            raise ValueError("approval_source_unverifiable") from None
        validate_execution_event(event)
        if event["source_ref"] != source_ref:
            raise ValueError("approval_source_unverifiable")
        if event["subject"] != self.subject:
            raise ValueError("decision_subject_mismatch")
        if instant(event["at"]) > now or event["decision"] != "approved":
            raise ValueError("execution_decision_not_effective")
        expected_kind = (
            "synthetic_execution_decision"
            if binding["simulation"]
            else "execution_decision"
        )
        if event["kind"] != expected_kind:
            raise ValueError("approval_source_unverifiable")
        return event

    def verify_execution(self, request, source_ref, binding, *, now=None):
        """Reverify material, original run and exact event before broker admission.

        Returns the source event, not an executable grant. Dispatch must separately
        enforce stage, time, identity, atomic money reservation and isolation.
        """
        validate_request(request)
        if request["scope"] not in ("run", "checkpoint"):
            raise ValueError("decision_scope_mismatch")
        now = datetime.now(UTC) if now is None else now
        materials = self.verify_materials(binding, now=now)
        if request["binding_sha256"] != digest(binding):
            raise ValueError("decision_scope_mismatch")
        event = self._read_execution(source_ref, binding, now)
        if event["request"] != request:
            raise ValueError("decision_scope_mismatch")
        material_times = [instant(materials["material_decision"]["at"])]
        material_times += [
            instant(d["scope_decision" if "scope_decision" in d else "decision"]["at"])
            for d in materials["disputes"]
        ]
        if instant(event["at"]) < max(material_times):
            raise ValueError("execution_decision_before_materials")
        if request["scope"] == "checkpoint":
            prior = request["run_decision"]
            run = self._read_execution(prior["source_ref"], binding, now)
            if (
                run["id"] != prior["id"]
                or digest(run["request"]) != prior["request_sha256"]
                or run["request"]["scope"] != "run"
                or run["request"]["job_id"] != request["job_id"]
                or run["request"]["binding_sha256"] != request["binding_sha256"]
                or instant(run["at"]) > instant(event["at"])
                or instant(run["at"]) < max(material_times)
            ):
                raise ValueError("checkpoint_run_mismatch")
        return deepcopy(event)
