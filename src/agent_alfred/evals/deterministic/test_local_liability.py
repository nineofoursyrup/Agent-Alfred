"""Offline settlement transitions, using an explicitly substituted proof fixture.

No local installation, real approval or billing fact is established by this test.
The public broker carries the real existing reservation/Attempt state machine;
only the final provider-proof seam is replaced after the MockTransport send.
"""

import hashlib
import json
import os
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agent_alfred.evals.acceptance.controlled.local_files import OwnerDirectory
from agent_alfred.evals.acceptance.controlled.local_persistence import (
    _LOCAL_STORAGE,
    LocalExecutionStore,
    LocalExecutionWitness,
)
from agent_alfred.evals.acceptance.controlled.local_runtime import LocalInstalledRuntime
from agent_alfred.evals.acceptance.controlled.store import MemoryExecutionStore
from agent_alfred.evals.acceptance.controlled_execution import completed_response
from agent_alfred.evals.acceptance.schema import digest, encode

from .test_controlled_execution import prepare, response, terms
from .test_controlled_persistence import durable_fixture


def test_known_upper_bound_keeps_full_liability_then_exact_readback_settles_once(
    tmp_path,
):
    box = {}

    def provider(request):
        auth = box["fixture"]["authority"]
        runtime = object.__new__(LocalInstalledRuntime)
        runtime.store, runtime.anchor = auth.store, auth.anchor
        runtime._config = {
            "worker": auth.worker,
            "controller": auth.controller,
            "subject": auth.subject,
            "installation_id": "offline-installation",
            "candidate_id": "a" * 64,
        }
        runtime._directory = box["directory"]
        runtime.verify_installation = lambda: None
        runtime._billing = lambda now: box["billing"]
        runtime._decision_source = SimpleNamespace(
            verify_owner_record=lambda source_ref, request: None
        )  # Explicit source-proof fixture; no owner authentication.
        auth.runtime = runtime
        return response(request)

    f = durable_fixture(tmp_path, handler=provider)
    f["ledger"].close()
    f["anchor"].close()
    owner_root = tmp_path.resolve() / "owner"
    owner_root.mkdir(mode=0o700)
    directory = OwnerDirectory(owner_root, os.geteuid())
    box["directory"] = directory
    f["ledger"] = LocalExecutionStore(directory, "ledger", token=_LOCAL_STORAGE)
    f["anchor"] = LocalExecutionWitness(directory, "witness", token=_LOCAL_STORAGE)
    f["authority"].store, f["authority"].anchor = f["ledger"], f["anchor"]
    original_quote = f["runtime"].quote
    box["billing"] = {
        "liability_mode": "bounded_final_liability_function",
        "pricing": deepcopy(f["submission"]["plan"]["pricing"]),
        "account_scope": "offline:account-A",
        "provider_origin": "offline:proof",
        "approval_ref": "offline:original-proof",
    }

    def fixture_quote(plan, operation, payload, now):
        quote = original_quote(plan, operation, payload, now)
        quote["token_evidence"] = f["ledger"].put_object(
            {
                "complete_payload_sha256": digest(payload),
                "billing_bound": deepcopy(box["billing"]),
            }
        )
        return quote

    f["runtime"].quote = fixture_quote
    box["fixture"] = f
    auth = f["authority"]
    try:
        auth.submit_job(f["submission"])
        client, _ = prepare(f)
        result = client.respond(f["request"])
        assert result.final_error is None
        state = auth.status("controlled-job")
        row = state["attempts"][0]
        assert completed_response(row)
        assert row["send_state"] == "RESPONSE_VERIFIED"
        assert row["actual_units"] is None
        assert state["state"]["spent_units"] == 0
        assert state["state"]["pending_units"] == row["worst_units"]
        assert state["state"]["active_attempt"] is None
        first_response, first_finished = row["response_ref"], row["completed_at"]
        before = digest(state)
        with pytest.raises(ValueError, match="final_liability_not_yet_verified"):
            auth.settle_attempt(
                "controlled-job",
                row["attempt_id"],
                first_response,
                principal=auth.controller,
            )
        assert digest(auth.status("controlled-job")) == before
        box["billing"]["liability_mode"] = "exact_final_liability_function"
        box["billing"]["account_scope"] = "offline:account-B"
        with pytest.raises(ValueError, match="final_liability_not_yet_verified"):
            auth.settle_attempt(
                "controlled-job", row["attempt_id"], first_response,
                principal=auth.controller,
            )
        assert digest(auth.status("controlled-job")) == before
        write_final_charge_fixture(
            auth.runtime, row, f["ledger"].get_object(first_response),
            f["ledger"].get_object(row["quote_ref"]), 5400000,
        )
        settled = auth.settle_attempt(
            "controlled-job",
            row["attempt_id"],
            first_response,
            principal=auth.controller,
        )
        assert settled["attempt"]["send_state"] == "SETTLED"
        assert settled["attempt"]["actual_units"] == 5400000
        assert settled["attempt"]["completed_at"] == first_finished
        final = auth.status("controlled-job")
        assert final["state"]["pending_units"] == 0
        assert final["state"]["spent_units"] == 5400000
        auth.settle_attempt(
            "controlled-job",
            row["attempt_id"],
            first_response,
            principal=auth.controller,
        )
        assert auth.status("controlled-job") == final
        assert len(f["sends"]) == 1
    finally:
        f["ledger"].close()
        f["anchor"].close()


