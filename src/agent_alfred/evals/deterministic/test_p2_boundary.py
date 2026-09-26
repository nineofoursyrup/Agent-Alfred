"""P2 protocol invariants before any cloud identity is available."""

import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest

from agent_alfred.evals.acceptance.admission import proposal
from agent_alfred.p2.authority import Authority
from agent_alfred.p2.common import ZERO_DIGEST, BoundaryError, digest, utc_now


def fixture():
    batch = {
        "schema_version": 1,
        "simulation": True,
        "phase": "offline_fixture",
        "parent": None,
        "batch_id": "p2-synthetic-batch",
        "candidate_id": "a" * 64,
        "cases": [],
        "rubric": {},
        "judge_profile": None,
        "candidate": {"files": {}},
        "profiles": [
            {
                "id": "p2-profile",
                "execution_policy": {"max_retries": 0, "sdk_max_retries": 0},
                "product_models": [
                    {
                        "endpoint_id": "p2-private-canary",
                        "model_id": "p2-canary-product",
                    }
                ],
                "judge_model": {
                    "endpoint_id": "p2-private-canary",
                    "model_id": "p2-canary-judge",
                },
            }
        ],
        "authorization": {
            "max_requests": 3,
            "max_output_tokens": 16,
            "total_seconds": 600,
            "cost": {"amount": 0, "source": "p2-private-canary"},
        },
    }
    proposed = proposal(
        batch,
        output_scope="/p2/synthetic/evidence",
        operations=["product"],
        nonce="b" * 32,
    )
    descriptor = {
        "profile_id": "p2-profile",
        "endpoint_id": "p2-private-canary",
        "model_id": "p2-canary-product",
        "max_tokens": 8,
        "body": {"input": "synthetic-only"},
    }
    return {"proposal": proposed, "batch": batch}, descriptor


class FakeStore:
    def __init__(self):
        self.rows = {}
        self.events = {}

    def get(self, job_id):
        return deepcopy(self.rows.get(job_id))

    def put(self, state, event, *, old_revision, old_digest):
        prior = self.rows.get(state["job_id"])
        if prior is None:
            assert old_revision == 0 and old_digest == ZERO_DIGEST
        else:
            assert (prior["revision"], prior["event_digest"]) == (
                old_revision,
                old_digest,
            )
        self.rows[state["job_id"]] = deepcopy(state)
        self.events[state["job_id"], state["revision"]] = deepcopy(event)


class FakeAnchor:
    def __init__(self):
        self.rows = {}
        self.fail_commit = False

    def read(self, job_id):
        revision, event_digest = self.rows.get(job_id, (0, ZERO_DIGEST))
        return {"job_id": job_id, "revision": revision, "digest": event_digest}

    def commit(self, event):
        if self.fail_commit:
            raise BoundaryError("anchor_unavailable")
        old = self.read(event["job_id"])
        assert old["revision"] + 1 == event["revision"]
        assert old["digest"] == event["previous_digest"]
        self.rows[event["job_id"]] = (event["revision"], event["event_digest"])
        return self.read(event["job_id"])


class FakeDispatch:
    def __init__(self):
        self.calls = []
        self.fail = False

    def send(self, payload):
        self.calls.append(deepcopy(payload))
        if self.fail:
            raise TimeoutError("response lost after possible send")
        return {
            "kind": "P2_CANARY_RESULT",
            "attempt_id": payload["attempt_id"],
            "request_digest": payload["request_digest"],
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "output": "p2-canary-ok",
        }


class FakeVerifier:
    def verify(self, _body, signature):
        return signature == "valid-synthetic-signature"


def service():
    store, anchor, dispatch = FakeStore(), FakeAnchor(), FakeDispatch()
    authority = Authority(
        store,
        anchor,
        dispatch,
        FakeVerifier(),
        worker_principal="arn:aws:iam::111111111111:role/p2-worker",
    )
    return authority, store, anchor, dispatch


def signed_event(job_id, proposed, kind="ISSUE"):
    now = utc_now()
    return {
        "body": {
            "version": 1,
            "kind": kind,
            "job_id": job_id,
            "proposal_digest": digest(proposed),
            "event_id": "event-" + kind.lower(),
            "nonce": proposed["nonce"],
            "issued_at": now.isoformat(),
            "activation_deadline": (now + timedelta(minutes=5)).isoformat(),
            "subject": "synthetic:operator",
            "fee_cap": {"currency": "USD", "amount": "0", "unknown": False},
        },
        "signature": "valid-synthetic-signature",
    }


def active():
    authority, store, anchor, dispatch = service()
    payload, descriptor = fixture()
    job_id = authority.submit_job(
        payload, "arn:aws:iam::111111111111:role/p2-executor"
    )["job_id"]
    authority.issuer_event(signed_event(job_id, payload["proposal"]))
    return authority, store, anchor, dispatch, job_id, descriptor


def routed_call(authority, action, **payload):
    from agent_alfred.p2.authority_handler import route

    principals = {
        "submit": "arn:aws:iam::111111111111:role/p2-executor",
        "preflight": "arn:aws:iam::111111111111:role/p2-dispatch",
    }
    principal = principals.get(action, authority.worker_principal)
    return route(authority, action, payload, principal, {action: {principal}})


def timed_job(*, operations=("product", "judge"), seconds=600, activation_seconds=3000):
    authority, store, anchor, dispatch = service()
    clock = [utc_now()]
    authority.now = lambda: clock[0]
    payload, descriptor = fixture()
    payload["batch"]["authorization"]["total_seconds"] = seconds
    payload["proposal"] = proposal(
        payload["batch"],
        output_scope="/p2/synthetic/evidence",
        operations=list(operations),
        nonce="b" * 32,
    )
    job_id = routed_call(authority, "submit", **payload)["job_id"]
    event = signed_event(job_id, payload["proposal"])
    event["body"]["issued_at"] = clock[0].isoformat()
    event["body"]["activation_deadline"] = (
        clock[0] + timedelta(seconds=activation_seconds)
    ).isoformat()
    authority.issuer_event(event)
    return authority, store, anchor, dispatch, job_id, descriptor, clock


@pytest.mark.parametrize(
    ("seconds", "reason"),
    [(11, "batch_deadline"), (-1, "authority_clock_unverifiable")],
)
def test_time_stop_survives_clock_recovery_and_new_handler(seconds, reason):
    authority, store, anchor, dispatch, job_id, descriptor, clock = timed_job(
        seconds=10
    )
    original = store.get(job_id)
    started_at = clock[0]
    clock[0] = started_at + timedelta(seconds=seconds)
    with pytest.raises(BoundaryError, match=f"^{reason}$"):
        routed_call(
            authority,
            "invoke",
            job_id=job_id,
            role="product",
            attempt_id="time-stop",
            request_descriptor=descriptor,
        )
    clock[0] = started_at + timedelta(seconds=9)
    restarted = Authority(
        store,
        anchor,
        dispatch,
        FakeVerifier(),
        now=lambda: clock[0],
        worker_principal=authority.worker_principal,
    )
    status = routed_call(restarted, "status", job_id=job_id)
    assert (status["state"], status["stop_reason"]) == ("SUSPENDED", reason)
    assert anchor.read(job_id)["revision"] == status["revision"]
    with pytest.raises(BoundaryError, match="^grant_not_active$"):
        routed_call(
            restarted,
            "invoke",
            job_id=job_id,
            role="product",
            attempt_id="after-clock-recovery",
            request_descriptor=descriptor,
        )
    event = signed_event(job_id, original["proposal"])
    event["body"]["event_id"] = "fresh-issue-cannot-resume"
    with pytest.raises(BoundaryError, match="^issuer_event_state_invalid$"):
        restarted.issuer_event(event)
    current = store.get(job_id)
    assert current["budget_started_at"] == original["budget_started_at"]
    assert current["activation_deadline"] == original["activation_deadline"]
    assert not dispatch.calls
    # Restoring the old ACTIVE row cannot erase C's independently anchored stop.
    store.rows[job_id] = original
    with pytest.raises(BoundaryError, match="^authority_anchor_mismatch$"):
        routed_call(
            restarted,
            "invoke",
            job_id=job_id,
            role="product",
            attempt_id="after-ledger-rollback",
            request_descriptor=descriptor,
        )
    assert not dispatch.calls


