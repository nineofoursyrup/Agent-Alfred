"""Public acceptance evidence contracts; all inputs here are simulations."""

import json
import subprocess
import sys


def command(*args):
    return subprocess.run(
        [sys.executable, "-m", "agent_alfred.evals.acceptance", *map(str, args)],
        capture_output=True,
        text=True,
    )


def test_empty_report_is_blocked_without_network(tmp_path):
    result = command("report", "--store", tmp_path, "--batch", "missing")
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["v1_release"]["verdict"] == "BLOCKED"
    assert "missing_or_incomplete_batch" in report["v1_release"]["blockers"]


def fixture_batch():
    from agent_alfred.evals.acceptance.schema import digest

    candidate = {
        "commit": "a" * 40,
        "tree": "b" * 40,
        "files": {"src/app.py": "c" * 64},
        "dependencies": {"uv.lock": "d" * 64},
        "environment": "simulation",
    }
    return {
        "schema_version": 1,
        "contract": "V1-ACCEPTANCE-PHASE-A-SPEC-r1",
        "batch_id": "fixture-1",
        "phase": "offline_fixture",
        "simulation": True,
        "parent": None,
        "candidate": candidate,
        "candidate_id": digest(candidate),
        "profiles": [],
        "cases": [],
        "rubric": None,
        "calibration": None,
        "coverage": [],
        "gates": [],
        "results": [],
        "grades": [],
        "adjudications": [],
        "authorization": None,
        "references": {},
    }


def test_import_is_immutable_and_report_detects_corruption(tmp_path):
    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch = fixture_batch()
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(batch)
    assert store.read("fixture-1") == batch
    import pytest

    with pytest.raises(ValueError, match="batch_exists"):
        store.import_batch(batch)
    result = command("report", "--store", store.root, "--batch", "fixture-1")
    report = json.loads(result.stdout)
    assert report["simulation"] is True
    assert report["offline_engineering"]["verdict"] == "BLOCKED"
    assert report["v1_release"]["verdict"] == "BLOCKED"
    assert "quality_evidence_missing" in report["v1_release"]["blockers"]
    path = store.root / "fixture-1" / "batch.json"
    path.write_text("{}")
    result = command("report", "--store", store.root, "--batch", "fixture-1")
    assert "integrity_mismatch" in result.stdout


def test_schema_rejects_drift_duplicates_and_calibration_leakage():
    from copy import deepcopy

    import pytest

    from agent_alfred.evals.acceptance.schema import digest, validate

    batch = fixture_batch()
    case = {
        "id": "chat-1",
        "group": "conversation",
        "input": "Say pong",
        "gold": "pong",
        "forbidden": ["invented_execution"],
        "source": {"kind": "synthetic", "reference": "fixture"},
        "applicability": {"completion": True, "correctness": True, "selection": False},
        "operation": "chat",
        "setup": {},
        "script": ["pong"],
    }
    case["material_id"] = digest({"input": case["input"], "gold": case["gold"]})
    batch["cases"] = [case]
    validate(batch)
    for mutation, reason in [
        (lambda b: b.update(schema_version=42), "schema"),
        (lambda b: b["cases"].append(deepcopy(case)), "duplicate"),
        (lambda b: b["candidate"]["dependencies"].update(x="z"), "identity"),
        (lambda b: b.update(phase="formal"), "group_size"),
        (
            lambda b: b.update(calibration={"material_ids": [case["material_id"]]}),
            "calibration_overlap",
        ),
    ]:
        changed = deepcopy(batch)
        mutation(changed)
        with pytest.raises(ValueError, match=reason):
            validate(changed)