def write_final_charge_fixture(runtime, row, response_value, quote, actual_units):
    original = runtime.store.get_object(quote["token_evidence"])["billing_bound"]
    directory = runtime._directory
    receipt = directory.file("offline-statement.txt", create=True)
    receipt.write_bytes(b"OFFLINE FINAL-CHARGE STRUCTURE FIXTURE")
    now = datetime.now(UTC)
    document = {
        "contract": "V1-LOCAL-FINAL-CHARGE",
        "version": 1,
        "installation_id": runtime._config["installation_id"],
        "candidate_id": runtime._config["candidate_id"],
        "attempt_id": row["attempt_id"],
        "payload_sha256": row["payload_sha256"],
        "response_sha256": digest(response_value),
        "quote_sha256": digest(quote),
        "account_scope": original["account_scope"],
        "provider_origin": original["provider_origin"],
        "actual_units": actual_units,
        "provider_record": {
            "path": str(receipt),
            "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
        },
        "observed_at": now.isoformat(),
        "valid_until": (now + timedelta(hours=1)).isoformat(),
        "approval_ref": "offline:final-charge-review",
    }
    path = directory.file(
        "final-charge-" + digest({"attempt_id": row["attempt_id"]}) + ".json",
        create=True,
    )
    path.write_bytes(encode(document))
    return path, document


@pytest.mark.parametrize("original_mode", ["bounded", "exact"])
def test_settlement_uses_original_account_contract_and_exact_final_receipt(
    tmp_path, original_mode
):
    directory_path = tmp_path.resolve() / "owner"
    directory_path.mkdir(mode=0o700)
    runtime = object.__new__(LocalInstalledRuntime)
    runtime.verify_installation = lambda: None
    runtime._directory = OwnerDirectory(directory_path, os.geteuid())
    runtime._config = {"installation_id": "offline-install", "candidate_id": "a" * 64}
    runtime.store = MemoryExecutionStore()
    pricing = {"flash": terms("deepseek-flash"), "pro": terms("deepseek-v4-pro")}
    original = {
        "account_scope": "offline:account-A",
        "provider_origin": "https://api.deepseek.com",
        "liability_mode": original_mode + "_final_liability_function",
        "pricing": pricing,
        "approval_ref": "offline:original-contract",
        "valid_until": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
    }

    def no_current_contract(now):
        raise AssertionError("current contract must not price an old Attempt")

    reviewed = []
    runtime._billing = no_current_contract
    runtime._decision_source = SimpleNamespace(
        verify_owner_record=lambda source_ref, request: reviewed.append(
            (source_ref, request)
        )
    )
    row = {
        "attempt_id": "offline-attempt", "payload_sha256": "b" * 64,
        "model": "deepseek-flash", "input_bound": 100, "output_bound": 100,
        "kind": "product",
    }
    quote = {
        "token_evidence": runtime.store.put_object({
            "complete_payload_sha256": row["payload_sha256"],
            "billing_bound": original,
        }),
        "terms": pricing["flash"],
    }
    response_value = {
        "status_code": 200,
        "raw": json.dumps({"model": "deepseek-flash", "usage": {
            "prompt_tokens": 10, "completion_tokens": 2,
        }}),
    }
    proof = runtime.settlement(row, response_value, quote)
    if original_mode == "bounded":
        assert proof["actual_units"] is None and proof["liability_state"] == "RESERVED"
    else:
        assert proof["actual_units"] == 5400000
    assert reviewed[-1][0] == original["approval_ref"]
    # A late authenticated final receipt is bound to the original account and
    # quote; its genuine overrun is neither clipped nor rejected for old expiry.
    path, document = write_final_charge_fixture(
        runtime, row, response_value, quote, 26_000_000_000_000
    )
    proof = runtime.settlement(row, response_value, quote)
    assert proof["actual_units"] == 26_000_000_000_000
    assert reviewed[-1][0] == "offline:final-charge-review"
    document["account_scope"] = "offline:account-B"
    path.write_bytes(encode(document))
    with pytest.raises(ValueError, match="local_final_charge_unverifiable"):
        runtime.settlement(row, response_value, quote)


def test_synthetic_proof_cannot_select_real_reserved_liability_protocol(tmp_path):
    f = durable_fixture(tmp_path)
    f["runtime"].settlement = lambda row, response, quote: {
        "contract": "V1-LOCAL-LIABILITY-READBACK",
        "version": 2,
        "payload_sha256": row["payload_sha256"],
        "response_sha256": digest(response),
        "liability_state": "RESERVED",
        "actual_units": None,
        "evidence": "synthetic",
    }
    try:
        f["authority"].submit_job(f["submission"])
        client, _ = prepare(f)
        assert client.respond(f["request"]).final_error is not None
        status = f["authority"].status("controlled-job")
        assert status["state"]["state"] == "SUSPENDED"
        assert status["attempts"][0]["send_state"] == "MAY_HAVE_SENT"
        assert status["state"]["pending_units"] > 0
    finally:
        f["ledger"].close()
        f["anchor"].close()
