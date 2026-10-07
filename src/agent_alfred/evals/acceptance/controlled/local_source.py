"""Current owner-authenticated decisions, separate from schema4 evidence copies.

Only the trusted host exposes the control object. Runner IPC must expose neither
record nor revoke. The append log authenticates a trusted local observation, not
an independent administrator, remote authority or the historical chat message.
"""

from copy import deepcopy
from datetime import UTC, datetime
from functools import partial
from secrets import token_hex
from types import SimpleNamespace

from agent_alfred.resource_rollback import capture_call_result

from ..execution_decisions import validate_execution_event, validate_request
from ..schema import digest
from ..supplement_decisions import instant, validate_event
from ..supplement_schema import signed
from .contract import exact, text

_LOCAL_SOURCE = object()


class LocalDecisionSource:
    def __init__(self, runtime, *, token=None):
        from .local_runtime import LocalInstalledRuntime

        if token is not _LOCAL_SOURCE or type(runtime) is not LocalInstalledRuntime:
            raise ValueError("trusted_local_installation_required")
        self._runtime = runtime

    def authorize(self, binding, subject):
        self._runtime.verify_installation()
        if binding["simulation"] or subject != self._runtime.config["subject"]:
            raise ValueError("approval_source_unverifiable")

    def _record(self, source_ref):
        self._runtime.verify_installation()
        backend = self._runtime._decisions
        record = backend.read("DECISION", text(source_ref))
        if record is None or backend.read("REVOKED", source_ref) is not None:
            raise ValueError("approval_source_unverifiable")
        exact(record, "contract version source_ref subject challenge receipt event")
        if (
            record["contract"] != "V1-LOCAL-OWNER-DECISION"
            or record["version"] != 1
            or record["source_ref"] != source_ref
            or record["subject"] != self._runtime.config["subject"]
            or record["receipt"]["request_sha256"] != digest(record["challenge"])
            or record["receipt"]["owner_uid"] != self._runtime.config["owner_uid"]
            or record["receipt"]["approved"] is not True
            or record["receipt"]["authentication"] != "deviceOwnerAuthentication"
            or instant(record["receipt"]["authenticated_at"]) > datetime.now(UTC)
        ):
            raise ValueError("approval_source_unverifiable")
        challenge = record["challenge"]
        exact(
            challenge,
            "contract version installation_id source_ref request "
            "decision reason evidence carry_forward",
        )
        if (
            challenge["contract"] != "V1-LOCAL-OWNER-DECISION"
            or challenge["version"] != 1
            or challenge["installation_id"] != self._runtime.config["installation_id"]
            or challenge["source_ref"] != source_ref
            or not instant(record["receipt"]["authenticated_at"])
            < instant(record["receipt"]["expires_at"])
        ):
            raise ValueError("approval_source_unverifiable")
        expected = self._runtime.decision_control._event(
            challenge["request"],
            challenge["decision"],
            challenge["reason"],
            challenge["evidence"],
            source_ref,
            record["receipt"]["authenticated_at"],
        )
        if record["event"] != expected:
            raise ValueError("approval_source_unverifiable")
        return record

    def read_decision(self, source_ref):
        record = self._record(source_ref)
        event = record["event"]
        if event is None or event["source_ref"] != source_ref:
            raise ValueError("approval_source_unverifiable")
        return deepcopy(event)

    def read_scoped_decision(self, request):
        self._runtime.verify_installation()
        head = self._runtime._decisions.read("SCOPED", digest(request))
        if head is None:
            raise ValueError("approval_source_unverifiable")
        record = self._record(head["source_ref"])
        if record["challenge"]["request"] != request:
            raise ValueError("approval_source_unverifiable")
        return deepcopy(record["event"])

    def verify_owner_record(self, source_ref, request):
        """For local installation/billing review, never an execution grant."""
        record = self._record(source_ref)
        if (
            record["challenge"]["request"] != request
            or record["challenge"]["decision"] != "approved"
        ):
            raise ValueError("local_owner_review_unverifiable")
        return deepcopy(record)


