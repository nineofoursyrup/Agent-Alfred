"""Attempt outcome has one model-owned closed set across projections."""

from __future__ import annotations

from typing import get_type_hints

import pytest

from agent_alfred.evals.deterministic._state_wire_test_helpers import (
    only_attempt_in_snapshot_wire,
    running_snapshot_wire,
)
from agent_alfred.gateway.web.progress import AttemptTerminal, RunProgress
from agent_alfred.gateway.web.state import snapshot_from_payload, snapshot_payload
from agent_alfred.model import (
    ATTEMPT_ABORTED,
    ATTEMPT_COMMITTED,
    ATTEMPT_OUTCOMES,
    AttemptOutcome,
    AttemptRecord,
    parse_attempt_outcome,
)


class LiteralImpostor:
    def __eq__(self, other: object) -> bool:
        return other == "committed"


def test_attempt_outcome_is_one_model_owned_closed_set() -> None:
    assert ATTEMPT_OUTCOMES == ("committed", "aborted")
    assert ATTEMPT_COMMITTED == "committed"
    assert ATTEMPT_ABORTED == "aborted"
    assert get_type_hints(AttemptRecord)["outcome"] is AttemptOutcome
    assert get_type_hints(AttemptTerminal)["outcome"] is AttemptOutcome
    assert (
        get_type_hints(RunProgress.note_attempt_terminal)["outcome"] is AttemptOutcome
    )


@pytest.mark.parametrize("outcome", ATTEMPT_OUTCOMES)
def test_every_attempt_outcome_round_trips_through_the_wire(
    outcome: AttemptOutcome,
) -> None:
    wire = running_snapshot_wire()
    attempt = only_attempt_in_snapshot_wire(wire)
    attempt["outcome"] = outcome

    assert snapshot_payload(snapshot_from_payload(wire)) == wire


@pytest.mark.parametrize(
    "outcome",
    ["invented", True, None, LiteralImpostor()],
    ids=["unknown", "bool", "null", "equality-impostor"],
)
def test_attempt_outcome_parser_rejects_values_outside_the_exact_closed_set(
    outcome: object,
) -> None:
    with pytest.raises(ValueError, match="attempt outcome"):
        parse_attempt_outcome(outcome)


@pytest.mark.parametrize(
    "outcome", ["invented", True, None], ids=["unknown", "bool", "null"]
)
def test_snapshot_wire_rejects_invalid_attempt_outcome(outcome: object) -> None:
    wire = running_snapshot_wire()
    only_attempt_in_snapshot_wire(wire)["outcome"] = outcome

    with pytest.raises(ValueError, match="attempt outcome"):
        snapshot_from_payload(wire)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("attempt_id", None, id="attempt-id-null"),
        pytest.param("attempt_id", True, id="attempt-id-bool"),
        pytest.param("attempt_id", 1, id="attempt-id-int"),
        pytest.param("attempt_id", "", id="attempt-id-empty"),
        pytest.param("stop_reason", True, id="stop-reason-bool"),
        pytest.param("stop_reason", 1, id="stop-reason-int"),
        pytest.param("error_code", True, id="error-code-bool"),
        pytest.param("error_code", 1, id="error-code-int"),
    ],
)
def test_snapshot_wire_rejects_invalid_attempt_field_shapes(
    field: str, value: object
) -> None:
    wire = running_snapshot_wire()
    only_attempt_in_snapshot_wire(wire)[field] = value

    with pytest.raises(ValueError, match=field):
        snapshot_from_payload(wire)


@pytest.mark.parametrize(
    "field",
    ["attempt_id", "outcome", "stop_reason", "error_code", "duration_ms"],
)
def test_snapshot_wire_requires_every_attempt_field(field: str) -> None:
    wire = running_snapshot_wire()
    del only_attempt_in_snapshot_wire(wire)[field]

    with pytest.raises(ValueError, match="attempt"):
        snapshot_from_payload(wire)


def test_snapshot_wire_rejects_extra_attempt_fields() -> None:
    wire = running_snapshot_wire()
    only_attempt_in_snapshot_wire(wire)["invented"] = "value"

    with pytest.raises(ValueError, match="attempt"):
        snapshot_from_payload(wire)


def test_valid_aborted_attempt_shape_round_trips_exactly() -> None:
    wire = running_snapshot_wire()
    attempt = only_attempt_in_snapshot_wire(wire)
    attempt.update(
        outcome="aborted",
        stop_reason=None,
        error_code="request_timeout",
        duration_ms=0,
    )

    assert snapshot_payload(snapshot_from_payload(wire)) == wire


def test_running_snapshot_wire_returns_independent_payloads() -> None:
    first_wire = running_snapshot_wire()
    second_wire = running_snapshot_wire()

    only_attempt_in_snapshot_wire(first_wire)["outcome"] = "aborted"

    assert only_attempt_in_snapshot_wire(second_wire)["outcome"] == "committed"
