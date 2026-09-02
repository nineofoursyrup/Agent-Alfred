"""Run-state wire documents prove their closed JSON shape before narrowing."""

from __future__ import annotations

import pytest

from agent_alfred.evals.deterministic._state_wire_test_helpers import (
    shaped_snapshot_wire,
)
from agent_alfred.gateway.web.state import snapshot_from_payload, snapshot_payload


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        pytest.param("top", "process_instance_id", 7, id="process-id-int"),
        pytest.param("active", "run_id", True, id="run-id-bool"),
        pytest.param("active", "purpose", [], id="purpose-list"),
        pytest.param("active", "gateway", {}, id="gateway-object"),
    ],
)
def test_snapshot_wire_rejects_non_string_required_fields(
    target: str, field: str, value: object
) -> None:
    wire = shaped_snapshot_wire()
    if target == "active":
        active = wire["active_run"]
        assert isinstance(active, dict)
        active[field] = value
    else:
        wire[field] = value

    with pytest.raises(ValueError, match=field):
        snapshot_from_payload(wire)


@pytest.mark.parametrize(
    ("target", "value"),
    [
        pytest.param("top", [], id="top-list"),
        pytest.param("active_run", [], id="active-list"),
        pytest.param("step", [], id="step-list"),
        pytest.param("unrecorded_terminal_projection", [], id="projection-list"),
    ],
)
def test_snapshot_wire_requires_exact_objects(target: str, value: object) -> None:
    wire = shaped_snapshot_wire(with_step=True, with_projection=True)

    if target == "top":
        candidate = value
    else:
        wire[target] = value
        candidate = wire

    with pytest.raises(ValueError, match=target):
        snapshot_from_payload(candidate)


@pytest.mark.parametrize(
    ("target", "field"),
    [
        pytest.param("top", "session_valid", id="top-missing"),
        pytest.param("active_run", "session_id", id="active-missing-nullable"),
        pytest.param("step", "attempts", id="step-missing"),
        pytest.param(
            "unrecorded_terminal_projection",
            "error",
            id="projection-missing-nullable",
        ),
    ],
)
def test_snapshot_wire_rejects_missing_fields(target: str, field: str) -> None:
    wire = shaped_snapshot_wire(with_step=True, with_projection=True)
    document = wire if target == "top" else wire[target]
    assert isinstance(document, dict)
    del document[field]

    with pytest.raises(ValueError, match=target):
        snapshot_from_payload(wire)


@pytest.mark.parametrize(
    "target",
    ["top", "active_run", "step", "unrecorded_terminal_projection"],
)
def test_snapshot_wire_rejects_extra_fields(target: str) -> None:
    wire = shaped_snapshot_wire(with_step=True, with_projection=True)
    document = wire if target == "top" else wire[target]
    assert isinstance(document, dict)
    document["invented"] = "value"

    with pytest.raises(ValueError, match=target):
        snapshot_from_payload(wire)


@pytest.mark.parametrize("value", [None, {}, (), "attempts"])
def test_snapshot_wire_requires_attempts_to_be_an_exact_list(value: object) -> None:
    wire = shaped_snapshot_wire(with_step=True)
    step = wire["step"]
    assert isinstance(step, dict)
    step["attempts"] = value

    with pytest.raises(ValueError, match="attempts"):
        snapshot_from_payload(wire)


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        pytest.param("active_run", "session_id", 1, id="active-session-int"),
        pytest.param("active_run", "prompt_preview", [], id="active-preview-list"),
        pytest.param("active_run", "started_at", True, id="active-started-bool"),
        pytest.param(
            "unrecorded_terminal_projection",
            "run_id",
            True,
            id="projection-run-bool",
        ),
        pytest.param(
            "unrecorded_terminal_projection",
            "purpose",
            {},
            id="projection-purpose-object",
        ),
        pytest.param(
            "unrecorded_terminal_projection",
            "reply_preview",
            1,
            id="projection-reply-int",
        ),
        pytest.param(
            "unrecorded_terminal_projection",
            "error",
            [],
            id="projection-error-list",
        ),
        pytest.param(
            "unrecorded_terminal_projection",
            "session_id",
            False,
            id="projection-session-bool",
        ),
        pytest.param(
            "unrecorded_terminal_projection",
            "prompt_preview",
            {},
            id="projection-preview-object",
        ),
    ],
)
def test_snapshot_wire_rejects_invalid_string_and_nullable_string_fields(
    target: str, field: str, value: object
) -> None:
    wire = shaped_snapshot_wire(with_projection=True)
    document = wire[target]
    assert isinstance(document, dict)
    document[field] = value

    with pytest.raises(ValueError, match=field):
        snapshot_from_payload(wire)


def test_snapshot_wire_valid_closed_document_round_trips() -> None:
    wire = shaped_snapshot_wire(with_step=True, with_projection=True)

    assert snapshot_payload(snapshot_from_payload(wire)) == wire


def test_snapshot_wire_nullable_metadata_strings_round_trip_as_null() -> None:
    wire = shaped_snapshot_wire(with_step=True, with_projection=True)
    active = wire["active_run"]
    projection = wire["unrecorded_terminal_projection"]
    step = wire["step"]
    assert isinstance(active, dict)
    assert isinstance(projection, dict)
    assert isinstance(step, dict)
    attempts = step["attempts"]
    assert isinstance(attempts, list)
    attempt = attempts[0]
    assert isinstance(attempt, dict)
    for field in ("session_id", "prompt_preview"):
        active[field] = None
    for field in ("reply_preview", "error", "session_id", "prompt_preview"):
        projection[field] = None
    for field in ("stop_reason", "error_code"):
        attempt[field] = None

    assert snapshot_payload(snapshot_from_payload(wire)) == wire
