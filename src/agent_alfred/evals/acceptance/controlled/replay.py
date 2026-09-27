"""Mock-only complete r7 replay. Reads a protected transfer; writes a NEW root.

Fixture opinions, continuation events and accounting values are explicitly
synthetic. The original real-material simulation flag and ancestry stay intact.
"""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

import httpx2 as httpx

from agent_alfred.resource_rollback import ConstructionOwner, ResumableRollback

from ..candidate import capture
from ..controlled_calibration import CalibrationDriver, calibration_operations
from ..controlled_diagnostics import DiagnosticDriver
from ..controlled_persistence import PersistentControlledAuthority
from ..execution_decisions import material_binding, run_request
from ..materials import ProtectedMaterialStore
from ..schema import digest, encode
from ..simulation_authority import SimulationAuthority
from ..store import EvidenceStore
from ..supplement_decisions import dispute_request
from .capacity import FixtureClock, fixture_terms
from .contract import execution_plan, initial_budget
from .persistence import SQLiteExecutionAnchor, SQLiteExecutionStore
from .runtime import SyntheticCredentials, SyntheticRuntime


def snapshot(roots):
    return {
        str(root): {
            p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*"))
            if p.is_file()
        }
        for root in roots
    }


class ScriptedReplay:
    """Actual HTTP wire is logged; no script output is a quality assertion."""

    def __init__(self, batch):
        self.batch, self.authority, self.wire, self.answers = batch, None, [], []
        self.tool_cases = set()

    def __call__(self, request):
        auth = self.authority
        state = auth.persistence_call(self.job_id, auth.store.get, self.job_id)
        row = auth.persistence_call(
            self.job_id, auth.store.get_attempt, self.job_id, state["active_attempt"]
        )
        plan = auth.read_object(state["plan_ref"], job_id=self.job_id)
        op = next(o for o in plan["operations"] if o["id"] == row["operation_id"])
        payload = json.loads(request.content)
        self.wire.append(
            {
                "attempt_id": row["attempt_id"],
                "kind": op["kind"],
                "object_id": op["object_id"],
                "payload": payload,
            }
        )
        message = {"role": "assistant"}
        finish = "stop"
        if op["kind"] in ("judge_test", "judge_review"):
            test = next(
                t for t in self.batch["judge_tests"] if t["id"] == op["object_id"]
            )
            item = {
                "status": test["expected"],
                "support": "unknown",
                "reason": "Scripted blind opinion; semantic support remains unknown",
                "evidence": ["judge-material#/answer"],
            }
            value = (
                {"judgment": item}
                if op["kind"] == "judge_review"
                else {
                    "label": test["expected"],
                    "reason": "Synthetic fixture opinion only",
                    "evidence": ["judge-material#/answer"],
                }
            )
            message["content"] = json.dumps(value)
        elif op["kind"] == "product":
            system = payload["messages"][0]["content"]
            if system.startswith("Decide whether long-term memory"):
                content = json.dumps(
                    {
                        "retrieve": True,
                        "query": "synthetic",
                        "reason_code": "conservative_retrieve",
                    }
                )
            elif system.startswith("Classify only the current task"):
                content = "full"
            elif system.startswith("Select relevant Skills"):
                content = '{"skills":[]}'
            else:
                tools = {t["function"]["name"] for t in payload.get("tools", [])}
                chosen = next(
                    (
                        name
                        for name in ("read_persona", "query_events")
                        if name in tools
                    ),
                    None,
                )
                if chosen and op["object_id"] not in self.tool_cases:
                    self.tool_cases.add(op["object_id"])
                    message.update(
                        content=None,
                        tool_calls=[
                            {
                                "id": "fixture-" + str(len(self.wire)),
                                "type": "function",
                                "function": {"name": chosen, "arguments": "{}"},
                            }
                        ],
                    )
                    finish, content = "tool_calls", None
                else:
                    # Preserve a real observed fixture business failure on the
                    # first product answer. Do not replace it with a better one.
                    content = (
                        ""
                        if not self.answers
                        else "Synthetic first output; no claim of task quality."
                    )
                    self.answers.append(op["object_id"])
            if "tool_calls" not in message:
                message["content"] = content
        else:
            inputs = json.loads(payload["messages"][-1]["content"])
            sources = inputs["sources"]
            case = next(v for k, v in sources.items() if k.startswith("case:"))
            ref = next(k for k in sources if k.startswith("result:")) + "#/output"

            def item(applies):
                return {
                    "status": "unknown" if applies else "na",
                    "reason": "Scripted fixture leaves semantic judgment unknown",
                    "evidence": [ref],
                }

            value = {
                "dimensions": {k: item(v) for k, v in case["applicability"].items()},
                "prohibitions": {k: item(True) for k in case["forbidden"]},
                "obligations": {
                    o["id"]: item(o["applies"]) for o in case["obligations"]
                },
                "disputed": True,
                "suspected_safety": False,
            }
            if op["kind"] == "grade_review":
                value = {
                    kind + ":" + key: {**opinion, "support": "unknown"}
                    for kind in ("obligations", "dimensions", "prohibitions")
                    for key, opinion in value[kind].items()
                }
            message["content"] = json.dumps(value)
        return httpx.Response(
            200,
            json={
                "model": payload["model"],
                "choices": [{"message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 12},
            },
        )


def demonstrate(
    *, reference_file, vault_root, output, protected_roots=(), stop_after_cases=None
):
    owner = ConstructionOwner()
    try:
        result = _demonstrate(
            reference_file=reference_file,
            vault_root=vault_root,
            output=output,
            protected_roots=protected_roots,
            stop_after_cases=stop_after_cases,
            rollback=owner.rollback,
        )
    except BaseException as failure:
        owner.fail(failure)
    else:
        owner.rollback.close()
        return result


def _demonstrate(
    *,
    reference_file,
    vault_root,
    output,
    protected_roots,
    stop_after_cases,
    rollback,
):
    root = Path(output)
    protected_roots = [Path(p).resolve() for p in protected_roots]
    if any(root.resolve() == p or p in root.resolve().parents for p in protected_roots):
        raise ValueError("protected_output_forbidden")
    root.mkdir(parents=True, exist_ok=False)
    before = snapshot(protected_roots)
    reference = json.loads(Path(reference_file).read_bytes())
    # #99 performs a full protected transfer in submit_job. This existing received
    # copy is read only to construct the exact binding, never edited or published.
    from ..materials import preflight_materials

    vault = ProtectedMaterialStore(vault_root, scope=reference["scope"])
    intake = preflight_materials(
        reference,
        vault=vault,
        allowed_objects={reference["object_sha256"]},
        expected_manifest_sha256=reference["manifest_sha256"],
        output=root / "plan-materials",
    )
    materials = EvidenceStore(root / "plan-materials" / "evidence")
    batch = materials.read(intake["batch_id"])
    binding = material_binding(
        materials,
        batch["batch_id"],
        package_manifest_sha256=reference["manifest_sha256"],
    )
    candidate = capture(Path(__file__).resolve().parents[5])
    (root / "candidate.json").write_bytes(encode(candidate))
    clock, credentials = FixtureClock(), SyntheticCredentials()
    source = SimulationAuthority(root / "fixture-source", clock=clock)
    plan = execution_plan(
        binding=binding,
        material_ref=reference,
        worker="simulation:worker",
        controller="simulation:controller",
        operations=calibration_operations(batch),
        pricing={
            "flash": fixture_terms("deepseek-flash"),
            "pro": fixture_terms("deepseek-v4-pro"),
        },
        execution_mode="synthetic_replay",
        runtime_candidate=candidate,
    )
    job_id = (
        "r7-full-calibration" if stop_after_cases is None else "r7-partial-calibration"
    )
    run = run_request(
        binding,
        job_id=job_id,
        plan_sha256=digest(plan),
        budget_sha256=digest(initial_budget(plan)),
    )
    event = source.issue_execution_decision(
        run,
        subject="simulation:user",
        decision="approved",
        reason=(
            "Exact synthetic replay only; real material approval "
            "source remains unverifiable"
        ),
        evidence=["synthetic:fixture-plan"],
    )
    script = ScriptedReplay(batch)
    script.job_id = job_id
    runtime = SyntheticRuntime(
        source=source,
        transport=httpx.MockTransport(script),
        credentials=credentials,
        input_tokens=1000,
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

    def make_authority():
        owner = ConstructionOwner(rollback)
        resources = ResumableRollback()
        owner.rollback.own(resources)
        try:
            store = SQLiteExecutionStore(root / "authority.sqlite", _rollback=resources)
            anchor = SQLiteExecutionAnchor(root / "anchor.sqlite", _rollback=resources)
            auth = PersistentControlledAuthority(**config, store=store, anchor=anchor)
            resources.own(runtime, runtime.retry_cleanup)
            owner.publish(auth, parts=(resources,), close=resources.close)
            return auth
        except BaseException as failure:
            owner.fail(failure)

    auth = script.authority = make_authority()
    auth.submit_job(
        {
            "job_id": job_id,
            "material_ref": reference,
            "plan": plan,
            "run_source_ref": event["source_ref"],
            "approved_batch_id": batch["batch_id"],
        }
    )
    diagnostic = DiagnosticDriver(auth, job_id, principal="simulation:controller")
    diag = diagnostic.run()
    print(
        json.dumps(
            {"stage": "checkpoint", "counts": auth.status(job_id)["state"]["counts"]}
        ),
        flush=True,
    )
    refs = [
        source.issue_decision(
            dispute_request(batch, dispute),
            subject="simulation:user",
            decision="dismissed",
            reason="Exact fixture-only protocol decision; real unknown stays unknown",
            evidence=["judge-material#/answer"],
        )["source_ref"]
        for dispute in diag["diagnostic_disputes"]
    ]
    checkpoint = diagnostic.checkpoint(adjudication_refs=refs)
    continuation = source.issue_execution_decision(
        checkpoint["request"],
        subject="simulation:user",
        decision="approved",
        reason="Synthetic checkpoint only; no real human/source/quality conclusion",
        evidence=["synthetic:exact-checkpoint"],
    )
    diagnostic.continue_with(continuation["source_ref"], adjudication_refs=refs)
    # A clean controller handoff consumes a capability, never a fresh grant.
    old_status = auth.status(job_id)
    handoff = auth.handoff(job_id, principal="simulation:controller")
    auth = script.authority = make_authority()
    auth.recover(job_id, principal="simulation:controller", handoff=handoff)
    resumed = auth.status(job_id)
    for key in ("started_at", "counts", "spent_units", "pending_units"):
        assert resumed["state"][key] == old_status["state"][key]
    driver = CalibrationDriver(
        auth, job_id, principal="simulation:controller", workspace=root / "host"
    )
    rollback.own(driver, driver.retry_cleanup)
    report = driver.run(stop_after_cases=stop_after_cases)
    attempts_before = len(script.wire)
    recovered = script.authority = make_authority()
    recovered.status(job_id)  # Terminal audit needs no execution capability.
    reread_driver = CalibrationDriver(
        recovered, job_id, principal="simulation:controller", workspace=root / "host"
    )
    rollback.own(reread_driver, reread_driver.retry_cleanup)
    reread = reread_driver.run()
    assert report == reread and len(script.wire) == attempts_before
    events = recovered.persistence_call(job_id, recovered.store.read_events, job_id)
    anchors = recovered.persistence_call(job_id, recovered.anchor.read_events, job_id)
    capacity = recovered.persistence_call(job_id, recovered.store.capacity, job_id)
    after = snapshot(protected_roots)
    assert before == after
    assert candidate == capture(Path(__file__).resolve().parents[5]), (
        "candidate_changed_during_replay"
    )
    assert len(events) == len(anchors)
    assert report["state"]["counts"]["total"] == len(script.wire) == credentials.reads
    assert (
        report["state"]["counts"]["flash"] > 0
        and report["quality_observations"]["failures"]
    )
    assert (
        report["simulation"] is batch["simulation"]
        and report["original_parent"] == batch["parent"]
    )
    assert not report["seven_day_clock_started"] and report["actual_sampled_at"] is None
    assert report["state"]["state"] != "ACTIVE"
    product_results = sum(s["results"] is not None for s in report["slots"])
    if stop_after_cases is None:
        assert report["state"]["phase"] == "closed" and product_results == 30
    else:
        assert report["state"]["stop_reason"] == "synthetic_partial_stop"
        assert product_results == stop_after_cases
    # Reuse #102's 576-Attempt shape; add all measured business events/objects.
    missing_attempts = 576 - len(script.wire)
    upper_events = len(events) + missing_attempts * 14 + 576 * 2 + 400
    upper_objects = (
        capacity["observed"]["objects"]["count"] + missing_attempts * 6 + 500
    )
    assert upper_events <= capacity["limits"]["events"]
    assert upper_objects <= capacity["limits"]["objects"]
    metrics = {
        "mode": "SYNTHETIC_REPLAY",
        "candidate_id": digest(candidate),
        "phase": report["state"]["phase"],
        "stop_reason": report["state"]["stop_reason"],
        "counts": report["state"]["counts"],
        "events": len(events),
        "anchors": len(anchors),
        "event_kinds": dict(Counter(e["kind"] for e in events)),
        "worst_576_event_bound": upper_events,
        "worst_576_object_bound": upper_objects,
        "capacity_method": (
            "measured business flow + remaining Attempts(14 events/6 objects) "
            "+ all control receipts + 400 event/500 object lifecycle margin"
        ),
        "product_results": product_results,
        "grades": sum(s["grades"] is not None for s in report["slots"]),
        "blind_reviews": sum(s["present"] for s in report["expected_blind_slots"]),
        "first_failures": report["quality_observations"]["failures"],
        "pending_units": report["state"]["pending_units"],
        "spent_units": report["state"]["spent_units"],
        "real_model_calls": 0,
        "real_source": "BLOCKED",
        "real_readiness": "BLOCKED",
        "calibration_quality": "BLOCKED_REAL_EVIDENCE",
        "v1_release": "BLOCKED",
        "original_files_unchanged": before == after,
        "reentry_extra_sends": len(script.wire) - attempts_before,
    }
    for name, value in {
        "report": report,
        "result": metrics,
        "plan": plan,
        "actual-wire": script.wire,
        "checkpoint": checkpoint,
        "events": events,
        "anchors": anchors,
        "capacity": capacity,
        "original-before": before,
        "original-after": after,
    }.items():
        (root / (name + ".json")).write_bytes(encode(value))
    print(json.dumps(metrics, ensure_ascii=False), flush=True)
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--material-reference", type=Path, required=True)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--protected-original", type=Path, action="append", default=[])
    parser.add_argument("--stop-after-cases", type=int)
    args = parser.parse_args()
    demonstrate(
        reference_file=args.material_reference,
        vault_root=args.vault,
        output=args.output,
        protected_roots=args.protected_original,
        stop_after_cases=args.stop_after_cases,
    )


if __name__ == "__main__":
    main()
