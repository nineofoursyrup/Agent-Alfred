"""Public profile and native descriptor regressions from the c29 review."""

import json
import os
import sys
from copy import deepcopy
from datetime import UTC, datetime

import httpx2 as httpx
import pytest

from agent_alfred.clock import FakeClock
from agent_alfred.evals.acceptance.budget import AuthorizedBatch, binding
from agent_alfred.evals.acceptance.schema import digest, judge_model
from agent_alfred.evals.acceptance.simulation_authority import SimulationAuthority
from agent_alfred.evals.deterministic.test_acceptance_admission import simulated_session
from agent_alfred.evals.deterministic.test_acceptance_execution_policy import (
    authorized_fixture,
)
from agent_alfred.messages import Message, TextBlock
from agent_alfred.model import ClientSnapshot, ModelAssignment, ModelRef, ModelRequest


def bound_client(tmp_path, *, role="product", duplicate_assignment=False, usage=True):
    batch, _ = authorized_fixture()
    profile = batch["profiles"][0]
    if duplicate_assignment:
        profile["product_models"].append(dict(profile["product_models"][0]))
    profile["parameters"].update(per_attempt_timeout_s=2, overall_deadline_s=5)
    profile["id"] = digest({k: v for k, v in profile.items() if k != "id"})
    batch["authorization"]["binding"] = binding(batch)
    clock = FakeClock(wall=datetime(2026, 9, 25, tzinfo=UTC))
    _, _, _, session = simulated_session(tmp_path, batch, clock=clock)
    session.bind(batch, operation=role, output_scope=tmp_path / "evidence")
    model = (
        profile["product_models"][0]
        if role == "product"
        else judge_model(batch, profile)
    )
    observed = []

    def send(request):
        observed.append(
            {
                "body": json.loads(request.content),
                "timeout": request.extensions["timeout"],
            }
        )
        body = {
            "id": "synthetic",
            "model": json.loads(request.content)["model"],
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        }
        if usage:
            body["usage"] = {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            }
        return httpx.Response(200, json=body)

    budget = AuthorizedBatch(
        batch, session=session, mock_transport=httpx.MockTransport(send)
    )
    config = ClientSnapshot(
        profile["id"],
        ModelAssignment(model["endpoint_id"], model["model_id"], model["wire_style"]),
        None,
        "synthetic",
        False,
        False,
        30,
        30,
    )
    request = ModelRequest(
        ModelRef(model["endpoint_id"], model["model_id"]),
        None,
        (Message("user", (TextBlock("synthetic"),)),),
        max_tokens=256,
    )
    return budget, budget.client(config, role=role), request, observed, clock


def test_product_request_cannot_exceed_signed_profile_tokens(tmp_path):
    from dataclasses import replace

    budget, client, request, observed, _ = bound_client(tmp_path)
    try:
        with pytest.raises(ValueError, match="approval_profile_mismatch"):
            client.respond(replace(request, max_tokens=1024))
        assert observed == []
        assert budget.session.snapshot()["requests"] == []
    finally:
        budget.close()


def test_actual_timeout_is_capped_by_signed_profile(tmp_path):
    budget, client, request, observed, _ = bound_client(tmp_path)
    try:
        result = client.respond(request)
        assert result.response is not None
        assert len(observed) == 1
        assert 0 < observed[0]["timeout"]["read"] <= 2
    finally:
        budget.close()


def test_signed_overall_deadline_applies_without_caller_deadline(tmp_path):
    from agent_alfred.stream_fallback import OverallDeadlineExceeded

    budget, client, request, observed, clock = bound_client(tmp_path)
    try:
        clock.monotonic_value += 6
        with pytest.raises(OverallDeadlineExceeded):
            client.respond(request)
        assert observed == []
        assert budget.session.snapshot()["requests"] == []
    finally:
        budget.close()


def test_primary_and_auxiliary_may_share_one_signed_model(tmp_path):
    budget, client, request, observed, _ = bound_client(
        tmp_path,
        duplicate_assignment=True,
    )
    try:
        assert client.respond(request).response is not None
        assert len(observed) == 1
    finally:
        budget.close()


def test_send_intent_without_local_continuation_stops_next_dispatch(tmp_path):
    from agent_alfred.runtime.telemetry import AttemptObservationFailed

    budget, client, request, observed, _ = bound_client(tmp_path)

    def fail_journal(*_args):
        raise OSError("synthetic_journal_failure")

    try:
        budget.journal = fail_journal
        with pytest.raises(AttemptObservationFailed):
            client.respond(request)
        state = budget.session.snapshot()
        assert len(state["requests"]) == 1
        assert state["requests"][0]["send_state"] == "SEND_INTENT"
        assert state["requests"][0]["usage"] is None
        assert state["stop_reason"] == "request_state_unresolved"
        budget.journal = None
        with pytest.raises(ValueError, match="request_state_unresolved"):
            client.respond(request)
        assert observed == []
        assert budget.session.snapshot()["requests"] == state["requests"]
    finally:
        budget.close()


