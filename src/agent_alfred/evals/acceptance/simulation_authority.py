"""Offline protocol simulator, NEVER a production issuer or trusted host adapter.

The live object models the external trust anchor. Losing it closes the protocol;
files alone cannot recover permission. Its test secret is not a real signing key.
"""

import hashlib
import hmac
import json
import math
import os
import secrets
import threading
from copy import deepcopy
from pathlib import Path

from agent_alfred.clock import SystemClock
from agent_alfred.resource_rollback import raise_if_rollback_pending

from .admission import proposal, require_source, validate_proposal_shape
from .artifacts import opened
from .report import instant
from .schema import digest, encode


def _unsettled_product(grant):
    return any(
        row["role"] == "product"
        and row["send_state"] in ("RESERVED", "SEND_INTENT")
        and row["outcome"] == "unknown"
        for row in grant["requests"]
    )


class SimulationAuthority:
    def __init__(self, root, *, clock=None):
        self.clock = clock or SystemClock()
        self.root = Path(os.path.abspath(root))
        try:
            self.root.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            raise ValueError("authority_state_unverifiable") from None
        self._lock = threading.RLock()
        self._key = secrets.token_bytes(32)
        self._issuer = "simulation:" + secrets.token_hex(16)
        self._blocked = False
        self._owner_pid = os.getpid()
        self._anchor = None
        self._last_time = self.clock.monotonic()
        self._dispatchers = {}
        self._save({"revision": 0, "grants": {}, "quarantined": []})

    def _load(self):
        path = self.root / "state.json"
        info = path.lstat()
        if path.is_symlink() or self.root.is_symlink():
            raise ValueError("authority_state_unverifiable")
        raw = path.read_bytes()
        after = path.lstat()
        if (info.st_dev, info.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError("authority_state_unverifiable")
        identity = (info.st_dev, info.st_ino, hashlib.sha256(raw).hexdigest())
        return identity, json.loads(raw)

    def _read(self):
        try:
            identity, state = self._load()
            if (
                self._blocked
                or os.getpid() != self._owner_pid
                or identity != self._anchor
                or self.clock.monotonic() < self._last_time
            ):
                raise ValueError("authority_state_unverifiable")
            self._last_time = self.clock.monotonic()
            return state
        except OSError, ValueError, TypeError:
            self._blocked = True
            raise ValueError("authority_state_unverifiable") from None

    def _save(self, state):
        try:
            pending = self.root / "pending.json"
            with pending.open("xb") as stream:
                stream.write(encode(state))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, self.root / "state.json")
            with opened(self.root, os.O_RDONLY | os.O_DIRECTORY) as descriptor:
                os.fsync(descriptor)
            identity, _ = self._load()
            if identity[2] != hashlib.sha256(encode(state)).hexdigest():
                raise ValueError("authority_state_unverifiable")
            self._anchor = identity
        except BaseException as failure:
            self._blocked = True
            raise_if_rollback_pending(failure)
            if isinstance(failure, (OSError, ValueError)):
                raise ValueError("authority_state_unverifiable") from None
            raise

    def _signature(self, body):
        return hmac.new(self._key, encode(body), hashlib.sha256).hexdigest()

    def issue(
        self,
        request,
        *,
        subject,
        source_event_id,
        source_event_digest,
        activation_deadline,
        cost_acceptance,
    ):
        """Synthetic confirmation only; all timing and monetary inputs explicit."""
        with self._lock:
            state = self._read()
            validate_proposal_shape(request)
            if any(
                g["receipt"]["proposal_digest"] == digest(request)
                for g in state["grants"].values()
            ):
                raise ValueError("proposal_already_issued")
            limits = request["limits"]
            if (
                not subject.startswith("simulation:")
                or not source_event_id
                or len(source_event_digest) != 64
            ):
                raise ValueError("synthetic_source_required")
            if any(
                request[k] is not None
                for k in (
                    "subject",
                    "confirmed_at",
                    "cost_acceptance",
                    "receipt",
                )
            ):
                raise ValueError("proposal_not_unsigned")
            if (
                any(
                    type(limits[k]) is not int or limits[k] <= 0
                    for k in ("max_requests", "max_output_tokens")
                )
                or type(limits["total_seconds"]) not in (int, float)
                or not math.isfinite(limits["total_seconds"])
                or limits["total_seconds"] <= 0
            ):
                raise ValueError("invalid_authorization_limit")
            if (
                not request["output_scope"]
                or not request["operations"]
                or set(request["operations"]) - {"product", "judge"}
            ):
                raise ValueError("invalid_proposal_scope")
            cost = request["cost"]
            if (
                not isinstance(cost, dict)
                or not cost.get("source")
                or "amount" not in cost
                or (
                    cost["amount"] is not None
                    and (
                        type(cost["amount"]) not in (int, float)
                        or not math.isfinite(cost["amount"])
                        or cost["amount"] < 0
                    )
                )
                or not isinstance(cost_acceptance, dict)
                or set(cost_acceptance) != {"amount", "accept_unknown"}
                or cost_acceptance["amount"] != cost["amount"]
                or (
                    cost["amount"] is None
                    and cost_acceptance["accept_unknown"] is not True
                )
            ):
                raise ValueError("cost_acceptance_missing")
            now = self.clock.wall_utc()
            if instant(activation_deadline) <= now:
                raise ValueError("activation_expired")
            body = {
                "version": 2,
                "issuer": self._issuer,
                "grant_id": secrets.token_hex(16),
                "subject": subject,
                "source_event_id": source_event_id,
                "source_event_digest": source_event_digest,
                "issued_at": now.isoformat(),
                "activation_deadline": activation_deadline,
                "proposal_digest": digest(request),
                "operations": request["operations"],
                "output_scope": request["output_scope"],
                "cost_acceptance": cost_acceptance,
                "limits_digest": digest(limits),
            }
            receipt = {**deepcopy(body), "signature": self._signature(body)}
            state["grants"][body["grant_id"]] = {
                "receipt": receipt,
                "proposal": deepcopy(request),
                "state": "AUTHORIZED",
                "requests": [],
                "operations": [],
            }
            state["revision"] += 1
            self._save(state)
            return deepcopy(receipt)

    def activate(self, receipt, request):
        with self._lock:
            state = self._read()
            try:
                body = {k: v for k, v in receipt.items() if k != "signature"}
                if receipt["issuer"] != self._issuer or not hmac.compare_digest(
                    receipt["signature"], self._signature(body)
                ):
                    raise ValueError("approval_source_unverifiable")
                grant = state["grants"][receipt["grant_id"]]
                if receipt != grant["receipt"]:
                    raise ValueError("approval_source_unverifiable")
                if digest(request) != receipt["proposal_digest"]:
                    raise ValueError("approval_binding_mismatch")
                now = self.clock.wall_utc()
                if (
                    not instant(receipt["issued_at"])
                    <= now
                    < instant(receipt["activation_deadline"])
                ):
                    raise ValueError("approval_time_invalid")
                if grant["state"] != "AUTHORIZED":
                    raise ValueError("grant_already_activated")
            except KeyError, TypeError:
                raise ValueError("approval_source_unverifiable") from None
            grant.update(
                state="ACTIVE",
                budget_started_at=now.isoformat(),
                started_monotonic=self.clock.monotonic(),
                stop_reason=None,
            )
            state["revision"] += 1
            self._save(state)
            return SimulationSession(self, receipt["grant_id"])

    def submit_job(self, proposal_ref):
        """Activate only a synthetic receipt already issued by this instance."""
        if type(proposal_ref) is not dict or set(proposal_ref) != {
            "receipt",
            "proposal",
        }:
            raise ValueError("approval_source_unverifiable")
        return self.activate(proposal_ref["receipt"], proposal_ref["proposal"])

    def status(self, job_id):
        with self._lock:
            state = self._read()
            try:
                grant = state["grants"][job_id]
            except KeyError:
                raise ValueError("grant_unknown") from None
            return deepcopy(
                {
                    "job_id": job_id,
                    "trust_scope": "SYNTHETIC_ONLY",
                    "state": grant["state"],
                    "quarantined": grant["proposal"]["binding"]["candidate_id"]
                    in state["quarantined"],
                    "fee_hard_cap_verified": False,
                    "budget_started_at": grant.get("budget_started_at"),
                    "stop_reason": grant.get("stop_reason"),
                    "requests": grant["requests"],
                    "finished_operations": grant.get("finished_operations", []),
                }
            )

    def invoke(self, job_id, role, attempt_id, request_descriptor):
        """Persist reservation and SEND_INTENT; return no dispatch capability."""
        if type(request_descriptor) is not dict or set(request_descriptor) != {
            "row",
            "batch",
            "max_tokens",
        }:
            raise ValueError("invalid_request_descriptor")
        if request_descriptor["row"].get("attempt_id") != attempt_id:
            raise ValueError("attempt_identity_mismatch")
        if (
            not isinstance(attempt_id, str)
            or not attempt_id
            or not isinstance(request_descriptor["row"].get("request_digest"), str)
            or len(request_descriptor["row"]["request_digest"]) != 64
        ):
            raise ValueError("invalid_request_descriptor")
        session = SimulationSession(self, job_id)
        with self._lock:
            state = self._read()
            try:
                grant = state["grants"][job_id]
            except KeyError:
                raise ValueError("grant_unknown") from None
            prior = next(
                (r for r in grant["requests"] if r["attempt_id"] == attempt_id), None
            )
            if prior is not None:
                return {"already_recorded": True, "send_state": prior["send_state"]}
            if request_descriptor["row"].get("role") != role:
                raise ValueError("operation_not_admitted")
        session.reserve(
            request_descriptor["row"],
            batch=request_descriptor["batch"],
            max_tokens=request_descriptor["max_tokens"],
        )
        session.send_intent(attempt_id)
        return {"already_recorded": False, "send_state": "SEND_INTENT"}

    def finish(self, job_id, role):
        SimulationSession(self, job_id).finish(role)

    def quarantine(self, candidate_id):
        with self._lock:
            state = self._read()
            if candidate_id not in state["quarantined"]:
                state["quarantined"].append(candidate_id)
            state["revision"] += 1
            self._save(state)

    def revoke(self, grant_id):
        with self._lock:
            state = self._read()
            state["grants"][grant_id]["state"] = "REVOKED"
            state["revision"] += 1
            self._save(state)


