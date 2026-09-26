"""Public counterexamples from the independent c26 review."""

import httpx2 as httpx
import pytest

from agent_alfred.evals.acceptance.budget import validate_execution_materials
from agent_alfred.evals.acceptance.calibration import validate_source
from agent_alfred.evals.acceptance.online_judge import grade_batch
from agent_alfred.evals.acceptance.runner import execute
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.deterministic.test_acceptance_admission import (
    UnreadableCredentials,
)
from agent_alfred.evals.deterministic.test_acceptance_approval_cutoff import START
from agent_alfred.evals.deterministic.test_acceptance_calibration_chronology import (
    formal_evidence,
)
from agent_alfred.evals.deterministic.test_acceptance_execution_policy import (
    authorized_fixture,
)


@pytest.mark.parametrize("entry", ["product", "judge"])
@pytest.mark.parametrize("injection", ["http", "factory", "none"])
def test_legacy_simulation_injection_cannot_read_credentials_or_dispatch(
    tmp_path, monkeypatch, entry, injection
):
    batch, root = authorized_fixture()
    store = EvidenceStore(tmp_path / "evidence")

    def forbidden(*args, **kwargs):
        pytest.fail("credential_factory_or_dispatch_before_admission")

    monkeypatch.setattr(
        "agent_alfred.evals.acceptance.online_judge.scoped_credentials", forbidden
    )
    with httpx.Client(
        transport=httpx.MockTransport(forbidden), trust_env=False
    ) as http:
        options = {"http_client": http} if injection == "http" else {}
        if injection == "factory":
            key = "product_factory_builder" if entry == "product" else "factory_builder"
            options[key] = forbidden
        with pytest.raises(ValueError, match="simulation_session_and_mock_required"):
            if entry == "product":
                execute(
                    batch,
                    tmp_path / "runtime",
                    credentials=UnreadableCredentials(),
                    candidate_root=root,
                    store=store,
                    **options,
                )
            else:
                grade_batch(
                    batch,
                    batch["authorization"],
                    candidate_root=root,
                    store=store,
                    new_batch="review-judge",
                    **options,
                )
    assert not (tmp_path / "runtime").exists()


def test_unverifiable_calibration_is_audit_readable_but_not_formally_qualified(
    tmp_path,
):
    source, formal = formal_evidence(START, START)
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    store.import_batch(formal)
    assert store.read(source["batch_id"]) == source
    assert store.read(formal["batch_id"]) == formal
    assert store.authorization_status(source["batch_id"])["validity"] == "UNVERIFIABLE"
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        validate_source(source, store)
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        validate_execution_materials(formal, store, started_at=START)


@pytest.mark.parametrize("command", ["execute", "judge"])
def test_cli_simulation_flag_does_not_read_environment(tmp_path, monkeypatch, command):
    import json
    import os
    from types import SimpleNamespace

    from agent_alfred.evals.acceptance.__main__ import dispatch

    class NoEnvironment(dict):
        def keys(self):
            pytest.fail("cli_read_environment_before_admission")

        def get(self, *args):
            pytest.fail("cli_read_environment_before_admission")

    batch, root = authorized_fixture()
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(batch)
    path = tmp_path / "input.json"
    path.write_text(
        json.dumps(batch if command == "execute" else batch["authorization"])
    )
    monkeypatch.setattr(os, "environ", NoEnvironment())
    args = SimpleNamespace(
        command=command,
        store=store.root,
        input=path,
        batch=batch["batch_id"],
        new_batch="cli-judge",
        workspace=tmp_path / "runtime",
        candidate_root=root,
    )
    with pytest.raises(ValueError, match="simulation_session_and_mock_required"):
        dispatch(args)
    assert not (tmp_path / "runtime").exists()