class LocalDecisionControl:
    """Authenticated control plane, deliberately absent from any runner facade."""

    def __init__(self, runtime, *, token=None):
        from .local_runtime import LocalInstalledRuntime

        if token is not _LOCAL_SOURCE or type(runtime) is not LocalInstalledRuntime:
            raise ValueError("trusted_local_installation_required")
        self._runtime = runtime

    def record(self, request, *, decision, reason, evidence, carry_forward=None):
        """Record an authenticated decision, retaining failed adoption for audit.

        A returned helper receipt remains on a caught exception as
        ``local_owner_auth_audit`` if later verification or persistence fails.
        Callers must seal that audit separately; it is not a source record or
        evidence of commit/rollback. This covers catchable failures only, not
        arbitrary process death before the caller's durable write.
        """
        from .native import authorize_owner

        runtime = self._runtime
        runtime.verify_installation()
        text(reason)
        if type(evidence) is not list or not evidence:
            raise ValueError("decision_evidence_required")
        for reference in evidence:
            text(reference)
        if carry_forward is not None:
            exact(carry_forward, "original_record original_event_verified")
            text(carry_forward["original_record"])
            if carry_forward["original_event_verified"] is not False:
                raise ValueError("historical_approval_not_authenticated")
        source_ref = (
            "local-owner:" + runtime.config["installation_id"] + ":" + token_hex(24)
        )
        challenge = {
            "contract": "V1-LOCAL-OWNER-DECISION",
            "version": 1,
            "installation_id": runtime.config["installation_id"],
            "source_ref": source_ref,
            "request": deepcopy(request),
            "decision": decision,
            "reason": reason,
            "evidence": deepcopy(evidence),
            "carry_forward": deepcopy(carry_forward),
        }
        # Validate before prompting. The helper supplies the actual current time
        # and uid; caller supplied identity/time is never part of this API.
        event = self._event(
            request,
            decision,
            reason,
            evidence,
            source_ref,
            datetime.now(UTC).isoformat(),
        )
        pending = SimpleNamespace(receipt=None)
        try:
            capture_call_result(
                pending, "receipt",
                partial(
                    authorize_owner, runtime.config["owner_helper"], challenge,
                    reason=reason,
                ),
            )
            receipt = pending.receipt
            runtime.verify_installation()
            if receipt["owner_uid"] != runtime.config["owner_uid"]:
                raise ValueError("local_owner_identity_mismatch")
            if event is not None:
                event = self._event(
                    request,
                    decision,
                    reason,
                    evidence,
                    source_ref,
                    receipt["authenticated_at"],
                )
            record = {
                "contract": "V1-LOCAL-OWNER-DECISION",
                "version": 1,
                "source_ref": source_ref,
                "subject": runtime.config["subject"],
                "challenge": challenge,
                "receipt": receipt,
                "event": event,
            }
            backend = runtime._decisions
            before = backend.read("SCOPED", digest(request))
            backend.transaction(
                [
                    ("DECISION", source_ref, record, None),
                    (
                        "SCOPED",
                        digest(request),
                        {"source_ref": source_ref},
                        digest(before) if before else None,
                    ),
                ]
            )
            return deepcopy(record)
        except BaseException as error:
            if pending.receipt is not None:
                error.local_owner_auth_audit = {
                    "classification": "AUDIT_ONLY",
                    "admission_status": "UNCONFIRMED",
                    "challenge": challenge,
                    "receipt": pending.receipt,
                }
            raise

    def _event(self, request, decision, reason, evidence, source_ref, at):
        base = {
            "version": 1,
            "subject": self._runtime.config["subject"],
            "source_ref": source_ref,
            "at": at,
            "decision": decision,
            "reason": reason,
            "evidence": deepcopy(evidence),
        }
        if type(request) is not dict:
            raise ValueError("local_owner_request_invalid")
        if request.get("object_type") in ("summary", "dispute"):
            exact(request, "object_type object_id object_sha256 manifest")
            event = signed({**base, "kind": "user_decision", **request})
            validate_event(event)
            return event
        if request.get("scope") in ("run", "checkpoint", "material_dispute"):
            validate_request(request)
            event = signed(
                {**base, "kind": "execution_decision", "request": deepcopy(request)}
            )
            validate_execution_event(event)
            return event
        exact(request, "scope installation_id object_sha256")
        if (
            request["scope"]
            not in (
                "local_readiness",
                "local_billing_bound",
                "local_advisory_budget",
                "local_final_charge",
                "local_revocation",
            )
            or request["installation_id"] != self._runtime.config["installation_id"]
            or decision not in ("approved", "rejected")
        ):
            raise ValueError("local_owner_request_invalid")
        from ..schema import hash_value

        hash_value(request["object_sha256"])
        return None

    def revoke(self, source_ref, *, reason):
        runtime = self._runtime
        original = runtime.decision_source._record(source_ref)
        request = {
            "scope": "local_revocation",
            "installation_id": runtime.config["installation_id"],
            "object_sha256": digest(original),
        }
        record = self.record(
            request, decision="approved", reason=reason, evidence=[source_ref]
        )
        runtime._decisions.transaction(
            [("REVOKED", source_ref, {"revocation_ref": record["source_ref"]}, None)]
        )
        return record