def test_explicit_profile_allows_shared_judge_model(tmp_path):
    batch, _ = authorized_fixture()
    extra = deepcopy(batch["profiles"][0])
    extra["parameters"]["per_attempt_timeout_s"] = 7
    extra["id"] = digest({k: v for k, v in extra.items() if k != "id"})
    batch["profiles"].append(extra)
    batch["authorization"]["binding"] = binding(batch)
    clock = FakeClock(wall=datetime(2026, 9, 25, tzinfo=UTC))
    _, _, _, session = simulated_session(tmp_path, batch, clock=clock)
    session.bind(batch, operation="judge", output_scope=tmp_path / "evidence")
    model = judge_model(batch, extra)
    observed = []

    def send(request):
        observed.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "model": observed[-1]["model"],
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    budget = AuthorizedBatch(
        batch, session=session, mock_transport=httpx.MockTransport(send)
    )
    snapshot = ClientSnapshot(
        extra["id"],
        ModelAssignment(model["endpoint_id"], model["model_id"], model["wire_style"]),
        None,
        "synthetic",
        False,
        False,
        30,
        30,
    )
    request = ModelRequest(
        ModelRef(model["endpoint_id"], model["model_id"]),
        None,
        (Message("user", (TextBlock("synthetic"),)),),
        max_tokens=256,
    )
    try:
        assert budget.client(snapshot, role="judge").respond(request).response
        assert len(observed) == 1
        assert session.snapshot()["requests"][0]["profile_id"] == extra["id"]
    finally:
        budget.close()


def test_judge_wire_format_comes_from_signed_model(tmp_path):
    budget, client, request, observed, _ = bound_client(tmp_path, role="judge")
    try:
        assert request.response_format is None
        result = client.respond(request)
        assert result.response is not None
        assert observed[0]["body"]["response_format"] == {"type": "json_object"}
    finally:
        budget.close()


def test_judge_keeps_original_monotonic_deadline_after_wall_clock_skew(tmp_path):
    from datetime import timedelta

    product, client, request, observed, clock = bound_client(tmp_path)
    original_start = product.started_at
    original_deadline = product.deadline
    try:
        assert client.respond(request).response is not None
    finally:
        product.close()
    session = product.session
    session.finish("product")
    clock.monotonic_value += 1199
    clock.wall += timedelta(seconds=1190)
    batch = product.source
    session.bind(batch, operation="judge", output_scope=tmp_path / "evidence")
    judge = AuthorizedBatch(
        batch, session=session, mock_transport=product.mock_transport
    )
    profile = batch["profiles"][0]
    model = judge_model(batch, profile)
    config = ClientSnapshot(
        profile["id"],
        ModelAssignment(model["endpoint_id"], model["model_id"], model["wire_style"]),
        None,
        "synthetic",
        False,
        False,
        30,
        30,
    )
    try:
        client = judge.client(config, role="judge")
        result = client.respond(
            ModelRequest(
                ModelRef(model["endpoint_id"], model["model_id"]),
                None,
                (Message("user", (TextBlock("synthetic judge"),)),),
                max_tokens=4096,
                response_format="json_object",
            )
        )
        assert result.response is not None
        assert observed[-1]["body"]["max_tokens"] == 4096
        assert 0 < observed[-1]["timeout"]["read"] <= 1
        assert judge.deadline == original_deadline
        assert judge.started_at == original_start
        assert len(session.snapshot()["requests"]) == 2
    finally:
        judge.close()


def test_authority_directory_open_interruption_retains_native_owner(tmp_path):
    opened = []
    injected = KeyboardInterrupt("synthetic directory-open interruption")

    def trace(frame, event, arg):
        if frame.f_code is SimulationAuthority._save.__code__ and event == "line":
            descriptor = frame.f_locals.get("descriptor")
            if type(descriptor) is int and not opened:
                opened.append(descriptor)
                raise injected
        return trace

    try:
        sys.settrace(trace)
        try:
            with pytest.raises(KeyboardInterrupt) as caught:
                SimulationAuthority(tmp_path / "authority")
            assert caught.value is injected
        finally:
            sys.settrace(None)
        assert len(opened) == 1
        with pytest.raises(OSError):
            os.fstat(opened[0])
    finally:
        sys.settrace(None)
        for descriptor in opened:
            try:
                os.fstat(descriptor)
            except OSError:
                pass
            else:
                os.close(descriptor)