@pytest.mark.parametrize(
    ("action", "invalid_sample"),
    [("invoke", 2), ("invoke", 4), ("invoke", 6), ("finish", 2)],
    ids=["reserve", "send-intent", "preflight", "finish"],
)
@pytest.mark.parametrize(
    ("seconds", "activation_seconds", "reason"),
    [
        (-1, 3000, "authority_clock_unverifiable"),
        (10, 3000, "batch_deadline"),
        (5, 5, "batch_deadline"),
    ],
    ids=["rollback", "budget-expiry", "activation-expiry"],
)
def test_fence_clear_checks_its_commit_sample_before_reopening(
    action, invalid_sample, seconds, activation_seconds, reason
):
    from unittest.mock import Mock

    from agent_alfred.p2.dispatch import Dispatch

    authority, store, anchor, dispatch, job_id, descriptor, clock = timed_job(
        operations=("product",), seconds=10, activation_seconds=activation_seconds
    )
    original = store.get(job_id)
    started_at = clock[0]
    samples = []

    def now():
        # Each action checks time, then samples its fence-clearing commit.
        # Recover immediately after the bad sample: it must not be forgotten.
        offset = seconds if len(samples) + 1 == invalid_sample else 1
        sample = started_at + timedelta(seconds=offset)
        samples.append(sample)
        return sample

    api, canary = Mock(), Mock()
    secret = Mock(return_value="synthetic-token")
    api.call.side_effect = lambda name, body: routed_call(authority, name, **body)
    canary.call.side_effect = lambda _, body: dispatch.send(body)
    authority.dispatch = Dispatch(api, canary, secret)
    authority.now = now
    body = {"job_id": job_id, "role": "product"}
    if action == "invoke":
        body.update(attempt_id="commit-time-stop", request_descriptor=descriptor)
    expected = "request_state_unresolved" if invalid_sample == 6 else reason
    with pytest.raises(BoundaryError, match=f"^{expected}$") as error:
        routed_call(authority, action, **body)
    if invalid_sample == 6:
        assert str(error.value.__cause__) == reason
    secret.assert_not_called()
    canary.call.assert_not_called()
    stops = [event for event in store.events.values() if event["kind"] == "TIME_STOP"]
    assert len(stops) == 1
    assert stops[0]["at"] == (started_at + timedelta(seconds=seconds)).isoformat()

    restarted = Authority(
        store,
        anchor,
        dispatch,
        FakeVerifier(),
        now=lambda: started_at + timedelta(seconds=2),
        worker_principal=authority.worker_principal,
    )
    status = routed_call(restarted, "status", job_id=job_id)
    assert (status["state"], status["stop_reason"]) == ("SUSPENDED", reason)
    assert anchor.read(job_id)["revision"] == status["revision"]
    with pytest.raises(BoundaryError, match="^grant_not_active$"):
        routed_call(
            restarted,
            "invoke",
            job_id=job_id,
            role="product",
            attempt_id="after-commit-clock-recovery",
            request_descriptor=descriptor,
        )
    current = store.get(job_id)
    assert current["budget_started_at"] == original["budget_started_at"]
    assert current["activation_deadline"] == original["activation_deadline"]
    assert current["finished_operations"] == []
    if invalid_sample == 2:
        assert current["requests"] == []
    else:
        assert current["requests"][0]["send_state"] == (
            "RESERVED" if invalid_sample == 4 else "MAY_HAVE_SENT"
        )
    assert not dispatch.calls


@pytest.mark.parametrize(
    ("failed_event", "failed_side", "invalid_sample", "next_error"),
    [
        ("TIME_CHECK", "anchor", 1, "authority_anchor_mismatch"),
        ("TIME_STOP", "store", 1, "grant_not_active"),
        ("TIME_STOP", "anchor", 1, "authority_anchor_mismatch"),
        ("TIME_STOP", "store", 2, "grant_not_active"),
        ("TIME_STOP", "anchor", 2, "authority_anchor_mismatch"),
        ("RESERVE", "store", None, "grant_not_active"),
        ("RESERVE", "anchor", None, "authority_anchor_mismatch"),
    ],
)
def test_time_check_partial_writes_cannot_resume(
    monkeypatch, failed_event, failed_side, invalid_sample, next_error
):
    authority, store, anchor, dispatch, job_id, descriptor, clock = timed_job(
        seconds=10
    )
    started_at = clock[0]
    put, commit = store.put, anchor.commit
    event_kind = [None]

    def fail_store(state, event, **previous):
        event_kind[0] = event["kind"]
        if failed_side == "store" and event["kind"] == failed_event:
            raise BoundaryError("injected_write_failure")
        return put(state, event, **previous)

    def fail_anchor(event):
        if failed_side == "anchor" and event_kind[0] == failed_event:
            raise BoundaryError("injected_write_failure")
        return commit(event)

    monkeypatch.setattr(store, "put", fail_store)
    monkeypatch.setattr(anchor, "commit", fail_anchor)
    samples = []

    def now():
        offset = 11 if len(samples) + 1 == invalid_sample else 1
        samples.append(started_at + timedelta(seconds=offset))
        return samples[-1]

    authority.now = now
    with pytest.raises(BoundaryError, match="^injected_write_failure$"):
        routed_call(
            authority,
            "invoke",
            job_id=job_id,
            role="product",
            attempt_id="failed-time-check",
            request_descriptor=descriptor,
        )
    # Both dependencies and the clock recover; the durable state must not.
    monkeypatch.setattr(store, "put", put)
    monkeypatch.setattr(anchor, "commit", commit)
    clock[0] = started_at + timedelta(seconds=2)
    restarted = Authority(
        store,
        anchor,
        dispatch,
        FakeVerifier(),
        now=lambda: clock[0],
        worker_principal=authority.worker_principal,
    )
    with pytest.raises(BoundaryError, match=f"^{next_error}$"):
        routed_call(
            restarted,
            "invoke",
            job_id=job_id,
            role="product",
            attempt_id="after-storage-recovery",
            request_descriptor=descriptor,
        )
    if failed_side == "store":
        status = routed_call(restarted, "status", job_id=job_id)
        assert (status["state"], status["stop_reason"]) == (
            "SUSPENDED",
            "time_check_pending",
        )
    assert not dispatch.calls


@pytest.mark.parametrize("crash_sample", [1, 2], ids=["check", "commit"])
def test_crash_while_sampling_time_leaves_anchored_fence(crash_sample):
    authority, store, anchor, dispatch, job_id, descriptor, clock = timed_job()
    samples = []

    def crash():
        samples.append(clock[0])
        if len(samples) == crash_sample:
            raise SystemExit("synthetic process crash")
        return clock[0]

    authority.now = crash
    with pytest.raises(SystemExit, match="synthetic process crash"):
        routed_call(
            authority,
            "invoke",
            job_id=job_id,
            role="product",
            attempt_id="crashed-clock",
            request_descriptor=descriptor,
        )
    restarted = Authority(
        store,
        anchor,
        dispatch,
        FakeVerifier(),
        now=lambda: clock[0],
        worker_principal=authority.worker_principal,
    )
    status = routed_call(restarted, "status", job_id=job_id)
    assert (status["state"], status["stop_reason"]) == (
        "SUSPENDED",
        "time_check_pending",
    )
    with pytest.raises(BoundaryError, match="^grant_not_active$"):
        routed_call(
            restarted,
            "invoke",
            job_id=job_id,
            role="product",
            attempt_id="after-crash",
            request_descriptor=descriptor,
        )
    assert not dispatch.calls