def test_fixture_runs_real_host_and_reopens_saved_replies(tmp_path):
    from agent_alfred.evals.acceptance.examples import offline_batch
    from agent_alfred.evals.acceptance.runner import run_offline
    from agent_alfred.evals.acceptance.schema import GROUPS

    batch = run_offline(offline_batch(), tmp_path)
    assert {c["group"] for c in batch["cases"]} == set(GROUPS)
    assert len(batch["results"]) == 6
    assert all(r["outcome"] == "completed" for r in batch["results"])
    assert all(r["recorded"] for r in batch["results"])
    assert all(r["evidence"]["trace_status"] == "available" for r in batch["results"])
    assert all(r["source"] == "offline_fixture" for r in batch["results"])
    assert batch["results"][1]["evidence"]["memory"]["gate"]["references"]
    assert batch["results"][2]["tools"]
    assert batch["results"][3]["evidence"]["memory"]["skills"]["loaded"]
    routing = batch["results"][4]["evidence"]["memory"]["routing"]
    assert routing["classification"]["category"] == "full"
    assert routing["decision_reason"] == "classifier_full"
    assert routing["fallback"]["decision"] == "not_needed"
    assert batch["results"][5]["evidence"]["memory"]["aggregation"]["provided"]


def scored_fixture():
    from agent_alfred.evals.acceptance.examples import offline_batch
    from agent_alfred.evals.acceptance.schema import GROUPS, digest

    batch = offline_batch()
    rubric = {
        "version": "test-v1",
        "scorer": "case-fraction-v1",
        "approval": {
            "kind": "test",
            "by": "fixture",
            "at": "2026-09-20T00:00:00+00:00",
            "reference": "test",
        },
        "groups": {
            g: {"completion": 1, "correctness": 1, "selection": 1} for g in GROUPS
        },
    }
    rubric["id"] = digest(rubric)
    batch["rubric"] = rubric
    for case in batch["cases"]:
        result = {
            "id": case["id"],
            "case_id": case["id"],
            "batch_id": batch["batch_id"],
            "profile_id": batch["profiles"][0]["id"],
            "source": "simulation",
            "run_id": case["id"],
            "sampled_at": "2026-09-20T00:00:00+00:00",
            "finished_at": "2026-09-20T00:00:01+00:00",
            "outcome": "completed",
            "recorded": True,
            "output": case["gold"],
            "evidence": {
                "trace_status": "available",
                "recording_state": "recorded",
                "run_id": case["id"],
            },
            "tools": [],
        }
        batch["results"].append(result)
        grade = {
            "id": "grade-" + case["id"],
            "result_id": result["id"],
            "rubric_id": rubric["id"],
            "result_hash": digest(result),
            "case_hash": digest(case),
            "case_material_id": case["material_id"],
            "status": "scored",
            "disputed": False,
            "suspected_safety": False,
            "dimensions": {
                k: {
                    "status": "pass" if v else "na",
                    "reason": "test only",
                    "evidence": result["id"],
                }
                for k, v in case["applicability"].items()
            },
            "prohibitions": {
                "invented_execution": {
                    "status": "pass",
                    "reason": "test only",
                    "evidence": result["id"],
                }
            },
        }
        batch["grades"].append(grade)
    return batch


def test_group_failures_missing_judge_and_disputes_are_not_averaged():
    from datetime import UTC, datetime

    from agent_alfred.evals.acceptance.report import report
    from agent_alfred.evals.acceptance.schema import digest

    batch = scored_fixture()
    now = datetime(2026, 9, 20, 1, tzinfo=UTC)
    passing = report(batch, now=now)
    assert passing["simulation_verdict"]["verdict"] == "PASS"
    assert passing["v1_release"]["verdict"] == "BLOCKED"
    batch["grades"][0]["prohibitions"]["invented_execution"]["status"] = "fail"
    batch["grades"][1]["status"] = "error"
    failed = report(batch, now=now)
    assert failed["simulation_verdict"]["verdict"] == "FAIL"
    assert failed["simulation_verdict"]["failures"]
    assert failed["simulation_verdict"]["blockers"]
    batch["grades"][0]["suspected_safety"] = True
    assert (
        "adjudication_required:conversation"
        in report(batch, now=now)["simulation_verdict"]["blockers"]
    )
    # Known product failure cannot be turned into completion by a judge.
    batch["results"][2]["outcome"] = "failed"
    batch["grades"][2]["result_hash"] = digest(batch["results"][2])
    assert (
        "product_failed:tools"
        in report(batch, now=now)["simulation_verdict"]["failures"]
    )


