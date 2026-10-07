"""Synthetic helper receipts exercise local source rules, never owner presence."""

import os
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agent_alfred.evals.acceptance.controlled import local_source
from agent_alfred.evals.acceptance.controlled.local_files import OwnerDirectory
from agent_alfred.evals.acceptance.controlled.local_persistence import (
    _LOCAL_STORAGE,
    AppendWitnessDocuments,
)
from agent_alfred.evals.acceptance.controlled.local_runtime import LocalInstalledRuntime
from agent_alfred.evals.acceptance.controlled.local_source import (
    _LOCAL_SOURCE,
    LocalDecisionControl,
    LocalDecisionSource,
)
from agent_alfred.evals.acceptance.schema import digest


def source_fixture(tmp_path, monkeypatch):
    path = tmp_path.resolve() / "owner"
    path.mkdir(mode=0o700)
    runtime = object.__new__(LocalInstalledRuntime)
    runtime._config = {
        "installation_id": "offline-fixture",
        "owner_uid": os.geteuid(),
        "subject": "offline:owner",
        "owner_helper": "/not-invoked",
    }
    runtime._decisions = AppendWitnessDocuments(
        OwnerDirectory(path, os.geteuid()), "decisions", token=_LOCAL_STORAGE
    )
    runtime._decision_source = LocalDecisionSource(runtime, token=_LOCAL_SOURCE)
    runtime.decision_control = LocalDecisionControl(runtime, token=_LOCAL_SOURCE)
    monkeypatch.setattr(runtime, "verify_installation", lambda: None)
    calls = []

    def authorize(helper, request, *, reason):
        calls.append(request)
        now = datetime.now(UTC)
        return {
            "contract": "V1-LOCAL-OWNER-AUTH",
            "version": 1,
            "nonce": "a" * 64,
            "request_sha256": digest(request),
            "owner_uid": os.geteuid(),
            "authenticated_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=300)).isoformat(),
            "authentication": "deviceOwnerAuthentication",
            "approved": True,
        }

    monkeypatch.setitem(
        sys.modules,
        "agent_alfred.evals.acceptance.controlled.native",
        SimpleNamespace(authorize_owner=authorize),
    )
    return runtime, calls


def test_source_records_current_exact_decision_without_backdating_then_revokes(
    tmp_path, monkeypatch
):
    runtime, calls = source_fixture(tmp_path, monkeypatch)
    request = {
        "object_type": "summary",
        "object_id": "1" * 64,
        "object_sha256": "2" * 64,
        "manifest": [],
    }
    try:
        before = datetime.now(UTC)
        record = runtime.decision_control.record(
            request,
            decision="approved",
            reason="carry forward exact objects",
            evidence=["offline:historical-copy"],
            carry_forward={
                "original_record": "offline:historical-copy",
                "original_event_verified": False,
            },
        )
        event = runtime.decision_source.read_decision(record["source_ref"])
        assert event["at"] == record["receipt"]["authenticated_at"]
        assert datetime.fromisoformat(event["at"]) >= before
        assert event["subject"] == "offline:owner"
        assert calls[0]["carry_forward"]["original_event_verified"] is False
        assert runtime.decision_source.read_scoped_decision(request) == event
        assert record["receipt"]["request_sha256"] == digest(calls[0])
        runtime.decision_control.revoke(record["source_ref"], reason="owner stopped")
        with pytest.raises(ValueError, match="approval_source_unverifiable"):
            runtime.decision_source.read_decision(record["source_ref"])
    finally:
        runtime._decisions.close()


def test_source_rejects_claimed_historical_auth_and_caller_identity_before_helper(
    tmp_path, monkeypatch
):
    runtime, calls = source_fixture(tmp_path, monkeypatch)
    request = {
        "scope": "local_readiness",
        "installation_id": "offline-fixture",
        "object_sha256": "1" * 64,
    }
    try:
        with pytest.raises(ValueError, match="historical_approval_not_authenticated"):
            runtime.decision_control.record(
                request,
                decision="approved",
                reason="fixture",
                evidence=["copy"],
                carry_forward={
                    "original_record": "copy",
                    "original_event_verified": True,
                },
            )
        with pytest.raises(ValueError, match="controlled_contract_invalid"):
            runtime.decision_control.record(
                {**request, "subject": "forged"},
                decision="approved",
                reason="fixture",
                evidence=["copy"],
            )
        assert calls == []
        assert runtime._decisions.generation() == 0
    finally:
        runtime._decisions.close()