@pytest.mark.parametrize("lost_ack_event", ["RESERVE", "SEND_INTENT", "PREFLIGHT"])
def test_lost_time_fence_clear_ack_cannot_admit_new_attempt(
    monkeypatch, lost_ack_event
):
    authority, store, anchor, dispatch, job_id, descriptor, clock = timed_job()
    put, commit, send = store.put, anchor.commit, dispatch.send
    event_kind = [None]
    ack_lost = [False]

    def track_event(state, event, **previous):
        event_kind[0] = event["kind"]
        return put(state, event, **previous)

    def lose_ack(event):
        result = commit(event)
        if event_kind[0] == lost_ack_event and not ack_lost[0]:
            ack_lost[0] = True
            raise TimeoutError("anchor committed but acknowledgement lost")
        return result

    def checked_send(payload):
        routed_call(
            authority,
            "preflight",
            job_id=job_id,
            attempt_id=payload["attempt_id"],
            request_digest=payload["request_digest"],
        )
        return send(payload)

    monkeypatch.setattr(store, "put", track_event)
    monkeypatch.setattr(anchor, "commit", lose_ack)
    monkeypatch.setattr(dispatch, "send", checked_send)
    expected = BoundaryError if lost_ack_event == "PREFLIGHT" else TimeoutError
    with pytest.raises(expected):
        routed_call(
            authority,
            "invoke",
            job_id=job_id,
            role="product",
            attempt_id="lost-ack",
            request_descriptor=descriptor,
        )
    assert ack_lost[0]
    restarted = Authority(
        store,
        anchor,
        dispatch,
        FakeVerifier(),
        now=lambda: clock[0],
        worker_principal=authority.worker_principal,
    )
    status = routed_call(restarted, "status", job_id=job_id)
    assert len(status["requests"]) == 1
    assert status["requests"][0]["send_state"] in (
        "RESERVED",
        "SEND_INTENT",
        "MAY_HAVE_SENT",
    )
    with pytest.raises(
        BoundaryError, match="^(grant_not_active|request_state_unresolved)$"
    ):
        routed_call(
            restarted,
            "invoke",
            job_id=job_id,
            role="product",
            attempt_id="fresh-after-lost-ack",
            request_descriptor=descriptor,
        )
    assert not dispatch.calls


def test_inflight_usage_settles_after_time_stop_without_reopening_grant(monkeypatch):
    authority, _, _, dispatch, job_id, descriptor, clock = timed_job(seconds=10)
    send = dispatch.send

    def inflight(payload):
        result = send(payload)
        clock[0] += timedelta(seconds=11)
        with pytest.raises(BoundaryError, match="^batch_deadline$"):
            routed_call(
                authority,
                "preflight",
                job_id=job_id,
                attempt_id=payload["attempt_id"],
                request_digest=payload["request_digest"],
            )
        return result

    monkeypatch.setattr(dispatch, "send", inflight)
    result = routed_call(
        authority,
        "invoke",
        job_id=job_id,
        role="product",
        attempt_id="inflight",
        request_descriptor=descriptor,
    )
    status = routed_call(authority, "status", job_id=job_id)
    assert (status["state"], status["stop_reason"]) == ("SUSPENDED", "batch_deadline")
    assert status["requests"][0]["send_state"] == "SETTLED"
    assert status["requests"][0]["usage"] == result["usage"]
    clock[0] -= timedelta(seconds=10)
    with pytest.raises(BoundaryError, match="^grant_not_active$"):
        routed_call(
            authority,
            "invoke",
            job_id=job_id,
            role="product",
            attempt_id="after-settlement",
            request_descriptor=descriptor,
        )
    assert len(dispatch.calls) == 1


@pytest.mark.parametrize("revoke_sample", [1, 2], ids=["check", "commit"])
def test_time_verification_cannot_clear_concurrent_revocation(revoke_sample):
    authority, store, _, dispatch, job_id, descriptor, clock = timed_job()
    now = clock[0]
    proposed = store.get(job_id)["proposal"]
    samples = []

    def revoke_during_check():
        samples.append(now)
        if len(samples) != revoke_sample:
            return now
        authority.now = lambda: now
        authority.issuer_event(signed_event(job_id, proposed, "REVOKE"))
        return now

    authority.now = revoke_during_check
    # FakeStore models the same exact revision/digest CAS as DynamoJobStore.
    with pytest.raises(AssertionError):
        routed_call(
            authority,
            "invoke",
            job_id=job_id,
            role="product",
            attempt_id="concurrent-revoke",
            request_descriptor=descriptor,
        )
    assert routed_call(authority, "status", job_id=job_id)["state"] == "REVOKED"
    assert not dispatch.calls


def test_full_request_budget_with_real_anchor_and_time_fences(monkeypatch):
    from agent_alfred.p2.anchor import Anchor

    authority, store, _, dispatch = service()
    # Exercise Anchor's actual revision bound and chain logic with local AWS fakes.
    import io

    class Ddb:
        def __init__(self):
            self.row = None

        def get_item(self, **kwargs):
            return {"Item": self.row} if self.row else {}

        def update_item(self, **kwargs):
            values = kwargs["ExpressionAttributeValues"]
            self.row = {"Revision": values[":new"], "Digest": values[":digest"]}

    class S3:
        def __init__(self):
            self.objects = {}

        def list_object_versions(self, **kwargs):
            return {"Versions": [{"Key": k, "VersionId": "v1"} for k in self.objects]}

        def put_object(self, **kwargs):
            self.objects[kwargs["Key"]] = kwargs

        def get_object_retention(self, **kwargs):
            obj = self.objects[kwargs["Key"]]
            return {
                "Retention": {
                    "Mode": obj["ObjectLockMode"],
                    "RetainUntilDate": obj["ObjectLockRetainUntilDate"],
                }
            }

        def get_object(self, **kwargs):
            return {"Body": io.BytesIO(self.objects[kwargs["Key"]]["Body"])}

    authority.anchor = Anchor(Ddb(), S3(), "table", "bucket", 1)
    payload, descriptor = fixture()
    payload["batch"]["authorization"]["max_requests"] = 16
    payload["proposal"] = proposal(
        payload["batch"],
        output_scope="/p2/synthetic/evidence",
        operations=["product", "judge"],
        nonce="b" * 32,
    )
    job_id = routed_call(authority, "submit", **payload)["job_id"]
    authority.issuer_event(signed_event(job_id, payload["proposal"]))
    send = dispatch.send

    def checked_send(request):
        routed_call(
            authority,
            "preflight",
            job_id=job_id,
            attempt_id=request["attempt_id"],
            request_digest=request["request_digest"],
        )
        return send(request)

    monkeypatch.setattr(dispatch, "send", checked_send)
    for index in range(16):
        if index == 15:
            routed_call(authority, "finish", job_id=job_id, role="product")
        role = "judge" if index == 15 else "product"
        routed_call(
            authority,
            "invoke",
            job_id=job_id,
            role=role,
            attempt_id=f"budget-{index}",
            request_descriptor={**descriptor, "model_id": f"p2-canary-{role}"},
        )
    assert (
        routed_call(authority, "finish", job_id=job_id, role="judge")["state"]
        == "CONSUMED"
    )
    assert len(dispatch.calls) == 16
    assert all(r["send_state"] == "SETTLED" for r in store.get(job_id)["requests"])


@pytest.mark.parametrize("activation_seconds", [5, 3000])
def test_shared_deadline_survives_restart_and_role_transition(activation_seconds):
    authority, store, anchor, dispatch, job_id, descriptor, clock = timed_job(
        seconds=10, activation_seconds=activation_seconds
    )
    started_at = clock[0]
    routed_call(
        authority,
        "invoke",
        job_id=job_id,
        role="product",
        attempt_id="product",
        request_descriptor=descriptor,
    )
    routed_call(authority, "finish", job_id=job_id, role="product")
    # A fresh handler instance must retain the original budget, including for judge.
    authority = Authority(
        store,
        anchor,
        dispatch,
        FakeVerifier(),
        now=lambda: clock[0],
        worker_principal=authority.worker_principal,
    )
    deadline_seconds = min(10, activation_seconds)
    clock[0] = started_at + timedelta(seconds=deadline_seconds - 0.001)
    judge = {**descriptor, "model_id": "p2-canary-judge"}
    routed_call(
        authority,
        "invoke",
        job_id=job_id,
        role="judge",
        attempt_id="judge",
        request_descriptor=judge,
    )
    clock[0] = started_at + timedelta(seconds=deadline_seconds)
    with pytest.raises(BoundaryError, match="^batch_deadline$"):
        routed_call(
            authority,
            "invoke",
            job_id=job_id,
            role="judge",
            attempt_id="expired",
            request_descriptor=judge,
        )
    assert len(dispatch.calls) == 2
    assert len(routed_call(authority, "status", job_id=job_id)["requests"]) == 2


def test_total_budget_starts_at_activation_before_first_request():
    authority, _, _, dispatch, job_id, descriptor, clock = timed_job(seconds=1)
    clock[0] += timedelta(seconds=2)
    with pytest.raises(BoundaryError, match="^batch_deadline$"):
        routed_call(
            authority,
            "invoke",
            job_id=job_id,
            role="product",
            attempt_id="late",
            request_descriptor=descriptor,
        )
    assert not dispatch.calls
    assert not routed_call(authority, "status", job_id=job_id)["requests"]