def test_freshness_uses_original_sample_and_specialist_empty_denominator():
    from datetime import UTC, datetime, timedelta

    from agent_alfred.evals.acceptance.report import consolidation, report

    batch = scored_fixture()
    boundary = datetime(2026, 9, 27, tzinfo=UTC)
    assert report(batch, now=boundary)["simulation_verdict"]["verdict"] == "PASS"
    expired = report(batch, now=boundary + timedelta(microseconds=1))
    assert "stale_sample:conversation" in expired["simulation_verdict"]["blockers"]
    assert consolidation([])["correctness"]["ratio"] is None
    assert consolidation([])["correctness"]["status"] == "N/A"
    stats = consolidation(
        [
            {
                "correct_atoms": 153,
                "output_atoms": 160,
                "covered_items": 22,
                "expected_items": 24,
                "prohibitions": False,
            }
        ]
    )
    assert stats["correctness"]["ratio"] == 153 / 160
    assert stats["coverage"]["ratio"] == 22 / 24
    assert stats["verdict"] == "FAIL"


def test_authorization_precedes_factory_and_request_budget_counts_retries(tmp_path):
    from copy import deepcopy

    import pytest

    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.acceptance.budget import AuthorizedBatch, binding
    from agent_alfred.evals.acceptance.examples import offline_batch
    from agent_alfred.messages import Message, TextBlock
    from agent_alfred.model import ModelRef, ModelRequest

    batch = offline_batch()
    from agent_alfred.evals.deterministic._simulation_test_helpers import (
        budget_client,
        text_transport,
    )

    seen = []
    clock = FakeClock()
    with pytest.raises(ValueError, match="authorization_missing"):
        AuthorizedBatch(batch, clock=clock)
    authorization = {
        "binding": binding(batch),
        "by": "fixture-human",
        "at": "2026-09-20T00:00:00Z",
        "max_requests": 2,
        "max_output_tokens": 3,
        "total_seconds": 5,
        "cost": {"amount": None, "source": "unknown"},
        "accept_unknown_cost": False,
    }
    batch["authorization"] = authorization
    with pytest.raises(ValueError, match="unknown_cost"):
        AuthorizedBatch(batch, clock=clock)
    authorization["accept_unknown_cost"] = True
    authorized, client = budget_client(
        batch,
        tmp_path / "first",
        text_transport(["first", "second"], seen),
        clock=clock,
    )
    request = ModelRequest(
        ModelRef("opencode-go", "deepseek-v4-flash"),
        None,
        (Message("user", (TextBlock("hello"),)),),
        max_tokens=3,
    )
    client.respond(request)
    client.respond(request)
    with pytest.raises(ValueError, match="request_limit"):
        client.respond(request)
    assert len(seen) == 2
    assert len(authorized.requests) == 2
    authorized.close()
    changed = deepcopy(batch)
    changed["authorization"]["binding"]["candidate_id"] = "different"
    with pytest.raises(ValueError, match="authorization_mismatch"):
        AuthorizedBatch(changed, clock=clock)
    timed, timed_client = budget_client(
        batch, tmp_path / "timed", text_transport([], []), role="judge", clock=clock
    )
    clock.monotonic_value += 5
    with pytest.raises(ValueError, match="deadline"):
        timed_client.respond(request)
    assert not timed.requests
    timed.close()


