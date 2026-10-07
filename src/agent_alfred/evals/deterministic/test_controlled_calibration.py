"""Same-job first sampling and blind grading, through the actual product Host."""

import json
from copy import deepcopy

import httpx2 as httpx
import pytest

from agent_alfred.evals.acceptance.controlled.persistence import (
    SQLiteExecutionAnchor,
    SQLiteExecutionStore,
)
from agent_alfred.evals.acceptance.controlled_calibration import (
    CalibrationDriver,
    calibration_operations,
)
from agent_alfred.evals.acceptance.controlled_diagnostics import DiagnosticDriver
from agent_alfred.evals.acceptance.controlled_persistence import (
    PersistentControlledAuthority,
)
from agent_alfred.evals.acceptance.examples_v4 import supplement_batch
from agent_alfred.evals.acceptance.schema import digest
from agent_alfred.evals.acceptance.supplement_schema import (
    manifest,
    material_id,
    signed,
)
from agent_alfred.evals.deterministic.test_controlled_diagnostics import (
    approve_checkpoint,
)
from agent_alfred.evals.deterministic.test_controlled_execution import fixture


def pipeline_fixture(tmp_path, *, prepare_checkpoint=True):
    batch = supplement_batch(phase="calibration")
    profile = batch["profiles"][0]
    profile["parameters"].update(
        max_steps=12,
        input_character_limit=64000,
        gate_input_character_limit=64000,
        working_memory_rounds=8,
        per_store_limit=8,
        per_store_character_budget=8000,
        max_tokens=8192,
        per_attempt_timeout_s=120.0,
        overall_deadline_s=900.0,
        gate_model_budget_s=120.0,
        stream=False,
        stream_fallback=False,
    )
    profile["product_models"] = [
        {
            "endpoint_id": "deepseek",
            "model_id": "deepseek-flash",
            "wire_style": "openai",
            "thinking": "disabled",
            "provider_version": "synthetic-only",
        }
    ]
    profile["judge_model"] = {
        "endpoint_id": "deepseek",
        "model_id": "deepseek-v4-pro",
        "wire_style": "openai",
        "thinking": "disabled",
        "response_format": "json_object",
        "provider_version": "synthetic-only",
    }
    profile["id"] = digest({k: v for k, v in profile.items() if k != "id"})
    batch["judge_profile"] = deepcopy(batch["judge_profile"])
    batch["judge_profile"]["model"] = deepcopy(profile["judge_model"])
    batch["judge_profile"]["id"] = digest(
        {k: v for k, v in batch["judge_profile"].items() if k != "id"}
    )
    for case in batch["cases"]:
        case["operation"] = "chat"
        case["setup"] = {"version": 1, "local_tool_allowlist": []}
        case["input"] = "Only this case: " + case["id"]
        case["gold"] = "PRIVATE GOLD MUST NEVER ENTER PRODUCT INPUT"
        case["material_id"] = material_id(case)
    batch["manifest"] = manifest(batch["cases"])
    originals = deepcopy(batch["judge_tests"])
    batch["judge_tests"] = [
        signed(
            {
                **{k: v for k, v in row.items() if k != "id"},
                "task": row["task"] + str(i),
                "source_family_id": row["source_family_id"] + str(i),
            }
        )
        for i in range(6)
        for row in originals
    ]
    wire = []
    answers = []

    def send(request):
        payload = json.loads(request.content)
        index = len(wire)
        wire.append(payload)
        if index < 36:
            label = batch["judge_tests"][index % 18]["expected"]
            raw = (
                {
                    "label": label,
                    "reason": "Synthetic",
                    "evidence": ["judge-material#/answer"],
                }
                if index < 18
                else {
                    "judgment": {
                        "status": label,
                        "support": "unknown",
                        "reason": "Synthetic blind",
                        "evidence": ["judge-material#/answer"],
                    }
                }
            )
            content = json.dumps(raw)
        elif "response_format" not in payload:
            # A valid empty model result is a first business failure, not an
            # infrastructure/send failure. Later cases must still run.
            system = payload["messages"][0]["content"]
            if system.startswith("Decide whether long-term memory"):
                content = json.dumps(
                    {"retrieve": False, "query": None, "reason_code": "greeting"}
                )
            else:
                content = "" if not answers else "Synthetic first answer"
                answers.append(content)
        else:
            inputs = json.loads(payload["messages"][-1]["content"])
            sources = inputs["sources"]
            case = next(v for k, v in sources.items() if k.startswith("case:"))
            result_ref = (
                next(k for k in sources if k.startswith("result:")) + "#/output"
            )

            def item(applies):
                return {
                    "status": "unknown" if applies else "na",
                    "reason": "Synthetic unknown",
                    "evidence": [result_ref],
                }

            grade = {
                "dimensions": {k: item(v) for k, v in case["applicability"].items()},
                "prohibitions": {k: item(True) for k in case["forbidden"]},
                "obligations": {
                    o["id"]: item(o["applies"]) for o in case["obligations"]
                },
                "disputed": True,
                "suspected_safety": False,
            }
            if payload["messages"][0]["content"].startswith("Independently judge"):
                raw = {
                    kind + ":" + key: {**value, "support": "unknown"}
                    for kind in ("dimensions", "prohibitions", "obligations")
                    for key, value in grade[kind].items()
                }
            else:
                raw = grade
            content = json.dumps(raw)
        return httpx.Response(
            200,
            json={
                "model": payload["model"],
                "choices": [
                    {
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    f = fixture(
        tmp_path,
        handler=send,
        batch_override=batch,
        operations_override=calibration_operations(batch),
        mode="authorized",
    )
    f["wire"] = wire
    f["mock_handler"] = send
    if not prepare_checkpoint:
        return f
    old = f["authority"]
    config = {
        key: getattr(old, key)
        for key in (
            "runtime",
            "vault",
            "allowed_objects",
            "expected_manifest",
            "output_root",
            "worker",
            "controller",
            "subject",
            "now",
        )
    }

    def authority():
        return PersistentControlledAuthority(
            **config,
            store=SQLiteExecutionStore(tmp_path / "ledger.sqlite"),
            anchor=SQLiteExecutionAnchor(tmp_path / "anchor.sqlite"),
        )

    f["authority"] = authority()
    f["make_authority"] = authority
    f["authority"].submit_job(f["submission"])
    f["driver"] = DiagnosticDriver(
        f["authority"], "controlled-job", principal="simulation:controller"
    )
    f["driver"].run()
    event, refs = approve_checkpoint(f)
    f["driver"].continue_with(event["source_ref"], adjudication_refs=refs)
    f["wire"] = wire
    f["workspace"] = tmp_path / "host"
    return f


def driver(f, *, recovered=False):
    authority = f["make_authority"]() if recovered else f["authority"]
    if recovered:
        authority.status("controlled-job")  # Completed-job audit, not activation.
    return CalibrationDriver(
        authority,
        "controlled-job",
        principal="simulation:controller",
        workspace=f["workspace"],
    )


def test_first_failure_and_all_slots_survive_full_host_pipeline_and_handler_reentry(
    tmp_path,
):
    f = pipeline_fixture(tmp_path)
    authority = f["authority"]
    original_report = driver(f).report()
    original_state = original_report["state"]
    for index in range(2):
        restored = authority.restore_materials(
            "controlled-job",
            tmp_path / f"material-cache-{index}",
            principal="simulation:controller",
        )
        after = driver(f).report()
        assert restored["state"]["state"] == "ACTIVE"
        assert after["diagnostics"] == original_report["diagnostics"]
        for key in (
            "started_at",
            "cap_units",
            "counts",
            "spent_units",
            "pending_units",
            "run_source_ref",
        ):
            assert restored["state"][key] == original_state[key]
    with pytest.raises(ValueError, match="calibration_phase_evidence_mismatch"):
        f["authority"].phase_event(
            "controlled-job",
            principal="simulation:controller",
            expected_phase="product",
            next_phase="grading",
            evidence={"fake": "skip"},
        )
    assert f["authority"].status("controlled-job")["state"]["counts"]["flash"] == 0
    result = driver(f).run()
    assert result["state"]["phase"] == "closed"
    assert len(result["slots"]) == 30 and len(result["expected_blind_slots"]) == 48
    first, second = result["slots"][:2]
    assert first["results"]["result"]["output"] in (None, "")
    assert second["results"]["result"]["output"] == "Synthetic first answer"
    assert result["quality_observations"]["failures"]
    assert result["calibration_quality"]["verdict"] == "FAIL"
    assert result["real_readiness"]["verdict"] == "BLOCKED"
    assert not result["seven_day_clock_started"]
    for row in result["slots"]:
        record = row["results"]["result"]
        assert record["source"] == "simulation" and record["sampled_at"] is None
        assert {r["attempt_id"] for r in record["evidence"]["captured_inputs"]} == {
            r["attempt_id"]
            for r in record["evidence"]["controlled_execution"]["attempts"]
        }
        if row["blind"]:
            assert (
                row["comparisons"]["sealed_revision"] > row["blind"]["sealed_revision"]
            )
            assert (
                row["blind"]["instance_id"]
                != row["grades"]["grade"]["producer"]["instance_id"]
            )
    product = [p for p in f["wire"] if p["model"] == "deepseek-flash"]
    assert len(product) == 60  # Every gate and answer shares the case allowance.
    assert all("PRIVATE GOLD" not in json.dumps(p) for p in product)
    assert result["remaining"]["counts"]["pro"] <= 96
    count = len(f["wire"])
    assert driver(f, recovered=True).run()["state"] == result["state"]
    assert len(f["wire"]) == count
    assert len(f["authority"].store.read_events("controlled-job")) == len(
        f["authority"].anchor.read_events("controlled-job")
    )


def test_material_restore_rejects_missing_sealed_diagnostics_before_pointer_change(
    tmp_path,
):
    from pathlib import Path

    f = pipeline_fixture(tmp_path)
    authority = f["authority"]
    before = authority.status("controlled-job")["state"]
    sealed = Path(before["material_output"]) / "evidence/controlled-job-diagnostics"
    (sealed / "complete.json").unlink()
    with pytest.raises(ValueError, match="missing_or_incomplete_batch"):
        authority.restore_materials(
            "controlled-job",
            tmp_path / "unusable-restored-cache",
            principal="simulation:controller",
        )
    after = authority.status("controlled-job")["state"]
    assert after["state"] == "SUSPENDED"
    assert after["material_output"] == before["material_output"]
    assert after["counts"] == before["counts"]
    assert after["started_at"] == before["started_at"]


def test_started_case_without_first_seal_is_never_resampled_by_new_handler(
    tmp_path, monkeypatch
):
    f = pipeline_fixture(tmp_path)
    from agent_alfred.evals.acceptance import controlled_calibration

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt("fixture process interruption after durable start")

    monkeypatch.setattr(controlled_calibration, "_run_case", interrupted)
    first = driver(f)
    write = first._write

    def lost_error_record(kind, *args, **kwargs):
        if kind == "CALIBRATION_SLOT_ERROR":
            raise KeyboardInterrupt("fixture process lost before error/stop recording")
        return write(kind, *args, **kwargs)

    monkeypatch.setattr(first, "_write", lost_error_record)
    with pytest.raises(KeyboardInterrupt):
        first.run()
    count = len(f["wire"])
    result = driver(f).run()
    assert len(f["wire"]) == count == 36
    assert result["slots"][0]["started"] and result["slots"][0]["results"] is None
    assert result["state"]["state"] == "SUSPENDED"
    assert result["state"]["stop_reason"].startswith("first_slot_unrecoverable:")
    assert len(result["expected_blind_slots"]) == 48


def test_missing_real_sample_time_remains_blocked_for_both_sources(tmp_path):
    f = pipeline_fixture(tmp_path)
    summary = driver(f).run(stop_after_cases=2)
    from agent_alfred.evals.acceptance.supplement_quality import quality

    batch = driver(f)._batch()
    batch["simulation"] = False
    for source in ("simulation", "online"):
        batch["results"][0]["source"] = source
        evaluated = quality(batch, f["clock"](), f["source"])
        assert (
            "actual_sample_time_missing:" + batch["results"][0]["case_id"]
            in evaluated["blockers"]
        )
        assert evaluated["failures"]
        assert (
            "real_sample_missing:" + batch["results"][0]["case_id"]
            in evaluated["blockers"]
        ) is (source == "simulation")
    assert summary["state"]["stop_reason"] == "synthetic_partial_stop"


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
def test_driver_retains_host_cleanup_after_recorded_failure_or_interruption(
    tmp_path,
    monkeypatch,
    error_type,
):
    from agent_alfred.runtime.host import RuntimeHost

    f = pipeline_fixture(tmp_path)
    subject = driver(f)
    close = RuntimeHost.close
    hosts = []
    calls = []
    first_failure = error_type("fixture Host release unavailable")

    def interrupted_close(host, *args, **kwargs):
        calls.append(host)
        if not hosts:
            hosts.append(host)
            raise first_failure
        if len(calls) == 2:
            raise KeyboardInterrupt("fixture interrupted cleanup retry")
        return close(host, *args, **kwargs)

    monkeypatch.setattr(RuntimeHost, "close", interrupted_close)
    try:
        if error_type is KeyboardInterrupt:
            with pytest.raises(KeyboardInterrupt) as raised:
                subject.run()
            assert raised.value is first_failure
        else:
            subject.run()
        before = subject.report()
        assert before["state"]["state"] == "SUSPENDED"
        assert before["state"]["stop_reason"] == "product_infrastructure_failure"
        assert before["slots"][0]["started"]
        assert before["slots"][0]["results"] is None
        assert not hosts[0].closed and hosts[0]._worker.is_alive()
        sends = len(f["wire"])
        assert sends == 36

        with pytest.raises(KeyboardInterrupt, match="interrupted cleanup retry"):
            subject.retry_cleanup()
        assert not hosts[0].closed
        assert subject.retry_cleanup()
        assert hosts[0].closed and not hosts[0]._worker.is_alive()
        assert subject.retry_cleanup()
        assert calls == [hosts[0]] * 3
        assert subject.run() == before
        assert len(f["wire"]) == sends
    finally:
        for host in hosts:
            close(host)
        f["authority"].store.close()
        f["authority"].anchor.close()


@pytest.mark.parametrize("interrupt_cleanup", [False, True])
def test_public_replay_owns_driver_cleanup_after_reported_host_failure(
    tmp_path, monkeypatch, interrupt_cleanup
):
    from agent_alfred.evals.acceptance.controlled import replay
    from agent_alfred.resource_rollback import IncompleteRollback
    from agent_alfred.runtime.host import RuntimeHost

    material_root = tmp_path / "materials"
    f = pipeline_fixture(material_root, prepare_checkpoint=False)
    reference = tmp_path / "reference.json"
    reference.write_text(json.dumps(f["submission"]["material_ref"]))
    original_close = RuntimeHost.close
    original_script_init = replay.ScriptedReplay.__init__
    original_run = CalibrationDriver.run
    hosts, close_calls, scripts, reports = [], [], [], []
    first_failure = OSError("public replay Host cleanup unavailable")
    interruption = KeyboardInterrupt("public replay cleanup retry interrupted")

    def close(host, *args, **kwargs):
        close_calls.append(host)
        if not hosts:
            hosts.append(host)
            raise first_failure
        if interrupt_cleanup and len(close_calls) == 2:
            raise interruption
        return original_close(host, *args, **kwargs)

    def script_init(script, batch):
        original_script_init(script, batch)
        scripts.append(script)

    def run(subject, *args, **kwargs):
        result = original_run(subject, *args, **kwargs)
        reports.append(result)
        return result

    monkeypatch.setattr(RuntimeHost, "close", close)
    monkeypatch.setattr(replay.ScriptedReplay, "__init__", script_init)
    monkeypatch.setattr(CalibrationDriver, "run", run)
    try:
        with pytest.raises(BaseException) as raised:
            replay.demonstrate(
                reference_file=reference,
                vault_root=f["authority"].vault.root,
                output=tmp_path / "replay",
                protected_roots=[material_root / "source-materials"],
                stop_after_cases=1,
            )
        assert len(reports) == 2 and reports[0] == reports[1]
        before = deepcopy(reports)
        assert reports[0]["state"]["stop_reason"] == "product_infrastructure_failure"
        first_case = reports[0]["slots"][0]["case_id"]
        assert reports[0]["slot_errors"]["product:" + first_case]["error"] == str(
            first_failure
        )
        assert len(scripts[0].wire) == 36
        assert all(row["kind"] != "product" for row in scripts[0].wire)
        if interrupt_cleanup:
            assert raised.value is interruption
            owner = raised.value.__cause__
            assert isinstance(owner, IncompleteRollback)
            assert not hosts[0].closed and hosts[0]._worker.is_alive()
            assert owner.retry()
            assert owner.retry()
        else:
            assert isinstance(raised.value, AssertionError)
        assert hosts[0].closed and not hosts[0]._worker.is_alive()
        assert len(close_calls) == (3 if interrupt_cleanup else 2)
        assert all(host is hosts[0] for host in close_calls)
        assert reports == before and len(scripts[0].wire) == 36
    finally:
        for host in hosts:
            original_close(host)