def test_total_budget_is_rechecked_before_send_intent():
    authority, _, _, dispatch, job_id, descriptor, clock = timed_job(seconds=1)

    def expire(_job, _attempt):
        clock[0] += timedelta(seconds=1)

    authority.before_intent = expire
    with pytest.raises(BoundaryError, match="^batch_deadline$"):
        routed_call(
            authority,
            "invoke",
            job_id=job_id,
            role="product",
            attempt_id="late",
            request_descriptor=descriptor,
        )
    assert not dispatch.calls
    status = routed_call(authority, "status", job_id=job_id)
    assert (status["state"], status["stop_reason"]) == ("SUSPENDED", "batch_deadline")
    requests = status["requests"]
    assert [r["send_state"] for r in requests] == ["RESERVED"]


def test_dispatch_deadline_preflight_precedes_secret_access():
    from unittest.mock import Mock

    from agent_alfred.p2.dispatch import Dispatch

    authority, _, _, _, job_id, descriptor, clock = timed_job(seconds=1)

    def preflight(action, payload):
        clock[0] += timedelta(seconds=1)
        return routed_call(authority, action, **payload)

    api, canary, secret = Mock(), Mock(), Mock()
    api.call.side_effect = preflight
    authority.dispatch = Dispatch(api, canary, secret)
    with pytest.raises(BoundaryError, match="^request_state_unresolved$") as error:
        routed_call(
            authority,
            "invoke",
            job_id=job_id,
            role="product",
            attempt_id="late",
            request_descriptor=descriptor,
        )
    assert str(error.value.__cause__) == "batch_deadline"
    secret.assert_not_called()
    canary.call.assert_not_called()
    status = routed_call(authority, "status", job_id=job_id)
    assert status["state"] == "SUSPENDED"
    assert status["stop_reason"] == "batch_deadline"
    assert status["requests"][0]["send_state"] == "MAY_HAVE_SENT"


def test_roles_follow_product_auxiliary_judge_and_completion_boundary():
    authority, _, _, dispatch, job_id, descriptor, _ = timed_job()
    judge = {**descriptor, "model_id": "p2-canary-judge"}

    def invoke(role, attempt, request=descriptor):
        return routed_call(
            authority,
            "invoke",
            job_id=job_id,
            role=role,
            attempt_id=attempt,
            request_descriptor=request,
        )

    for role, request in (("auxiliary", descriptor), ("judge", judge)):
        with pytest.raises(BoundaryError, match="^operation_order_invalid$"):
            invoke(role, "premature-" + role, request)
    with pytest.raises(BoundaryError, match="^operation_order_invalid$"):
        routed_call(authority, "finish", job_id=job_id, role="judge")
    invoke("product", "product")
    invoke("auxiliary", "auxiliary")
    with pytest.raises(BoundaryError, match="^operation_order_invalid$"):
        invoke("product", "product-after-auxiliary")
    with pytest.raises(BoundaryError, match="^operation_order_invalid$"):
        invoke("judge", "judge-before-finish", judge)
    assert (
        routed_call(authority, "finish", job_id=job_id, role="product")["state"]
        == "ACTIVE"
    )
    for role in ("product", "auxiliary"):
        with pytest.raises(BoundaryError, match="^operation_already_finished$"):
            invoke(role, "finished-" + role)
    with pytest.raises(BoundaryError, match="^operation_already_finished$"):
        routed_call(authority, "finish", job_id=job_id, role="product")
    invoke("judge", "judge", judge)
    assert [r["role"] for r in dispatch.calls] == ["product", "auxiliary", "judge"]
    assert (
        routed_call(authority, "finish", job_id=job_id, role="judge")["state"]
        == "CONSUMED"
    )
    with pytest.raises(BoundaryError, match="^grant_not_active$"):
        invoke("judge", "finished-judge", judge)


def test_judge_only_grant_does_not_require_unapproved_product():
    authority, _, _, dispatch, job_id, descriptor, _ = timed_job(operations=("judge",))
    with pytest.raises(BoundaryError, match="^operation_not_admitted$"):
        routed_call(
            authority,
            "invoke",
            job_id=job_id,
            role="auxiliary",
            attempt_id="wrong",
            request_descriptor=descriptor,
        )
    routed_call(
        authority,
        "invoke",
        job_id=job_id,
        role="judge",
        attempt_id="judge",
        request_descriptor={**descriptor, "model_id": "p2-canary-judge"},
    )
    assert len(dispatch.calls) == 1
    assert (
        routed_call(authority, "finish", job_id=job_id, role="judge")["state"]
        == "CONSUMED"
    )


def test_synthetic_issuer_to_dispatch_settles_once_and_keeps_default_gate():
    from agent_alfred.evals.acceptance.admission import UnconfiguredAuthority

    authority, _, anchor, dispatch, job_id, descriptor = active()
    worker = "arn:aws:iam::111111111111:role/p2-worker"
    result = authority.invoke(job_id, "product", "attempt-1", descriptor, worker)
    assert result["output"] == "p2-canary-ok"
    assert len(dispatch.calls) == 1
    assert authority.status(job_id, worker)["requests"][0]["send_state"] == "SETTLED"
    assert anchor.read(job_id)["revision"] >= 4
    assert authority.finish(job_id, "product", worker)["state"] == "CONSUMED"
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        UnconfiguredAuthority().submit_job(None)


def test_revoke_between_reserve_and_send_intent_prevents_dispatch():
    authority, _, _, dispatch, job_id, descriptor = active()
    proposed = authority.proposal_view(job_id)["proposal"]
    authority.before_intent = lambda *_: authority.issuer_event(
        signed_event(job_id, proposed, "REVOKE")
    )
    with pytest.raises(BoundaryError, match="grant_not_active"):
        authority.invoke(
            job_id,
            "product",
            "attempt-1",
            descriptor,
            "arn:aws:iam::111111111111:role/p2-worker",
        )
    assert dispatch.calls == []
    assert (
        authority.status(job_id, "arn:aws:iam::111111111111:role/p2-worker")["state"]
        == "REVOKED"
    )


def test_lost_response_suspends_and_does_not_retry_or_refund():
    authority, _, _, dispatch, job_id, descriptor = active()
    dispatch.fail = True
    worker = "arn:aws:iam::111111111111:role/p2-worker"
    with pytest.raises(BoundaryError, match="request_state_unresolved"):
        authority.invoke(job_id, "product", "attempt-1", descriptor, worker)
    assert len(dispatch.calls) == 1
    status = authority.status(job_id, worker)
    assert status["state"] == "SUSPENDED"
    assert status["requests"][0]["send_state"] == "MAY_HAVE_SENT"
    with pytest.raises(BoundaryError, match="grant_not_active"):
        authority.invoke(job_id, "product", "attempt-2", descriptor, worker)
    assert len(dispatch.calls) == 1


def test_anchor_rollback_and_partial_commit_fail_closed():
    authority, store, anchor, dispatch, job_id, descriptor = active()
    worker = "arn:aws:iam::111111111111:role/p2-worker"
    anchor.rows[job_id] = (1, store.events[job_id, 1]["state_digest"])
    with pytest.raises(BoundaryError, match="authority_anchor_mismatch"):
        authority.invoke(job_id, "product", "attempt-1", descriptor, worker)
    assert dispatch.calls == []
    authority, _, anchor, dispatch, job_id, descriptor = active()
    anchor.fail_commit = True
    with pytest.raises(BoundaryError, match="anchor_unavailable"):
        authority.invoke(job_id, "product", "attempt-1", descriptor, worker)
    with pytest.raises(BoundaryError, match="authority_anchor_mismatch"):
        authority.status(job_id, worker)
    assert dispatch.calls == []


def test_worker_cannot_choose_url_or_non_canary_model():
    authority, _, _, dispatch, job_id, descriptor = active()
    worker = "arn:aws:iam::111111111111:role/p2-worker"
    with pytest.raises(BoundaryError, match="invalid_wire_shape"):
        authority.invoke(
            job_id,
            "product",
            "attempt-1",
            {**descriptor, "url": "https://provider.example"},
            worker,
        )
    with pytest.raises(BoundaryError, match="approval_profile_mismatch"):
        authority.invoke(
            job_id,
            "product",
            "attempt-1",
            {**descriptor, "endpoint_id": "deepseek"},
            worker,
        )
    assert dispatch.calls == []