class SimulationSession:
    """A handle to one authority-owned shared ledger, never an online permit."""

    def __init__(self, authority, grant_id):
        self.authority, self.grant_id = authority, grant_id

    def open_client(self, snapshot, budget, role, transport, *, _rollback):
        """Keep the raw MockTransport/SDK object in the simulator, not worker."""
        from .budget import _SimulationDispatchClient

        raw = _SimulationDispatchClient(
            snapshot, budget, role, transport, _rollback=_rollback
        )
        handle = secrets.token_hex(16)
        client = RestrictedModelClient(self, handle)
        _rollback.own(client)
        try:
            with self.authority._lock:
                self.authority._read()
                self.authority._dispatchers[handle] = (self.grant_id, role, raw)
            return client
        except BaseException as failure:
            _rollback.raise_failure(failure)

    def respond_with_client(self, handle, request, *, events=None, deadline=None):
        with self.authority._lock:
            self.authority._read()
            try:
                job_id, _, raw = self.authority._dispatchers[handle]
            except KeyError:
                raise ValueError("dispatch_handle_closed") from None
            if job_id != self.grant_id:
                raise ValueError("approval_binding_mismatch")
        return raw.respond(request, events=events, deadline=deadline)

    def close_client(self, handle):
        with self.authority._lock:
            entry = self.authority._dispatchers.get(handle)
        if entry is None:
            return
        if entry[0] != self.grant_id:
            raise ValueError("approval_binding_mismatch")
        entry[2].close()
        with self.authority._lock:
            self.authority._dispatchers.pop(handle, None)

    def snapshot(self):
        with self.authority._lock:
            return deepcopy(self.authority._read()["grants"][self.grant_id])

    def validate_binding(self, batch):
        require_source(batch)
        with self.authority._lock:
            grant = self.authority._read()["grants"][self.grant_id]
            expected = proposal(
                batch,
                output_scope=grant["proposal"]["output_scope"],
                operations=grant["proposal"]["operations"],
                nonce=grant["proposal"]["nonce"],
                fee_terms=grant["proposal"]["fee_condition"]["executor_claim"],
            )
            if expected != grant["proposal"]:
                raise ValueError("approval_binding_mismatch")
            return deepcopy(grant)

    def bind(self, batch, *, operation, output_scope):
        require_source(batch)
        with self.authority._lock:
            state = self.authority._read()
            grant = state["grants"][self.grant_id]
            expected = proposal(
                batch,
                output_scope=output_scope,
                operations=grant["proposal"]["operations"],
                nonce=grant["proposal"]["nonce"],
                fee_terms=grant["proposal"]["fee_condition"]["executor_claim"],
            )
            if expected != grant["proposal"] or operation not in expected["operations"]:
                raise ValueError("approval_binding_mismatch")
            if operation in grant["operations"]:
                raise ValueError("operation_already_consumed")
            if grant["state"] != "ACTIVE":
                raise ValueError("grant_not_active")
            self.check()
            if operation == "judge" and _unsettled_product(grant):
                raise ValueError("request_state_unresolved")
            grant["operations"].append(operation)
            state["revision"] += 1
            self.authority._save(state)

    def check(self):
        with self.authority._lock:
            state = self.authority._read()
            grant = state["grants"][self.grant_id]
            if grant["state"] != "ACTIVE":
                raise ValueError("grant_not_active")
            if grant["stop_reason"]:
                raise ValueError(grant["stop_reason"])
            if grant["proposal"]["binding"]["candidate_id"] in state["quarantined"]:
                raise ValueError("execution_authorization_invalid")
            limits = grant["proposal"]["limits"]
            if (
                self.authority.clock.monotonic() - grant["started_monotonic"]
                >= limits["total_seconds"]
            ):
                raise ValueError("batch_deadline")
            if len(grant["requests"]) >= limits["max_requests"]:
                raise ValueError("request_limit")
            if any(r["send_state"] == "RESERVED" for r in grant["requests"]):
                raise ValueError("request_state_unresolved")

    def reserve(self, row, *, batch, max_tokens):
        """Linearization point: checked durable reservation precedes dispatch."""
        with self.authority._lock:
            grant = self.validate_binding(batch)
            required = {
                "attempt_id",
                "role",
                "model",
                "started_at",
                "usage",
                "outcome",
                "request_digest",
            }
            if (
                set(row) not in (required, required | {"profile_id"})
                or row["usage"] is not None
                or row["outcome"] != "unknown"
                or (
                    "profile_id" in row
                    and (
                        not isinstance(row["profile_id"], str) or not row["profile_id"]
                    )
                )
            ):
                raise ValueError("invalid_request_descriptor")
            from .schema import judge_model

            matching = []
            for profile in batch["profiles"]:
                models = (
                    profile["product_models"]
                    if row["role"] == "product"
                    else [judge_model(batch, profile)]
                    if row["role"] == "judge"
                    else []
                )
                if any(
                    row["model"]
                    == {
                        "endpoint_id": model["endpoint_id"],
                        "model_id": model["model_id"],
                    }
                    for model in models
                ):
                    matching.append(profile)
            if "profile_id" in row:
                matching = [
                    profile
                    for profile in matching
                    if profile["id"] == row["profile_id"]
                ]
            if len(matching) != 1:
                raise ValueError("unauthorized_model")
            ceiling = grant["proposal"]["limits"]["max_output_tokens"]
            if row["role"] == "product":
                ceiling = min(
                    ceiling, matching[0]["parameters"].get("max_tokens", ceiling)
                )
            if type(max_tokens) is not int or not 0 < max_tokens <= ceiling:
                raise ValueError("output_token_limit")
            self.check()
            state = self.authority._read()
            grant = state["grants"][self.grant_id]
            if row["role"] == "judge" and _unsettled_product(grant):
                raise ValueError("request_state_unresolved")
            if any(r["attempt_id"] == row["attempt_id"] for r in grant["requests"]):
                raise ValueError("duplicate_attempt")
            if row["role"] not in grant["operations"]:
                raise ValueError("operation_not_admitted")
            if row["role"] in grant.get("finished_operations", []):
                raise ValueError("operation_already_finished")
            grant["requests"].append(
                {
                    **deepcopy(row),
                    "send_state": "RESERVED",
                }
            )
            state["revision"] += 1
            self.authority._save(state)

    def send_intent(self, attempt_id):
        """Second linearization point immediately before synthetic transport send."""
        with self.authority._lock:
            state = self.authority._read()
            grant = state["grants"][self.grant_id]
            row = next(
                (r for r in grant["requests"] if r["attempt_id"] == attempt_id), None
            )
            if row is None or row["send_state"] != "RESERVED":
                raise ValueError("send_intent_unverifiable")
            if row["request_digest"] is None:
                raise ValueError("send_intent_unverifiable")
            if grant["proposal"]["binding"]["candidate_id"] in state["quarantined"]:
                row["send_state"] = "CANCELLED_BEFORE_SEND"
                state["revision"] += 1
                self.authority._save(state)
                raise ValueError("execution_authorization_invalid")
            if grant["state"] != "ACTIVE" or row["role"] in grant.get(
                "finished_operations", []
            ):
                row["send_state"] = "CANCELLED_BEFORE_SEND"
                state["revision"] += 1
                self.authority._save(state)
                raise ValueError("grant_not_active")
            if (
                grant["stop_reason"]
                or self.authority.clock.monotonic() - grant["started_monotonic"]
                >= grant["proposal"]["limits"]["total_seconds"]
            ):
                row["send_state"] = "CANCELLED_BEFORE_SEND"
                state["revision"] += 1
                self.authority._save(state)
                raise ValueError(grant["stop_reason"] or "batch_deadline")
            row["send_state"] = "SEND_INTENT"
            state["revision"] += 1
            self.authority._save(state)

    def invoke(self, role, attempt_id, request_descriptor):
        return self.authority.invoke(
            self.grant_id, role, attempt_id, request_descriptor
        )

    def suspend_unresolved(self, attempt_id):
        """Persist a stop if a sent-intent request lost local continuation."""
        with self.authority._lock:
            state = self.authority._read()
            grant = state["grants"][self.grant_id]
            row = next(
                (r for r in grant["requests"] if r["attempt_id"] == attempt_id), None
            )
            if row is None:
                raise ValueError("unobserved_attempt")
            if row["send_state"] != "SEND_INTENT" or grant["stop_reason"]:
                return
            usage = row["usage"]
            if (
                row["outcome"] != "unknown"
                and isinstance(usage, dict)
                and usage.get("total_input_tokens") is not None
                and usage.get("output_tokens") is not None
            ):
                return
            grant["stop_reason"] = "request_state_unresolved"
            state["revision"] += 1
            self.authority._save(state)

    def record(self, rows, stop_reason):
        """Completion may settle an in-flight request after revoke; never refund."""
        with self.authority._lock:
            state = self.authority._read()
            grant = state["grants"][self.grant_id]
            known = {r["attempt_id"]: r for r in grant["requests"]}
            for row in rows:
                prior = known.get(row["attempt_id"])
                if prior is None or any(
                    row[k] != prior[k]
                    for k in (
                        "attempt_id",
                        "model",
                        "role",
                        "started_at",
                    )
                ):
                    raise ValueError("unobserved_attempt")
                if prior["outcome"] != "unknown" and prior != row:
                    raise ValueError("attempt_already_settled")
                prior.update(deepcopy(row))
                if prior["send_state"] == "SEND_INTENT" and (
                    not isinstance(prior["usage"], dict)
                    or prior["usage"].get("total_input_tokens") is None
                    or prior["usage"].get("output_tokens") is None
                ):
                    stop_reason = stop_reason or "unknown_usage"
            grant["stop_reason"] = grant["stop_reason"] or stop_reason
            state["revision"] += 1
            self.authority._save(state)

    def finish(self, operation):
        with self.authority._lock:
            state = self.authority._read()
            grant = state["grants"][self.grant_id]
            if operation not in grant["operations"]:
                raise ValueError("operation_not_admitted")
            finished = grant.setdefault("finished_operations", [])
            if operation not in finished:
                finished.append(operation)
            if (
                set(finished) == set(grant["proposal"]["operations"])
                and grant["state"] == "ACTIVE"
            ):
                grant["state"] = "CONSUMED"
            state["revision"] += 1
            self.authority._save(state)


class RestrictedModelClient:
    """Worker-visible ModelClient facade; no SDK, key, HTTP client or ticket."""

    __slots__ = ("_session", "_handle")

    def __init__(self, session, handle):
        self._session, self._handle = session, handle

    def respond(self, request, *, events=None, deadline=None):
        return self._session.respond_with_client(
            self._handle, request, events=events, deadline=deadline
        )

    def close(self):
        self._session.close_client(self._handle)
