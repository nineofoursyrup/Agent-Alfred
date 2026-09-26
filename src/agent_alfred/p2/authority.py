"""Account B Authority: conditional grant ledger, independent anchor, fixed dispatch.

This is a P2 synthetic service. It has no provider credential and cannot turn
the ordinary acceptance production gate on by being imported or deployed.
"""

import base64
import secrets
from copy import deepcopy
from datetime import timedelta

from agent_alfred.evals.acceptance.admission import (
    proposal as p1_proposal,
)
from agent_alfred.evals.acceptance.admission import (
    validate_proposal_shape,
)

from .common import (
    ZERO_DIGEST,
    BoundaryError,
    check_id,
    digest,
    encode,
    exact,
    parse_utc,
    utc_now,
)

CANARY_ENDPOINT = "p2-private-canary"
MAX_REQUESTS = 16
MAX_OUTPUT_TOKENS = 128
MAX_GRANT_SECONDS = 3600


def validate_submission(payload):
    exact(payload, {"proposal", "batch"})
    proposed, batch = payload["proposal"], payload["batch"]
    validate_proposal_shape(proposed)
    if type(batch) is not dict or batch.get("simulation") is not True:
        raise BoundaryError("p2_synthetic_batch_required")
    if batch.get("phase") != "offline_fixture" or batch.get("parent") is not None:
        raise BoundaryError("p2_synthetic_batch_required")
    cost = proposed.get("cost")
    limits = proposed.get("limits")
    if (
        type(cost) is not dict
        or cost.get("amount") != 0
        or type(limits) is not dict
        or type(limits.get("max_requests")) is not int
        or not 0 < limits["max_requests"] <= MAX_REQUESTS
        or type(limits.get("max_output_tokens")) is not int
        or not 0 < limits["max_output_tokens"] <= MAX_OUTPUT_TOKENS
        or type(limits.get("total_seconds")) not in (int, float)
        or not 0 < limits["total_seconds"] <= MAX_GRANT_SECONDS
        or proposed.get("fee_condition", {}).get("broker_verified") is not False
    ):
        raise BoundaryError("p2_zero_cost_canary_required")
    profiles = batch.get("profiles")
    if type(profiles) is not list or not profiles:
        raise BoundaryError("p2_canary_profile_required")
    for profile in profiles:
        if type(profile) is not dict:
            raise BoundaryError("p2_canary_profile_required")
        models = [*profile.get("product_models", []), profile.get("judge_model")]
        if any(
            type(model) is not dict
            or model.get("endpoint_id") != CANARY_ENDPOINT
            or not isinstance(model.get("model_id"), str)
            or not model["model_id"].startswith("p2-canary-")
            for model in models
        ):
            raise BoundaryError("p2_canary_profile_required")
    try:
        expected = p1_proposal(
            batch,
            output_scope=proposed["output_scope"],
            operations=proposed["operations"],
            nonce=proposed["nonce"],
            fee_terms=proposed["fee_condition"]["executor_claim"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise BoundaryError("p2_proposal_invalid") from error
    if proposed != expected:
        raise BoundaryError("p2_proposal_binding_mismatch")
    return proposed, batch


def validate_descriptor(batch, proposed, role, descriptor):
    exact(descriptor, {"profile_id", "endpoint_id", "model_id", "max_tokens", "body"})
    if role not in proposed["request_roles"]:
        raise BoundaryError("operation_not_admitted")
    operation = "judge" if role == "judge" else "product"
    if operation not in proposed["operations"]:
        raise BoundaryError("operation_not_admitted")
    profile = next(
        (item for item in batch["profiles"] if item["id"] == descriptor["profile_id"]),
        None,
    )
    if profile is None:
        raise BoundaryError("approval_profile_mismatch")
    models = [profile["judge_model"]] if role == "judge" else profile["product_models"]
    if not any(
        item["endpoint_id"] == descriptor["endpoint_id"] == CANARY_ENDPOINT
        and item["model_id"] == descriptor["model_id"]
        for item in models
    ):
        raise BoundaryError("approval_profile_mismatch")
    limit = descriptor["max_tokens"]
    if (
        type(limit) is not int
        or not 0 < limit <= proposed["limits"]["max_output_tokens"]
    ):
        raise BoundaryError("output_token_limit")
    exact(descriptor["body"], {"input"})
    user_text = descriptor["body"]["input"]
    if not isinstance(user_text, str) or not 0 < len(user_text) <= 4096:
        raise BoundaryError("p2_canary_input_invalid")
    return digest(descriptor)


class AwsIssuerVerifier:
    def __init__(self, kms, fixed_key_arn):
        self.kms, self.fixed_key_arn = kms, fixed_key_arn

    def verify(self, body, signature):
        try:
            raw = base64.b64decode(signature, validate=True)
            result = self.kms.verify(
                KeyId=self.fixed_key_arn,
                Message=encode(body),
                MessageType="RAW",
                Signature=raw,
                SigningAlgorithm="ECDSA_SHA_256",
            )
        except ValueError, TypeError:
            return False
        return (
            result.get("SignatureValid") is True
            and result.get("KeyId") == self.fixed_key_arn
        )


class LambdaAnchorClient:
    def __init__(self, client, function_arn):
        self.client, self.function_arn = client, function_arn

    def read(self, job_id):
        from .common import lambda_json

        return lambda_json(
            self.client,
            self.function_arn,
            {
                "action": "read",
                "job_id": job_id,
            },
        )

    def commit(self, event):
        from .common import lambda_json

        return lambda_json(
            self.client,
            self.function_arn,
            {
                "action": "commit",
                "event": event,
            },
        )


class LambdaDispatchClient:
    def __init__(self, client, function_arn):
        self.client, self.function_arn = client, function_arn

    def send(self, payload):
        from .common import lambda_json

        return lambda_json(self.client, self.function_arn, payload)


class Authority:
    def __init__(
        self,
        store,
        anchor,
        dispatch,
        verifier,
        *,
        now=utc_now,
        before_intent=None,
        worker_principal=None,
    ):
        self.store, self.anchor, self.dispatch = store, anchor, dispatch
        self.verifier, self.now = verifier, now
        self.before_intent = (
            before_intent  # fault/race seam; absent in deployed handler
        )
        self.worker_principal = worker_principal

    def _verified(self, job_id):
        state = self.store.get(check_id(job_id))
        if state is None:
            raise BoundaryError("grant_unknown")
        high = self.anchor.read(job_id)
        if high != {
            "job_id": job_id,
            "revision": state["revision"],
            "digest": state["event_digest"],
        }:
            raise BoundaryError("authority_anchor_mismatch")
        return state

    def _commit(self, state, kind, *, previous=None, at=None):
        old_rev = previous["revision"] if previous else 0
        old_digest = previous["event_digest"] if previous else ZERO_DIGEST
        state["revision"] = old_rev + 1
        event = {
            "job_id": state["job_id"],
            "revision": state["revision"],
            "kind": kind,
            # The write-ahead time fence must precede even reading the clock.
            "at": None if kind == "TIME_CHECK" else (at or self.now()).isoformat(),
            "previous_digest": old_digest,
            "state_digest": digest(
                {k: v for k, v in state.items() if k != "event_digest"}
            ),
        }
        state["event_digest"] = digest(event)
        self.store.put(state, event, old_revision=old_rev, old_digest=old_digest)
        anchor_event = {
            "job_id": state["job_id"],
            "revision": state["revision"],
            "previous_digest": old_digest,
            "event_digest": state["event_digest"],
        }
        self.anchor.commit(anchor_event)
        high = self.anchor.read(state["job_id"])
        if high != {
            "job_id": state["job_id"],
            "revision": state["revision"],
            "digest": state["event_digest"],
        }:
            raise BoundaryError("authority_anchor_mismatch")
        print(
            encode(
                {
                    "p2_audit": "state_commit",
                    "job_id": state["job_id"],
                    "revision": state["revision"],
                    "kind": kind,
                    "event_digest": state["event_digest"],
                }
            ).decode()
        )
        return state

    def submit_job(self, payload, principal):
        proposed, batch = validate_submission(payload)
        if not principal or not self.worker_principal:
            raise BoundaryError("worker_identity_unverifiable")
        job_id = secrets.token_hex(16)
        state = {
            "job_id": job_id,
            "revision": 0,
            "event_digest": ZERO_DIGEST,
            "state": "PENDING",
            "proposal": deepcopy(proposed),
            "batch": deepcopy(batch),
            "worker_principal": self.worker_principal,
            "submitter_principal": principal,
            "issuer_event_id": None,
            "activation_deadline": None,
            "budget_started_at": None,
            "requests": [],
            "finished_operations": [],
            "stop_reason": None,
        }
        self._commit(state, "PROPOSAL")
        return {"job_id": job_id, "state": "PENDING_USER_CONFIRMATION"}

    def proposal_view(self, job_id):
        state = self._verified(job_id)
        return {
            "job_id": job_id,
            "state": state["state"],
            "proposal": state["proposal"],
            "batch": state["batch"],
        }

    def issuer_event(self, envelope):
        exact(envelope, {"body", "signature"})
        body = exact(
            envelope["body"],
            {
                "version",
                "kind",
                "job_id",
                "proposal_digest",
                "event_id",
                "nonce",
                "issued_at",
                "activation_deadline",
                "subject",
                "fee_cap",
            },
        )
        if body["version"] != 1 or body["kind"] not in ("ISSUE", "REVOKE"):
            raise BoundaryError("issuer_event_invalid")
        job_id = check_id(body["job_id"])
        check_id(body["event_id"])
        if not self.verifier.verify(body, envelope["signature"]):
            raise BoundaryError("issuer_signature_unverifiable")
        now = self.now()
        issued_at = parse_utc(body["issued_at"])
        if issued_at > now + timedelta(seconds=300):
            raise BoundaryError("issuer_event_time_invalid")
        deadline = parse_utc(body["activation_deadline"])
        if body["kind"] == "ISSUE" and (
            (now - issued_at).total_seconds() > 300
            or not now < deadline <= now + timedelta(seconds=MAX_GRANT_SECONDS)
        ):
            raise BoundaryError("issuer_event_time_invalid")
        if body["fee_cap"] != {"currency": "USD", "amount": "0", "unknown": False}:
            raise BoundaryError("p2_zero_cost_canary_required")
        state = self._verified(job_id)
        if (
            digest(state["proposal"]) != body["proposal_digest"]
            or state["proposal"]["nonce"] != body["nonce"]
            or not isinstance(body["subject"], str)
            or not body["subject"].startswith("synthetic:")
            or state["issuer_event_id"] is not None
            and state["issuer_event_id"] == body["event_id"]
        ):
            raise BoundaryError("issuer_event_binding_mismatch")
        next_state = deepcopy(state)
        if body["kind"] == "ISSUE" and state["state"] == "PENDING":
            next_state["state"] = "ACTIVE"
            next_state["activation_deadline"] = deadline.isoformat()
            next_state["budget_started_at"] = now.isoformat()
        elif body["kind"] == "REVOKE" and state["state"] in (
            "ACTIVE",
            "PENDING",
            "SUSPENDED",
        ):
            next_state["state"] = "REVOKED"
        else:
            raise BoundaryError("issuer_event_state_invalid")
        next_state["issuer_event_id"] = body["event_id"]
        self._commit(next_state, body["kind"], previous=state)
        return {"job_id": job_id, "state": next_state["state"]}

    def _active(self, state):
        if state["state"] != "ACTIVE" or state["stop_reason"]:
            raise BoundaryError("grant_not_active")

    def _time_stop_reason(self, state, now):
        started_at = parse_utc(state.get("budget_started_at"))
        if now < started_at:
            return "authority_clock_unverifiable"
        budget_deadline = started_at + timedelta(
            seconds=state["proposal"]["limits"]["total_seconds"]
        )
        if now >= min(parse_utc(state["activation_deadline"]), budget_deadline):
            return "batch_deadline"
        return None

    def _commit_after_time_check(self, state, kind, *, previous):
        # The fence still guards this sample. Validate and record that same
        # sample; a fresh unchecked timestamp must never clear the fence.
        now = self.now()
        reason = self._time_stop_reason(previous, now)
        if reason:
            stopped = deepcopy(previous)
            stopped.update(state="SUSPENDED", stop_reason=reason)
            self._commit(stopped, "TIME_STOP", previous=previous, at=now)
            raise BoundaryError(reason)
        return self._commit(state, kind, previous=previous, at=now)

    def _check_time(self, state):
        self._active(state)
        # Fence admission in both domains before observing time. A failed stop
        # write or crash cannot leave an unfenced ACTIVE grant. Only this call
        # can clear its exact fence revision after a good check, together with
        # the caller's next ledger action; a new call cannot resume the fence.
        fenced = deepcopy(state)
        fenced.update(state="SUSPENDED", stop_reason="time_check_pending")
        self._commit(fenced, "TIME_CHECK", previous=state)
        now = self.now()
        reason = self._time_stop_reason(fenced, now)
        checked = deepcopy(fenced)
        if reason:
            checked["stop_reason"] = reason
            self._commit(checked, "TIME_STOP", previous=fenced, at=now)
            raise BoundaryError(reason)
        checked.update(state="ACTIVE", stop_reason=None)
        # Do not persist an idle ACTIVE interval before RESERVE/SEND_INTENT:
        # if that write commits but its ACK is lost, the occupied attempt must
        # still block a new request. The fence's revision/digest remain the CAS.
        return checked

    def _operation_open(self, state, operation):
        operations = state["proposal"]["operations"]
        finished = state["finished_operations"]
        if operation not in operations:
            raise BoundaryError("operation_not_admitted")
        if operation in finished:
            raise BoundaryError("operation_already_finished")
        if (
            operation == "judge"
            and "product" in operations
            and "product" not in finished
        ):
            raise BoundaryError("operation_order_invalid")

    def _request_role_open(self, state, role):
        if role not in state["proposal"]["request_roles"]:
            raise BoundaryError("operation_not_admitted")
        # Auxiliary requests share the product operation's completion boundary.
        self._operation_open(state, "judge" if role == "judge" else "product")
        if role == "auxiliary" and not any(
            r["role"] == "product" and r["send_state"] == "SETTLED"
            for r in state["requests"]
        ):
            raise BoundaryError("operation_order_invalid")
        if role == "product" and any(
            r["role"] == "auxiliary" for r in state["requests"]
        ):
            raise BoundaryError("operation_order_invalid")

    def preflight(self, payload):
        exact(payload, {"job_id", "attempt_id", "request_digest"})
        state = self._verified(payload["job_id"])
        self._active(state)
        attempt = next(
            (r for r in state["requests"] if r["attempt_id"] == payload["attempt_id"]),
            None,
        )
        if (
            attempt is None
            or attempt["send_state"] != "SEND_INTENT"
            or attempt["request_digest"] != payload["request_digest"]
        ):
            raise BoundaryError("send_intent_unverifiable")
        self._request_role_open(state, attempt["role"])
        state = self._check_time(state)
        state = self._commit_after_time_check(
            deepcopy(state), "PREFLIGHT", previous=state
        )
        return {"allowed": True, "revision": state["revision"]}

    def invoke(self, job_id, role, attempt_id, descriptor, principal):
        check_id(attempt_id)
        state = self._verified(job_id)
        if principal != state["worker_principal"]:
            raise BoundaryError("worker_identity_mismatch")
        self._active(state)
        self._request_role_open(state, role)
        request_digest = validate_descriptor(
            state["batch"], state["proposal"], role, descriptor
        )
        if len(state["requests"]) >= state["proposal"]["limits"]["max_requests"]:
            raise BoundaryError("request_limit")
        if any(r["attempt_id"] == attempt_id for r in state["requests"]):
            raise BoundaryError("duplicate_attempt")
        if any(
            r["send_state"] in ("RESERVED", "SEND_INTENT", "MAY_HAVE_SENT")
            for r in state["requests"]
        ):
            raise BoundaryError("request_state_unresolved")
        state = self._check_time(state)
        reserved = deepcopy(state)
        reserved["requests"].append(
            {
                "attempt_id": attempt_id,
                "role": role,
                "request_digest": request_digest,
                "send_state": "RESERVED",
                "usage": None,
                "outcome": "unknown",
            }
        )
        self._commit_after_time_check(reserved, "RESERVE", previous=state)
        if self.before_intent:
            self.before_intent(job_id, attempt_id)
        current = self._verified(job_id)
        if current["state"] != "ACTIVE":
            cancelled = deepcopy(current)
            cancelled["requests"][-1]["send_state"] = "CANCELLED_BEFORE_SEND"
            self._commit(cancelled, "CANCEL_BEFORE_SEND", previous=current)
            raise BoundaryError("grant_not_active")
        self._request_role_open(current, role)
        current = self._check_time(current)
        intent = deepcopy(current)
        intent["requests"][-1]["send_state"] = "SEND_INTENT"
        self._commit_after_time_check(intent, "SEND_INTENT", previous=current)
        payload = {
            "job_id": job_id,
            "attempt_id": attempt_id,
            "role": role,
            "request_digest": request_digest,
            "descriptor": descriptor,
        }
        try:
            result = self.dispatch.send(payload)
            exact(result, {"kind", "attempt_id", "request_digest", "usage", "output"})
            if (
                result["kind"] != "P2_CANARY_RESULT"
                or result["attempt_id"] != attempt_id
                or result["request_digest"] != request_digest
                or type(result["usage"]) is not dict
                or set(result["usage"]) != {"input_tokens", "output_tokens"}
                or any(type(v) is not int or v < 0 for v in result["usage"].values())
            ):
                raise BoundaryError("dispatch_result_unverifiable")
        except Exception as error:
            self._suspend_unknown(job_id, attempt_id)
            raise BoundaryError("request_state_unresolved") from error
        current = self._verified(job_id)
        settled = deepcopy(current)
        attempt = next(r for r in settled["requests"] if r["attempt_id"] == attempt_id)
        if attempt["send_state"] != "SEND_INTENT":
            raise BoundaryError("request_state_unresolved")
        attempt.update(send_state="SETTLED", usage=result["usage"], outcome="completed")
        self._commit(settled, "SETTLE", previous=current)
        return result

    def _suspend_unknown(self, job_id, attempt_id):
        # If C is unavailable, even recording SUSPENDED may be impossible. The
        # unanchored/mismatched ledger itself then blocks every new action.
        try:
            current = self._verified(job_id)
            stopped = deepcopy(current)
            attempt = next(
                r for r in stopped["requests"] if r["attempt_id"] == attempt_id
            )
            if attempt["send_state"] == "SEND_INTENT":
                attempt["send_state"] = "MAY_HAVE_SENT"
                if current["state"] == "ACTIVE":
                    stopped["state"] = "SUSPENDED"
                stopped["stop_reason"] = (
                    current["stop_reason"] or "request_state_unresolved"
                )
                self._commit(stopped, "UNKNOWN_SEND", previous=current)
        except Exception:
            pass

    def finish(self, job_id, role, principal):
        state = self._verified(job_id)
        if principal != state["worker_principal"]:
            raise BoundaryError("worker_identity_mismatch")
        self._active(state)
        self._operation_open(state, role)
        if any(r["send_state"] != "SETTLED" for r in state["requests"]):
            raise BoundaryError("request_state_unresolved")
        state = self._check_time(state)
        finished = deepcopy(state)
        finished["finished_operations"].append(role)
        if set(finished["finished_operations"]) == set(state["proposal"]["operations"]):
            finished["state"] = "CONSUMED"
        self._commit_after_time_check(finished, "FINISH", previous=state)
        return {"job_id": job_id, "state": finished["state"]}

    def status(self, job_id, principal):
        state = self._verified(job_id)
        if principal not in (state["worker_principal"], state["submitter_principal"]):
            raise BoundaryError("worker_identity_mismatch")
        return {
            "job_id": job_id,
            "state": state["state"],
            "revision": state["revision"],
            "stop_reason": state["stop_reason"],
            "requests": deepcopy(state["requests"]),
            "fee_hard_cap_verified": False,
            "trust_scope": "P2_SYNTHETIC_ONLY",
        }