def test_dynamo_state_same_revision_tamper_is_rejected_before_anchor_read():
    from agent_alfred.p2.store import DynamoJobStore

    class Ddb:
        rows = {}

        def get_item(self, *, Key, **_):
            return {"Item": self.rows.get((Key["PK"]["S"], Key["SK"]["S"]))}

        def transact_write_items(self, *, TransactItems, **_):
            for action in TransactItems:
                item = action["Put"]["Item"]
                self.rows[item["PK"]["S"], item["SK"]["S"]] = deepcopy(item)

    client = Ddb()
    store = DynamoJobStore(client, "p2-synthetic-table")
    state = {
        "job_id": "synthetic-job",
        "revision": 1,
        "state": "PENDING",
    }
    event = {
        "job_id": state["job_id"],
        "revision": 1,
        "state_digest": digest(state),
    }
    state["event_digest"] = digest(event)
    store.put(state, event, old_revision=0, old_digest=ZERO_DIGEST)
    assert store.get(state["job_id"]) == state
    row = client.rows[(state["job_id"], "STATE")]
    tampered = {**state, "state": "ACTIVE"}
    row["Document"]["S"] = __import__("json").dumps(tampered)
    with pytest.raises(BoundaryError, match="authority_event_unverifiable"):
        store.get(state["job_id"])


def test_p2_deploy_config_requires_reviewed_budget_and_independent_roles(
    tmp_path, monkeypatch
):
    root = Path(__file__).resolve().parents[4]
    monkeypatch.syspath_prepend(str(root))
    from infra.p2.ctl import parse_config

    example = root / "infra/p2/config.example.json"
    config = json.loads(example.read_text())
    config["region"] = "us-east-1"
    config["accounts"] = {"a": "111111111111", "b": "222222222222", "c": "333333333333"}
    config["admin_role_arns"] = {
        key: f"arn:aws:iam::{account}:role/p2-deployer"
        for key, account in config["accounts"].items()
    }
    config["auditor_role_arns"] = {
        key: f"arn:aws:iam::{account}:role/p2-auditor"
        for key, account in config["accounts"].items()
    }
    config["operator_role_arn"] = "arn:aws:iam::111111111111:role/p2-operator"
    config["expires_at_utc"] = (utc_now() + timedelta(hours=2)).isoformat()
    config["budget"] = {
        "currency": "USD",
        "approved_amount": "10",
        "reviewed_upper_bound": "9",
        "tax_included": "yes",
        "duration_hours": 3,
        "quote_source": "synthetic-test-quote",
    }
    config["retention"] = {"object_lock_days": 2, "log_days": 3}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    assert parse_config(path)["accounts"]["c"] == "333333333333"
    config["operator_role_arn"] = (
        "arn:aws:iam::999999999999:role/p2-operator-111111111111"
    )
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="issuer operator role"):
        parse_config(path)
    config["operator_role_arn"] = "arn:aws:iam::111111111111:role/p2-operator"
    config["budget"]["reviewed_upper_bound"] = "11"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="reviewed upper bound"):
        parse_config(path)
    config["budget"]["reviewed_upper_bound"] = "9"
    config["auditor_role_arns"]["c"] = config["admin_role_arns"]["c"]
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="independent auditor"):
        parse_config(path)

    config["auditor_role_arns"]["c"] = "arn:aws:iam::333333333333:role/p2-auditor"
    config["expires_at_utc"] = (utc_now() - timedelta(hours=1)).isoformat()
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="expiry"):
        parse_config(path)
    assert parse_config(path, require_active=False)["accounts"]["b"] == ("222222222222")


def test_late_revoke_is_processed_while_new_issue_is_stopped(monkeypatch):
    from agent_alfred.p2 import authority_handler

    authority, _, _, _, job_id, _ = active()
    proposed = authority.proposal_view(job_id)["proposal"]
    late = signed_event(job_id, proposed, "REVOKE")
    late["body"]["issued_at"] = (utc_now() - timedelta(hours=2)).isoformat()
    late["body"]["activation_deadline"] = (utc_now() - timedelta(hours=1)).isoformat()
    assert authority.issuer_event(late)["state"] == "REVOKED"

    monkeypatch.setenv("P2_SYNTHETIC_ONLY", "1")
    monkeypatch.setenv("P2_DEPLOYMENT_ACTIVE", "true")
    monkeypatch.setenv("P2_STOP_DISPATCH", "true")
    monkeypatch.setenv("P2_ISSUER_QUEUE_ARN", "synthetic-queue-arn")
    seen = []
    monkeypatch.setattr(
        authority_handler,
        "service",
        lambda: type("Api", (), {"issuer_event": lambda _, row: seen.append(row)})(),
    )
    event = {
        "Records": [
            {
                "eventSource": "aws:sqs",
                "eventSourceARN": "synthetic-queue-arn",
                "body": json.dumps(late),
            }
        ]
    }
    authority_handler.handler(event, None)
    assert len(seen) == 1
    event["Records"][0]["body"] = json.dumps(signed_event(job_id, proposed))
    with pytest.raises(BoundaryError, match="p2_dispatch_stopped"):
        authority_handler.handler(event, None)


def test_expired_window_blocks_new_issue_but_keeps_revoke_available(monkeypatch):
    from agent_alfred.p2.issuer import SyntheticIssuer

    monkeypatch.setenv("P2_EXPIRES_AT", (utc_now() - timedelta(seconds=1)).isoformat())
    sent = []
    issuer = SyntheticIssuer(None, lambda _: b"synthetic-signature", sent.append)
    issuer.view = lambda _: {
        "state": "ACTIVE",
        "proposal": {"nonce": "b" * 32},
    }
    with pytest.raises(BoundaryError, match="p2_deployment_expired"):
        issuer.emit("synthetic-job", "ISSUE", "operator")
    assert issuer.emit("synthetic-job", "REVOKE", "operator")["decision"] == "REVOKE"
    assert len(sent) == 1


def test_negative_probe_does_not_count_an_unexpected_sdk_error_as_blocked():
    from botocore.exceptions import ClientError

    from agent_alfred.p2.probe import _outcome

    def missing_resource():
        raise ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "missing"}},
            "GetSecretValue",
        )

    assert _outcome(missing_resource) == {
        "blocked": False,
        "category": "OTHER_ERROR",
        "error_type": "ClientError",
    }

    def allowed_anchor_write_with_impossible_condition():
        raise ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
        )

    assert _outcome(allowed_anchor_write_with_impossible_condition)["blocked"] is False


def test_cloudformation_suppresses_default_allow_all_egress():
    from infra.p2.stacks import account_a, account_b, account_c

    for builder in (account_a, account_b, account_c):
        resources = builder()["Resources"]
        groups = [
            row["Properties"]
            for row in resources.values()
            if row["Type"] == "AWS::EC2::SecurityGroup"
        ]
        assert groups
        for group in groups:
            assert group["SecurityGroupEgress"] == [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 443,
                    "ToPort": 443,
                    "CidrIp": "192.0.2.0/32",
                }
            ]


@pytest.mark.parametrize("region", ["us-east-1", "eu-west-1"])
def test_anchor_sdk_requests_match_the_deployed_dns_allowlist(monkeypatch, region):
    import os
    from unittest.mock import Mock
    from urllib.parse import urlsplit

    import boto3
    import botocore.session
    from infra.p2.stacks import account_c

    from agent_alfred.p2.anchor import handler

    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("P2_SYNTHETIC_ONLY", "1")
    monkeypatch.setenv("P2_ANCHOR_TABLE", "synthetic-table")
    monkeypatch.setenv("P2_ANCHOR_BUCKET", "synthetic-anchor-bucket")
    monkeypatch.setenv("P2_RETENTION_DAYS", "1")
    session = botocore.session.Session()
    ddb = Mock()
    ddb.get_item.return_value = {}
    clients, requests = [], []

    class BeforeNetwork(Exception):
        pass

    def intercept(request, **_kwargs):
        requests.append(urlsplit(request.url))
        raise BeforeNetwork

    def client(service, **kwargs):
        if service == "dynamodb":
            return ddb
        assert service == "s3"
        s3 = session.create_client(
            service,
            region_name=region,
            aws_access_key_id="synthetic",
            aws_secret_access_key="synthetic",
            **kwargs,
        )
        s3.meta.events.register("before-send.s3", intercept)
        clients.append(s3)
        return s3

    monkeypatch.setattr(boto3, "client", client)
    with pytest.raises(BeforeNetwork):
        handler({"action": "read", "job_id": "synthetic-job"}, None)
    s3 = clients[0]
    for method, extra in (
        (s3.get_object_retention, {"VersionId": "synthetic-version"}),
        (s3.get_object, {"VersionId": "synthetic-version"}),
        (s3.put_object, {"Body": b"synthetic"}),
    ):
        with pytest.raises(BeforeNetwork):
            method(
                Bucket="synthetic-anchor-bucket", Key="anchors/job/event.json", **extra
            )
    resources = account_c()["Resources"]
    allowlist = resources["AnchorAllowedDns"]["Properties"]["Domains"]
    domains = [d["Fn::Sub"].replace("${AWS::Region}", region) for d in allowlist]
    assert len(requests) == 4
    assert all(r.hostname in domains for r in requests)
    assert all(r.path.startswith("/synthetic-anchor-bucket") for r in requests)


