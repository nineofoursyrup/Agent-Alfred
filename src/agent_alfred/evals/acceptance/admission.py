"""Execution source boundary. No production trust adapter is installed."""

from copy import deepcopy
from os.path import abspath
from secrets import token_hex
from typing import Protocol

from .budget import binding
from .schema import digest


class AuthorityDispatch(Protocol):
    """The worker's bounded broker surface; no reserve or send ticket escapes."""

    def submit_job(self, proposal_ref): ...

    def invoke(self, job_id, role, attempt_id, request_descriptor): ...

    def finish(self, job_id, role): ...

    def status(self, job_id): ...


class UnconfiguredAuthority:
    """Production remains closed until a separately deployed trusted broker exists."""

    def submit_job(self, proposal_ref):
        raise ValueError("approval_source_unverifiable")

    def invoke(self, job_id, role, attempt_id, request_descriptor):
        raise ValueError("approval_source_unverifiable")

    def finish(self, job_id, role):
        raise ValueError("approval_source_unverifiable")

    def status(self, job_id):
        raise ValueError("approval_source_unverifiable")


def require_source(batch, *, store=None):
    """Local declarations, hashes and simulation issuers cannot authorize online IO."""
    if batch.get("simulation") is not True:
        UnconfiguredAuthority().submit_job(None)
    from .authorization_history import assess

    try:
        status = assess(batch, store=store)
    except OSError, ValueError:
        raise ValueError("authorization_ancestry_unverifiable") from None
    if status["validity"] == "INVALID":
        raise ValueError("execution_authorization_invalid")
    if "authorization_registry_unverifiable" in status["blockers"]:
        raise ValueError("authorization_registry_unverifiable")
    if batch.get("parent"):
        raise ValueError("authorization_ancestry_unverifiable")


def proposal(batch, *, output_scope, operations, nonce=None, fee_terms=None):
    """Describe an unsigned request; never copy executor-filled consent fields."""
    if not output_scope:
        raise ValueError("invalid_proposal_scope")
    if (
        not operations
        or set(operations) - {"product", "judge"}
        or len(set(operations)) != len(operations)
    ):
        raise ValueError("invalid_proposal_scope")
    nonce = token_hex(16) if nonce is None else nonce
    if (
        not isinstance(nonce, str)
        or len(nonce) != 32
        or any(c not in "0123456789abcdef" for c in nonce)
    ):
        raise ValueError("invalid_proposal_nonce")
    auth = batch.get("authorization") or {}
    candidate = batch.get("candidate") or {}
    package = {
        name: entry
        for name, entry in candidate.get("files", {}).items()
        if name.startswith("src/agent_alfred/")
    }
    output_fields = {
        "authorization",
        "results",
        "grades",
        "adjudications",
        "reviews",
        "review_adjudications",
        "gates",
        "requests",
        "request_history",
        "budget_started_at",
        "budget_scope",
        "stop_reason",
    }
    immutable_batch = {k: v for k, v in batch.items() if k not in output_fields}
    return {
        "version": 2,
        "state": "PENDING_USER_CONFIRMATION",
        "nonce": nonce,
        "binding": binding(batch),
        "batch_input_digest": digest(immutable_batch),
        "candidate_manifest_digest": digest(candidate),
        "runtime_package_digest": digest(package),
        "output_scope": abspath(output_scope),
        "evidence_namespace": digest(
            {"batch_id": batch["batch_id"], "output_scope": abspath(output_scope)}
        ),
        "worker_identity": None,
        "operations": list(operations),
        "request_roles": ["product", "auxiliary", "judge"]
        if "product" in operations and "judge" in operations
        else (["product", "auxiliary"] if "product" in operations else ["judge"]),
        "dispatch_policy": deepcopy(batch["profiles"][0].get("execution_policy")),
        "payload_policy": {
            "source": "controlled_worker_required",
            "independent_verification": "NOT_AVAILABLE",
        },
        "model_profiles": digest(
            {"profiles": batch["profiles"], "judge_profile": batch.get("judge_profile")}
        ),
        "limits": {
            k: auth.get(k)
            for k in (
                "max_requests",
                "max_output_tokens",
                "total_seconds",
            )
        },
        "cost": deepcopy(auth.get("cost")),
        "fee_condition": {
            "mode": "hard_cap_required",
            "executor_claim": deepcopy(fee_terms),
            "broker_verified": False,
        },
        "subject": None,
        "confirmed_at": None,
        "cost_acceptance": None,
        "receipt": None,
    }


def validate_proposal_shape(request):
    """Reject executor attempts to pre-fill trust or weaken the fee boundary."""
    if (
        type(request) is not dict
        or request.get("version") != 2
        or request.get("state") != "PENDING_USER_CONFIRMATION"
        or type(request.get("fee_condition")) is not dict
        or request["fee_condition"].get("mode") != "hard_cap_required"
        or request["fee_condition"].get("broker_verified") is not False
        or request.get("worker_identity") is not None
        or request.get("payload_policy")
        != {
            "source": "controlled_worker_required",
            "independent_verification": "NOT_AVAILABLE",
        }
        or any(
            request.get(key) is not None
            for key in ("subject", "confirmed_at", "cost_acceptance", "receipt")
        )
        or not isinstance(request.get("nonce"), str)
        or len(request["nonce"]) != 32
        or any(c not in "0123456789abcdef" for c in request["nonce"])
        or any(
            not isinstance(request.get(key), str) or len(request[key]) != 64
            for key in (
                "batch_input_digest",
                "candidate_manifest_digest",
                "runtime_package_digest",
                "evidence_namespace",
                "model_profiles",
            )
        )
    ):
        raise ValueError("proposal_not_unsigned")


def admit_simulation(batch, session, transport, store, operation):
    """Admit one bound operation; budget clients own the restricted transport."""
    import httpx2 as httpx

    from .simulation_authority import SimulationSession

    require_source(batch, store=store)
    if (
        type(session) is not SimulationSession
        or type(transport) is not httpx.MockTransport
    ):
        raise ValueError("simulation_mock_transport_required")
    if store is None:
        raise ValueError("candidate_and_store_required")
    session.bind(batch, operation=operation, output_scope=store.root)
