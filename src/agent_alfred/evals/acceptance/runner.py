"""Collect real Host / SQLite / Registry facts in disposable dedicated state."""

import uuid
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from agent_alfred.connections import CredentialOverlay
from agent_alfred.memory.commands import CommandContext
from agent_alfred.memory.types import ManualOrigin
from agent_alfred.messages import ToolCallBlock, message_plain_text
from agent_alfred.model import (
    AttemptRecord,
    ModelError,
    ModelRef,
    ModelResponse,
    ModelResult,
    ScriptedModel,
    ScriptedModelFactory,
    Usage,
)
from agent_alfred.resource_rollback import ConstructionOwner
from agent_alfred.runtime.host import SubmitRequest
from agent_alfred.settings import Settings

from .budget import AuthorizedBatch, validate_execution_materials
from .input_capture import InputCapture
from .safety import credential_secrets, ensure_safe
from .schema import identifier, validate


def scripted(items):
    for item in items:
        if isinstance(item, dict) and "error" in item:
            yield ModelError(False, None, None, uuid.uuid4().hex, code=item["error"])
        elif isinstance(item, dict) and "tool" in item:
            call_id = uuid.uuid4().hex
            yield ModelResult(
                (AttemptRecord(call_id, False, "committed", Usage()),),
                ModelResponse(
                    (ToolCallBlock(call_id, item["tool"], item["arguments"]),),
                    "tool_use",
                    ModelRef("opencode-go", "deepseek-v4-flash"),
                ),
                None,
            )
        else:
            yield item


def run_offline(batch, workspace):
    validate(batch)
    if any(c["operation"] == "consolidate" for c in batch["cases"]):
        raise ValueError("consolidation_requires_specialist_import")
    if batch["phase"] != "offline_fixture" or not batch["simulation"]:
        raise ValueError("offline_fixture_required")
    if batch["results"] or len(batch["profiles"]) != 1:
        raise ValueError("fresh_single_profile_batch_required")
    return _run(batch, workspace)