@pytest.mark.parametrize("account", ["b", "c"])
def test_readback_lambda_names_are_indexed_by_output_arn(account):
    from unittest.mock import MagicMock

    from infra.p2.readback import snapshot

    outputs = (
        ["AnchorFunctionArn"]
        if account == "c"
        else [
            "DispatchProbeFunctionArn",
            "DispatchIamProbeFunctionArn",
            "AuthorityProbeFunctionArn",
            "AuthorityIamProbeFunctionArn",
        ]
    )
    account_id = "333333333333" if account == "c" else "222222222222"
    names = {key: "p2-" + key.removesuffix("Arn").lower() for key in outputs}
    arns = {
        key: f"arn:aws:lambda:us-east-1:{account_id}:function:{name}"
        for key, name in names.items()
    }
    config = {
        "accounts": {account: account_id},
        "region": "us-east-1",
        "stack_prefix": "p2synthetic",
        "admin_role_arns": {account: f"arn:aws:iam::{account_id}:role/admin"},
        "auditor_role_arns": {account: f"arn:aws:iam::{account_id}:role/auditor"},
    }
    cf, iam, lam = MagicMock(), MagicMock(), MagicMock()
    cf.describe_stacks.side_effect = lambda **kw: {
        "Stacks": [
            {
                "StackStatus": "CREATE_COMPLETE",
                "Outputs": [
                    {"OutputKey": key, "OutputValue": arn} for key, arn in arns.items()
                ]
                if kw["StackName"].endswith("service-" + account)
                else [],
            }
        ]
    }
    cf.get_paginator.return_value.paginate.side_effect = lambda **kw: [
        {
            "StackResourceSummaries": [
                {
                    "LogicalResourceId": key.removesuffix("Arn"),
                    "PhysicalResourceId": name,
                    "ResourceType": "AWS::Lambda::Function",
                    "ResourceStatus": "CREATE_COMPLETE",
                }
                for key, name in names.items()
            ]
            if kw["StackName"].endswith("service-" + account)
            else [],
        }
    ]
    iam.get_role.side_effect = lambda **kw: {
        "Role": {
            "Arn": f"arn:aws:iam::{account_id}:role/" + kw["RoleName"],
            "AssumeRolePolicyDocument": {"Statement": []},
        }
    }
    iam.get_paginator.return_value.paginate.return_value = []
    lam.get_function_configuration.side_effect = lambda **kw: {
        "FunctionArn": f"arn:aws:lambda:us-east-1:{account_id}:function:"
        + kw["FunctionName"],
        "Role": f"arn:aws:iam::{account_id}:role/synthetic",
    }
    lam.get_policy.return_value = {"Policy": "{}"}
    session = MagicMock()
    session.client.side_effect = lambda service: {
        "cloudformation": cf,
        "iam": iam,
        "lambda": lam,
    }.get(service, MagicMock())
    record = snapshot(session, config, account)
    # Same producer/consumer join as accept's Anchor and four probe checks.
    for key in outputs:
        arn = record["stacks"]["service-" + account]["outputs"][key]
        assert record["functions"][arn]["arn"] == arns[key]
    assert set(record["functions"]) == set(arns.values())


def test_finalize_stop_rejects_active_grant_even_when_queue_looks_empty():
    from infra.p2.ctl import require_drained

    class Sqs:
        def get_queue_attributes(self, **_):
            return {"Attributes": {"ApproximateNumberOfMessages": "0"}}

    class Ddb:
        def scan(self, **_):
            return {
                "Items": [
                    {
                        "PK": {"S": "synthetic-job"},
                        "SK": {"S": "STATE"},
                        "Document": {"S": json.dumps({"state": "ACTIVE"})},
                    }
                ]
            }

    class Ecs:
        def get_paginator(self, _):
            return self

        def paginate(self, **_):
            return [{"taskArns": []}]

    class Session:
        def client(self, name):
            return {"sqs": Sqs(), "dynamodb": Ddb(), "ecs": Ecs()}[name]

    state = {
        "foundation-b": {
            "IssuerQueueUrl": "synthetic-q",
            "IssuerDlqUrl": "synthetic-dlq",
        },
        "service-b": {
            "GrantTableName": "synthetic-ledger",
            "WorkerClusterName": "synthetic-cluster",
        },
        "service-a": {"P2TrafficState": "false"},
    }
    with pytest.raises(ValueError, match="active synthetic grant remains"):
        require_drained(Session(), state)


def test_iam_probe_cannot_pass_from_network_timeout_or_canary_business_403():
    from infra.p2.accept import _probe

    result = {
        name: {"blocked": True, "category": "AWS_ACCESS_DENIED"}
        for name in (
            "issuer_sign",
            "preflight_sign",
            "grant_read",
            "grant_write",
            "anchor_table_write",
            "anchor_invoke",
            "admin_assume",
            "secret_read",
        )
    }
    result.update(
        {
            name: {"blocked": True, "category": "NETWORK_BLOCKED"}
            for name in (
                "public_ipv4",
                "public_ipv6",
                "public_dns",
                "direct_dns",
                "proxy_bypass",
            )
        }
    )
    result["canary_direct"] = {"blocked": True, "category": "GATEWAY_DENIED"}
    result["all_blocked"] = True
    _probe(result, "worker IAM", role_name="worker", iam=True)
    result["issuer_sign"]["category"] = "NETWORK_BLOCKED"
    with pytest.raises(ValueError, match="protected identity action"):
        _probe(result, "worker IAM", role_name="worker", iam=True)
    result["issuer_sign"]["category"] = "AWS_ACCESS_DENIED"
    result["grant_read"]["category"] = "OPEN"
    with pytest.raises(ValueError, match="protected identity action"):
        _probe(result, "worker IAM", role_name="worker", iam=True)
    result["grant_read"]["category"] = "AWS_ACCESS_DENIED"
    result["anchor_table_write"]["category"] = "NETWORK_BLOCKED"
    with pytest.raises(ValueError, match="protected endpoint route"):
        _probe(result, "worker IAM", role_name="worker", iam=True)
    result["anchor_table_write"]["category"] = "AWS_ACCESS_DENIED"
    result["canary_direct"]["category"] = "OTHER_ERROR"
    with pytest.raises(ValueError, match="unauthorized canary application access"):
        _probe(result, "worker IAM", role_name="worker", iam=True)


def test_canary_application_403_is_not_an_access_boundary_denial():
    import io
    import urllib.error

    from botocore.credentials import Credentials

    from agent_alfred.p2.remote import PrivateApi

    class Session:
        def get_credentials(self):
            return Credentials("synthetic-access", "synthetic-secret")

    class Opener:
        def open(self, request, **_):
            raise urllib.error.HTTPError(
                request.full_url,
                403,
                "Forbidden",
                {},
                io.BytesIO(b'{"error":"canary_token_denied"}'),
            )

    api = PrivateApi(
        "https://example.execute-api.us-east-1.amazonaws.com/p2",
        "us-east-1",
        session=Session(),
        opener=Opener(),
    )
    with pytest.raises(BoundaryError, match="private_api_application_denied"):
        api.call("canary", {})


def test_live_stop_gate_rejects_a_stale_local_stopped_marker():
    from infra.p2.ctl import require_live_stopped

    class CloudFormation:
        def describe_stacks(self, **_):
            return {
                "Stacks": [
                    {
                        "StackStatus": "UPDATE_COMPLETE",
                        "Parameters": [
                            {"ParameterKey": "EnableTraffic", "ParameterValue": "true"},
                            {"ParameterKey": "StopDispatch", "ParameterValue": "false"},
                        ],
                    }
                ]
            }

    with pytest.raises(ValueError, match="still active in AWS"):
        require_live_stopped(
            CloudFormation(), {"stack_prefix": "p2synthetic"}, "service-b"
        )