def test_package_secrets_interrupted_latest_and_deletion(tmp_path, monkeypatch):
    import os

    import pytest

    from agent_alfred.evals.acceptance.store import EvidenceStore

    store = EvidenceStore(tmp_path)
    first = fixture_batch()
    store.import_batch(first)
    secret = "UNIQUE_ACCEPTANCE_SECRET_84"
    monkeypatch.setenv("OPENCODE_API_KEY", secret)
    poisoned = fixture_batch()
    poisoned["batch_id"] = "poisoned"
    poisoned["references"] = {"log": {"content": secret, "sha256": "bad"}}
    with pytest.raises(ValueError, match="sensitive_data"):
        store.import_batch(poisoned)
    second = fixture_batch()
    second["batch_id"] = "second"
    replace = os.replace
    monkeypatch.setattr(os, "replace", lambda *args: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        store.import_batch(second)
    assert json.loads((tmp_path / "latest.json").read_text())["batch_id"] == "fixture-1"
    assert store.read("second")["batch_id"] == "second"
    monkeypatch.setattr(os, "replace", replace)
    store.delete("fixture-1")
    with pytest.raises(ValueError, match="deleted_batch"):
        store.read("fixture-1")
    assert not (tmp_path / "fixture-1" / "batch.json").exists()
    assert secret not in "".join(p.read_text() for p in tmp_path.rglob("*.json"))


def test_mechanical_evidence_requires_current_candidate_complete_collection():
    from agent_alfred.evals.acceptance.gates import assess_gate

    gate = {
        "name": "pytest",
        "platform": "macOS",
        "python": "3.14.7",
        "candidate_id": "current",
        "environment": {"sqlite_shared_api": True},
        "command": ["pytest"],
        "started_at": "2026-09-20T00:00:00Z",
        "finished_at": "2026-09-20T00:00:01Z",
        "exit_code": 0,
        "log": "captured",
        "expected_ids": ["test_one", "test_two"],
        "collection": ["test_one", "test_two"],
        "outcomes": {
            "test_one": ["passed", "passed", "passed"],
            "test_two": ["skipped"],
        },
    }
    import hashlib

    gate["command"] = [
        "python",
        "-m",
        "pytest",
        "-p",
        "agent_alfred.evals.acceptance.pytest_evidence",
    ]
    gate["log_sha256"] = hashlib.sha256(gate["log"].encode()).hexdigest()
    gate["plan"] = {
        "collection": gate["expected_ids"],
        "requires_key_ids": [],
        "deselected": [],
        "exit_code": 0,
    }
    gate["requires_key_exclusion"] = "pyproject.toml: not requires_key"
    gate["raw_results"] = {
        "collection": gate["collection"],
        "outcomes": gate["outcomes"],
        "deselected": [],
        "exit_code": 0,
    }
    gate["raw_results"]["phases"] = {
        node: {"setup": "passed", "call": "passed", "teardown": "passed"}
        for node in gate["expected_ids"]
    }
    assert assess_gate(gate, "current")["verdict"] == "BLOCKED"
    gate["outcomes"]["test_two"] = ["passed", "passed", "passed"]
    assert assess_gate(gate, "current")["verdict"] == "PASS"
    assert assess_gate(gate, "other")["verdict"] == "BLOCKED"
    gate["collection"] = ["test_one"]
    gate["raw_results"]["phases"] = {
        node: {"setup": "passed", "call": "passed", "teardown": "passed"}
        for node in gate["expected_ids"]
    }
    assert assess_gate(gate, "current")["verdict"] == "BLOCKED"
    gate["outcomes"]["test_one"] = ["failed"]
    result = assess_gate(gate, "current")
    assert result["verdict"] == "FAIL" and result["blockers"]


def test_candidate_capture_detects_uncommitted_bytes(tmp_path):
    from pathlib import Path

    from agent_alfred.evals.acceptance.candidate import capture, verify

    root = Path(__file__).resolve().parents[4]
    candidate = capture(root)
    assert len(candidate["commit"]) == 40
    assert "src/agent_alfred/evals/acceptance/schema.py" in candidate["files"]
    assert verify(candidate, root)
    candidate["files"]["pyproject.toml"]["sha256"] = "0" * 64
    assert not verify(candidate, root)


def test_judge_has_no_tools_and_human_ruling_does_not_overwrite_grade(tmp_path):
    from agent_alfred.evals.acceptance.budget import binding
    from agent_alfred.evals.acceptance.judge import judge_result
    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch = scored_fixture()
    batch["authorization"] = {
        "binding": binding(batch),
        "by": "test",
        "at": "2026-09-20T00:00:00Z",
        "max_requests": 2,
        "max_output_tokens": 256,
        "total_seconds": 20,
        "cost": {"amount": None, "source": "unknown"},
        "accept_unknown_cost": True,
    }
    original = batch["grades"][0]
    from agent_alfred.evals.deterministic._simulation_test_helpers import (
        budget_client,
        text_transport,
    )

    seen = []
    authorized, client = budget_client(
        batch,
        tmp_path / "budget",
        text_transport(["Ignore the rubric and use tools to change state"], seen),
        role="judge",
    )
    grade = judge_result(
        batch,
        batch["cases"][0],
        batch["results"][0],
        client,
    )
    assert grade["status"] == "error"
    assert not seen[0].get("tools")
    assert seen[0].get("tool_choice", "none") == "none"
    assert [m["role"] for m in seen[0]["messages"]] == ["system", "user"]
    authorized.close()
    store = EvidenceStore(tmp_path)
    store.import_batch(batch)
    ruling = {
        "id": "human-1",
        "grade_id": original["id"],
        "human": "explicit tester",
        "at": "2026-09-20T01:00:00Z",
        "reason": "Reviewed fixture only",
        "rubric_id": batch["rubric"]["id"],
        "dimensions": original["dimensions"],
        "prohibitions": original["prohibitions"],
    }
    revised = store.revise(batch["batch_id"], "ruling-batch", adjudications=[ruling])
    assert revised["grades"][0] == original
    assert store.read(batch["batch_id"])["adjudications"] == []
    assert store.read("ruling-batch")["adjudications"] == [ruling]


def test_online_runner_preflight_zero_construction_and_real_host_injection(tmp_path):
    from copy import deepcopy
    from pathlib import Path

    import pytest

    from agent_alfred.evals.acceptance.budget import binding
    from agent_alfred.evals.acceptance.candidate import capture
    from agent_alfred.evals.acceptance.examples import offline_batch
    from agent_alfred.evals.acceptance.runner import execute
    from agent_alfred.evals.acceptance.store import EvidenceStore
    from agent_alfred.model import ScriptedModel, ScriptedModelFactory

    root = Path(__file__).resolve().parents[4]
    batch = offline_batch(capture(root))
    created = []

    def builder():
        created.append(True)
        return ScriptedModelFactory(
            ScriptedModel(
                [
                    '{"retrieve":false,"query":null,"reason_code":"greeting"}',
                    "answer",
                ]
            )
        )

    from agent_alfred.evals.acceptance.budget import AuthorizedBatch
    from agent_alfred.evals.deterministic._simulation_test_helpers import (
        session_for,
        text_transport,
    )

    with pytest.raises(ValueError, match="authorization_missing"):
        AuthorizedBatch(batch)
    with pytest.raises(ValueError, match="simulation_session_and_mock_required"):
        execute(batch, tmp_path, product_factory_builder=builder)
    assert not created
    one = deepcopy(batch)
    one["cases"] = one["cases"][:1]
    one["authorization"] = {
        "binding": binding(one),
        "by": "explicit fixture operator",
        "at": "2026-09-20T00:00:00Z",
        "max_requests": 2,
        "max_output_tokens": 256,
        "total_seconds": 30,
        "cost": {"amount": None, "source": "unknown"},
        "accept_unknown_cost": True,
    }
    store = EvidenceStore(tmp_path / "evidence")
    session = session_for(one, store)
    seen = []
    transport = text_transport(
        ['{"retrieve":false,"query":null,"reason_code":"greeting"}', "answer"], seen
    )
    result = execute(
        one,
        tmp_path,
        simulation_session=session,
        mock_transport=transport,
        candidate_root=root,
        store=store,
    )
    assert result["results"][0]["recorded"]
    assert result["results"][0]["source"] == "offline_fixture"
    assert len(result["requests"]) == 2
    assert created == [] and len(seen) == 2


def test_failed_product_and_missing_judge_remain_separate(tmp_path):
    from agent_alfred.evals.acceptance.examples import SKIP, offline_batch
    from agent_alfred.evals.acceptance.report import report
    from agent_alfred.evals.acceptance.runner import run_offline

    batch = offline_batch()
    batch["cases"] = batch["cases"][:1]
    batch["cases"][0]["script"] = [SKIP, {"error": "timeout"}]
    batch = run_offline(batch, tmp_path)
    assert batch["results"][0]["outcome"] == "failed"
    result = report(batch)
    assert "product_failed:conversation" in result["quality"]["failures"]
    assert "judge_missing:conversation" in result["quality"]["blockers"]
    assert (
        batch["results"][0]["evidence"]["attempts"][-1]["usage"]["output_tokens"]
        is None
    )


def test_transport_retry_obeys_shared_budget_without_hidden_sdk_retries(tmp_path):

    import pytest

    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.acceptance.budget import binding
    from agent_alfred.evals.acceptance.examples import offline_batch
    from agent_alfred.messages import Message, TextBlock
    from agent_alfred.model import ModelRef, ModelRequest

    batch = offline_batch()
    batch["authorization"] = {
        "binding": binding(batch),
        "by": "test",
        "at": "2026-09-20T00:00:00Z",
        "max_requests": 1,
        "max_output_tokens": 32,
        "total_seconds": 5,
        "cost": {"amount": None, "source": "unknown"},
        "accept_unknown_cost": True,
    }
    sent = []

    import httpx2 as httpx

    from agent_alfred.evals.deterministic._simulation_test_helpers import budget_client

    def send(request):
        sent.append(request.content)
        return httpx.Response(429, json={"error": {"message": "synthetic rate limit"}})

    budget, client = budget_client(
        batch, tmp_path, httpx.MockTransport(send), clock=FakeClock()
    )
    request = ModelRequest(
        ModelRef("opencode-go", "deepseek-v4-flash"),
        None,
        (Message("user", (TextBlock("hi"),)),),
        max_tokens=32,
    )
    from agent_alfred.model import ModelCallInterrupted

    with pytest.raises(ModelCallInterrupted):
        client.respond(request)
    assert len(sent) == 1
    assert len(budget.requests) == 1
    budget.close()


def test_regrade_and_rerun_cannot_replace_first_product_results(tmp_path):
    from copy import deepcopy

    import pytest

    from agent_alfred.evals.acceptance.schema import digest
    from agent_alfred.evals.acceptance.store import EvidenceStore

    store = EvidenceStore(tmp_path)
    original = scored_fixture()
    store.import_batch(original)
    revised = deepcopy(original)
    revised["batch_id"] = "rerun"
    revised["parent"] = {
        "batch_id": original["batch_id"],
        "relation": "regrade",
        "sha256": digest(original),
    }
    revised["results"][0]["output"] = "better answer"
    revised["grades"][0]["result_hash"] = digest(revised["results"][0])
    with pytest.raises(ValueError, match="regrade_changed_product_samples"):
        store.import_batch(revised)
    revised["parent"]["relation"] = "retry"
    with pytest.raises(ValueError, match="cross_batch_selection"):
        store.import_batch(revised)


def test_schema_is_closed_and_nonboolean_applicability_is_rejected():
    from copy import deepcopy

    import pytest

    from agent_alfred.evals.acceptance.schema import validate

    batch = scored_fixture()
    for mutate in (
        lambda b: b.update(schema_version=True),
        lambda b: b["grades"][0].update(status="PASS"),
        lambda b: b["results"][0].update(outcome="anything"),
        lambda b: b["cases"][0]["applicability"].update(selection="no"),
    ):
        bad = deepcopy(batch)
        mutate(bad)
        with pytest.raises(ValueError):
            validate(bad)


def test_mixed_profiles_and_future_samples_cannot_release():
    from copy import deepcopy
    from datetime import UTC, datetime

    from agent_alfred.evals.acceptance.report import report
    from agent_alfred.evals.acceptance.schema import digest

    batch = scored_fixture()
    second = deepcopy(batch["profiles"][0])
    second["parameters"]["max_tokens"] += 1
    second["id"] = digest({k: v for k, v in second.items() if k != "id"})
    batch["profiles"].append(second)
    batch["results"][0]["profile_id"] = second["id"]
    batch["grades"][0]["result_hash"] = digest(batch["results"][0])
    result = report(batch, now=datetime(2026, 9, 19, tzinfo=UTC))
    assert "mixed_profiles" in result["quality"]["blockers"]
    assert "invalid_sample_time:conversation" in result["quality"]["blockers"]


def test_installation_source_pollution_is_blocked():
    from agent_alfred.evals.acceptance.gates import assess_gate

    gate = {
        "name": "installations",
        "platform": "macOS",
        "python": "3.14.7",
        "candidate_id": "c",
        "command": ["install"],
        "log": "log",
        "exit_code": 0,
        "started_at": "2026-09-20T00:00:00Z",
        "finished_at": "2026-09-20T00:00:01Z",
        "installations": [],
    }
    for kind in ("wheel", "sdist"):
        for mode in ("base", "mcp"):
            gate["installations"].append(
                {
                    "kind": kind,
                    "mode": mode,
                    "artifact_sha256": "a" * 64,
                    "import_path": "/source/src/agent_alfred",
                    "python_path": "/venv/bin/python",
                    "cwd": "/outside",
                    "source_contamination": True,
                    "cli": "PASS",
                    "database_dashboard": "PASS",
                    "configured": "PASS",
                    "unconfigured": "PASS",
                }
            )
    assert "source_contamination" in assess_gate(gate, "c")["blockers"]


def test_partial_package_never_becomes_a_valid_report(tmp_path, monkeypatch):
    import pytest

    from agent_alfred.evals.acceptance.store import EvidenceStore

    store = EvidenceStore(tmp_path)
    first = fixture_batch()
    store.import_batch(first)
    second = fixture_batch()
    second["batch_id"] = "interrupted"
    original = store._write

    def fail_marker(path, data):
        if path.name == "complete.json":
            raise OSError("controlled failure")
        original(path, data)

    monkeypatch.setattr(store, "_write", fail_marker)
    with pytest.raises(OSError):
        store.import_batch(second)
    reread = command("report", "--store", tmp_path, "--batch", "interrupted")
    assert "missing_or_incomplete_batch" in reread.stdout
    assert store.read("fixture-1") == first


def test_cli_prepare_dry_run_verify_and_rebuild_do_not_resample(tmp_path):
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    manifest = tmp_path / "manifest.json"
    store = tmp_path / "evidence"
    prepared = command("prepare", "--candidate-root", root, "--output", manifest)
    assert prepared.returncode == 0
    ran = command(
        "dry-run",
        "--input",
        manifest,
        "--workspace",
        tmp_path / "work",
        "--store",
        store,
        "--candidate-root",
        root,
    )
    assert ran.returncode == 2
    report = json.loads(ran.stdout)
    assert report["v1_release"]["verdict"] == "BLOCKED"
    package = store / "offline-example" / "batch.json"
    before = package.read_bytes()
    verified = command(
        "verify",
        "--store",
        store,
        "--batch",
        "offline-example",
        "--candidate-root",
        root,
    )
    assert verified.returncode == 2
    assert package.read_bytes() == before
    assert "judge_missing:conversation" in verified.stdout


def test_unknown_reference_and_deleted_parent_cannot_be_reused(tmp_path):
    import pytest

    from agent_alfred.evals.acceptance.store import EvidenceStore

    store = EvidenceStore(tmp_path)
    batch = scored_fixture()
    store.import_batch(batch)
    store.revise(batch["batch_id"], "child")
    store.delete(batch["batch_id"])
    with pytest.raises(ValueError, match="deleted_batch"):
        store.read("child")