def _run(batch, workspace, *, budget=None, factory=None, credentials=None):
    loaded_secrets = credential_secrets(credentials.values()) if credentials else ()
    ensure_safe(batch, secrets=loaded_secrets)
    result = deepcopy(batch)
    root = Path(workspace) / identifier(batch["batch_id"])
    root.mkdir(parents=True, exist_ok=False)
    profile = batch["profiles"][0]
    for case in batch["cases"]:
        state = root / case["id"]
        state.mkdir()
        if budget is not None:
            try:
                budget.check()
            except ValueError:
                result["stop_reason"] = budget.stop_reason or "batch_budget_exhausted"
                break
        if batch["schema_version"] == 3:
            from .case_setup import SimulatedModel

            offline_model = SimulatedModel
        else:
            offline_model = ScriptedModel
        model = (
            offline_model(list(scripted(case["script"]))) if factory is None else None
        )
        settings = Settings(
            **profile["parameters"],
            persona=profile["inputs"]["persona"],
        )
        if batch["schema_version"] == 3:
            from .case_setup import effective_settings

            settings = effective_settings(case["setup"], profile)
        builtin = root / ("skills-" + case["id"])
        builtin.mkdir()
        if "skill" in case["setup"]:
            skill = builtin / "Fixture"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: Fixture\ndescription: Synthetic procedure\n---\n"
                + case["setup"]["skill"],
                encoding="utf-8",
            )
        _configure_models(state, profile)

        case_factory = factory or ScriptedModelFactory(model)
        capture = (
            InputCapture(case_factory) if case["operation"] == "aggregate" else None
        )

        fault_observations = []
        recovery_model = ScriptedModel([])

        def build(*, _rollback, preparing=False, recovering=False):
            from .case_setup import build_case_host

            return build_case_host(
                _rollback=_rollback,
                fault_fixture=(
                    case["setup"].get("fault_fixture")
                    if batch["schema_version"] == 3 and not preparing and not recovering
                    else None
                ),
                observations=fault_observations,
                state_dir=state,
                settings=(
                    replace(
                        settings,
                        gate_input_character_limit=None,
                        consolidation_source_threshold=100,
                    )
                    if preparing
                    else settings
                ),
                skill_builtin=builtin,
                local_tool_allowlist=(
                    case["setup"]["local_tool_allowlist"]
                    if batch["schema_version"] == 3
                    else profile.get("local_tool_allowlist")
                ),
                factory=(
                    ScriptedModelFactory(seed_model(case["setup"]))
                    if preparing
                    else ScriptedModelFactory(recovery_model)
                    if recovering
                    else capture or case_factory
                ),
                credentials=(
                    CredentialOverlay({}, None)
                    if preparing or recovering
                    else credentials or CredentialOverlay({}, None)
                ),
            )

        setup_evidence = None
        if batch["schema_version"] == 3:
            from .artifacts import local_business, tool_projections
            from .case_setup import (
                install_skills,
                prepare_calendar,
                prepare_memory,
                prepare_persona,
                prepare_session,
                seed_model,
                verify_calendar,
                verify_memory,
                verify_runtime_setup,
                verify_session,
                verify_skills,
            )

            persona_receipt = prepare_persona(state, settings, case["setup"])
            owner = ConstructionOwner()
            try:
                preparing = build(preparing=True, _rollback=owner.rollback)
                preparing.start()
                memory_rows = prepare_memory(preparing, case["setup"])
                session, session_rows = prepare_session(preparing, case["setup"])
            except BaseException as failure:
                owner.fail(failure)
            else:
                owner.rollback.close()
            calendar_receipts = prepare_calendar(state, case["setup"])
            before_business = local_business(state, settings)
            verify_calendar(case["setup"], before_business)
            if (
                "persona" in case["setup"]
                and before_business["persona"]["content"] != case["setup"]["persona"]
            ):
                raise ValueError("setup_persona_readback_mismatch")
            install_skills(builtin, case["setup"])
        owner = ConstructionOwner()
        try:
            host = build(_rollback=owner.rollback)
            host.start()
            sampled_at = datetime.now(UTC).isoformat()
            if batch["schema_version"] != 3:
                session = host.create_session()
            if batch["schema_version"] == 3:
                setup_evidence = {
                    "status": "verified",
                    "source": "simulation",
                    "memory": verify_memory(host, case["setup"], memory_rows),
                    "calendar_receipts": calendar_receipts,
                    "business": before_business,
                    "persona_receipt": persona_receipt,
                    "session_seed": verify_session(
                        host, session, case["setup"], session_rows
                    ),
                    "aggregate": case["setup"].get("aggregate"),
                    "skills": verify_skills(host, case["setup"]),
                    "limits": case["setup"].get("limits", {}),
                }
            if "fact" in case["setup"]:
                host.memory_service.execute(
                    {
                        "operation_id": uuid.uuid4().hex,
                        "action": "save",
                        "kind": "semantic",
                        "payload": {
                            "subject": "fixture",
                            "fact": case["setup"]["fact"],
                        },
                    },
                    CommandContext(ManualOrigin("web"), "web"),
                )
            if case["setup"].get("routing"):
                host.apply_behaviour(
                    {"action": "save", "expected_revision": 0, "enabled": True}
                )
            if setup_evidence is not None:
                setup_evidence.update(verify_runtime_setup(host, case["setup"]))
            if case["operation"] == "aggregate":
                accepted = host.aggregate(
                    session_id=session,
                    goal=case["input"],
                    keywords=(
                        case["setup"]["aggregate"]["keywords"]
                        if batch["schema_version"] == 3
                        else "coffee"
                    ),
                    sources=(
                        case["setup"]["aggregate"]["sources"]
                        if batch["schema_version"] == 3
                        else ("semantic",)
                    ),
                )
            else:
                accepted = host.submit(SubmitRequest(case["input"], session_id=session))
            if accepted.kind != "accepted":
                raise ValueError("fixture_admission_failed")
            settled = host.wait(accepted.run_id)
            evidence = host.read_run_evidence(
                accepted.run_id, trace_root=state / "traces"
            )
            if setup_evidence is not None:
                if case["setup"].get("fault_fixture"):
                    from .fault_fixtures import action_coverage

                    setup_evidence["fault_fixture"] = {
                        "id": case["setup"]["fault_fixture"],
                        "triggered": bool(fault_observations),
                        "status": (
                            "triggered" if fault_observations else "not_triggered"
                        ),
                        "observations": list(fault_observations),
                        **action_coverage(
                            case["setup"]["fault_fixture"],
                            fault_observations,
                            host.tool_requests(accepted.run_id),
                        ),
                    }
                evidence["setup"] = setup_evidence
                evidence["tool_projections"] = tool_projections(
                    host, state, accepted.run_id
                )
                from .artifacts import drafts

                evidence["local_artifacts_before_reopen"] = drafts(state)
            if capture is not None:
                evidence["aggregation_inputs"] = capture.aggregation_evidence(evidence)
            output = message_plain_text(settled.reply) if settled.reply else None
            record = {
                "id": uuid.uuid4().hex,
                "batch_id": batch["batch_id"],
                "case_id": case["id"],
                "profile_id": profile["id"],
                "source": "offline_fixture" if batch["simulation"] else "online",
                "sampled_at": sampled_at,
                "finished_at": datetime.now(UTC).isoformat(),
                "run_id": accepted.run_id,
                "outcome": settled.outcome,
                "output": output,
                "error": settled.error,
                "evidence": evidence,
                "tools": host.tool_requests(accepted.run_id),
                "recorded": False,
            }
            if case["setup"].get("fault_fixture") == "file_publication_unknown_v1":
                from .artifacts import seal_observation

                page = host.open_session(session, page_size=20)
                record["recorded"] = any(
                    message.run_id == accepted.run_id and message.role == "assistant"
                    for message in page.messages
                )
                ensure_safe(record, secrets=loaded_secrets)
                evidence["setup"]["fault_fixture"]["original_observation"] = (
                    seal_observation(state, record)
                )
        except BaseException as failure:
            owner.fail(failure)
        else:
            owner.rollback.close()
        owner = ConstructionOwner()
        try:
            reopened = build(_rollback=owner.rollback, recovering=True)
            reopened.start()
            page = reopened.open_session(session, page_size=20)
            record["recorded"] = any(
                message.run_id == accepted.run_id and message.role == "assistant"
                for message in page.messages
            )
            record["persisted_messages"] = len(page.messages)
            if batch["schema_version"] == 3:
                if (
                    case["setup"].get("fault_fixture") == "file_publication_unknown_v1"
                    and fault_observations
                ):
                    from .fault_fixtures import observe_recovery

                    recovery = observe_recovery(
                        reopened,
                        state,
                        fault_observations[0]["operation_id"],
                        recovery_model,
                    )
                    record["evidence"]["setup"]["fault_fixture"]["recovery"] = recovery
                record["evidence"]["tool_verifications_after_reopen"] = [
                    {
                        **reopened.tool_verification(
                            accepted.run_id, row["step_index"], row["call_id"]
                        ),
                        "collected_at": datetime.now(UTC).isoformat(),
                    }
                    for row in record["tools"]
                ]
        except BaseException as failure:
            owner.fail(failure)
        else:
            owner.rollback.close()
        if batch["schema_version"] == 3:
            record["evidence"]["local_business"] = local_business(
                state,
                settings,
                operation_ids=[
                    row["operation_id"]
                    for row in record["tools"]
                    if row.get("operation_id")
                    and row["tool_name"]
                    in ("draft_message", "update_persona", "create_skill")
                ],
            )
        if (batch["phase"] == "trial" and case["group"] == "tools") or batch[
            "schema_version"
        ] == 3:
            from .artifacts import drafts

            artifacts = drafts(state)
            record["evidence"]["local_artifacts"] = artifacts
            if artifacts["status"] == "unavailable" and budget is not None:
                budget.stop_reason = "trial_isolation_failure"
        ensure_safe(record, secrets=loaded_secrets)
        if budget is not None and budget.journal:
            budget.journal("product_result", record)
        result["results"].append(record)
    if budget is not None:
        result["requests"] = budget.requests
        result["budget_started_at"] = budget.started_at
        if budget.stop_reason:
            result["stop_reason"] = budget.stop_reason
    ensure_safe(result, secrets=loaded_secrets)
    return validate(result)