def test_closed_service_cannot_reactivate_or_launch_probe():
    from infra.p2.ctl import require_staged, run_task

    state = {
        "service-b": {
            "P2TrafficState": "false",
            "P2NeverActivated": False,
            "P2Closed": True,
        }
    }
    with pytest.raises(ValueError, match="never-stopped staged service"):
        require_staged(state, "service-b")
    with pytest.raises(ValueError, match="environment is closed"):
        run_task(None, {}, state, "probe", None, None)


def test_failed_stack_can_be_deleted_and_retained_resource_is_reported():
    from infra.p2.ctl import delete_stack

    class CloudFormation:
        def describe_stacks(self, **_):
            return {"Stacks": [{"StackStatus": "ROLLBACK_COMPLETE", "Outputs": []}]}

        def get_paginator(self, _):
            return self

        def paginate(self, **_):
            return [
                {
                    "StackResourceSummaries": [
                        {
                            "LogicalResourceId": "AnchorTable",
                            "PhysicalResourceId": "synthetic-anchor-table",
                            "ResourceStatus": "CREATE_COMPLETE",
                            "ResourceType": "AWS::DynamoDB::Table",
                        }
                    ]
                }
            ]

        def delete_stack(self, **_):
            return None

        def get_waiter(self, _):
            return self

        def wait(self, **_):
            return None

    class Session:
        def client(self, name):
            assert name == "cloudformation"
            return CloudFormation()

    result = delete_stack(Session(), {"stack_prefix": "p2synthetic"}, "service-c")
    assert result["cleanup_status"] == "DELETED"
    assert result["retained_resources"] == ["synthetic-anchor-table"]


def test_ecr_cleanup_batches_the_service_limit():
    from infra.p2.ctl import empty_repository

    class Ecr:
        batches = []

        def get_paginator(self, _):
            return self

        def paginate(self, **_):
            return [{"imageIds": [{"imageDigest": str(index)} for index in range(201)]}]

        def batch_delete_image(self, **kwargs):
            self.batches.append(len(kwargs["imageIds"]))
            return {"failures": []}

    ecr = Ecr()

    class Session:
        def client(self, name):
            assert name == "ecr"
            return ecr

    empty_repository(Session(), "arn:aws:ecr:us-east-1:111111111111:repository/p2")
    assert ecr.batches == [100, 100, 1]


def test_cleanup_failure_keeps_live_state_and_first_failure():
    from infra.p2.ctl import record_phase_result

    state = {"service-b": {"P2Closed": True, "P2Incident": "first-failure"}}
    record_phase_result(
        state,
        "service-b",
        {"cleanup_status": "DELETE_FAILED", "cleanup_error": "ResourceInUse"},
    )
    assert state["service-b"]["P2Incident"] == "first-failure"
    assert state["cleanup_attempts"][0]["cleanup_error"] == "ResourceInUse"
    record_phase_result(state, "service-b", {"cleanup_status": "DELETED"})
    assert state["service-b"]["prior_state"]["P2Incident"] == "first-failure"


def test_c_cleanup_requires_fresh_independent_a_and_b_absence(tmp_path):
    import hashlib

    from infra.p2.ctl import require_ab_absent

    config = {
        "accounts": {"a": "111111111111", "b": "222222222222"},
        "region": "us-east-1",
        "auditor_role_arns": {
            "a": "arn:aws:iam::111111111111:role/p2-auditor",
            "b": "arn:aws:iam::222222222222:role/p2-auditor",
        },
    }

    def receipt(account, stacks):
        path = tmp_path / f"{account}.json"
        content = json.dumps(
            {
                "account": config["accounts"][account],
                "region": config["region"],
                "auditor_role": config["auditor_role_arns"][account],
                "at": utc_now().isoformat(),
                "stacks": stacks,
            }
        ).encode()
        path.write_bytes(content)
        path.with_suffix(".json.sha256").write_text(hashlib.sha256(content).hexdigest())
        return path

    a = receipt("a", {"service-a": {}})
    b = receipt("b", {})
    with pytest.raises(ValueError, match="C creation timestamp missing"):
        require_ab_absent(config, {}, a, b)
    state = {
        "service-c": {"P2CreatedAt": (utc_now() - timedelta(seconds=30)).isoformat()}
    }
    with pytest.raises(ValueError, match="A/B service still exists"):
        require_ab_absent(config, state, a, b)
    a = receipt("a", {})
    require_ab_absent(config, state, a, b)
    state.update(
        {
            "service-a": {
                "cleanup_status": "DELETED",
                "deleted_at": (utc_now() + timedelta(minutes=1)).isoformat(),
            }
        }
    )
    with pytest.raises(ValueError, match="freshness mismatch"):
        require_ab_absent(config, state, a, b)
    state["service-a"]["deleted_at"] = (utc_now() - timedelta(seconds=30)).isoformat()
    state["service-b"] = {
        "cleanup_status": "DELETED",
        "deleted_at": (utc_now() - timedelta(seconds=30)).isoformat(),
    }
    require_ab_absent(config, state, a, b)


def test_accept_rejects_iam_alternate_selector_and_unreviewed_image():
    import base64

    from infra.p2.accept import actions, check_artifacts

    for selector in ("NotAction", "NotResource", "NotPrincipal"):
        policy = {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}
        policy[selector] = "iam:*"
        with pytest.raises(ValueError, match="unbounded alternate selector"):
            actions({"inline_policies": {"unsafe": {"Statement": [policy]}}})

    sha = "a" * 64
    package = "b" * 64
    input_digest = "c" * 64
    zip_digest = "d" * 64
    image_uri = "111111111111.dkr.ecr.us-east-1.amazonaws.com/p2@sha256:" + sha
    manifest = {
        "source_merge_sha": "merge",
        "base_image": "python@sha256:" + sha,
        "image_tag": "p2-synthetic-local:abc",
        "package_sha256": package,
        "input_sha256": input_digest,
        "lambda_zip_sha256": zip_digest,
    }
    candidate = {"baseline_sha": "merge", "lambda_artifact": manifest}
    state = {
        "artifact-" + account: {
            "LambdaZipSha256": zip_digest,
            "PackageSha256": package,
            "InputSha256": input_digest,
        }
        for account in "abc"
    }
    state["image-b"] = {
        "ImageUri": image_uri,
        "ImageTag": "p2-" + package[:12] + "-" + input_digest[:12],
    }
    records = {
        account: {
            "functions": {
                "f": {
                    "code_sha256": base64.b64encode(bytes.fromhex(zip_digest)).decode()
                }
            }
        }
        for account in "abc"
    }
    records["b"]["stacks"] = {
        "foundation-b": {"outputs": {"ImageRepositoryUri": image_uri.split("@")[0]}}
    }
    records["b"]["ecr_images"] = [
        {"imageDigest": "sha256:" + sha, "imageTags": [state["image-b"]["ImageTag"]]}
    ]
    records["b"]["task_definitions"] = {
        "task": {
            "containers": [
                {
                    "image": image_uri,
                    "package_sha256": package,
                    "input_sha256": input_digest,
                }
            ]
        }
    }
    with pytest.raises(ValueError, match="independently reviewed candidate"):
        check_artifacts({"source_merge_sha": "merge"}, records, state, candidate)
    candidate["deployed_image_uri"] = image_uri
    check_artifacts({"source_merge_sha": "merge"}, records, state, candidate)
    records["a"]["functions"]["f"]["code_sha256"] = "different"
    with pytest.raises(ValueError, match="running Lambda code"):
        check_artifacts({"source_merge_sha": "merge"}, records, state, candidate)


def test_resource_policy_comparison_rejects_hidden_service_principal():
    from infra.p2.accept import api_resource_policy_exact, policy_statements_equal

    expected = [
        {
            "Effect": "Allow",
            "Principal": {"AWS": "arn:aws:iam::333333333333:role/p2-admin"},
            "Action": "kms:Sign",
            "Resource": "*",
        }
    ]
    actual = deepcopy(expected)
    assert policy_statements_equal({"Statement": actual}, expected)
    actual[0]["Principal"]["Service"] = "lambda.amazonaws.com"
    assert not policy_statements_equal({"Statement": actual}, expected)
    api_policy = {
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": "*",
                "Action": "execute-api:Invoke",
                "Resource": "arn:aws:execute-api:us-east-1:222222222222:api-id/*",
            }
        ]
    }
    config = {"region": "us-east-1", "accounts": {"b": "222222222222"}}
    assert api_resource_policy_exact(api_policy, config, "b", "api-id", "*")
    api_policy["Statement"][0]["NotPrincipal"] = {"AWS": "safe"}
    assert not api_resource_policy_exact(api_policy, config, "b", "api-id", "*")


