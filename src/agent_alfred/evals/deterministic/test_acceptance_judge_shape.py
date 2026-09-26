"""Schema3 independent judge overrides cannot carry unconsumed inputs."""

from copy import deepcopy

import pytest

from agent_alfred.evals.acceptance.examples_v3 import controlled_batch
from agent_alfred.evals.acceptance.schema import digest, validate
from agent_alfred.evals.acceptance.store import EvidenceStore


def rehash(value):
    value["id"] = digest({k: v for k, v in value.items() if k != "id"})


def detached_judge():
    batch = controlled_batch()
    batch["judge_profile"] = deepcopy(batch["judge_profile"])
    assert batch["judge_profile"]["model"] is not batch["profiles"][0]["judge_model"]
    return batch


@pytest.mark.parametrize(
    "key,value",
    [
        ("max_retries", 2),
        ("temperature", 0.75),
        ("timeout", 3600),
    ],
)
@pytest.mark.parametrize("boundary", ["validate", "store"])
def test_schema3_rejects_unknown_judge_profile_fields_without_writing(
    tmp_path,
    key,
    value,
    boundary,
):
    batch = detached_judge()
    original_profiles = deepcopy(batch["profiles"])
    batch["judge_profile"][key] = value
    rehash(batch["judge_profile"])
    assert batch["profiles"] == original_profiles
    store = EvidenceStore(tmp_path / "evidence")
    with pytest.raises(ValueError, match="unknown_judge_profile_field"):
        if boundary == "validate":
            validate(batch)
        else:
            store.import_batch(batch)
    assert not store.root.exists()


@pytest.mark.parametrize(
    "key,value",
    [
        ("max_retries", 2),
        ("temperature", 0.75),
        ("timeout", 3600),
    ],
)
@pytest.mark.parametrize("boundary", ["validate", "store"])
def test_schema3_rejects_unknown_detached_judge_model_fields_without_writing(
    tmp_path,
    key,
    value,
    boundary,
):
    batch = detached_judge()
    original_profiles = deepcopy(batch["profiles"])
    batch["judge_profile"]["model"][key] = value
    rehash(batch["judge_profile"])
    assert batch["profiles"] == original_profiles
    store = EvidenceStore(tmp_path / "evidence")
    with pytest.raises(ValueError, match="unknown_model_field"):
        if boundary == "validate":
            validate(batch)
        else:
            store.import_batch(batch)
    assert not store.root.exists()


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"thinking": "disabled"},
        {"response_format": "json_object"},
        {"thinking": "disabled", "response_format": "json_object"},
    ],
)
def test_schema3_roundtrips_legal_detached_judge_options(tmp_path, options):
    batch = detached_judge()
    model = batch["judge_profile"]["model"]
    for key in ("thinking", "response_format"):
        model.pop(key, None)
    model.update(options)
    rehash(batch["judge_profile"])
    validate(batch)
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(batch)
    assert store.read(batch["batch_id"]) == batch


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("location", ["profile", "model"])
@pytest.mark.parametrize(
    "key,value",
    [
        ("max_retries", 2),
        ("temperature", 0.75),
        ("timeout", 3600),
    ],
)
def test_legacy_judge_override_shapes_keep_their_original_interpretation(
    tmp_path,
    version,
    location,
    key,
    value,
):
    from agent_alfred.evals.acceptance.examples import offline_batch
    from agent_alfred.evals.acceptance.judge_protocol import current_profile
    from agent_alfred.evals.deterministic.test_acceptance_trial import trial_batch

    batch = offline_batch() if version == 1 else trial_batch()
    batch["judge_profile"] = current_profile(
        deepcopy(batch["profiles"][0]["judge_model"])
    )
    target = (
        batch["judge_profile"]
        if location == "profile"
        else batch["judge_profile"]["model"]
    )
    target[key] = value
    rehash(batch["judge_profile"])
    validate(batch)
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(batch)
    assert store.read(batch["batch_id"]) == batch


@pytest.mark.parametrize(
    "location,reason",
    [
        ("profile", "unknown_judge_profile_field"),
        ("model", "unknown_model_field"),
    ],
)
def test_unknown_judge_shape_does_not_change_existing_store_or_latest(
    tmp_path,
    location,
    reason,
):
    batch = detached_judge()
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(batch)
    before = {
        p.relative_to(store.root): p.read_bytes()
        for p in store.root.rglob("*")
        if p.is_file()
    }
    changed = deepcopy(batch)
    changed["batch_id"] = "rejected-judge-shape"
    target = (
        changed["judge_profile"]
        if location == "profile"
        else changed["judge_profile"]["model"]
    )
    target["max_retries"] = 2
    rehash(changed["judge_profile"])
    with pytest.raises(ValueError, match=reason):
        store.import_batch(changed)
    after = {
        p.relative_to(store.root): p.read_bytes()
        for p in store.root.rglob("*")
        if p.is_file()
    }
    assert after == before
    assert store.read(batch["batch_id"]) == batch
    assert not (store.root / changed["batch_id"]).exists()


@pytest.mark.parametrize(
    "location,reason",
    [
        ("profile", "unknown_judge_profile_field"),
        ("model", "unknown_model_field"),
    ],
)
def test_public_read_rejects_preexisting_unknown_judge_shape(
    tmp_path, location, reason
):
    import hashlib

    from agent_alfred.evals.acceptance.schema import encode

    batch = detached_judge()
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(batch)
    target = (
        batch["judge_profile"]
        if location == "profile"
        else batch["judge_profile"]["model"]
    )
    target["timeout"] = 3600
    rehash(batch["judge_profile"])
    # Synthetic historical/corrupted package, with self-consistent integrity metadata.
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
    before = {
        p.relative_to(store.root): p.read_bytes()
        for p in store.root.rglob("*")
        if p.is_file()
    }
    with pytest.raises(ValueError, match=reason):
        store.read(batch["batch_id"])
    after = {
        p.relative_to(store.root): p.read_bytes()
        for p in store.root.rglob("*")
        if p.is_file()
    }
    assert after == before
