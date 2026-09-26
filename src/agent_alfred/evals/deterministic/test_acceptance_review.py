"""Independent-review regressions, initially run against frozen c1."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_alfred.evals.acceptance.examples import offline_batch
from agent_alfred.evals.acceptance.report import report
from agent_alfred.evals.acceptance.schema import digest, validate
from agent_alfred.evals.deterministic.test_acceptance import scored_fixture


def test_execution_without_source_rejects_credentials_before_factory(tmp_path):
    from agent_alfred.connections import CredentialOverlay
    from agent_alfred.evals.acceptance.budget import binding
    from agent_alfred.evals.acceptance.runner import execute

    secret = "unique-explicit-credential-84"
    batch = offline_batch()
    batch["cases"] = batch["cases"][:1]
    case = batch["cases"][0]
    case["input"] = secret
    case["material_id"] = digest({"input": case["input"], "gold": case["gold"]})
    batch["authorization"] = {
        "binding": binding(batch),
        "by": "fixture",
        "at": "2026-09-20T00:00:00Z",
        "max_requests": 2,
        "max_output_tokens": 256,
        "total_seconds": 20,
        "cost": {"amount": None, "source": "unknown"},
        "accept_unknown_cost": True,
    }
    created = []

    def forbidden_factory():
        created.append(True)
        raise AssertionError("factory must not be reached")

    from agent_alfred.evals.acceptance.safety import ensure_safe

    with pytest.raises(ValueError, match="sensitive_data"):
        ensure_safe(batch, secrets=(secret,))
    with pytest.raises(ValueError, match="simulation_session_and_mock_required"):
        execute(
            batch,
            tmp_path,
            product_factory_builder=forbidden_factory,
            credentials=CredentialOverlay({"OPENCODE_API_KEY": secret}, None),
        )
    assert not created


def test_human_confirmed_failure_is_a_failure_not_unknown():
    batch = scored_fixture()
    grade = batch["grades"][0]
    grade["disputed"] = True
    ruling = {
        "id": "human-fail",
        "grade_id": grade["id"],
        "human": "test human",
        "at": "2026-09-20T00:00:02Z",
        "reason": "confirmed incorrect",
        "rubric_id": batch["rubric"]["id"],
        "dimensions": deepcopy(grade["dimensions"]),
        "prohibitions": deepcopy(grade["prohibitions"]),
    }
    ruling["dimensions"]["correctness"]["status"] = "fail"
    batch["adjudications"] = [ruling]
    assert (
        report(batch, now=datetime(2026, 9, 20, 1, tzinfo=UTC))["quality"]["verdict"]
        == "FAIL"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda b: b.update(calibration={"material_ids": []}),
        lambda b: b.update(coverage=[{}]),
        lambda b: b["rubric"].update(approval={"kind": "approved"}),
    ],
)
def test_incomplete_proof_structures_are_rejected(mutation):
    batch = scored_fixture()
    mutation(batch)
    batch["rubric"]["id"] = digest(
        {k: v for k, v in batch["rubric"].items() if k != "id"}
    )
    with pytest.raises(ValueError):
        validate(batch)


def test_execute_checks_candidate_and_claims_batch_before_construction(tmp_path):
    from agent_alfred.evals.acceptance.budget import binding
    from agent_alfred.evals.acceptance.candidate import capture
    from agent_alfred.evals.acceptance.runner import execute
    from agent_alfred.evals.acceptance.store import EvidenceStore

    root = Path(__file__).resolve().parents[4]
    batch = offline_batch(capture(root))
    batch["cases"] = batch["cases"][:1]
    batch["authorization"] = {
        "binding": binding(batch),
        "by": "fixture",
        "at": "2026-09-20T00:00:00Z",
        "max_requests": 2,
        "max_output_tokens": 256,
        "total_seconds": 20,
        "cost": {"amount": None, "source": "unknown"},
        "accept_unknown_cost": True,
    }
    built = []

    from agent_alfred.evals.deterministic._simulation_test_helpers import (
        session_for,
        text_transport,
    )

    transport = text_transport(
        ['{"retrieve":false,"query":null,"reason_code":"greeting"}', "answer"], built
    )
    store = EvidenceStore(tmp_path / "store")
    bad = deepcopy(batch)
    bad["candidate"]["files"]["pyproject.toml"]["sha256"] = "0" * 64
    bad["candidate_id"] = digest(bad["candidate"])
    bad["authorization"]["binding"] = binding(bad)
    bad_store = EvidenceStore(tmp_path / "bad-store")
    bad_session = session_for(bad, bad_store)
    with pytest.raises(ValueError, match="candidate_changed"):
        execute(
            bad,
            tmp_path / "first",
            simulation_session=bad_session,
            mock_transport=transport,
            candidate_root=root,
            store=bad_store,
        )
    assert not built
    session = session_for(batch, store)
    result = execute(
        batch,
        tmp_path / "second",
        simulation_session=session,
        mock_transport=transport,
        candidate_root=root,
        store=store,
    )
    store.import_batch(result)
    with pytest.raises(ValueError, match="operation_already_consumed|grant_not_active"):
        execute(
            batch,
            tmp_path / "third",
            simulation_session=session,
            mock_transport=transport,
            candidate_root=root,
            store=store,
        )
    assert len(built) == 2


def test_collector_rejects_ambient_test_selection_before_spawn(tmp_path, monkeypatch):
    from agent_alfred.evals.acceptance.candidate import capture
    from agent_alfred.evals.acceptance.collect import collect_gate

    root = Path(__file__).resolve().parents[4]
    candidate = capture(root)
    monkeypatch.setenv(
        "PYTEST_ADDOPTS", "-k test_empty_report_is_blocked_without_network"
    )
    with pytest.raises(ValueError, match="test_selection_environment"):
        collect_gate("pytest", candidate, root, tmp_path)


def test_specialist_missing_or_failed_cannot_hide_behind_general_pass():
    batch = scored_fixture()
    missing = report(batch, now=datetime(2026, 9, 20, 1, tzinfo=UTC))
    assert missing["consolidation"]["verdict"] == "BLOCKED"
    assert "consolidation_missing" in missing["v1_release"]["blockers"]


def test_public_judge_persists_shared_budget_and_refuses_parent_replay(tmp_path):
    from agent_alfred.evals.acceptance import online_judge
    from agent_alfred.evals.acceptance.budget import binding
    from agent_alfred.evals.acceptance.candidate import capture
    from agent_alfred.evals.acceptance.store import EvidenceStore

    root = Path(__file__).resolve().parents[4]
    from agent_alfred.evals.acceptance.examples import SKIP
    from agent_alfred.evals.acceptance.judge_protocol import current_profile
    from agent_alfred.evals.acceptance.runner import execute
    from agent_alfred.evals.deterministic._simulation_test_helpers import (
        session_for,
        text_transport,
    )

    batch = offline_batch(capture(root))
    batch["cases"] = batch["cases"][:2]
    batch["rubric"] = scored_fixture()["rubric"]
    batch["judge_profile"] = current_profile(batch["profiles"][0]["judge_model"])
    store = EvidenceStore(tmp_path / "store")
    authorization = {
        "binding": binding(batch),
        "by": "fixture",
        "at": "2026-09-20T00:00:00Z",
        "max_requests": 5,
        "max_output_tokens": 256,
        "total_seconds": 60,
        "cost": {"amount": None, "source": "unknown"},
        "accept_unknown_cost": True,
    }
    batch["authorization"] = authorization
    seen = []
    transport = text_transport(
        [SKIP, "answer", SKIP, "answer", "not valid judge JSON"], seen
    )
    session = session_for(batch, store)
    batch = execute(
        batch,
        tmp_path / "runtime",
        simulation_session=session,
        mock_transport=transport,
        candidate_root=root,
        store=store,
    )
    assert len(batch["requests"]) == 4
    store.import_batch(batch)
    judged = online_judge.grade_batch(
        batch,
        authorization,
        simulation_session=session,
        mock_transport=transport,
        candidate_root=root,
        store=store,
        new_batch="judged",
    )
    store.revise(batch["batch_id"], "judged", grades=judged["grades"], execution=judged)
    store.publish_report("judged")
    loaded = store.read("judged")
    assert len(loaded["requests"]) == 5 and loaded["requests"][:4] == batch["requests"]
    assert loaded["authorization"] == authorization
    assert (
        loaded["budget_started_at"]
        and loaded["stop_reason"] == "batch_budget_exhausted"
    )
    assert loaded["grades"][0]["raw"] == "not valid judge JSON"
    with pytest.raises(ValueError, match="operation_already_consumed|grant_not_active"):
        online_judge.grade_batch(
            batch,
            authorization,
            simulation_session=session,
            mock_transport=transport,
            candidate_root=root,
            store=store,
            new_batch="judge-replay",
        )
    assert len(seen) == 5
    events = [
        json.loads(p.read_text())["payload"]
        for p in store.root.glob(".journal-judge-*/*.json")
    ]
    assert any(
        e["event"] == "budget" and len(e["value"]["requests"]) == 5 for e in events
    )


@pytest.mark.parametrize(
    "output", ["", {"error": "invalid_response"}, {"error": "timeout"}]
)
def test_real_host_empty_illegal_and_timeout_keep_case_and_unknown_usage(
    tmp_path, output
):
    from agent_alfred.evals.acceptance.examples import SKIP
    from agent_alfred.evals.acceptance.runner import run_offline

    batch = offline_batch()
    batch["cases"] = batch["cases"][:1]
    batch["cases"][0]["script"] = [SKIP, output]
    actual = run_offline(batch, tmp_path)
    evaluated = report(actual)
    assert len(actual["cases"]) == len(actual["results"]) == 1
    assert "product_failed:conversation" in evaluated["quality"]["failures"]
    assert "judge_missing:conversation" in evaluated["quality"]["blockers"]
    assert (
        actual["results"][0]["evidence"]["attempts"][-1]["usage"]["output_tokens"]
        is None
    )


def test_failed_tool_claim_does_not_replace_execution_fact(tmp_path):
    from agent_alfred.evals.acceptance.examples import SKIP
    from agent_alfred.evals.acceptance.runner import run_offline

    batch = offline_batch()
    batch["cases"] = batch["cases"][:1]
    batch["cases"][0]["script"] = [
        SKIP,
        {"tool": "draft_message", "arguments": {}},
        "The draft was saved successfully.",
    ]
    actual = run_offline(batch, tmp_path)
    result = actual["results"][0]
    assert result["output"] == "The draft was saved successfully."
    assert result["tools"][0]["result"] == "failed"
    assert result["tools"][0]["start_confirmation"] == "not_started"
    assert result["tools"][0]["reason"] == "invalid_input"
    assert not list(tmp_path.rglob("outbox/*.md"))
    assert report(actual)["quality"]["verdict"] == "BLOCKED"


def test_production_bait_is_not_read_and_judge_does_not_receive_it(
    tmp_path, monkeypatch
):
    from agent_alfred.evals.acceptance.runner import run_offline

    production = tmp_path / "production"
    production.mkdir()
    sentinel = "private-state-bait-84-UNIQUE"
    (production / "persona.md").write_text(sentinel)
    (production / "model_settings.json").write_text(sentinel)
    monkeypatch.setenv("AGENT_ALFRED_HOME", str(production))
    batch = offline_batch()
    batch["cases"] = batch["cases"][:1]
    actual = run_offline(batch, tmp_path / "evaluation")
    assert sentinel not in json.dumps(actual)
    assert (production / "model_settings.json").read_text() == sentinel


def test_regrade_configuration_preserves_original_product_and_requires_calibration(
    tmp_path,
):
    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch = scored_fixture()
    store = EvidenceStore(tmp_path)
    store.import_batch(batch)
    override = {
        "model": {**batch["profiles"][0]["judge_model"], "model_id": "different-judge"}
    }
    override["id"] = digest(override)
    revised = store.revise(
        batch["batch_id"],
        "reconfigured",
        configuration={"judge_profile": override, "calibration": None},
    )
    assert revised["results"] == batch["results"] and not revised["grades"]
    assert revised["authorization"] is None
    assert store.read(batch["batch_id"]) == batch


def test_report_publication_failure_keeps_batch_and_explicit_latest_identity(
    tmp_path, monkeypatch
):
    import os

    from agent_alfred.evals.acceptance.store import EvidenceStore

    store = EvidenceStore(tmp_path)
    batch = scored_fixture()
    store.import_batch(batch)
    store.publish_report(batch["batch_id"])
    latest = (tmp_path / "latest-report.json").read_bytes()
    original = os.replace

    def fail_report(source, target):
        if Path(target).name == "latest-report.json":
            raise OSError("injected rename failure")
        return original(source, target)

    monkeypatch.setattr(os, "replace", fail_report)
    with pytest.raises(OSError):
        store.publish_report(batch["batch_id"])
    assert (tmp_path / "latest-report.json").read_bytes() == latest
    assert len(list((tmp_path / "reports").glob("*.json"))) == 2
    assert store.read(batch["batch_id"]) == batch


def specialist_fixture(batch):
    approval = {
        "kind": "test",
        "by": "fixture",
        "at": "2026-09-20T00:00:00Z",
        "reference": "offline-only",
    }
    rows = []
    for i in range(30):
        row = {
            "case_id": f"s{i}",
            "run_id": f"run{i}",
            "input": f"input{i}",
            "gold": f"gold{i}",
            "output": f"output{i}",
            "source": "simulation",
            "outcome": "completed",
            "recorded": True,
            "sampled_at": "2026-09-20T00:00:00Z",
            "finished_at": "2026-09-20T00:00:01Z",
            "evidence": {
                "run_id": f"run{i}",
                "trace_status": "available",
                "recording_state": "recorded",
            },
            "review": approval,
            "correct_atoms": 1,
            "output_atoms": 1,
            "covered_items": 1,
            "expected_items": 1,
            "prohibitions": True,
            "structure_safe": True,
        }
        row["material_id"] = digest({"input": row["input"], "gold": row["gold"]})
        rows.append(row)
    doc = {
        "schema_version": 1,
        "candidate_id": batch["candidate_id"],
        "profile_id": batch["profiles"][0]["id"],
        "simulation": True,
        "approval": approval,
        "rows": rows,
    }
    doc["id"] = digest(doc)
    return doc


def test_specialist_public_report_preserves_counts_failure_staleness_and_na(tmp_path):
    from agent_alfred.evals.acceptance.store import EvidenceStore
    from agent_alfred.evals.deterministic.test_acceptance import command

    batch = scored_fixture()
    batch["specialist"] = specialist_fixture(batch)
    # CLI owns its wall clock; use samples from this invocation, not a calendar window.
    sampled = datetime.now(UTC).isoformat()
    for row in batch["specialist"]["rows"]:
        row.update(sampled_at=sampled, finished_at=sampled)
    batch["specialist"]["id"] = digest(
        {k: v for k, v in batch["specialist"].items() if k != "id"}
    )
    store = EvidenceStore(tmp_path / "store")
    store.import_batch(batch)
    first = json.loads(
        command("report", "--store", store.root, "--batch", batch["batch_id"]).stdout
    )
    assert first["consolidation"]["verdict"] == "PASS"
    assert first["v1_release"]["verdict"] == "BLOCKED" and first["simulation"]
    changed = deepcopy(batch)
    changed["batch_id"] = "specialist-fail"
    for r in changed["results"]:
        r["batch_id"] = changed["batch_id"]
    for g, r in zip(changed["grades"], changed["results"]):
        g["result_hash"] = digest(r)
    changed["specialist"]["rows"][0]["prohibitions"] = False
    changed["specialist"]["rows"][1]["sampled_at"] = "2026-09-01T00:00:00Z"
    changed["specialist"]["id"] = digest(
        {k: v for k, v in changed["specialist"].items() if k != "id"}
    )
    store.import_batch(changed)
    failed = json.loads(
        command("report", "--store", store.root, "--batch", changed["batch_id"]).stdout
    )
    assert failed["v1_release"]["verdict"] == "FAIL"
    assert "consolidation_failed" in failed["v1_release"]["failures"]
    assert any("sample_invalid" in s for s in failed["v1_release"]["blockers"])
    for row in changed["specialist"]["rows"]:
        row.update(correct_atoms=0, output_atoms=0, covered_items=0, expected_items=0)
    changed["specialist"]["id"] = digest(
        {k: v for k, v in changed["specialist"].items() if k != "id"}
    )
    empty = report(changed)["consolidation"]["counts"]
    assert (
        empty["correctness"]["status"] == "N/A" and empty["coverage"]["ratio"] is None
    )


def test_interrupted_judge_publish_recovers_without_new_calls(tmp_path, monkeypatch):
    from agent_alfred.evals.acceptance.budget import binding
    from agent_alfred.evals.acceptance.candidate import capture
    from agent_alfred.evals.acceptance.online_judge import grade_batch
    from agent_alfred.evals.acceptance.store import EvidenceStore

    root = Path(__file__).resolve().parents[4]
    b = scored_fixture()
    b["grades"] = []
    from agent_alfred.evals.acceptance.judge_protocol import current_profile

    b["judge_profile"] = current_profile(b["profiles"][0]["judge_model"])
    b["candidate"] = capture(root)
    b["candidate_id"] = digest(b["candidate"])
    store = EvidenceStore(tmp_path / "store")
    store.import_batch(b)
    auth = {
        "binding": binding(b),
        "by": "fixture",
        "at": "2026-09-20T00:00:00Z",
        "max_requests": 1,
        "max_output_tokens": 256,
        "total_seconds": 60,
        "cost": {"amount": None, "source": "unknown"},
        "accept_unknown_cost": True,
    }
    from agent_alfred.evals.deterministic._simulation_test_helpers import (
        session_for,
        text_transport,
    )

    seen = []
    transport = text_transport(["invalid judge JSON"], seen)
    session = session_for({**b, "authorization": auth}, store, operations=("judge",))
    actual = grade_batch(
        b,
        auth,
        simulation_session=session,
        mock_transport=transport,
        candidate_root=root,
        store=store,
        new_batch="unwritten",
    )
    original_write = EvidenceStore._write

    def failed_write(path, data):
        if path.name == "complete.json":
            raise OSError("injected publication failure")
        original_write(path, data)

    with monkeypatch.context() as m:
        m.setattr(EvidenceStore, "_write", staticmethod(failed_write))
        with pytest.raises(OSError):
            store.revise(
                b["batch_id"], "unwritten", grades=actual["grades"], execution=actual
            )
    recovered = store.recover(b["batch_id"], "judge", new_batch="recovered")
    assert len(recovered["requests"]) == 1 and len(recovered["grades"]) == 1
    assert len(seen) == 1
    assert recovered["stop_reason"] == "recovered_interrupted_execution"
    assert recovered["results"] == b["results"]


def sized_fixture(phase, size, batch_id):
    source = scored_fixture()
    batch = deepcopy(source)
    batch.update(phase=phase, batch_id=batch_id, cases=[], results=[], grades=[])
    for case, result, grade in zip(
        source["cases"], source["results"], source["grades"]
    ):
        for index in range(size):
            c, r, g = deepcopy(case), deepcopy(result), deepcopy(grade)
            c["id"] = f"{case['id']}-{phase}-{index}"
            c["input"] = c["id"]
            c["material_id"] = digest({"input": c["input"], "gold": c["gold"]})
            r.update(
                id="r-" + c["id"],
                case_id=c["id"],
                run_id="run-" + c["id"],
                batch_id=batch_id,
            )
            r["evidence"]["run_id"] = r["run_id"]
            g.update(
                id="g-" + c["id"],
                result_id=r["id"],
                result_hash=digest(r),
                case_hash=digest(c),
                case_material_id=c["material_id"],
            )
            for values in (g["dimensions"], g["prohibitions"]):
                for value in values.values():
                    value["evidence"] = r["id"]
            batch["cases"].append(c)
            batch["results"].append(r)
            batch["grades"].append(g)
    return batch


def test_complete_calibration_link_and_real_formal_denominator(tmp_path):
    from agent_alfred.evals.acceptance.schema import calibration_identity
    from agent_alfred.evals.acceptance.store import EvidenceStore

    store = EvidenceStore(tmp_path)
    calibration = sized_fixture("calibration", 5, "calibration")
    calibration["calibration_approval"] = {
        "kind": "test",
        "by": "fixture",
        "at": "2026-09-20T00:00:03Z",
        "reference": "fixture only",
        "evidence_sha256": calibration_identity(calibration),
    }
    store.import_batch(calibration)
    formal = sized_fixture("formal", 20, "formal")
    formal["calibration"] = {
        "batch_id": "calibration",
        "sha256": digest(calibration),
        "material_ids": [c["material_id"] for c in calibration["cases"]],
    }
    store.import_batch(formal)
    assert len(store.read("formal")["cases"]) == 120
    assert (
        store.publish_report("formal", now=datetime(2026, 9, 20, 1, tzinfo=UTC))[
            "quality"
        ]["verdict"]
        == "PASS"
    )
    changed = deepcopy(formal)
    changed["batch_id"] = "invalid-calibration"
    changed["calibration"]["material_ids"] = []
    for r, g in zip(changed["results"], changed["grades"]):
        r["batch_id"] = changed["batch_id"]
        g["result_hash"] = digest(r)
    with pytest.raises(ValueError, match="calibration_material_mismatch"):
        store.import_batch(changed)
    changed = deepcopy(formal)
    changed["batch_id"] = "changed-judge"
    override = {
        "model": {**formal["profiles"][0]["judge_model"], "model_id": "changed-judge"}
    }
    override["id"] = digest(override)
    changed["judge_profile"] = override
    for r, g in zip(changed["results"], changed["grades"]):
        r["batch_id"] = changed["batch_id"]
        g["result_hash"] = digest(r)
    with pytest.raises(ValueError, match="judge_requires_recalibration"):
        store.import_batch(changed)


def test_explicit_new_regrade_budget_preserves_old_spend_without_old_deadline(tmp_path):
    from agent_alfred.evals.acceptance.budget import AuthorizedBatch, binding
    from agent_alfred.evals.acceptance.store import EvidenceStore

    b = scored_fixture()
    spent = {
        "attempt_id": "old-product",
        "role": "product",
        "usage": None,
        "outcome": "unknown",
    }
    b.update(requests=[spent], budget_started_at="2026-09-01T00:00:00Z")
    store = EvidenceStore(tmp_path)
    store.import_batch(b)
    changed = store.revise(
        b["batch_id"], "explicit-regrade", configuration={"calibration": None}
    )
    changed["authorization"] = {
        "binding": binding(changed),
        "by": "explicit tester",
        "at": "2026-09-20T00:00:00Z",
        "max_requests": 1,
        "max_output_tokens": 256,
        "total_seconds": 30,
        "cost": {"amount": None, "source": "unknown"},
        "accept_unknown_cost": True,
    }
    budget = AuthorizedBatch(changed)
    budget.check()
    assert changed["request_history"] == [spent]
    assert store.read(b["batch_id"])["requests"] == [spent]


def test_partial_product_package_finishes_from_journal_without_resampling(
    tmp_path, monkeypatch
):
    from agent_alfred.evals.acceptance.budget import binding
    from agent_alfred.evals.acceptance.candidate import capture
    from agent_alfred.evals.acceptance.examples import SKIP
    from agent_alfred.evals.acceptance.runner import execute
    from agent_alfred.evals.acceptance.store import EvidenceStore

    root = Path(__file__).resolve().parents[4]
    batch = offline_batch(capture(root))
    batch["cases"] = batch["cases"][:1]
    batch["authorization"] = {
        "binding": binding(batch),
        "by": "fixture",
        "at": "2026-09-20T00:00:00Z",
        "max_requests": 2,
        "max_output_tokens": 256,
        "total_seconds": 30,
        "cost": {"amount": None, "source": "unknown"},
        "accept_unknown_cost": True,
    }
    from agent_alfred.evals.deterministic._simulation_test_helpers import (
        session_for,
        text_transport,
    )

    seen = []
    transport = text_transport([SKIP, "persisted answer"], seen)
    store = EvidenceStore(tmp_path / "store")
    session = session_for(batch, store)
    actual = execute(
        batch,
        tmp_path / "runtime",
        simulation_session=session,
        mock_transport=transport,
        candidate_root=root,
        store=store,
    )
    original = EvidenceStore._write

    def fail_complete(path, data):
        if path.name == "complete.json":
            raise OSError("interrupted write")
        return original(path, data)

    with monkeypatch.context() as m:
        m.setattr(EvidenceStore, "_write", staticmethod(fail_complete))
        with pytest.raises(OSError):
            store.import_batch(actual)
    recovered = store.recover(batch["batch_id"], "product")
    assert recovered == actual and len(seen) == 2
    assert store.read(batch["batch_id"]) == actual
