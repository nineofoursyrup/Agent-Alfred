"""Public bounded AuthorityDispatch and ModelClient for the versioned execution.

Default construction is closed. The installed service, not a worker-supplied
role/factory, owns the plan, complete payload binding, credentials and ledger.
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from uuid import uuid4

from agent_alfred.model import (
    AttemptRecord,
    ModelCallInterrupted,
    ModelError,
    ModelRef,
    ModelResponse,
    ModelResult,
    Usage,
)
from agent_alfred.openai_compatible import _decode_message, _stop_reason

from .controlled.contract import (
    KINDS,
    MODELS,
    PHASES,
    cost_units,
    exact,
    initial_budget,
    integer,
    text,
    validate_payload,
    validate_plan,
    validate_terms,
    wire_payload,
)
from .controlled.runtime import (
    InstalledRuntime,
    SyntheticRuntime,
    TransportFailure,
    validate_runtime,
)
from .controlled.store import ZERO, MemoryExecutionAnchor, MemoryExecutionStore
from .execution_decisions import (
    DecisionAdmission,
    material_binding,
    run_request,
    validate_execution_event,
)
from .materials import preflight_materials, strict_json
from .schema import digest
from .store import EvidenceStore
from .supplement_decisions import instant


def utc_now():
    return datetime.now(UTC)


class ControlledAuthority:
    """Trusted-side service. Worker identity is installed on its bounded facade.

    Storage/anchor implement controlled.store protocols. The controller's payload
    registration is a distinct authenticated action, never part of worker invoke.
    The phase extension API commits into this same ledger (#103).
    """

    def __init__(
        self,
        *,
        runtime=None,
        store=None,
        anchor=None,
        vault=None,
        allowed_objects=frozenset(),
        expected_manifest=None,
        output_root=None,
        worker=None,
        controller=None,
        subject=None,
        dispute_scope_refs=(),
        now=utc_now,
        before_intent=None,
    ):
        self.runtime, self.store, self.anchor = runtime, store, anchor
        self.vault, self.allowed_objects = vault, frozenset(allowed_objects)
        self.expected_manifest, self.output_root = expected_manifest, output_root
        self.worker, self.controller, self.subject = worker, controller, subject
        self.dispute_scope_refs, self.now = tuple(dispute_scope_refs), now
        self.before_intent = before_intent
        if type(runtime) is InstalledRuntime and (
            type(store) is MemoryExecutionStore
            or type(anchor) is MemoryExecutionAnchor
            or before_intent is not None
            or now is not utc_now
        ):
            raise ValueError("real_execution_prerequisites_unverified")

    def _installed(self):
        validate_runtime(self.runtime)
        if self.store is None or self.anchor is None:
            raise ValueError("execution_ledger_required")

    def persistence_call(self, job_id, operation, *args, **kwargs):
        """Trusted controller IO scoped by its authenticated job, never object data.

        The persistent implementation records failures in each surviving domain.
        A plain audit object read may omit the job, but execution drivers must
        supply it. This method confers no authentication on untrusted arguments.
        """
        return operation(*args, **kwargs)

    def _commit(
        self, state, kind, *, previous=None, attempt=None, at=None, detail=None
    ):
        previous = previous or {"revision": 0, "event_digest": ZERO}
        state = deepcopy(state)
        state["revision"] = previous["revision"] + 1
        if attempt is not None:
            state["active_attempt_sha256"] = (
                digest(attempt)
                if state["active_attempt"] == attempt["attempt_id"]
                else None
            )
        event = {
            "contract": "V1-CONTROLLED-EVENT",
            "version": 1,
            "job_id": state["job_id"],
            "revision": state["revision"],
            "kind": kind,
            "previous_digest": previous["event_digest"],
            "at": at.isoformat() if at is not None else None,
            "detail": deepcopy(detail),
            "attempt_sha256": digest(attempt) if attempt else None,
            "attempt_id": attempt["attempt_id"] if attempt else None,
            "state_digest": digest(
                {k: v for k, v in state.items() if k != "event_digest"}
            ),
        }
        state["event_digest"] = digest(event)
        self.persistence_call(
            state["job_id"],
            self.store.put,
            state,
            event,
            old_revision=previous["revision"],
            old_digest=previous["event_digest"],
            attempt=attempt,
        )
        self.persistence_call(
            state["job_id"],
            self.anchor.commit,
            {
                "job_id": state["job_id"],
                "revision": state["revision"],
                "previous_digest": previous["event_digest"],
                "event_digest": state["event_digest"],
            },
        )
        if self.persistence_call(
            state["job_id"], self.anchor.read, state["job_id"]
        ) != {
            "job_id": state["job_id"],
            "revision": state["revision"],
            "digest": state["event_digest"],
        }:
            raise ValueError("authority_anchor_mismatch")
        return state

    def _verified(self, job_id):
        self._installed()
        state = self.persistence_call(job_id, self.store.get, text(job_id))
        if state is None:
            raise ValueError("job_unknown")
        if self.persistence_call(job_id, self.anchor.read, job_id) != {
            "job_id": job_id,
            "revision": state["revision"],
            "digest": state["event_digest"],
        }:
            raise ValueError("authority_anchor_mismatch")
        if state["active_attempt"]:
            row = self.persistence_call(
                job_id, self.store.get_attempt, job_id, state["active_attempt"]
            )
            if row is None or digest(row) != state["active_attempt_sha256"]:
                raise ValueError("attempt_ledger_unverifiable")
        return state

    def _context(self, state):
        plan = self.persistence_call(
            state["job_id"], self.store.get_object, state["plan_ref"]
        )
        binding = self.persistence_call(
            state["job_id"], self.store.get_object, state["binding_ref"]
        )
        evidence = EvidenceStore(
            Path(state.get("evidence_output", state["material_output"])) / "evidence",
            decision_source=self.runtime.decision_source,
        )
        reader = DecisionAdmission(
            evidence,
            source=self.runtime.decision_source,
            subject=self.subject,
            dispute_scope_refs=self.dispute_scope_refs,
        )
        return plan, binding, evidence, reader

    def _current_permission(self, state, now):
        plan, binding, evidence, reader = self._context(state)
        self._verify_source(
            plan,
            binding,
            evidence,
            reader,
            self.persistence_call(
                state["job_id"], self.store.get_object, state["run_request_ref"]
            ),
            state["run_source_ref"],
            now,
        )
        batch = self.persistence_call(
            state["job_id"], evidence.read, binding["batch"]["batch_id"]
        )
        proof = self.runtime.check(plan, binding, batch, now)
        if plan["execution_mode"] != "synthetic_capacity" and state["phase"] in (
            "product",
            "grading",
        ):
            from .controlled_diagnostics import verify_continuation

            reference = state["phase_facts"].get("checkpoint")
            if not reference:
                raise ValueError("checkpoint_evidence_required")
            verify_continuation(
                self, state, self.read_object(reference, job_id=state["job_id"]), now
            )
        # This observation is local to one action, never added to the signed plan.
        return {
            **plan,
            "_permission_valid_until": proof.get("identities", {}).get("valid_until"),
        }

    def _verify_source(self, plan, binding, evidence, reader, request, source_ref, now):
        if plan["execution_mode"] == "synthetic_capacity" and (
            type(self.runtime) is not SyntheticRuntime or not binding["simulation"]
        ):
            raise ValueError("synthetic_capacity_only")
        if plan["execution_mode"] != "synthetic_replay":
            return reader.verify_execution(request, source_ref, binding, now=now)
        if type(self.runtime) is not SyntheticRuntime or not str(
            self.subject
        ).startswith("simulation:"):
            raise ValueError("synthetic_replay_only")
        current = material_binding(
            evidence,
            binding["batch"]["batch_id"],
            approved_batch_id=binding["approved_batch"]["batch_id"],
            package_manifest_sha256=binding["package_manifest_sha256"],
        )
        if current != binding:
            raise ValueError("execution_binding_changed")
        event = self.runtime.decision_source.read_decision(source_ref)
        validate_execution_event(event)
        if (
            event["kind"] != "synthetic_execution_decision"
            or event["subject"] != self.subject
            or event["request"] != request
            or event["decision"] != "approved"
            or event["source_ref"] != source_ref
            or instant(event["at"]) > now
        ):
            raise ValueError("synthetic_replay_scope_mismatch")
        # This fixture permits only MockTransport replay of unchanged protected
        # materials. It deliberately does not satisfy real material approval.
        return event

    def submit_job(self, proposal_ref):
        self._installed()
        exact(proposal_ref, "job_id material_ref plan run_source_ref approved_batch_id")
        job_id = text(proposal_ref["job_id"])
        if any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for c in job_id
        ):
            raise ValueError("job_identity_invalid")
        if self.persistence_call(job_id, self.store.get, job_id) is not None:
            raise ValueError("job_already_submitted")
        plan = deepcopy(proposal_ref["plan"])
        validate_plan(plan)
        if plan["worker"] != self.worker or plan["controller"] != self.controller:
            raise ValueError("worker_identity_mismatch")
        if plan["material_ref"] != proposal_ref["material_ref"]:
            raise ValueError("material_plan_mismatch")
        output = Path(self.output_root) / job_id
        report = preflight_materials(
            proposal_ref["material_ref"],
            vault=self.vault,
            allowed_objects=self.allowed_objects,
            expected_manifest_sha256=self.expected_manifest,
            output=output,
        )
        evidence = EvidenceStore(
            output / "evidence", decision_source=self.runtime.decision_source
        )
        binding = material_binding(
            evidence,
            report["batch_id"],
            approved_batch_id=proposal_ref["approved_batch_id"],
            package_manifest_sha256=report["manifest_sha256"],
        )
        if (
            digest(binding) != plan["binding_sha256"]
            or binding["candidate_id"] != plan["material_candidate_id"]
        ):
            raise ValueError("execution_binding_changed")
        if (
            binding["budget"]["started_at"] is not None
            or binding["budget"]["scope"] is not None
        ):
            raise ValueError("existing_budget_requires_original_ledger")
        budget = initial_budget(plan)
        request = run_request(
            binding,
            job_id=job_id,
            plan_sha256=digest(plan),
            budget_sha256=digest(budget),
        )
        now = self.now()
        reader = DecisionAdmission(
            evidence,
            source=self.runtime.decision_source,
            subject=self.subject,
            dispute_scope_refs=self.dispute_scope_refs,
        )
        self._verify_source(
            plan,
            binding,
            evidence,
            reader,
            request,
            proposal_ref["run_source_ref"],
            now,
        )
        batch = self.persistence_call(job_id, evidence.read, report["batch_id"])
        self.runtime.check(plan, binding, batch, now)
        self._validate_objects(plan, batch)
        state = {
            "contract": "V1-CONTROLLED-STATE",
            "version": 1,
            "job_id": job_id,
            "state": "ACTIVE",
            "stop_reason": None,
            "phase": plan["initial_phase"],
            "plan_ref": self.persistence_call(job_id, self.store.put_object, plan),
            "binding_ref": self.persistence_call(
                job_id, self.store.put_object, binding
            ),
            "material_output": str(output),
            "material_receipt_ref": self.persistence_call(
                job_id, self.store.put_object, report
            ),
            "run_request_ref": self.persistence_call(
                job_id, self.store.put_object, request
            ),
            "run_source_ref": proposal_ref["run_source_ref"],
            "started_at": now.isoformat(),
            "last_at": now.isoformat(),
            "active_attempt": None,
            "active_attempt_sha256": None,
            "spent_units": 0,
            "pending_units": 0,
            "cap_units": plan["cap_units"],
            "counts": {"flash": 0, "pro": 0, "total": 0},
            "case_counts": {},
            "operation_counts": {},
            "case_started_at": {},
            "finished_operations": [],
            "phase_facts": {},
            "prepared_ref": None,
            "source_status": "SOURCE_UNVERIFIABLE_AUDIT_ONLY"
            if plan["execution_mode"] == "synthetic_replay"
            else (
                "VERIFIED_SYNTHETIC_ONLY"
                if binding["simulation"]
                else "VERIFIED_INSTALLED_SOURCE"
            ),
        }
        self._commit(state, "ACTIVATE", at=now)
        return self.status(job_id)

    def _validate_objects(self, plan, batch):
        cases = {c["id"]: c for c in batch["cases"]}
        tests = {t["id"]: t for t in batch["judge_tests"]}
        for operation in plan["operations"]:
            diagnostic = operation["kind"] in ("judge_test", "judge_review")
            objects = tests if diagnostic else cases
            target = objects.get(operation["object_id"])
            if (
                target is None
                or digest(target) != operation["data_scope_sha256"]
                or (diagnostic and operation["case_id"] is not None)
                or (not diagnostic and operation["case_id"] != operation["object_id"])
            ):
                raise ValueError("plan_material_object_mismatch")

    def _active(self, state):
        if state["state"] != "ACTIVE" or state["stop_reason"]:
            raise ValueError(state["stop_reason"] or "job_not_active")

    def _time_reason(self, state, now):
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now < instant(state["last_at"])
        ):
            return "authority_clock_unverifiable"
        if now >= instant(state["started_at"]) + timedelta(seconds=10800):
            return "batch_deadline"
        return None

    def _stop(self, state, reason, *, attempt=None, detail=None):
        stopped = deepcopy(state)
        stopped["state"] = "SUSPENDED"
        if stopped["stop_reason"] in (None, "time_check_pending"):
            stopped["stop_reason"] = reason
        return self._commit(
            stopped,
            "STOP",
            previous=state,
            attempt=attempt,
            detail={"reason": reason, "evidence": detail},
        )

    def _action(self, state, kind, mutate, *, permission=True, validate=None):
        """Fence before reading time/source; clear only in the action's CAS."""
        self._active(state)
        fenced = deepcopy(state)
        fenced.update(state="SUSPENDED", stop_reason="time_check_pending")
        fenced = self._commit(fenced, "TIME_CHECK", previous=state)
        try:
            now = self.now()
            reason = self._time_reason(fenced, now)
            if reason:
                raise ValueError(reason)
            observation = self._current_permission(fenced, now) if permission else {}
            changed = deepcopy(fenced)
            changed.update(state="ACTIVE", stop_reason=None, last_at=now.isoformat())
            attempt, detail = mutate(changed, now)
            terms = (
                self.persistence_call(
                    state["job_id"], self.store.get_object, attempt["quote_ref"]
                )["terms"]
                if attempt
                else None
            )
            # The action's own sample must be validated, including elapsed time
            # spent reading approval/model/billing services, before clearing fence.
            at = self.now()
            reason = self._time_reason(changed, at)
            if reason:
                raise ValueError(reason)
            if attempt is not None:
                self._attempt_time(attempt, at)
                if (
                    not instant(terms["valid_from"])
                    <= at
                    < instant(terms["valid_until"])
                ):
                    raise ValueError("billing_price_expired")
            permission_until = observation.get("_permission_valid_until")
            if permission_until is not None and at >= instant(permission_until):
                raise ValueError("actual_model_identity_expired")
            if validate is not None:
                validate(observation)
            changed["last_at"] = at.isoformat()
            return self._commit(
                changed, kind, previous=fenced, attempt=attempt, at=at, detail=detail
            )
        except BaseException as error:
            # CAS failure after a concurrent revoke/stop cannot overwrite it.
            current = self._verified(state["job_id"])
            self._stop(
                current,
                str(error) if isinstance(error, ValueError) else type(error).__name__,
            )
            raise

    def _operation(self, state, operation_id):
        plan = self.persistence_call(
            state["job_id"], self.store.get_object, state["plan_ref"]
        )
        operation = next(
            (o for o in plan["operations"] if o["id"] == operation_id), None
        )
        if operation is None:
            raise ValueError("operation_not_in_plan")
        if (
            KINDS[operation["kind"]][0] != state["phase"]
            or operation_id in state["finished_operations"]
        ):
            raise ValueError("operation_phase_closed")
        return plan, operation

    def prepare_request(
        self, job_id, operation_id, request, *, principal, data_scope_sha256
    ):
        """Controller-only projection registration; never exposed to the worker.

        #103/#104's trusted driver constructs the projection from approved input
        and local tool output. Deployment must authenticate its separate identity.
        The immutable binding prevents subsequent worker-side modifications.
        """
        if principal != self.controller:
            raise ValueError("controller_identity_required")
        state = self._verified(job_id)
        self._active(state)
        plan, operation = self._operation(state, operation_id)
        if principal != self.controller or principal != plan["controller"]:
            raise ValueError("controller_identity_required")
        if operation["data_scope_sha256"] != data_scope_sha256:
            raise ValueError("data_scope_mismatch")
        payload = wire_payload(request)
        if request.model.endpoint_id != "deepseek":
            raise ValueError("controlled_endpoint_mismatch")
        validate_payload(payload, operation)
        from .controlled_diagnostics import validate_projection

        validate_projection(self, state, operation, payload)
        if state["active_attempt"] or state["prepared_ref"]:
            raise ValueError("prepared_request_pending")
        prepared = {
            "job_id": job_id,
            "plan_sha256": digest(plan),
            "worker": plan["worker"],
            "operation_id": operation_id,
            "object_id": operation["object_id"],
            "data_scope_sha256": data_scope_sha256,
            "instance_id": operation["instance_id"],
            "conversation_id": request.conversation_id,
            "payload": payload,
            "payload_sha256": digest(payload),
            "projection_id": uuid4().hex,
        }
        reference = self.persistence_call(job_id, self.store.put_object, prepared)

        def bind(changed, now):
            changed["prepared_ref"] = reference
            return None, {
                "prepared_ref": reference,
                "controller": self.controller,
                "operation_id": operation_id,
                "payload_sha256": digest(payload),
            }

        self._action(state, "BIND_PAYLOAD", bind)
        return {"prepared_ref": reference, "payload_sha256": digest(payload)}

    def client(self, job_id, operation_id, prepared):
        self._installed()
        state = self._verified(job_id)
        if state["prepared_ref"] != prepared["prepared_ref"]:
            raise ValueError("prepared_request_mismatch")
        value = self.persistence_call(
            job_id, self.store.get_object, prepared["prepared_ref"]
        )
        return ControlledModelClient(
            WorkerAuthorityDispatch(self),
            job_id,
            operation_id,
            deepcopy(prepared),
            value["conversation_id"],
        )

    def invoke(self, job_id, role, attempt_id, request_descriptor):
        """AuthorityDispatch shape; role is an opaque signed operation ID."""
        text(attempt_id)
        exact(request_descriptor, "prepared_ref payload_sha256 timeout_seconds")
        integer(request_descriptor["timeout_seconds"], minimum=1, maximum=120)
        state = self._verified(job_id)
        self._active(state)
        if state["active_attempt"]:
            raise ValueError("request_state_unresolved")
        plan, operation = self._operation(state, role)
        if state["prepared_ref"] != request_descriptor["prepared_ref"]:
            raise ValueError("prepared_request_mismatch")
        prepared = self.persistence_call(
            job_id, self.store.get_object, request_descriptor["prepared_ref"]
        )
        if (
            state["prepared_ref"] != request_descriptor["prepared_ref"]
            or prepared["job_id"] != job_id
            or prepared["operation_id"] != role
            or prepared["worker"] != self.worker
            or prepared["plan_sha256"] != digest(plan)
            or prepared["object_id"] != operation["object_id"]
            or prepared["instance_id"] != operation["instance_id"]
            or prepared["data_scope_sha256"] != operation["data_scope_sha256"]
            or prepared["payload_sha256"] != request_descriptor["payload_sha256"]
            or digest(prepared["payload"]) != request_descriptor["payload_sha256"]
        ):
            raise ValueError("prepared_request_mismatch")
        validate_payload(prepared["payload"], operation)
        if (
            self.persistence_call(job_id, self.store.get_attempt, job_id, attempt_id)
            is not None
        ):
            raise ValueError("duplicate_attempt")
        consumed = (
            self.persistence_call(
                job_id,
                self.store.prepared_consumed,
                job_id,
                request_descriptor["prepared_ref"],
            )
            if hasattr(self.store, "prepared_consumed")
            else any(
                r["prepared_ref"] == request_descriptor["prepared_ref"]
                for r in self.persistence_call(job_id, self.store.list_attempts, job_id)
            )
        )
        if consumed:
            raise ValueError("prepared_request_consumed")
        group = KINDS[operation["kind"]][1]

        def reserve(changed, now):
            quote = self.runtime.quote(plan, operation, prepared["payload"], now)
            exact(
                quote,
                "payload_sha256 input_tokens terms token_evidence identity_evidence",
                "billing_unverifiable",
            )
            validate_terms(quote["terms"], group)
            if (
                quote["payload_sha256"] != prepared["payload_sha256"]
                or not quote["token_evidence"]
                or not quote["identity_evidence"]
                or quote["terms"] != plan["pricing"][group]
            ):
                raise ValueError("billing_or_token_proof_mismatch")
            terms = quote["terms"]
            if not instant(terms["valid_from"]) <= now < instant(terms["valid_until"]):
                raise ValueError("billing_price_expired")
            integer(quote["input_tokens"], maximum=MODELS[group][1])
            worst = cost_units(terms, quote["input_tokens"], MODELS[group][2])
            if (
                changed["spent_units"] + changed["pending_units"] + worst
                > plan["cap_units"]
            ):
                raise ValueError("amount_cap_exceeded")
            if (
                changed["counts"][group] >= plan["limits"][group]
                or changed["counts"]["total"] >= plan["limits"]["total"]
                or changed["operation_counts"].get(role, 0) >= operation["max_calls"]
            ):
                raise ValueError("request_quota_exhausted")
            case_id = operation["case_id"]
            if group == "flash":
                if changed["case_counts"].get(case_id, 0) >= plan["limits"]["per_case"]:
                    raise ValueError("case_quota_exhausted")
                started = changed["case_started_at"].setdefault(
                    case_id, now.isoformat()
                )
                if now >= instant(started) + timedelta(seconds=900):
                    raise ValueError("run_deadline")
                changed["case_counts"][case_id] = (
                    changed["case_counts"].get(case_id, 0) + 1
                )
            changed["counts"][group] += 1
            changed["counts"]["total"] += 1
            changed["operation_counts"][role] = (
                changed["operation_counts"].get(role, 0) + 1
            )
            changed["pending_units"] += worst
            changed["active_attempt"] = attempt_id
            changed["prepared_ref"] = None
            row = {
                "attempt_id": attempt_id,
                "operation_id": role,
                "kind": operation["kind"],
                "phase": changed["phase"],
                "object_id": operation["object_id"],
                "case_id": case_id,
                "instance_id": operation["instance_id"],
                "model": MODELS[group][0],
                "prepared_ref": request_descriptor["prepared_ref"],
                "payload_sha256": prepared["payload_sha256"],
                "quote_ref": self.persistence_call(
                    job_id, self.store.put_object, quote
                ),
                "worst_units": worst,
                "actual_units": None,
                "input_bound": quote["input_tokens"],
                "output_bound": MODELS[group][2],
                "send_state": "RESERVED",
                "outcome": "unknown",
                "response_ref": None,
                "error": None,
                "reserved_at": now.isoformat(),
                "completed_at": None,
                "deadline": min(
                    now + timedelta(seconds=request_descriptor["timeout_seconds"]),
                    instant(changed["started_at"]) + timedelta(seconds=10800),
                    instant(changed["case_started_at"][case_id])
                    + timedelta(seconds=900)
                    if group == "flash"
                    else now + timedelta(seconds=120),
                ).isoformat(),
            }
            return row, {"attempt_id": attempt_id}

        self._action(state, "RESERVE", reserve)
        if self.before_intent:
            self.before_intent(job_id, attempt_id)
        current = self._verified(job_id)
        if current["state"] != "ACTIVE":
            row = self.persistence_call(
                job_id, self.store.get_attempt, job_id, attempt_id
            )
            row.update(send_state="CANCELLED_BEFORE_SEND", error=current["stop_reason"])
            # A cancellation is not a trusted billing settlement; keep liability.
            self._commit(current, "CANCEL_BEFORE_SEND", previous=current, attempt=row)
            raise ValueError(current["stop_reason"] or "job_not_active")

        def intent(changed, now):
            self._operation(changed, role)
            row = self.persistence_call(
                job_id, self.store.get_attempt, job_id, attempt_id
            )
            self._attempt_time(row, now)
            row["send_state"] = "SEND_INTENT"
            return row, {"attempt_id": attempt_id}

        intent_state = self._action(current, "SEND_INTENT", intent)
        try:
            result = self.runtime.send(
                prepared["payload"],
                preflight=lambda stage: self._dispatch_preflight(
                    job_id, attempt_id, prepared, stage
                ),
                timeout=max(
                    0.001,
                    (
                        instant(
                            self.persistence_call(
                                job_id, self.store.get_attempt, job_id, attempt_id
                            )["deadline"]
                        )
                        - instant(intent_state["last_at"])
                    ).total_seconds(),
                ),
            )
            return self._settle(job_id, attempt_id, result)
        except BaseException as error:
            if isinstance(error, TransportFailure) and error.response is not None:
                # A completed HTTP response survives a later cleanup failure.
                # Settle only verified usage, while keeping the job stopped.
                return self._settle(
                    job_id, attempt_id, error.response, transport_failure=error
                )
            current = self._verified(job_id)
            row = self.persistence_call(
                job_id, self.store.get_attempt, job_id, attempt_id
            )
            if row["send_state"] not in ("SETTLED", "MAY_HAVE_SENT"):
                # Exceptions never contain credential values in public state.
                row.update(
                    send_state="MAY_HAVE_SENT"
                    if row["send_state"] == "SENDING"
                    else "CANCELLED_BEFORE_SEND",
                    error=getattr(error, "original_type", type(error).__name__),
                    error_ref=self.persistence_call(
                        job_id,
                        self.store.put_object,
                        error.record()
                        if isinstance(error, TransportFailure)
                        else {
                            "type": getattr(
                                error, "original_type", type(error).__name__
                            ),
                            "message": getattr(error, "safe_message", str(error)),
                        },
                    ),
                )
                self._stop(
                    current,
                    "request_state_unresolved",
                    attempt=row,
                    detail={"original_error": row["error"]},
                )
            raise

    def _attempt_time(self, row, now):
        if now >= instant(row["deadline"]):
            raise ValueError("attempt_deadline")

    def _dispatch_preflight(self, job_id, attempt_id, prepared, stage):
        state = self._verified(job_id)
        self._active(state)

        def check(changed, now):
            row = self.persistence_call(
                job_id, self.store.get_attempt, job_id, attempt_id
            )
            if (
                changed["active_attempt"] != attempt_id
                or row["send_state"] != "SEND_INTENT"
                or row["payload_sha256"] != digest(prepared["payload"])
            ):
                raise ValueError("send_intent_unverifiable")
            plan, operation = self._operation(changed, row["operation_id"])
            self._attempt_time(row, now)
            # Price/token/identity proof is checked again immediately before each
            # privileged boundary. Its full scope must equal the reserved proof.
            quote = self.runtime.quote(plan, operation, prepared["payload"], now)
            original = self.persistence_call(
                job_id, self.store.get_object, row["quote_ref"]
            )
            if quote != original or now >= instant(quote["terms"]["valid_until"]):
                raise ValueError("billing_or_token_proof_mismatch")
            if stage == "send":
                row["send_state"] = "SENDING"
            return row, {"attempt_id": attempt_id, "boundary": stage}

        observation = {}
        checked = self._action(
            state, "DISPATCH_" + stage.upper(), check, validate=observation.update
        )
        # This sample is the last time observation before privileged IO. It
        # cannot extend the original Attempt or ignore a transient clock rollback
        # just because the prior action's fence was successfully committed.
        try:
            row = self.persistence_call(
                job_id, self.store.get_attempt, job_id, attempt_id
            )
            terms = self.persistence_call(
                job_id, self.store.get_object, row["quote_ref"]
            )["terms"]
            at = self.now()
            reason = self._time_reason(checked, at)
            if reason:
                raise ValueError(reason)
            self._attempt_time(row, at)
            if not instant(terms["valid_from"]) <= at < instant(terms["valid_until"]):
                raise ValueError("billing_price_expired")
            permission_until = observation.get("_permission_valid_until")
            if permission_until is not None and at >= instant(permission_until):
                raise ValueError("actual_model_identity_expired")
            return min(120, (instant(row["deadline"]) - at).total_seconds())
        except BaseException as error:
            current = self._verified(job_id)
            row = self.persistence_call(
                job_id, self.store.get_attempt, job_id, attempt_id
            )
            row.update(
                send_state="CANCELLED_BEFORE_SEND",
                error=row["error"] or type(error).__name__,
            )
            self._stop(
                current,
                str(error) if isinstance(error, ValueError) else type(error).__name__,
                attempt=row,
            )
            raise

    def _settle(self, job_id, attempt_id, response, *, transport_failure=None):
        current = self._verified(job_id)
        row = self.persistence_call(job_id, self.store.get_attempt, job_id, attempt_id)
        if row is None:
            raise ValueError("attempt_unknown")
        if row["response_ref"] is not None and row["response_ref"] != digest(response):
            raise ValueError("original_response_immutable")
        if row["send_state"] == "SETTLED":
            return {
                "attempt": row,
                "response": self.persistence_call(
                    job_id, self.store.get_object, row["response_ref"]
                ),
            }
        if (
            current["active_attempt"] != attempt_id
            or current["pending_units"] < row["worst_units"]
        ):
            raise ValueError("settlement_attempt_not_active")
        original_error = row.get("error")
        response_ref = self.persistence_call(job_id, self.store.put_object, response)
        row["response_ref"] = response_ref
        if transport_failure is not None:
            # Keep this gap alongside any earlier parse, billing or stop error.
            row["cleanup_error_ref"] = self.persistence_call(
                job_id, self.store.put_object, transport_failure.record()
            )
        current = self._commit(
            current,
            "RESPONSE_RECEIVED",
            previous=current,
            attempt=row,
            detail={"attempt_id": attempt_id, "response_ref": response_ref},
        )
        try:
            exact(response, "status_code raw")
            if response["status_code"] != 200:
                raise ValueError("provider_http_error:" + str(response["status_code"]))
            raw = strict_json(response["raw"].encode(), limit=4 * 1024 * 1024)
            if raw.get("model") != row["model"]:
                raise ValueError("response_model_mismatch")
            usage = raw.get("usage")
            if type(usage) is not dict:
                raise ValueError("usage_unknown")
            actual_input = integer(
                usage.get("prompt_tokens"), maximum=row["input_bound"]
            )
            actual_output = integer(
                usage.get("completion_tokens"), maximum=row["output_bound"]
            )
            if row["send_state"] not in ("SENDING", "MAY_HAVE_SENT"):
                raise ValueError("send_state_unverifiable")
            quote = self.persistence_call(
                job_id, self.store.get_object, row["quote_ref"]
            )
            settlement = self.runtime.settlement(row, response, quote)
            exact(settlement, "payload_sha256 response_sha256 actual_units evidence")
            if (
                settlement["payload_sha256"] != row["payload_sha256"]
                or settlement["response_sha256"] != digest(response)
                or not settlement["evidence"]
            ):
                raise ValueError("settlement_unverifiable")
            actual = integer(settlement["actual_units"])
            failure = "billing_bound_violated" if actual > row["worst_units"] else None
            # Validate response without repairing a malformed provider result.
            try:
                choice = raw["choices"][0]
                _decode_message(choice["message"])
                _stop_reason(choice.get("finish_reason"))
            except ValueError, KeyError, TypeError, IndexError:
                failure = failure or "invalid_response"
            ended = self.now()
            reason = self._time_reason(current, ended)
            if reason or ended >= instant(row["deadline"]):
                # A late response still settles verified charges, but cannot
                # resurrect the original run or permit another dispatch.
                failure = failure or reason or "attempt_deadline"
            failure = failure or (
                "transport_cleanup_failed" if transport_failure is not None else None
            )
            if failure:
                current = self._stop(current, failure)
            if failure == "transport_cleanup_failed":
                row.setdefault("error_ref", row["cleanup_error_ref"])
            row.update(
                send_state="SETTLED",
                outcome="aborted" if failure or original_error else "completed",
                error=original_error or failure,
                actual_units=actual,
                usage={"input_tokens": actual_input, "output_tokens": actual_output},
                completed_at=ended.isoformat(),
                settlement_ref=self.persistence_call(
                    job_id, self.store.put_object, settlement
                ),
            )
            settled = deepcopy(current)
            settled["pending_units"] -= row["worst_units"]
            settled["spent_units"] += actual
            settled["active_attempt"] = None
            self._commit(settled, "SETTLE", previous=current, attempt=row, at=ended)
            return {"attempt": row, "response": response}
        except Exception as error:
            message = (
                str(error) if isinstance(error, ValueError) else type(error).__name__
            )
            row.update(send_state="MAY_HAVE_SENT", error=original_error or message)
            self._stop(
                current,
                "request_state_unresolved",
                attempt=row,
                detail={"original_error": message},
            )
            return {"attempt": row, "response": response}

    def finish(self, job_id, role):
        state = self._verified(job_id)
        self._active(state)
        self._operation(state, role)
        if state["active_attempt"] or state["prepared_ref"]:
            raise ValueError("request_state_unresolved")

        def finished(changed, now):
            changed["finished_operations"].append(role)
            return None, {"operation_id": role}

        self._action(state, "FINISH_OPERATION", finished)
        return self.status(job_id)

    def phase_event(self, job_id, *, principal, expected_phase, next_phase, evidence):
        """Trusted driver extension; #103/#104 validate each complete phase set.

        A transition cannot replenish any count, amount, start time or operation.
        Its evidence is immutable and retained on the same anchored event chain.
        """
        if principal != self.controller:
            raise ValueError("controller_identity_required")
        state = self._verified(job_id)
        self._active(state)
        if principal != self.controller:
            raise ValueError("controller_identity_required")
        if (
            state["phase"] != expected_phase
            or next_phase not in PHASES
            or PHASES.index(next_phase) != PHASES.index(expected_phase) + 1
            or state["active_attempt"]
            or state["prepared_ref"]
        ):
            raise ValueError("phase_transition_invalid")
        if not evidence:
            raise ValueError("phase_evidence_required")

        from .controlled_calibration import validate_phase as validate_calibration_phase
        from .controlled_diagnostics import validate_phase

        validate_phase(self, state, next_phase, evidence, self.now())
        validate_calibration_phase(self, state, next_phase, evidence)

        def transition(changed, now):
            validate_phase(self, changed, next_phase, evidence, now)
            validate_calibration_phase(self, changed, next_phase, evidence)
            changed["phase"] = next_phase
            reference = self.persistence_call(job_id, self.store.put_object, evidence)
            changed["phase_facts"][expected_phase] = reference
            if next_phase == "closed":
                changed["state"] = "CLOSED"
            return None, {
                "from": expected_phase,
                "to": next_phase,
                "evidence_ref": reference,
            }

        self._action(state, "PHASE", transition)
        return self.status(job_id)

    def revoke(self, job_id, *, principal, reason="revoked"):
        if principal != self.controller:
            raise ValueError("controller_identity_required")
        state = self._verified(job_id)
        self._stop(state, reason)
        return self.status(job_id)

    def status(self, job_id):
        state = self._verified(job_id)
        return {
            "state": deepcopy(state),
            "attempts": self.persistence_call(job_id, self.store.list_attempts, job_id),
            "mode": "SYNTHETIC_ONLY"
            if type(self.runtime) is SyntheticRuntime
            else "INSTALLED",
            # A status observation cannot vouch for the next live source,
            # quote, phase and dispatch checks or serve as a capability.
            "online_executable": False,
            "release_eligible": False,
        }

    def read_object(self, identity, *, job_id=None):
        """Authenticated audit/controller use; worker receives only its result."""
        self._installed()
        return self.persistence_call(job_id, self.store.get_object, identity)


class WorkerAuthorityDispatch:
    """Worker transport surface. No controller, credentials, stores or factories."""

    __slots__ = ("__service",)

    def __init__(self, service):
        self.__service = service

    def submit_job(self, proposal_ref):
        return self.__service.submit_job(proposal_ref)

    def invoke(self, job_id, role, attempt_id, request_descriptor):
        return self.__service.invoke(job_id, role, attempt_id, request_descriptor)

    def finish(self, job_id, role):
        return self.__service.finish(job_id, role)

    def status(self, job_id):
        return self.__service.status(job_id)


class ControlledModelClient:
    __slots__ = (
        "__authority",
        "__job",
        "__operation",
        "__prepared",
        "__used",
        "__conversation",
    )

    def __init__(self, authority, job_id, operation_id, prepared, conversation_id):
        self.__authority, self.__job, self.__operation = authority, job_id, operation_id
        self.__prepared, self.__used = prepared, False
        self.__conversation = conversation_id

    def respond(self, request, *, events=None, deadline=None):
        del events
        if self.__used:
            raise ValueError("prepared_client_consumed")
        if digest(wire_payload(request)) != self.__prepared["payload_sha256"]:
            raise ValueError("prepared_request_mismatch")
        if (
            request.conversation_id != self.__conversation
            or request.model.endpoint_id != "deepseek"
        ):
            raise ValueError("prepared_request_mismatch")
        timeout = 120 if deadline is None else min(120, int(deadline - monotonic()))
        if timeout <= 0:
            raise ValueError("attempt_deadline")
        self.__used = True
        attempt_id = uuid4().hex
        request.notify_attempt_started(attempt_id, deadline)
        try:
            result = self.__authority.invoke(
                self.__job,
                self.__operation,
                attempt_id,
                {**self.__prepared, "timeout_seconds": timeout},
            )
        except BaseException as failure:
            try:
                status = self.__authority.status(self.__job)
            except BaseException as audit_failure:
                # Failure to read the ledger does not prove that no request was
                # sent. Keep the notified Attempt and original exception chain.
                error = ModelError(
                    False, None, None, attempt_id, "attempt_admission_unverifiable"
                )
                record = AttemptRecord(
                    attempt_id, False, "aborted", Usage(), error, request.model
                )
                raise ModelCallInterrupted(
                    failure, ModelResult((record,), None, error)
                ) from audit_failure
            row = next(
                (r for r in status["attempts"] if r["attempt_id"] == attempt_id), None
            )
            if row is None or row["send_state"] != "MAY_HAVE_SENT":
                raise
            error = ModelError(False, None, None, attempt_id, row["error"])
            record = AttemptRecord(
                attempt_id,
                False,
                "aborted",
                Usage(),
                error,
                ModelRef("deepseek", row["model"]),
            )
            raise ModelCallInterrupted(
                failure, ModelResult((record,), None, error)
            ) from failure
        row, response = result["attempt"], result["response"]
        model = ModelRef("deepseek", row["model"])
        usage = row.get("usage", {})
        measured = Usage(
            total_input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )
        if row["send_state"] != "SETTLED" or row["outcome"] != "completed":
            error = ModelError(
                False,
                response["status_code"],
                response["raw"],
                attempt_id,
                row["error"],
            )
            record = AttemptRecord(attempt_id, False, "aborted", measured, error, model)
            return ModelResult((record,), None, error)
        raw = strict_json(response["raw"].encode(), limit=4 * 1024 * 1024)
        choice = raw["choices"][0]
        record = AttemptRecord(attempt_id, False, "committed", measured, model=model)
        return ModelResult(
            (record,),
            ModelResponse(
                tuple(_decode_message(choice["message"])),
                _stop_reason(choice.get("finish_reason")),
                model,
                raw["model"],
            ),
            None,
        )

    def close(self):
        self.__used = True
