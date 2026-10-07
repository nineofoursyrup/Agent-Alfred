"""V3 orchestration with synthetic material/source and mocked external boundaries.

The installed runtime's OS/account check is replaced ONLY inside this test.
Its real simulation guard is checked first. No real approval, API, credential,
native isolation or calibration result is established by this fixture.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from time import monotonic

import httpx2 as httpx
import pytest

from agent_alfred.clock import SystemClock
from agent_alfred.evals.acceptance.controlled import local_runtime, native
from agent_alfred.evals.acceptance.controlled.contract import (
    ADVISORY_BUDGET,
    CAP_UNITS,
    advisory_run_disclosure,
    execution_plan,
    initial_budget,
)
from agent_alfred.evals.acceptance.controlled.local_clock import ContinuityGuard
from agent_alfred.evals.acceptance.controlled.local_files import OwnerDirectory
from agent_alfred.evals.acceptance.controlled.local_persistence import (
    _LOCAL_STORAGE,
    LocalExecutionStore,
    LocalExecutionWitness,
)
from agent_alfred.evals.acceptance.controlled.local_runner import _synthetic_launch
from agent_alfred.evals.acceptance.controlled.local_runtime import LocalInstalledRuntime
from agent_alfred.evals.acceptance.controlled_calibration import CalibrationDriver
from agent_alfred.evals.acceptance.controlled_diagnostics import DiagnosticDriver
from agent_alfred.evals.acceptance.controlled_persistence import (
    PersistentControlledAuthority,
)
from agent_alfred.evals.acceptance.execution_decisions import (
    material_binding,
    run_request,
)
from agent_alfred.evals.acceptance.schema import digest
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.resource_rollback import RollbackSlot

from .test_controlled_calibration import pipeline_fixture
from .test_controlled_diagnostics import approve_checkpoint
from .test_local_advisory_contract import _terms


class OfflineSlot:
    """Only substitutes the external native launch; no Seatbelt claim."""

    def __init__(self, root):
        self.root = root

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def launch(self):
        return _synthetic_launch(self.root)

    def cleanup_observation(self):
        return {"scope": "OFFLINE_SUBPROCESS_NO_NATIVE_ISOLATION"}


class OfflineClock:
    """Portable orchestration clock; real sleep detection is tested separately."""

    def sample(self):
        return {
            "wall": datetime.now(UTC).timestamp(),
            "awake": monotonic(),
            "sleep_interval": [0, 0],
        }


@pytest.mark.parametrize(
    "usage_overrun", [False, True], ids=["full-flow", "usage-overrun"]
)
def test_v3_public_diagnostics_checkpoint_restore_and_flash_grading(
    tmp_path, monkeypatch, usage_overrun
):
    f = pipeline_fixture(tmp_path, prepare_checkpoint=False)
    old, source = f["authority"], f["source"]
    assert f["batch"]["simulation"] is True
    # SimulationAuthority remains the source; it can never authorize real material.
    source.clock = SystemClock()
    source._last_time = source.clock.monotonic()
    monkeypatch.setattr(
        source, "verify_owner_record", lambda *args: None, raising=False
    )
    root = tmp_path / "offline-owner"
    root.mkdir(mode=0o700)
    directory = OwnerDirectory(root, os.geteuid())
    key = directory.file("offline-key", create=True)
    key.write_text("synthetic-not-a-provider-credential")
    runtime = object.__new__(LocalInstalledRuntime)
    runtime._directory = directory
    runtime._cleanup = RollbackSlot()
    runtime._continuity, runtime._clock = ContinuityGuard(), OfflineClock()
    runtime._last_readiness = None
    runtime._bundle_manifest, runtime._bundle_verifier = {}, None
    runtime._decision_source = source
    runtime._config = {
        "worker": old.worker,
        "controller": old.controller,
        "subject": old.subject,
        "installation_id": "offline-fixture",
        "candidate_id": "b" * 64,
        "credential_path": str(key),
        "protected_root": str(root),
        "bundle_root": str(root / "not-a-native-bundle"),
        "bundle_manifest": {},
        "host_environment": {},
    }
    monkeypatch.setattr(runtime, "verify_installation", lambda: None)
    runtime.store = LocalExecutionStore(directory, "ledger", token=_LOCAL_STORAGE)
    runtime.anchor = LocalExecutionWitness(directory, "witness", token=_LOCAL_STORAGE)
    evidence = EvidenceStore(tmp_path / "source-materials/evidence")
    material = f["submission"]["material_ref"]
    binding = material_binding(
        evidence,
        f["batch"]["batch_id"],
        package_manifest_sha256=material["manifest_sha256"],
    )
    terms = _terms("deepseek-flash", advisory=True)
    terms.update(
        valid_from=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        valid_until=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    plan = execution_plan(
        binding=binding,
        material_ref=material,
        worker=old.worker,
        controller=old.controller,
        operations=f["submission"]["plan"]["operations"],
        pricing={"flash": terms},
        runtime_candidate={"files": {}},
        budget_policy=ADVISORY_BUDGET,
        flash_judge_profile=f["batch"]["judge_profile"],
    )
    # No new production escape hatch: real LocalInstalledRuntime.check rejects this.
    with pytest.raises(ValueError, match="runtime_candidate_mismatch"):
        runtime.check(plan, binding, f["batch"], datetime.now(UTC))
    monkeypatch.setattr(
        runtime,
        "check",
        lambda *args: {
            "mode": "SYNTHETIC_ONLY",
            "online_executable": False,
        },
    )
    budget = {
        "contract": "V3-LOCAL-FLASH-ADVISORY-BUDGET",
        "pricing": plan["pricing"],
        "input_measurement": {"numerator": 1, "denominator": 1, "overhead": 0},
        "input_policy": plan["input_policy"],
        "provider_evidence": {"path": "offline", "sha256": "e" * 64},
        "approval_ref": "offline-no-owner-approval",
    }
    monkeypatch.setattr(runtime, "_billing", lambda now, **kwargs: budget)
    monkeypatch.setattr(
        local_runtime,
        "read_readiness",
        lambda *args, **kwargs: {
            "identities": {"evidence": "offline-model"},
            "evidence": {"advisory_budget": budget["provider_evidence"]},
        },
    )

    def provider(request):
        response = f["mock_handler"](request)
        value = response.json()
        payload = json.loads(request.content)
        if usage_overrun:
            value["usage"]["prompt_tokens"] = 32001
        elif len(f["wire"]) > 36 and "response_format" in payload:
            inputs = json.loads(payload["messages"][-1]["content"])
            if "response_schema" in inputs:
                # Independent fixture usage above old Flash20k, below every
                # full grade estimate (measured minimum 21,648 before overhead).
                value["usage"]["prompt_tokens"] = 21000
        return httpx.Response(response.status_code, json=value)

    monkeypatch.setattr(
        local_runtime.httpx,
        "HTTPTransport",
        lambda **kwargs: httpx.MockTransport(provider),
    )
    monkeypatch.setattr(
        native,
        "prepare_runner_slot",
        lambda *args, **kwargs: OfflineSlot(
            root / ("case-" + str(kwargs["slot_index"]))
        ),
    )

    def authority():
        return PersistentControlledAuthority(
            runtime=runtime,
            store=runtime.store,
            anchor=runtime.anchor,
            vault=old.vault,
            allowed_objects=old.allowed_objects,
            expected_manifest=old.expected_manifest,
            output_root=old.output_root,
            worker=old.worker,
            controller=old.controller,
            subject=old.subject,
        )

    try:
        request = run_request(
            binding,
            job_id="controlled-job",
            plan_sha256=digest(plan),
            budget_sha256=digest(initial_budget(plan)),
            disclosure=advisory_run_disclosure(
                CAP_UNITS,
                judge_profile_change=plan["judge_profile_change"],
            ),
        )
        event = source.issue_execution_decision(
            request,
            subject=old.subject,
            decision="approved",
            reason="Synthetic V3 orchestration only",
            evidence=["synthetic:V3"],
        )
        auth = authority()
        auth.submit_job(
            dict(f["submission"], plan=plan, run_source_ref=event["source_ref"])
        )
        diagnostic = DiagnosticDriver(auth, "controlled-job", principal=old.controller)
        report = diagnostic.run()
        if usage_overrun:
            final = auth.status("controlled-job")
            assert len(f["wire"]) == len(final["attempts"]) == 1
            assert final["state"]["stop_reason"] == "input_preflight_exceeded"
            row = final["attempts"][0]
            assert row["usage"]["input_tokens"] == 32001
            assert row["input_policy"] == plan["input_policy"]
            assert row["response_ref"] and row["actual_units"] is None
            assert final["state"]["pending_units"] == row["planned_units"]
            return
        assert report["complete"] is True and report["errors"] == []
        assert report["simulation"] is True
        assert report["remaining"]["counts"] == {"flash": 36, "pro": 0, "total": 36}
        assert len(f["wire"]) == 36
        state = auth.status("controlled-job")["state"]
        assert state["case_counts"] == {}  # judge case_id=None never shares a 16 cap
        started = state["started_at"]
        checkpoint = diagnostic.checkpoint()
        assert checkpoint["blockers"]  # no implicit quality approval
        f.update(authority=auth, driver=diagnostic)
        event, refs = approve_checkpoint(f)
        handoff = auth.handoff("controlled-job", principal=old.controller)
        recovered = authority()
        recovered.recover("controlled-job", principal=old.controller, handoff=handoff)
        restored = recovered.restore_materials(
            "controlled-job", tmp_path / "restored", principal=old.controller
        )
        assert restored["state"]["started_at"] == started
        assert restored["state"]["counts"] == {"flash": 36, "pro": 0, "total": 36}
        driver = DiagnosticDriver(recovered, "controlled-job", principal=old.controller)
        driver.continue_with(event["source_ref"], adjudication_refs=refs)
        calibration = CalibrationDriver(
            recovered,
            "controlled-job",
            principal=old.controller,
            workspace=tmp_path / "host",
        )
        outcome = calibration.run()
        assert outcome["flow_complete"] is True
        final = recovered.status("controlled-job")
        assert final["state"]["started_at"] == started
        assert final["state"]["counts"]["pro"] == 0
        assert all(row["model"] == "deepseek-flash" for row in final["attempts"])
        assert all(
            p["model"] == "deepseek-flash" and p["max_tokens"] == 8192
            for p in f["wire"]
        )
        assert sum(row["kind"] == "grade" for row in final["attempts"]) == 30
        assert all(
            row["usage"]["input_tokens"] == 21000
            for row in final["attempts"]
            if row["kind"] == "grade"
        )
        assert sum(row["kind"] == "grade_review" for row in final["attempts"]) == 30
        assert (
            "user_materials_approval_missing"
            in outcome["calibration_quality"]["blockers"]
        )
        assert final["state"]["pending_units"] > 0
        assert all(row["actual_units"] is None for row in final["attempts"])
    finally:
        runtime._cleanup.retry()
        runtime.store.close()
        runtime.anchor.close()
