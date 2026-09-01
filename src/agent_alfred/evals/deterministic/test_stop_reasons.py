"""Stop reason has one model-owned closed set across every projection."""

from __future__ import annotations

import json
from typing import get_type_hints

import pytest

from agent_alfred.evals.deterministic._state_wire_test_helpers import (
    only_attempt_in_snapshot_wire,
    running_snapshot_wire,
)
from agent_alfred.events import AttemptCommitted, StepFinished, event_json_default
from agent_alfred.gateway.web.progress import AttemptTerminal, RunProgress
from agent_alfred.gateway.web.state import snapshot_from_payload, snapshot_payload
from agent_alfred.messages import TextBlock
from agent_alfred.model import (
    STOP_REASONS,
    ModelRef,
    ModelResponse,
    StopReason,
    parse_stop_reason,
)


def test_stop_reason_is_one_model_owned_closed_set() -> None:
    assert STOP_REASONS == (
        "end_turn",
        "tool_use",
        "max_tokens",
        "stop_sequence",
        "refusal",
        "content_filter",
        "paused",
        "context_exceeded",
        "error",
        "unknown",
    )
    assert get_type_hints(ModelResponse)["stop_reason"] is StopReason


@pytest.mark.parametrize("stop_reason", STOP_REASONS)
def test_every_stop_reason_parses_and_constructs_a_model_response(
    stop_reason: StopReason,
) -> None:
    assert parse_stop_reason(stop_reason) == stop_reason
    assert ModelResponse(
        blocks=(TextBlock("done"),),
        stop_reason=stop_reason,
        model=ModelRef(endpoint_id="endpoint", model_id="model"),
    ).stop_reason == stop_reason


@pytest.mark.parametrize(
    "stop_reason", ["invented_stop", True], ids=["unknown", "bool"]
)
def test_stop_reason_parser_and_model_response_reject_invalid_values(
    stop_reason: object,
) -> None:
    with pytest.raises(ValueError, match=r"stop[_ ]reason"):
        parse_stop_reason(stop_reason)
    with pytest.raises(ValueError, match=r"stop[_ ]reason"):
        ModelResponse(
            blocks=(),
            stop_reason=stop_reason,  # type: ignore[arg-type]
            model=ModelRef(endpoint_id="endpoint", model_id="model"),
        )


def test_event_stop_reasons_use_the_model_owned_type() -> None:
    assert get_type_hints(StepFinished)["stop_reason"] is StopReason
    assert get_type_hints(AttemptCommitted)["stop_reason"] is StopReason


@pytest.mark.parametrize("stop_reason", STOP_REASONS)
@pytest.mark.parametrize("event_type", [StepFinished, AttemptCommitted])
def test_every_stop_reason_round_trips_through_event_serialization(
    event_type: type[StepFinished] | type[AttemptCommitted],
    stop_reason: StopReason,
) -> None:
    event = event_type(stop_reason=stop_reason)

    assert json.loads(json.dumps(event, default=event_json_default))["stop_reason"] == (
        stop_reason
    )


@pytest.mark.parametrize(
    "stop_reason", ["invented_stop", True], ids=["unknown", "bool"]
)
@pytest.mark.parametrize("event_type", [StepFinished, AttemptCommitted])
def test_events_reject_invalid_stop_reasons_at_construction(
    event_type: type[StepFinished] | type[AttemptCommitted],
    stop_reason: object,
) -> None:
    with pytest.raises(ValueError, match=r"stop[_ ]reason"):
        event_type(stop_reason=stop_reason)  # type: ignore[arg-type]


def test_progress_stop_reasons_use_the_model_owned_optional_type() -> None:
    assert get_type_hints(AttemptTerminal)["stop_reason"] == StopReason | None
    assert (
        get_type_hints(RunProgress.note_attempt_terminal)["stop_reason"]
        == StopReason | None
    )


@pytest.mark.parametrize("stop_reason", [*STOP_REASONS, None])
def test_every_optional_stop_reason_survives_progress_recording(
    stop_reason: StopReason | None,
) -> None:
    progress = RunProgress()
    progress.note_run_started("run-1")
    progress.note_attempt_terminal(
        "run-1",
        attempt_id="attempt-1",
        outcome="committed",
        stop_reason=stop_reason,
        error_code=None,
        duration_ms=1,
    )

    projection = progress.projection(0)
    assert projection is not None
    assert projection.attempts[0].stop_reason == stop_reason


@pytest.mark.parametrize(
    "stop_reason", ["invented_stop", True], ids=["unknown", "bool"]
)
def test_progress_construction_and_recording_reject_invalid_stop_reasons(
    stop_reason: object,
) -> None:
    with pytest.raises(ValueError, match=r"stop[_ ]reason"):
        AttemptTerminal(
            attempt_id="attempt-1",
            outcome="committed",
            stop_reason=stop_reason,  # type: ignore[arg-type]
            error_code=None,
            duration_ms=1,
        )

    progress = RunProgress()
    progress.note_run_started("run-1")
    with pytest.raises(ValueError, match=r"stop[_ ]reason"):
        progress.note_attempt_terminal(
            "run-1",
            attempt_id="attempt-1",
            outcome="committed",
            stop_reason=stop_reason,  # type: ignore[arg-type]
            error_code=None,
            duration_ms=1,
        )


@pytest.mark.parametrize("stop_reason", [*STOP_REASONS, None])
def test_every_optional_stop_reason_round_trips_through_snapshot_wire(
    stop_reason: StopReason | None,
) -> None:
    wire = running_snapshot_wire()
    only_attempt_in_snapshot_wire(wire)["stop_reason"] = stop_reason

    assert snapshot_payload(snapshot_from_payload(wire)) == wire


@pytest.mark.parametrize(
    "stop_reason", ["invented_stop", True], ids=["unknown", "bool"]
)
def test_snapshot_wire_rejects_invalid_stop_reasons(stop_reason: object) -> None:
    wire = running_snapshot_wire()
    only_attempt_in_snapshot_wire(wire)["stop_reason"] = stop_reason

    with pytest.raises(ValueError, match=r"stop[_ ]reason"):
        snapshot_from_payload(wire)