def test_authority_negative_probe_skips_allowed_grant_and_anchor_invocation(
    monkeypatch,
):
    from botocore.exceptions import ClientError

    from agent_alfred.p2 import probe
    from agent_alfred.p2.remote import PrivateApi

    monkeypatch.setenv("P2_SYNTHETIC_ONLY", "1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    def denied(*_args, **_kwargs):
        raise OSError("synthetic network block")

    monkeypatch.setattr(probe.socket, "create_connection", denied)
    monkeypatch.setattr(probe.socket, "getaddrinfo", denied)
    monkeypatch.setattr(probe, "_direct_dns", denied)
    monkeypatch.setattr(probe, "_proxy_probe", denied)
    monkeypatch.setattr(
        PrivateApi,
        "call",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            BoundaryError("private_api_rejected:403")
        ),
    )

    class Client:
        calls = []

        def __getattr__(self, name):
            def blocked(**kwargs):
                self.calls.append((name, kwargs))
                raise ClientError({"Error": {"Code": "AccessDeniedException"}}, name)

            return blocked

    class Session:
        used = []

        def client(self, name, **_):
            self.used.append(name)
            return Client()

    session = Session()
    targets = {
        "secret_arn": "arn:aws:secretsmanager:us-east-1:222222222222:secret:p2",
        "signing_key_arn": "arn:aws:kms:us-east-1:111111111111:key/p2",
        "preflight_key_arn": "arn:aws:kms:us-east-1:333333333333:key/p2",
        "grant_table_name": "synthetic-table",
        "anchor_table_arn": "arn:aws:dynamodb:us-east-1:333333333333:table/p2-anchor",
        "anchor_function_arn": "synthetic-anchor",
        "admin_role_arn": "arn:aws:iam::222222222222:role/p2-admin",
        "canary_api_base": "https://apiid.execute-api.us-east-1.amazonaws.com/p2",
    }
    result = probe.run(targets, role="authority", aws_session=session)
    assert result["all_blocked"] is True
    assert result["preflight_sign"]["category"] == "AWS_ACCESS_DENIED"
    assert "lambda" not in session.used
    assert [
        (name, kwargs["TableName"])
        for name, kwargs in Client.calls
        if name == "update_item"
    ] == [("update_item", targets["anchor_table_arn"])]
    assert all(name not in ("get_item", "put_item") for name, _ in Client.calls)
    assert next(kwargs for name, kwargs in Client.calls if name == "update_item")[
        "ConditionExpression"
    ] == ("attribute_exists(PK) AND attribute_not_exists(PK)")


def test_dispatch_probe_preserves_failed_invocation_receipt(monkeypatch):
    from infra.p2 import ctl

    monkeypatch.setattr(ctl, "probe_targets", lambda *_: {})

    class Lambda:
        def invoke(self, **_):
            return {
                "StatusCode": 200,
                "ResponseMetadata": {"RequestId": "request-1"},
                "Payload": type("Payload", (), {"read": lambda _: b"not-json"})(),
            }

    class Session:
        def client(self, _):
            return Lambda()

    result = ctl.probe_dispatch(
        Session(), {}, {"service-b": {"DispatchProbeFunctionArn": "synthetic-f"}}
    )
    assert result["request_id"] == "request-1"
    assert result["result"] is None
    assert result["invoke_error"] == "response_invalid"


def test_activation_requires_fresh_reviewed_boundary_preflight(tmp_path):
    import base64
    import hashlib

    from infra.p2.ctl import preflight_signature_message, require_preflight

    config = tmp_path / "config.json"
    config.write_text('{"synthetic":true}')
    receipt = {
        "checked_at": utc_now().isoformat(),
        "candidate_sha256": "a" * 64,
        "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        "preflight_boundary": "PASS_SYNTHETIC_CANARY_ONLY",
        "control_plane": "PASS",
        "network_probes": "PASS",
        "identity_probes": "PASS_TESTED_PATHS_ONLY",
        "anchor_table_route_probe": "PASS_ENDPOINT_DENIAL_ONLY",
        "identity_privilege_expansion": "PENDING_REAL_EVIDENCE",
        "unverified_identity_paths": [
            "C_anchor_table_role_IAM_direct_write",
            "C_anchor_S3_direct_write",
            "IAM_role_trust_or_policy_mutation",
            "ECS_RunTask_and_PassRole",
            "task_definition_mutation",
            "VPC_SG_DNS_policy_mutation",
        ],
        "readback_sha256": {account: "b" * 64 for account in "abc"},
        "probe_sha256": {
            name: "c" * 64
            for name in (
                "worker",
                "executor",
                "dispatch",
                "authority",
                "worker_iam",
                "executor_iam",
                "dispatch_iam",
                "authority_iam",
            )
        },
    }
    path = tmp_path / "preflight.json"
    path.write_text(json.dumps(receipt))
    reviewed_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    key_arn = "arn:aws:kms:us-east-1:333333333333:key/synthetic-key"
    signature_path = tmp_path / "signature.json"
    signature_path.write_text(
        json.dumps(
            {
                "receipt_sha256": reviewed_sha,
                "key_arn": key_arn,
                "signer_role_arn": "arn:aws:iam::333333333333:role/p2-deployer",
                "signing_algorithm": "ECDSA_SHA_256",
                "signed_at": utc_now().isoformat(),
                "signature": base64.b64encode(b"synthetic-signature").decode(),
            }
        )
    )
    config_data = {
        "region": "us-east-1",
        "accounts": {"c": "333333333333"},
        "admin_role_arns": {"c": "arn:aws:iam::333333333333:role/p2-deployer"},
    }
    state = {"service-c": {"PreflightSignerKeyArn": key_arn}}

    class Kms:
        def verify(self, **kwargs):
            assert kwargs["Message"] == preflight_signature_message(
                json.loads(signature_path.read_text())
            )
            return {"SignatureValid": True, "KeyId": key_arn}

    class Session:
        def client(self, name):
            assert name == "kms"
            return Kms()

    with pytest.raises(ValueError, match="C administrator preflight signature"):
        require_preflight(
            Session(), config_data, config, state, path, reviewed_sha, None
        )
    assert (
        require_preflight(
            Session(), config_data, config, state, path, reviewed_sha, signature_path
        )
        == receipt
    )
    receipt["network_probes"] = "FAIL"
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="reviewed SHA256"):
        require_preflight(
            Session(), config_data, config, state, path, reviewed_sha, signature_path
        )


def test_dispatch_probe_requires_independent_matching_log_event():
    from infra.p2.dispatch_result import snapshot

    class Logs:
        def get_paginator(self, _):
            return self

        def paginate(self, **_):
            return [
                {
                    "events": [
                        {
                            "eventId": "log-event-1",
                            "logStreamName": "synthetic-stream",
                            "timestamp": 1,
                            "message": json.dumps(
                                {
                                    "p2_audit": "lambda_negative_probe",
                                    "request_id": "request-1",
                                    "result": {"all_blocked": False},
                                }
                            ),
                        }
                    ]
                }
            ]

    class Session:
        def client(self, name):
            assert name == "logs"
            return Logs()

    prefix = "arn:aws:lambda:us-east-1:222222222222:function:"
    state = {
        "service-b": {
            "DispatchProbeFunctionArn": prefix + "p2-probe",
            "DispatchIamProbeFunctionArn": prefix + "p2-probe-iam",
            "AuthorityProbeFunctionArn": prefix + "p2-authority-probe",
            "AuthorityIamProbeFunctionArn": prefix + "p2-authority-probe-iam",
        }
    }
    config = {
        "accounts": {"b": "222222222222"},
        "region": "us-east-1",
        "auditor_role_arns": {"b": "arn:aws:iam::222222222222:role/auditor"},
    }
    invocation = {
        "function_arn": state["service-b"]["DispatchProbeFunctionArn"],
        "request_id": "request-1",
        "observed_at": utc_now().isoformat(),
        "result": {"all_blocked": False},
    }
    assert snapshot(Session(), config, state, invocation)["result"] == {
        "all_blocked": False
    }
    invocation["result"] = {"all_blocked": True}
    with pytest.raises(ValueError, match="not independently found"):
        snapshot(Session(), config, state, invocation)