def _configure_models(state, profile):
    from agent_alfred.runtime.model_settings import (
        Assignments,
        ModelSettingsStore,
        PinRecord,
    )

    store = ModelSettingsStore(state / "model_settings.json")
    old = store.load()
    models = profile["product_models"]
    pins = tuple(
        PinRecord(m["endpoint_id"], m["model_id"], m["wire_style"]) for m in models
    )
    # Assignment changes cannot retire the formerly assigned pin in one mutation.
    keys = {(p.endpoint_id, p.model_id) for p in pins}
    pins += tuple(p for p in old.pins if (p.endpoint_id, p.model_id) not in keys)
    refs = [ModelRef(m["endpoint_id"], m["model_id"]) for m in models]
    store.mutate(
        old.revision,
        lambda snapshot: replace(
            snapshot,
            pins=pins,
            assignments=Assignments(refs[0], refs[1] if len(refs) > 1 else None),
        ),
    )


class BudgetedFactory:
    def __init__(self, budget):
        self.budget = budget

    def create(self, snapshot):
        return self.budget.client(snapshot, role="product")

    def close(self):
        self.budget.close()


def execute(
    batch,
    workspace,
    *,
    product_factory_builder=None,
    http_client=None,
    credentials=None,
    clock=None,
    candidate_root=None,
    store=None,
    simulation_session=None,
    mock_transport=None,
):
    from .admission import admit_simulation, require_source

    require_source(batch, store=store)
    options = dict(clock=clock, candidate_root=candidate_root, store=store)
    if simulation_session is not None or mock_transport is not None:
        if (
            product_factory_builder is not None
            or http_client is not None
            or credentials is not None
        ):
            raise ValueError("ambiguous_transport_injection")
        admit_simulation(batch, simulation_session, mock_transport, store, "product")
        result = _execute(
            batch,
            workspace,
            simulation_session=simulation_session,
            mock_transport=mock_transport,
            **options,
        )
        simulation_session.finish("product")
        return result
    raise ValueError("simulation_session_and_mock_required")


