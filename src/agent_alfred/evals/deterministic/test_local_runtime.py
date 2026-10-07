"""Fail-closed local contract checks without auth, credentials or network."""

import os
from datetime import UTC, datetime

import pytest

from agent_alfred.evals.acceptance.controlled.local_files import OwnerDirectory
from agent_alfred.evals.acceptance.controlled.local_installation import (
    inspect_local_installation,
    read_billing,
)
from agent_alfred.evals.acceptance.controlled.local_runtime import (
    LocalInstalledRuntime,
    install_local_runtime,
)
from agent_alfred.evals.acceptance.controlled.runtime import validate_runtime
from agent_alfred.evals.acceptance.controlled.store import (
    MemoryExecutionAnchor,
    MemoryExecutionStore,
)
from agent_alfred.evals.acceptance.controlled_execution import ControlledAuthority
from agent_alfred.evals.acceptance.schema import encode


def test_unconfigured_local_entry_stops_before_any_credentials_or_client(tmp_path):
    directory = tmp_path.resolve() / "owner"
    directory.mkdir(mode=0o700)
    missing = directory / "not-installed.json"
    report = inspect_local_installation(missing)
    assert report["online_executable"] is False
    assert report["blockers"] == ["local_protected_file_missing"]
    assert list(directory.iterdir()) == []
    with pytest.raises(
        ValueError,
        match="local_protected_file_missing|trusted_local_installation_required",
    ):
        install_local_runtime(missing)
    assert list(directory.iterdir()) == []


def test_local_runtime_cannot_be_constructed_or_structurally_injected():
    with pytest.raises(ValueError, match="trusted_local_installation_required"):
        LocalInstalledRuntime("unused")

    class Lookalike:
        trust_profile = "owner_trusted_local"

    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        validate_runtime(Lookalike())
    real = object.__new__(LocalInstalledRuntime)
    with pytest.raises(ValueError, match="real_execution_prerequisites_unverified"):
        ControlledAuthority(
            runtime=real, store=MemoryExecutionStore(), anchor=MemoryExecutionAnchor()
        )
    with pytest.raises(ValueError, match="real_execution_prerequisites_unverified"):
        ControlledAuthority(runtime=real, now=lambda: datetime.now(UTC))


def test_public_tariff_or_local_approval_boolean_is_not_a_billing_bound(tmp_path):
    root = tmp_path.resolve() / "owner"
    root.mkdir(mode=0o700)
    directory = OwnerDirectory(root, os.geteuid())
    target = directory.file("billing.json", create=True)
    target.write_bytes(
        encode(
            {
                "approved": True,
                "cap_usd": 25,
                "price_url": "https://api-docs.deepseek.com/quick_start/pricing/",
            }
        )
    )
    with pytest.raises(ValueError, match="local_billing_bound_unverifiable"):
        read_billing(directory, {"billing_path": str(target)}, now=datetime.now(UTC))


def test_sleep_or_clock_discontinuity_is_latched_without_extending_grant():
    from agent_alfred.evals.acceptance.controlled.local_clock import ContinuityGuard

    guard = ContinuityGuard()
    guard.observe(1000, 20)
    guard.observe(1010, 30)
    with pytest.raises(ValueError, match="local_clock_continuity_lost"):
        guard.observe(1070, 31)  # Wall advanced during suspended awake clock.
    with pytest.raises(ValueError, match="local_clock_continuity_lost"):
        guard.observe(1071, 32)
    backwards = ContinuityGuard()
    backwards.observe(1000, 20)
    with pytest.raises(ValueError, match="local_clock_continuity_lost"):
        backwards.observe(999, 21)
    brief_sleep = ContinuityGuard()
    brief_sleep.observe(1000, 20, [100, 110])
    with pytest.raises(ValueError, match="local_clock_continuity_lost"):
        brief_sleep.observe(1000.1, 20.05, [1000, 1010])


def test_final_charge_uses_original_attempt_scope_and_does_not_clip_overrun(tmp_path):
    import hashlib
    from datetime import timedelta

    from agent_alfred.evals.acceptance.controlled.local_installation import (
        read_final_charge,
    )
    from agent_alfred.evals.acceptance.schema import digest

    root = tmp_path.resolve() / "owner"
    root.mkdir(mode=0o700)
    directory = OwnerDirectory(root, os.geteuid())
    receipt = directory.file("fixture-statement", create=True)
    receipt.write_bytes(b"OFFLINE STRUCTURE FIXTURE ONLY, NOT A PROVIDER STATEMENT")
    now = datetime.now(UTC)
    row = {"attempt_id": "original-attempt", "payload_sha256": "1" * 64}
    response = {"status_code": 200, "raw": "preserved first raw"}
    quote = {"terms": {"valid_until": (now - timedelta(days=1)).isoformat()}}
    original = {
        "account_scope": "offline:account",
        "provider_origin": "https://platform.deepseek.com/",
    }
    config = {"installation_id": "offline:installation", "candidate_id": "2" * 64}
    value = {
        "contract": "V1-LOCAL-FINAL-CHARGE",
        "version": 1,
        **config,
        "attempt_id": row["attempt_id"],
        "payload_sha256": row["payload_sha256"],
        "response_sha256": digest(response),
        "quote_sha256": digest(quote),
        **original,
        "actual_units": 26_000_000_000_000,
        "provider_record": {
            "path": str(receipt),
            "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
        },
        "observed_at": now.isoformat(),
        "valid_until": (now + timedelta(hours=1)).isoformat(),
        "approval_ref": "offline:unverified-owner-record",
    }
    path = directory.file(
        "final-charge-" + digest({"attempt_id": row["attempt_id"]}) + ".json",
        create=True,
    )
    path.write_bytes(encode(value))
    observed = read_final_charge(
        directory, config, row, response, quote, original, now=now
    )
    assert observed["actual_units"] == 26_000_000_000_000
    # This structural read is not authenticated until LocalDecisionSource reads
    # the exact review event; it cannot independently settle or authorize a send.
    with pytest.raises(ValueError, match="local_final_charge_unverifiable"):
        read_final_charge(
            directory,
            config,
            row,
            {**response, "raw": "replacement"},
            quote,
            original,
            now=now,
        )