@pytest.mark.parametrize("scope", ["local_billing_bound", "local_advisory_budget"])
def test_scoped_owner_review_cannot_be_read_as_execution_permission(
    tmp_path, monkeypatch, scope
):
    runtime, _ = source_fixture(tmp_path, monkeypatch)
    request = {
        "scope": scope,
        "installation_id": "offline-fixture",
        "object_sha256": "1" * 64,
    }
    try:
        record = runtime.decision_control.record(
            request,
            decision="approved",
            reason="offline mechanics only",
            evidence=["offline:proof"],
        )
        assert (
            runtime.decision_source.verify_owner_record(record["source_ref"], request)[
                "event"
            ]
            is None
        )
        with pytest.raises(ValueError, match="approval_source_unverifiable"):
            runtime.decision_source.read_decision(record["source_ref"])
        with pytest.raises(ValueError, match="local_owner_review_unverifiable"):
            runtime.decision_source.verify_owner_record(
                record["source_ref"], {**request, "object_sha256": "2" * 64}
            )
    finally:
        runtime._decisions.close()


@pytest.mark.parametrize(
    "failure_point", ["verify", "read", "write", "return_edge", "after_commit"]
)
def test_returned_owner_receipt_survives_failed_adoption_as_audit_only(
    tmp_path, monkeypatch, failure_point
):
    runtime, calls = source_fixture(tmp_path, monkeypatch)
    original_read = runtime._decisions.read
    original_transaction = runtime._decisions.transaction
    request = {
        "scope": "local_readiness",
        "installation_id": "offline-fixture",
        "object_sha256": "1" * 64,
    }
    failure = (
        KeyboardInterrupt("offline_return_boundary")
        if failure_point == "return_edge"
        else ValueError("offline_post_auth_" + failure_point)
    )

    def fail(*args, **kwargs):
        raise failure

    if failure_point == "verify":
        monkeypatch.setattr(
            runtime, "verify_installation", lambda: fail() if calls else None
        )
    elif failure_point in ("read", "write"):
        monkeypatch.setattr(
            runtime._decisions,
            "read" if failure_point == "read" else "transaction",
            fail,
        )
    elif failure_point == "after_commit":
        def commit_then_fail(*args, **kwargs):
            original_transaction(*args, **kwargs)
            raise failure

        monkeypatch.setattr(runtime._decisions, "transaction", commit_then_fail)
    else:
        from agent_alfred.resource_rollback import capture_call_result

        def interrupt_after_capture(*args):
            capture_call_result(*args)
            raise failure

        monkeypatch.setattr(
            local_source, "capture_call_result", interrupt_after_capture, raising=False
        )
    try:
        with pytest.raises(type(failure)) as raised:
            runtime.decision_control.record(
                request, decision="approved", reason="offline only",
                evidence=["offline:receipt-retention"],
            )
        assert raised.value is failure
        audit = raised.value.local_owner_auth_audit
        assert audit["classification"] == "AUDIT_ONLY"
        assert audit["admission_status"] == "UNCONFIRMED"
        assert audit["challenge"] == calls[0]
        assert audit["receipt"]["request_sha256"] == digest(calls[0])
        assert len(calls) == 1
        monkeypatch.setattr(runtime, "verify_installation", lambda: None)
        monkeypatch.setattr(runtime._decisions, "read", original_read)
        if failure_point == "after_commit":
            # An error is not proof of rollback; retain the independently
            # readable transaction without repeating authentication.
            assert runtime._decisions.generation() == 1
            saved = runtime.decision_source.verify_owner_record(
                audit["challenge"]["source_ref"], request
            )
            assert saved["receipt"] == audit["receipt"]
        else:
            # The saved audit cannot create a decision or a scoped approval.
            assert runtime._decisions.generation() == 0
            with pytest.raises(ValueError, match="approval_source_unverifiable"):
                runtime.decision_source.verify_owner_record(
                    audit["challenge"]["source_ref"], request
                )
    finally:
        runtime._decisions.close()