def _execute(
    batch,
    workspace,
    *,
    clock=None,
    candidate_root=None,
    store=None,
    simulation_session=None,
    mock_transport=None,
):
    """Run an admitted synthetic product operation through the real host."""
    from .admission import require_source

    require_source(batch, store=store)
    credentials = CredentialOverlay({}, None)
    ensure_safe(batch, secrets=())
    if any(c["operation"] == "consolidate" for c in batch["cases"]):
        raise ValueError("consolidation_requires_specialist_import")
    budget = AuthorizedBatch(
        batch, clock=clock, session=simulation_session, mock_transport=mock_transport
    )
    from .execution_policy import policy, scoped_credentials

    execution = policy(batch)
    if execution["credential_scope"] is not None:
        credentials = scoped_credentials(batch, credentials)
    if batch["results"] or len(batch["profiles"]) != 1:
        raise ValueError("fresh_single_profile_batch_required")
    if candidate_root is None or store is None:
        raise ValueError("candidate_and_store_required")
    from .candidate import verify, verify_runtime

    if not verify(batch["candidate"], candidate_root):
        raise ValueError("candidate_changed")
    if not verify_runtime(batch["candidate"]):
        raise ValueError("runtime_candidate_mismatch")
    validate_execution_materials(batch, store, started_at=budget.started_at)
    budget.journal = store.reserve_execution(batch["batch_id"], "product")
    budget.journal("manifest", batch)
    factory = BudgetedFactory(budget)
    try:
        return _run(
            batch, workspace, budget=budget, factory=factory, credentials=credentials
        )
    finally:
        factory.close()
