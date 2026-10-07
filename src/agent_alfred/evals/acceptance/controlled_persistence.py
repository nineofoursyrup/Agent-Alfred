"""Recovery of the original controlled job; never recovery of permission."""

from copy import deepcopy
from pathlib import Path
from threading import RLock
from uuid import uuid4
from weakref import WeakKeyDictionary

from .controlled.contract import ADVISORY_BUDGET, text
from .controlled.persistence import DurableExecutionAnchor, DurableExecutionStore
from .controlled.runtime import is_installed_runtime
from .controlled_execution import ControlledAuthority
from .execution_decisions import material_binding
from .materials import preflight_materials
from .schema import digest
from .store import EvidenceStore

# A live handler can witness two successive outages even if the first surviving
# domain is unavailable during the second one. These causal witnesses are copied
# into every subsequent durable fault, and never reconstructed from wall time.
# This cache is deliberately NOT copied when a replacement Authority is created.
_FAULT_WITNESSES = WeakKeyDictionary()
_HANDLER_EPOCHS = WeakKeyDictionary()
_FAULT_LOCK = RLock()
_REQUEST_REJECTIONS = frozenset(("ledger_conflict", "control_id_reused", "job_unknown"))


def _first_faults(records):
    rows = {
        digest({k: v for k, v in record.items() if k != "domain"}): record
        for record in records
    }

    def roots_for(record, seen):
        observation = record["observation"]
        roots = observation["detail"].get("first_faults")
        if roots:
            return roots
        previous = record["previous"]
        if previous in seen:
            raise ValueError("execution_fault_causality_unverifiable")
        if previous in rows:
            return roots_for(rows[previous], seen | {previous})
        return [{"identity": digest(observation), "reason": observation["reason"]}]

    values = {}
    for record in records:
        for root in roots_for(record, set()):
            if root["identity"] in values and values[root["identity"]] != root:
                raise ValueError("execution_fault_causality_unverifiable")
            values[root["identity"]] = root
    return values


def _first_reason(roots):
    if len(roots) == 1:
        return next(iter(roots.values()))["reason"]
    return "fault_order_unverifiable" if roots else None


