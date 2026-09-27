"""Explicit synthetic-only 576-Attempt persistence/control demonstration.

This exercises transport/ledger capacity, not the diagnostic/product business
driver or checkpoint quality policy. The signed synthetic_capacity mode cannot
be installed for real materials or a real runtime. All providers are MockTransport.
"""

import argparse
import hashlib
import json
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2 as httpx

from agent_alfred.messages import Message, TextBlock
from agent_alfred.model import ModelRef, ModelRequest
from agent_alfred.resource_rollback import ConstructionOwner, ResumableRollback

from ..admission import proposal
from ..candidate import capture
from ..controlled_persistence import PersistentControlledAuthority
from ..examples_v4 import supplement_batch
from ..execution_decisions import material_binding, run_request
from ..materials import ProtectedMaterialStore, export_materials
from ..schema import digest, encode
from ..simulation_authority import SimulationAuthority
from ..store import EvidenceStore
from ..supplement_decisions import decision_request, make_summary
from .contract import COVERAGE, execution_plan, initial_budget, operation_for, signed
from .control import ControlService, control_request
from .persistence import SQLiteExecutionAnchor, SQLiteExecutionStore
from .runtime import SyntheticCredentials, SyntheticRuntime


class FixtureClock:
    def __init__(self):
        self.value = datetime(2026, 9, 27, tzinfo=UTC)

    def __call__(self):
        return self.value

    def wall_utc(self):
        return self.value

    def monotonic(self):
        return self.value.timestamp()


def fixture_terms(model):
    return {
        "version": 1,
        "model": model,
        "currency": "USD",
        "input_per_million": 300000000000,
        "output_per_million": 1200000000000,
        "request_fee": 0,
        "other_fee": 0,
        "tax_numerator": 0,
        "tax_denominator": 1,
        "fx_numerator": 1,
        "fx_denominator": 1,
        "quantum": 1,
        "coverage": COVERAGE,
        "valid_from": "2026-09-26T00:00:00+00:00",
        "valid_until": "2026-09-28T00:00:00+00:00",
        "evidence": "synthetic:price",
    }


def capacity_fixture(output, *, runtime_candidate=None):
    root = Path(output)
    root.mkdir(parents=True, exist_ok=False)
    if runtime_candidate is not None:
        (root / "candidate.json").write_bytes(encode(runtime_candidate))
    clock = FixtureClock()
    source = SimulationAuthority(root / "source", clock=clock)
    material_root = root / "materials"
    evidence = EvidenceStore(material_root / "evidence", decision_source=source)
    batch = supplement_batch(phase="calibration", prefix="synthetic-capacity")
    originals = batch["judge_tests"]
    batch["judge_tests"] = []
    for family in range(6):
        for test in originals:
            value = {k: v for k, v in deepcopy(test).items() if k != "id"}
            value["task"] += " synthetic family " + str(family)
            value["source_family_id"] += "-" + str(family)
            batch["judge_tests"].append(signed(value))
    summary = make_summary(batch)
    batch["summaries"].append(summary)
    batch["user_decisions"].append(
        source.issue_decision(
            decision_request(summary),
            subject="simulation:user",
            decision="approved",
            reason="Synthetic persistence fixture only",
            evidence=["synthetic:scope"],
        )
    )
    evidence.import_batch(batch)
    (material_root / "proposal.json").write_bytes(
        encode(
            proposal(
                batch,
                output_scope=str(root / "output"),
                operations=["product", "judge"],
            )
        )
    )
    members = [
        {
            "path": p.relative_to(material_root).as_posix(),
            "bytes": p.stat().st_size,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        }
        for p in sorted(material_root.rglob("*"))
        if p.is_file()
    ]
    manifest = encode({"version": 1, "file_count": len(members), "files": members})
    (material_root / "BUNDLE-MANIFEST.json").write_bytes(manifest)
    vault = ProtectedMaterialStore(root / "vault", scope="synthetic-capacity")
    reference = export_materials(
        material_root,
        vault=vault,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        batch_path="evidence/" + batch["batch_id"] + "/batch.json",
        proposal_path="proposal.json",
        evidence_store_path="evidence",
    )
    binding = material_binding(
        evidence,
        batch["batch_id"],
        package_manifest_sha256=reference["manifest_sha256"],
    )
    operations = []
    for kind in (
        "judge_test",
        "judge_review",
        "product",
        "auxiliary",
        "grade",
        "grade_review",
    ):
        for index, target in enumerate(
            batch["judge_tests"] if kind.startswith("judge") else batch["cases"]
        ):
            operations.append(
                operation_for(
                    batch,
                    operation_id=f"{kind}-{index}",
                    kind=kind,
                    object_id=target["id"],
                    instance_id=f"synthetic:{kind}:{index}",
                    max_calls=14
                    if kind == "product"
                    else 2
                    if kind == "auxiliary"
                    else 1,
                )
            )
    plan = execution_plan(
        binding=binding,
        material_ref=reference,
        worker="simulation:worker",
        controller="simulation:controller",
        operations=operations,
        pricing={
            "flash": fixture_terms("deepseek-flash"),
            "pro": fixture_terms("deepseek-v4-pro"),
        },
        execution_mode="synthetic_capacity",
        runtime_candidate=runtime_candidate,
    )
    run = run_request(
        binding,
        job_id="capacity-job",
        plan_sha256=digest(plan),
        budget_sha256=digest(initial_budget(plan)),
    )
    event = source.issue_execution_decision(
        run,
        subject="simulation:user",
        decision="approved",
        reason="Synthetic capacity plan only",
        evidence=["synthetic:capacity-plan"],
    )
    metrics = {"sends": 0, "max_wire_bytes": 0, "max_control_bytes": 0}

    def send(request):
        metrics["sends"] += 1
        metrics["max_wire_bytes"] = max(metrics["max_wire_bytes"], len(request.content))
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "synthetic-" + str(metrics["sends"]),
                "model": payload["model"],
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "{}"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    credentials = SyntheticCredentials()
    runtime = SyntheticRuntime(
        source=source, transport=httpx.MockTransport(send), credentials=credentials
    )
    config = dict(
        runtime=runtime,
        vault=vault,
        allowed_objects={reference["object_sha256"]},
        expected_manifest=reference["manifest_sha256"],
        output_root=root / "received",
        worker="simulation:worker",
        controller="simulation:controller",
        subject="simulation:user",
        now=clock,
    )

    def authority(*, _rollback=None):
        owner = ConstructionOwner(_rollback)
        resources = ResumableRollback()
        owner.rollback.own(resources)
        try:
            store = SQLiteExecutionStore(root / "authority.sqlite", _rollback=resources)
            anchor = SQLiteExecutionAnchor(root / "anchor.sqlite", _rollback=resources)
            result = PersistentControlledAuthority(**config, store=store, anchor=anchor)
            resources.own(runtime, runtime.retry_cleanup)
            owner.publish(result, parts=(resources,), close=resources.close)
            return result
        except BaseException as failure:
            owner.fail(failure)

    submission = {
        "job_id": "capacity-job",
        "material_ref": reference,
        "plan": plan,
        "run_source_ref": event["source_ref"],
        "approved_batch_id": batch["batch_id"],
    }
    return authority, submission, operations, clock, metrics, credentials


