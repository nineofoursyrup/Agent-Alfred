"""Synthetic approval compatibility through public reports and stored references."""

from datetime import UTC, datetime

import pytest

from agent_alfred.evals.acceptance.report import report
from agent_alfred.evals.acceptance.schema import calibration_identity, digest
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.deterministic.test_acceptance import scored_fixture
from agent_alfred.evals.deterministic.test_acceptance_review import sized_fixture

NOW = datetime(2026, 9, 20, 1, tzinfo=UTC)


def rehash(value):
    value["id"] = digest({key: item for key, item in value.items() if key != "id"})


def rubric_kind(batch, kind):
    batch["rubric"]["approval"]["kind"] = kind
    batch["rubric"]["approval"]["reference"] = "SYNTHETIC compatibility test only"
    rehash(batch["rubric"])
    for grade in batch["grades"]:
        grade["rubric_id"] = batch["rubric"]["id"]


def legacy_calibration_pair(kind):
    source = sized_fixture("calibration", 5, "calibration")
    source["calibration_approval"] = {
        "kind": kind,
        "by": "SYNTHETIC fixture",
        "at": "2026-09-20T00:00:03Z",
        "reference": "SYNTHETIC compatibility test only",
        "evidence_sha256": calibration_identity(source),
    }
    target = sized_fixture("formal", 20, "formal")
    target["calibration"] = {
        "batch_id": source["batch_id"],
        "sha256": digest(source),
        "material_ids": [case["material_id"] for case in source["cases"]],
    }
    return source, target


def contents(store):
    return {
        p.relative_to(store.root): p.read_bytes()
        for p in store.root.rglob("*")
        if p.is_file()
    }


def test_legacy_simulation_approved_rubric_stays_blocked_after_roundtrip(tmp_path):
    batch = scored_fixture()
    rubric_kind(batch, "approved")
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(batch)
    saved = store.read(batch["batch_id"])
    assert saved == batch
    before = contents(store)
    value = report(saved, store=store, now=NOW)
    assert value["quality"]["verdict"] == "BLOCKED"
    assert value["quality"]["blockers"] == ["rubric_approval_missing"]
    assert value["simulation_verdict"]["verdict"] == "BLOCKED"
    assert contents(store) == before


def test_legacy_simulation_approved_calibration_is_not_admitted_as_test(tmp_path):
    source, target = legacy_calibration_pair("approved")
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    assert store.read(source["batch_id"]) == source
    before = contents(store)
    with pytest.raises(ValueError, match="calibration_incomplete"):
        store.import_batch(target)
    assert contents(store) == before
    assert not (store.root / target["batch_id"]).exists()


@pytest.mark.parametrize("simulation", [False, True])
@pytest.mark.parametrize("kind", ["approved", "test"])
def test_legacy_calibration_case_approval_keeps_its_approved_contract(
    tmp_path,
    simulation,
    kind,
):
    from agent_alfred.evals.acceptance.calibration import validate_source
    from agent_alfred.evals.deterministic.test_acceptance_c4 import (
        declared_online_fixture,
        seal,
    )

    # Adversarial metadata: this validator historically requires approved case
    # material regardless of the outer simulation flag; no model is called.
    source = declared_online_fixture("calibration", 5, "source")
    source["simulation"] = simulation
    source["case_set_approval"]["kind"] = kind
    seal(source)
    store = EvidenceStore(tmp_path / "evidence")
    if kind == "approved":
        from agent_alfred.evals.acceptance.calibration import validate_source_facts

        validate_source_facts(source, store)
        with pytest.raises(ValueError, match="approval_source_unverifiable"):
            validate_source(source, store)
    else:
        with pytest.raises(ValueError, match="calibration_case_approval_missing"):
            validate_source(source, store)
    assert not store.root.exists()


def legacy_online(version, phase, size, batch_id):
    from agent_alfred.evals.acceptance.budget import binding
    from agent_alfred.evals.deterministic.test_acceptance_c4 import (
        declared_online_fixture,
    )

    batch = declared_online_fixture(phase, size, batch_id)
    batch["schema_version"] = version
    if version == 2:
        profile = batch["profiles"][0]
        profile["local_tool_allowlist"] = ["draft_message"]
        rehash(profile)
        for result, grade in zip(batch["results"], batch["grades"], strict=True):
            result["profile_id"] = profile["id"]
            grade["result_hash"] = digest(result)
    batch["authorization"]["binding"] = binding(batch)
    return batch


@pytest.mark.parametrize("version,simulation", [(1, True), (1, False), (2, False)])
@pytest.mark.parametrize("kind", ["test", "approved"])
def test_legacy_rubric_approval_matrix_preserves_public_report(
    tmp_path,
    version,
    simulation,
    kind,
):
    batch = (
        scored_fixture()
        if simulation
        else legacy_online(
            version,
            "trial" if version == 2 else "calibration",
            1 if version == 2 else 5,
            "legacy-report",
        )
    )
    rubric_kind(batch, kind)
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(batch)
    saved = store.read(batch["batch_id"])
    assert saved == batch
    value = report(saved, store=store, now=NOW)
    accepted = kind == ("test" if simulation else "approved")
    assert value["quality"]["verdict"] == ("PASS" if accepted else "BLOCKED")
    assert value["quality"]["blockers"] == (
        [] if accepted else ["rubric_approval_missing"]
    )