class PersistentControlledAuthority(ControlledAuthority):
    """Same public dispatch and phase policy with durable failure barriers.

    Unprotected SQLite fixtures remain synthetic. Installed AWS and owner-trusted
    local adapters preserve this interface with their distinct trust profiles.
    A failed ledger/witness commit is never automatically repaired or skipped.
    """

    def _installed(self):
        super()._installed()
        if not isinstance(self.store, DurableExecutionStore) or not isinstance(
            self.anchor, DurableExecutionAnchor
        ):
            raise ValueError("durable_execution_backend_required")
        if is_installed_runtime(self.runtime) and (
            getattr(self.store, "synthetic_only", False)
            or getattr(self.anchor, "synthetic_only", False)
        ):
            raise ValueError("real_execution_prerequisites_unverified")

    def _handler_epoch(self):
        with _FAULT_LOCK:
            if self not in _HANDLER_EPOCHS:
                _HANDLER_EPOCHS[self] = uuid4().hex
            return _HANDLER_EPOCHS[self]

    def _active(self, state):
        super()._active(state)
        if (
            state.get("handler_epoch") != self._handler_epoch()
            or state.get("clean_handoff") is not None
        ):
            raise ValueError("handler_recovery_required")

    def persistence_call(self, job_id, operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except BaseException as error:
            if job_id is not None:
                self._remember_fault(job_id, error)
            raise

    def _remember_fault(self, job_id, error):
        if isinstance(error, ValueError) and str(error) in _REQUEST_REJECTIONS:
            return False
        text(job_id)
        # The same exception may cross a store gate, _verified and _commit.
        # Record it once, retaining the original exception as the caller's error.
        marker = (self._handler_epoch(), job_id)
        remembered = getattr(error, "_controlled_fault_jobs", {})
        if marker in remembered:
            return remembered[marker]
        with _FAULT_LOCK:
            retained = self._record_fault(job_id, error)
        try:
            error._controlled_fault_jobs = {**remembered, marker: retained}
        except Exception:
            pass  # An immutable exception must not hide its original failure.
        return retained

    def _record_fault(self, job_id, error):
        reason = (
            str(error) if isinstance(error, ValueError) else type(error).__name__
        )[:256] or "persistence_failure"
        chain, seen, current = [], set(), error
        while current is not None and id(current) not in seen and len(chain) < 8:
            seen.add(id(current))
            chain.append({"type": type(current).__name__, "message": str(current)})
            if hasattr(current, "safe_message"):
                # TransportFailure is the credential-redaction boundary. Its
                # lower-level cause may still contain the unredacted secret.
                break
            current = current.__cause__ or current.__context__
        detail = {
            "type": type(error).__name__,
            "message": str(error),
            "exception_chain": chain,
        }
        witnesses = _FAULT_WITNESSES.setdefault(self, {})
        roots = dict(witnesses.get(job_id, {}))
        for domain in (self.store, self.anchor):
            try:
                roots.update(_first_faults(domain.faults(job_id)))
            except Exception:
                continue
        if not roots:
            identity = uuid4().hex
            roots[identity] = {"identity": identity, "reason": reason[:256]}
        witnesses[job_id] = roots
        detail["first_faults"] = [roots[key] for key in sorted(roots)]
        try:
            local = self.store.get(job_id)
            detail["active_attempt"] = local["active_attempt"] if local else None
        except Exception:
            detail["active_attempt"] = None
        retained = False
        for domain in (self.store, self.anchor):
            try:
                domain.record_fault(
                    job_id, reason[:256] or "persistence_failure", detail
                )
                retained = True
            except Exception:
                # The surviving independent domain retains the barrier. If both
                # domains are unavailable, the caller still fails closed; no
                # claim that the original fault was durably recorded is made.
                continue
        return retained

    def _commit(self, state, kind, **kwargs):
        if kind == "ACTIVATE":
            state = {
                **state,
                "handler_epoch": self._handler_epoch(),
                "clean_handoff": None,
            }
        try:
            return super()._commit(state, kind, **kwargs)
        except BaseException as error:
            # Losing an ordinary CAS race is not an integrity failure. The
            # winning anchored state is still the only admissible state.
            if str(error) != "ledger_conflict":
                self._remember_fault(state["job_id"], error)
            raise

    def _verified(self, job_id):
        try:
            state = super()._verified(job_id)
            faults = self.persistence_call(
                job_id, self.anchor.faults, job_id
            ) + self.persistence_call(job_id, self.store.faults, job_id)
            roots = _first_faults(faults)
            with _FAULT_LOCK:
                roots.update(_FAULT_WITNESSES.get(self, {}).get(job_id, {}))
            if roots and state["state"] == "ACTIVE":
                state = self._stop(state, _first_reason(roots))
            return state
        except BaseException as error:
            self._remember_fault(job_id, error)
            raise

    def submit_job(self, proposal_ref):
        self._installed()
        job_id = proposal_ref["job_id"]
        # A fresh directory / missing B store cannot reuse the same A decision
        # against the original independently retained activation in C.
        if self.persistence_call(job_id, self.store.get, job_id) is not None:
            raise ValueError("job_already_submitted")
        if self.persistence_call(job_id, self.anchor.read, job_id) is not None:
            error = ValueError("original_job_ledger_required")
            self._remember_fault(job_id, error)
            raise error
        if self.persistence_call(
            job_id, self.store.faults, job_id
        ) or self.persistence_call(job_id, self.anchor.faults, job_id):
            raise ValueError("original_job_stopped")
        return super().submit_job(proposal_ref)

    def handoff(self, job_id, *, principal):
        """Release this handler once, while both domains still verify the job.

        The controller transfers the returned capability to its replacement.
        It is not persisted in cleartext and does not reset the original grant.
        """
        if principal != self.controller:
            raise ValueError("controller_identity_required")
        state = self._verified(job_id)
        self._active(state)
        if state["active_attempt"] or state["prepared_ref"]:
            raise ValueError("request_state_unresolved")
        capability = {
            "version": 1,
            "job_id": job_id,
            "previous_handler": self._handler_epoch(),
            "revision": state["revision"] + 1,
            "nonce": uuid4().hex,
        }
        from .controlled.local_runtime import LocalInstalledRuntime

        if type(self.runtime) is LocalInstalledRuntime:
            capability.update(
                version=2,
                trust_profile="owner_trusted_local",
                local_continuity=self.runtime.continuity_checkpoint(),
            )
        changed = {
            **state,
            "handler_epoch": None,
            "clean_handoff": digest(capability),
        }
        self._commit(
            changed,
            "HANDLER_RELEASED",
            previous=state,
            detail={"handoff_sha256": digest(capability)},
        )
        return capability

    def recover(self, job_id, *, principal, material_output=None, handoff=None):
        """Read the full chains and suspend any interrupted in-flight request.

        Caller must establish that this is the replacement handler. No recovered
        request is sent: existing intent can only settle, or remain conservative
        debt. A completed healthy job retains its exact budget and activation.
        """
        if principal != self.controller:
            raise ValueError("controller_identity_required")
        state = self._verified(job_id)
        try:
            self.persistence_call(job_id, self.store.read_events, job_id)
            self.persistence_call(job_id, self.anchor.read_events, job_id)
        except BaseException as error:
            self._remember_fault(job_id, error)
            raise
        if handoff is not None:
            if (
                state["state"] != "ACTIVE"
                or state.get("handler_epoch") is not None
                or state.get("clean_handoff") != digest(handoff)
                or handoff.get("job_id") != job_id
                or state["active_attempt"]
                or state["prepared_ref"]
            ):
                raise ValueError("clean_handoff_unverifiable")
            from .controlled.local_runtime import LocalInstalledRuntime

            if type(self.runtime) is LocalInstalledRuntime:
                if (
                    handoff.get("version") != 2
                    or handoff.get("trust_profile") != "owner_trusted_local"
                ):
                    raise ValueError("clean_handoff_unverifiable")
                self.persistence_call(
                    job_id, self.runtime.resume_continuity, handoff["local_continuity"]
                )
            state = self._commit(
                {
                    **state,
                    "handler_epoch": self._handler_epoch(),
                    "clean_handoff": None,
                },
                "HANDLER_ACCEPTED",
                previous=state,
                detail={"handoff_sha256": digest(handoff)},
            )
        elif (
            state["state"] == "ACTIVE"
            and state.get("handler_epoch") != self._handler_epoch()
        ):
            # No failure record is not proof of a clean previous process. Both
            # domains may have rejected all writes before that process vanished.
            state = self._stop(state, "handler_recovery_unverifiable")
        if state["active_attempt"]:
            row = self.persistence_call(
                job_id, self.store.get_attempt, job_id, state["active_attempt"]
            )
            if row["send_state"] in ("SENDING", "MAY_HAVE_SENT"):
                row["send_state"] = "MAY_HAVE_SENT"
            elif row["send_state"] in ("RESERVED", "SEND_INTENT"):
                row["send_state"] = "CANCELLED_BEFORE_SEND"
            row["error"] = row["error"] or "handler_interrupted"
            state = self._stop(
                state,
                "request_state_unresolved",
                attempt=row,
                detail={"recovered_original_attempt": row["attempt_id"]},
            )
        elif state["stop_reason"] == "time_check_pending":
            state = self._stop(state, "interrupted_time_check")
        if material_output is not None:
            self.restore_materials(job_id, material_output, principal=principal)
        return self.status(job_id)

    def status(self, job_id):
        status = super().status(job_id)
        state = status["state"]
        status["handler_access"] = (
            "EXECUTION_OWNER"
            if state["state"] == "ACTIVE"
            and state.get("handler_epoch") == self._handler_epoch()
            and state.get("clean_handoff") is None
            else "AUDIT_SETTLEMENT_ONLY"
        )
        return status

    def restore_materials(self, job_id, output, *, principal):
        """Rebuild a local material cache from the same protected object reference.

        Output must be new. The receipt, full manifest, approved ancestor and
        binding are verified again before changing the cache pointer. It cannot
        change job identity, source grant, plan, amounts, counts or started_at.
        Sealed derivatives retain their stable EvidenceStore: its paths are
        part of the approved checkpoint. A material cache is not that store.
        """
        if principal != self.controller:
            raise ValueError("controller_identity_required")
        state = self._verified(job_id)
        plan = self.persistence_call(job_id, self.store.get_object, state["plan_ref"])
        binding = self.persistence_call(
            job_id, self.store.get_object, state["binding_ref"]
        )
        try:
            report = preflight_materials(
                plan["material_ref"],
                vault=self.vault,
                allowed_objects=self.allowed_objects,
                expected_manifest_sha256=binding["package_manifest_sha256"],
                output=Path(output),
            )
            evidence = EvidenceStore(
                Path(output) / "evidence", decision_source=self.runtime.decision_source
            )
            observed = material_binding(
                evidence,
                binding["batch"]["batch_id"],
                approved_batch_id=binding["approved_batch"]["batch_id"],
                package_manifest_sha256=report["manifest_sha256"],
            )
            if observed != binding:
                raise ValueError("execution_binding_changed")

            evidence_output = str(Path(output).resolve())
            diagnostic_ref = state["phase_facts"].get("diagnostic")
            if diagnostic_ref and plan["execution_mode"] != "synthetic_capacity":
                frozen = self.read_object(diagnostic_ref, job_id=job_id)
                descriptor = frozen["evidence"]
                if descriptor is None:
                    raise ValueError("diagnostic_evidence_missing")
                evidence_output = state.get("evidence_output", state["material_output"])
                stable = EvidenceStore(
                    Path(evidence_output) / "evidence",
                    decision_source=self.runtime.decision_source,
                )
                saved = self.persistence_call(
                    job_id, stable.read, descriptor["batch_id"]
                )
                if digest(saved) != descriptor["sha256"] or descriptor["path"] != str(
                    stable.root / descriptor["batch_id"] / "batch.json"
                ):
                    raise ValueError("diagnostic_evidence_changed")
                results_id = job_id + "-first-results"
                if (stable.root / results_id).exists():
                    self.persistence_call(job_id, stable.read, results_id)

            def restore(changed, now):
                changed["material_output"] = str(Path(output).resolve())
                changed["evidence_output"] = evidence_output
                changed["material_receipt_ref"] = self.persistence_call(
                    job_id, self.store.put_object, report
                )
                self._current_permission(changed, now)
                return None, {"material_ref": plan["material_ref"], "cache_only": True}

            if state["state"] == "ACTIVE":
                self._action(state, "RESTORE_MATERIAL_VIEW", restore, permission=False)
            else:
                # Audit reconstruction does not reactivate a stopped grant.
                changed = deepcopy(state)
                changed["material_output"] = str(Path(output).resolve())
                changed["evidence_output"] = evidence_output
                changed["material_receipt_ref"] = self.persistence_call(
                    job_id, self.store.put_object, report
                )
                self._commit(changed, "RESTORE_MATERIAL_VIEW", previous=state)
        except BaseException as error:
            self._remember_fault(job_id, error)
            raise
        return self.status(job_id)

    def settle_attempt(self, job_id, attempt_id, response_ref, *, principal):
        """Trusted settlement/readback, not a model resend or a permission reset."""
        if principal != self.controller:
            raise ValueError("controller_identity_required")
        self._verified(job_id)
        return self._settle(
            job_id,
            attempt_id,
            self.persistence_call(job_id, self.store.get_object, response_ref),
        )

    def record_observation(self, job_id, observation, *, principal):
        """Append audit facts without hiding old FAILs or changing budget/clock."""
        if principal != self.controller:
            raise ValueError("controller_identity_required")
        if observation.get("kind") not in ("failure", "blocker", "resource", "cleanup"):
            raise ValueError("audit_observation_invalid")
        if observation.get("status") == "deleted_verified" and not observation.get(
            "evidence"
        ):
            raise ValueError("resource_deletion_evidence_required")
        state = self._verified(job_id)
        reference = self.persistence_call(job_id, self.store.put_object, observation)
        self._commit(
            state,
            "AUDIT_OBSERVATION",
            previous=state,
            detail={"observation_ref": reference},
        )
        return reference

    def stop_report(self, job_id):
        """Readable even when a domain is unavailable; unknown values stay unknown."""
        state, attempts, observations, faults, blockers = None, None, [], [], []
        status = None
        stops = []
        verified = False
        fault_inventory_complete = True
        try:
            status = self.status(job_id)
            state, attempts = status["state"], status["attempts"]
            verified = True
        except Exception as error:
            blockers.append(
                {"kind": "blocker", "status": "BLOCKED", "reason": str(error)}
            )
        for label, domain in (("authority", self.store), ("anchor", self.anchor)):
            try:
                faults.extend(
                    {"domain": label, **row}
                    for row in self.persistence_call(job_id, domain.faults, job_id)
                )
            except Exception as error:
                fault_inventory_complete = False
                blockers.append(
                    {
                        "kind": "blocker",
                        "status": "BLOCKED",
                        "reason": label + ":" + str(error),
                    }
                )
        try:
            if state is None:
                state = self.persistence_call(job_id, self.store.get, job_id)
                attempts = self.persistence_call(
                    job_id, self.store.list_attempts, job_id
                )
            for event in self.persistence_call(job_id, self.store.read_events, job_id):
                if event["kind"] == "STOP":
                    stops.append(
                        {"revision": event["revision"], "detail": event["detail"]}
                    )
                if event["kind"] == "AUDIT_OBSERVATION":
                    observations.append(
                        self.persistence_call(
                            job_id,
                            self.store.get_object,
                            event["detail"]["observation_ref"],
                        )
                    )
        except Exception as error:
            blockers.append(
                {"kind": "blocker", "status": "BLOCKED", "reason": str(error)}
            )
        counts = (
            {
                "reserved": 0,
                "cancelled_before_send": 0,
                "possibly_sent": 0,
                "settled": 0,
            }
            if attempts is not None
            else None
        )
        for row in attempts or []:
            key = {
                "RESERVED": "reserved",
                "SEND_INTENT": "reserved",
                "CANCELLED_BEFORE_SEND": "cancelled_before_send",
                "SENDING": "possibly_sent",
                "MAY_HAVE_SENT": "possibly_sent",
                "SETTLED": "settled",
                "RESPONSE_VERIFIED": "response_verified_liability_reserved",
            }[row["send_state"]]
            counts[key] = counts.get(key, 0) + 1
        roots = _first_faults(faults)
        with _FAULT_LOCK:
            roots.update(_FAULT_WITNESSES.get(self, {}).get(job_id, {}))
        report = {
            "contract": "V1-CONTROLLED-STOP-REPORT",
            "version": 1,
            "job_id": job_id,
            "state": state,
            "attempts": attempts,
            "ledger_and_anchor_verified": verified,
            "first_stop_reason": (state or {}).get("stop_reason")
            or (
                _first_reason(roots)
                if fault_inventory_complete
                else "fault_order_unverifiable"
            ),
            "fault_inventory_complete": fault_inventory_complete,
            "first_fault_order_verified": (
                (len(roots) == 1 if roots else None)
                if fault_inventory_complete
                else False
            ),
            "first_fault_candidates": list(roots.values()),
            "stop_events": stops,
            "original_errors": [r for r in attempts or [] if r.get("error")],
            "faults": faults,
            "observations": observations,
            "blockers": blockers,
            "send_accounting": counts,
            "spent_units": state["spent_units"] if state else None,
            "pending_units": state["pending_units"] if state else None,
            "resource_cleanup": "NOT_INFERRED_FROM_STOP",
            "offline_engineering": "FIXTURE_EVIDENCE_ONLY",
            "real_cloud_capacity": "NOT_VERIFIED",
            "real_readiness": "BLOCKED",
            "v1_release": "BLOCKED",
            "online_executable": False,
        }
        from .controlled.local_runtime import LocalInstalledRuntime

        if type(self.runtime) is LocalInstalledRuntime:
            policy = (
                status.get("billing", {}).get("budget_policy")
                if status is not None
                else None
            )
            advisory = policy == ADVISORY_BUDGET
            strict = status is not None and status.get("contract") == (
                "V1-LOCAL-CONTROLLED-STATUS"
            )
            report.update(
                contract=(
                    "V2-LOCAL-CONTROLLED-STOP-REPORT"
                    if advisory
                    else (
                        "V1-LOCAL-CONTROLLED-STOP-REPORT"
                        if strict
                        else "V1-CONTROLLED-STOP-REPORT"
                    )
                ),
                version=2 if advisory else 1,
                trust_profile="owner_trusted_local",
                independent_administrator_anchor=False,
                administrator_rollback_protection=False,
                offline_engineering="NOT_INFERRED_FROM_STOP",
                real_readiness="NOT_INFERRED_FROM_STOP",
                local_witness_verified=verified,
            )
            if advisory:
                report.update(
                    budget_policy=ADVISORY_BUDGET,
                    unsettled_planned_units=(state["pending_units"] if state else None),
                    all_attempts_reconciled=(
                        state["pending_units"] == 0
                        and attempts is not None
                        and all(row["send_state"] == "SETTLED" for row in attempts)
                        if state
                        else False
                    ),
                    final_bill_ceiling_proven=False,
                )
            elif strict:
                report["final_bill_verified"] = (
                    state["pending_units"] == 0 if state else False
                )
            else:
                report["contract"] = "LOCAL-CONTROLLED-STOP-REPORT-UNVERIFIED"
                report["version"] = None
                report["budget_policy"] = "UNVERIFIED"
            report.pop("real_cloud_capacity")
        return report
