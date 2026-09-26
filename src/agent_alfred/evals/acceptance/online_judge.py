"""Explicit independent judge batch; no product tools or assignments are exposed."""

from copy import deepcopy

from agent_alfred.endpoints import list_endpoints
from agent_alfred.model import ClientSnapshot, ModelAssignment

from .budget import AuthorizedBatch, validate_execution_materials
from .execution_policy import scoped_credentials
from .judge import judge_result
from .safety import credential_secrets, ensure_safe
from .schema import judge_model


def grade_batch(
    original,
    authorization,
    *,
    factory_builder=None,
    http_client=None,
    clock=None,
    candidate_root=None,
    store=None,
    new_batch=None,
    simulation_session=None,
    mock_transport=None,
):
    from .admission import admit_simulation, require_source

    require_source(original, store=store)
    options = dict(
        clock=clock, candidate_root=candidate_root, store=store, new_batch=new_batch
    )
    if simulation_session is not None or mock_transport is not None:
        if factory_builder is not None or http_client is not None:
            raise ValueError("ambiguous_transport_injection")
        bound = {**original, "authorization": authorization}
        admit_simulation(bound, simulation_session, mock_transport, store, "judge")
        result = _grade_batch(
            original,
            authorization,
            simulation_session=simulation_session,
            mock_transport=mock_transport,
            **options,
        )
        simulation_session.finish("judge")
        return result
    raise ValueError("simulation_session_and_mock_required")


def _grade_batch(
    original,
    authorization,
    *,
    clock=None,
    candidate_root=None,
    store=None,
    new_batch=None,
    simulation_session=None,
    mock_transport=None,
):
    batch = deepcopy(original)
    batch["authorization"] = authorization
    budget = AuthorizedBatch(
        batch, clock=clock, session=simulation_session, mock_transport=mock_transport
    )
    if original["grades"]:
        raise ValueError("existing_grades_require_new_regrade_batch")
    credentials = scoped_credentials(batch, {}).values()
    ensure_safe(batch, secrets=credential_secrets(credentials))
    from .candidate import verify, verify_runtime

    if candidate_root is None or store is None or new_batch is None:
        raise ValueError("candidate_and_store_required")
    if not verify(batch["candidate"], candidate_root):
        raise ValueError("candidate_changed")
    if not verify_runtime(batch["candidate"]):
        raise ValueError("runtime_candidate_mismatch")
    from .judge_protocol import descriptor, protocol

    if protocol(batch) != descriptor():
        raise ValueError("judge_protocol_required")
    validate_execution_materials(batch, store, started_at=budget.started_at)
    budget.journal = store.reserve_execution(
        original["batch_id"], "judge", output_batch=new_batch
    )
    budget.journal("manifest", batch)
    try:
        for result in batch["results"]:
            try:
                budget.check()
            except ValueError:
                batch["stop_reason"] = budget.stop_reason or "batch_budget_exhausted"
                break
            profile = next(
                p for p in batch["profiles"] if p["id"] == result["profile_id"]
            )
            model = judge_model(batch, profile)
            endpoint = next(
                e for e in list_endpoints() if e.endpoint_id == model["endpoint_id"]
            )
            snapshot = ClientSnapshot(
                config_version=profile["id"],
                primary=ModelAssignment(
                    model["endpoint_id"], model["model_id"], model["wire_style"]
                ),
                retrieval_gate=None,
                api_key=credentials.get(endpoint.api_key_env),
                stream=False,
                stream_fallback=False,
                overall_deadline_s=authorization["total_seconds"],
                per_attempt_timeout_s=min(
                    authorization["total_seconds"],
                    profile["parameters"].get("per_attempt_timeout_s", 60),
                ),
            )
            case = next(c for c in batch["cases"] if c["id"] == result["case_id"])
            client = DeferredJudge(snapshot, budget)
            grade = judge_result(
                batch, case, result, client, secrets=credential_secrets(credentials)
            )
            budget.journal("grade", grade)
            batch["grades"].append(grade)
            if grade.get("error") == "judge_interrupted":
                batch["stop_reason"] = "judge_interrupted"
                break
        batch["requests"] = budget.requests
        batch["budget_started_at"] = budget.started_at
        if budget.stop_reason:
            batch["stop_reason"] = budget.stop_reason
        return batch
    finally:
        budget.close()


class DeferredJudge:
    """Construct inside the judge error boundary, retaining earlier batch evidence."""

    def __init__(self, snapshot, budget):
        self.snapshot, self.budget = snapshot, budget

    def respond(self, request):
        client = self.budget.client(self.snapshot, role="judge")
        return client.respond(request)