@pytest.mark.parametrize("boundary", ["report", "store"])
@pytest.mark.parametrize("kind", ["test", "approved"])
def test_schema2_simulation_remains_invalid_before_approval_evaluation(
    tmp_path,
    boundary,
    kind,
):
    batch = legacy_online(2, "trial", 1, "invalid-schema2-simulation")
    batch["simulation"] = True
    rubric_kind(batch, kind)
    store = EvidenceStore(tmp_path / "evidence")
    with pytest.raises(ValueError, match="trial_requires_real_execution"):
        if boundary == "report":
            report(batch, now=NOW)
        else:
            store.import_batch(batch)
    assert not store.root.exists()


def test_legacy_test_calibration_roundtrip_remains_accepted(tmp_path):
    source, target = legacy_calibration_pair("test")
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    store.import_batch(target)
    assert store.read(source["batch_id"]) == source
    assert store.read(target["batch_id"]) == target
    assert report(target, store=store, now=NOW)["quality"]["verdict"] == "PASS"


@pytest.mark.parametrize("kind", ["test", "approved"])
def test_legacy_nonsimulation_calibration_requires_approved(tmp_path, kind):
    from agent_alfred.evals.deterministic.test_acceptance_c4 import seal

    source = legacy_online(1, "calibration", 5, "online-source")
    seal(source)
    source["calibration_approval"]["kind"] = kind
    target = legacy_online(1, "formal", 20, "online-target")
    target["calibration"] = {
        "batch_id": source["batch_id"],
        "sha256": digest(source),
        "material_ids": [case["material_id"] for case in source["cases"]],
    }
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    before = contents(store)
    if kind == "approved":
        store.import_batch(target)
        assert store.read(target["batch_id"]) == target
    else:
        with pytest.raises(ValueError, match="calibration_incomplete"):
            store.import_batch(target)
        assert contents(store) == before
    assert store.read(source["batch_id"]) == source


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("defect", ["test_approval", "case_source", "case_hash"])
def test_legacy_case_material_contract_still_blocks_public_preflight(
    tmp_path,
    version,
    defect,
):
    from agent_alfred.evals.acceptance.budget import validate_execution_materials

    batch = legacy_online(
        version,
        "trial" if version == 2 else "calibration",
        1 if version == 2 else 5,
        "case-contract",
    )
    if defect == "test_approval":
        batch["case_set_approval"]["kind"] = "test"
    elif defect == "case_source":
        batch["cases"][0]["source"]["kind"] = "synthetic"
        batch["case_set_approval"]["cases_sha256"] = digest(batch["cases"])
        batch["grades"][0]["case_hash"] = digest(batch["cases"][0])
    else:
        batch["case_set_approval"]["cases_sha256"] = "0" * 64
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(batch)
    saved = store.read(batch["batch_id"])
    before = contents(store)
    with pytest.raises(ValueError, match="case_set_approval_missing"):
        validate_execution_materials(saved, store)
    assert contents(store) == before


@pytest.mark.parametrize(
    "simulation,kind",
    [
        (True, "test"),
        (True, "agent_approved"),
        (False, "agent_approved"),
    ],
)
def test_schema3_test_and_agent_approval_rules_keep_public_quality(
    tmp_path,
    simulation,
    kind,
):
    from agent_alfred.evals.acceptance.judge import judge_result
    from agent_alfred.evals.deterministic.test_acceptance_material_chronology import (
        declared_calibration_with_time,
    )
    from agent_alfred.evals.deterministic.test_acceptance_policy_provenance import (
        material_approval,
    )
    from agent_alfred.evals.deterministic.test_acceptance_schema3 import (
        aggregation_for,
        scored_v3,
    )
    from agent_alfred.model import ScriptedModel

    if simulation:
        batch = scored_v3("offline_fixture", 1, "schema3-policy")
        if kind == "agent_approved":
            batch["semantic_rubric"]["approval"] = material_approval(batch)
            rehash(batch["semantic_rubric"])
            raw = [grade["raw"] for grade in batch["grades"]]
            batch["authorization"] = {"max_output_tokens": 256}
            batch["grades"] = [
                judge_result(batch, case, result, ScriptedModel([text]))
                for case, result, text in zip(
                    batch["cases"], batch["results"], raw, strict=True
                )
            ]
            batch["authorization"] = None
    else:
        batch, _ = declared_calibration_with_time("case", "2026-09-23T00:00:00Z")
    batch["aggregation_policy"] = aggregation_for(batch)
    if not simulation:
        batch["aggregation_policy"]["approval"]["kind"] = "approved"
        rehash(batch["aggregation_policy"])
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(batch)
    saved = store.read(batch["batch_id"])
    assert saved == batch
    value = report(saved, store=store, now=datetime(2026, 9, 23, 2, tzinfo=UTC))
    assert value["quality"]["verdict"] == "PASS"
    assert value["quality"]["blockers"] == []


def test_read_rejects_preexisting_legacy_simulated_approved_calibration(tmp_path):
    import hashlib

    from agent_alfred.evals.acceptance.schema import encode

    source, target = legacy_calibration_pair("test")
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    store.import_batch(target)
    source["calibration_approval"]["kind"] = "approved"
    target["calibration"]["sha256"] = digest(source)
    # Synthetic historical package with self-consistent hashes, never old user data.
    for batch in (source, target):
        package = store.root / batch["batch_id"]
        payload = encode(batch)
        (package / "batch.json").write_bytes(payload)
        (package / "complete.json").write_bytes(
            encode(
                {
                    "batch.json": hashlib.sha256(payload).hexdigest(),
                }
            )
        )
    before = contents(store)
    with pytest.raises(ValueError, match="calibration_incomplete"):
        store.read(target["batch_id"])
    assert contents(store) == before