def demonstrate(output):
    owner = ConstructionOwner()
    try:
        result = _demonstrate(output, owner.rollback)
    except BaseException as failure:
        owner.fail(failure)
    else:
        owner.rollback.close()
        return result


def _demonstrate(output, rollback):
    candidate = capture(Path(__file__).resolve().parents[5])
    make_authority, submission, operations, clock, metrics, credentials = (
        capacity_fixture(output, runtime_candidate=candidate)
    )
    auth = make_authority(_rollback=rollback)
    auth.submit_job(submission)
    service = ControlService(auth)
    restarted_at = None
    for phase, kinds in (
        ("diagnostic", ("judge_test", "judge_review")),
        ("product", ("product", "auxiliary")),
        ("grading", ("grade", "grade_review")),
    ):
        selected = [op for op in operations if op["kind"] in kinds]
        # Main/auxiliary interleave per case so every case shares its 900-second
        # clock. One simulated second per request is only a fixture time source.
        if phase == "product":
            selected.sort(key=lambda op: (op["case_id"], op["kind"] == "auxiliary"))
        for operation in selected:
            flash = operation["kind"] in ("product", "auxiliary")
            request = ModelRequest(
                ModelRef("deepseek", "deepseek-flash" if flash else "deepseek-v4-pro"),
                (TextBlock("Synthetic capacity input; no quality conclusion."),),
                (Message("user", (TextBlock(operation["object_id"]),)),),
                max_tokens=8192 if flash else 16384,
                thinking="disabled",
                response_format=None if flash else "json_object",
            )
            for _ in range(operation["max_calls"]):
                prepared = auth.prepare_request(
                    "capacity-job",
                    operation["id"],
                    request,
                    principal="simulation:controller",
                    data_scope_sha256=operation["data_scope_sha256"],
                )
                number = metrics["sends"] + 1
                envelope = control_request(
                    request_id=f"control-{number}",
                    job_id="capacity-job",
                    action="invoke",
                    arguments={
                        "operation_id": operation["id"],
                        "attempt_id": f"attempt-{number}",
                        "descriptor": {**prepared, "timeout_seconds": 120},
                    },
                    deadline=clock.value + timedelta(seconds=120),
                )
                metrics["max_control_bytes"] = max(
                    metrics["max_control_bytes"], len(encode(envelope))
                )
                receipt = service.call(envelope, principal="simulation:worker")
                outcome = service.read_result(
                    receipt["result_ref"],
                    principal="simulation:worker",
                    job_id="capacity-job",
                )
                if (
                    not outcome["ok"]
                    or outcome["result"]["attempt"]["send_state"] != "SETTLED"
                ):
                    raise ValueError("capacity_attempt_failed:" + str(outcome))
                if number == 288:
                    before = auth.status("capacity-job")["state"]
                    handoff = auth.handoff(
                        "capacity-job", principal="simulation:controller"
                    )
                    auth.store.close()
                    auth.anchor.close()
                    auth = make_authority(_rollback=rollback)
                    after = auth.recover(
                        "capacity-job",
                        principal="simulation:controller",
                        handoff=handoff,
                    )["state"]
                    transfer_fields = {
                        "revision",
                        "event_digest",
                        "handler_epoch",
                        "clean_handoff",
                    }
                    if {
                        k: v for k, v in before.items() if k not in transfer_fields
                    } != {k: v for k, v in after.items() if k not in transfer_fields}:
                        raise ValueError("capacity_restart_changed_original_ledger")
                    restarted_at = {
                        "count": number,
                        "revision": after["revision"],
                        "started_at": after["started_at"],
                        "clean_handoff_sha256": digest(handoff),
                        "previous_revision": before["revision"],
                    }
                    service = ControlService(auth)
                clock.value += timedelta(seconds=1)
            auth.finish("capacity-job", operation["id"])
        if phase == "diagnostic":
            auth.phase_event(
                "capacity-job",
                principal="simulation:controller",
                expected_phase="diagnostic",
                next_phase="checkpoint",
                evidence={"synthetic_capacity_only": True},
            )
            clock.value += timedelta(seconds=3600)  # Waiting consumes original clock.
            auth.phase_event(
                "capacity-job",
                principal="simulation:controller",
                expected_phase="checkpoint",
                next_phase="product",
                evidence={"synthetic_capacity_only": True},
            )
        else:
            auth.phase_event(
                "capacity-job",
                principal="simulation:controller",
                expected_phase=phase,
                next_phase="grading" if phase == "product" else "closed",
                evidence={"synthetic_capacity_only": True},
            )
    auth.revoke(
        "capacity-job", principal="simulation:controller", reason="fixture_final_stop"
    )
    for observation in (
        {
            "kind": "failure",
            "status": "FAIL",
            "reason": "preserved synthetic original failure",
        },
        {
            "kind": "blocker",
            "status": "BLOCKED",
            "reason": "real cloud capacity not verified",
        },
        {
            "kind": "resource",
            "status": "retained",
            "resource": "fixture:audit-database",
        },
        {
            "kind": "cleanup",
            "status": "cleanup_failed",
            "reason": "fixture:deletion denied",
        },
    ):
        auth.record_observation(
            "capacity-job", observation, principal="simulation:controller"
        )
    events, anchors = (
        auth.store.read_events("capacity-job"),
        auth.anchor.read_events("capacity-job"),
    )
    status = auth.status("capacity-job")
    if (
        len(events) != len(anchors)
        or metrics["sends"] != 576
        or status["state"]["counts"] != {"flash": 480, "pro": 96, "total": 576}
    ):
        raise ValueError("capacity_readback_incomplete")
    result = {
        "runtime_candidate_sha256": digest(candidate),
        "offline_engineering": "PASS",
        "scope": "synthetic persistence/control only",
        "real_cloud_capacity": "NOT_VERIFIED",
        "real_readiness": "BLOCKED",
        "v1_release": "BLOCKED",
        "metrics": metrics,
        "credential_reads": credentials.reads,
        "restart": restarted_at,
        "state": status["state"],
        "event_count": len(events),
        "anchor_count": len(anchors),
        "event_kinds": dict(Counter(event["kind"] for event in events)),
        "events_sha256": digest(events),
        "anchors_sha256": digest(anchors),
        "capacity": auth.store.capacity("capacity-job"),
        "stop_report": auth.stop_report("capacity-job"),
    }
    root = Path(output)
    for name, value in (
        ("result.json", result),
        ("events.json", events),
        ("anchors.json", anchors),
    ):
        (root / name).write_bytes(encode(value))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    result = demonstrate(parser.parse_args().output)
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "offline_engineering",
                    "scope",
                    "event_count",
                    "anchor_count",
                    "metrics",
                    "real_cloud_capacity",
                    "real_readiness",
                    "v1_release",
                )
            },
            indent=2,
        )
    )
