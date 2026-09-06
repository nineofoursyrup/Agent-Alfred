"""The closed Session-validity contract shared by Host and SSE transport."""

from __future__ import annotations

from collections.abc import Callable
from typing import get_type_hints

import pytest

from agent_alfred.gateway.web.broker import SSEBroker, StreamAdmissionProof
from agent_alfred.runtime.host import RuntimeHost
from agent_alfred.runtime.recording import RecordingUnavailable
from agent_alfred.runtime.snapshot import RuntimeSnapshot
from agent_alfred.session_validity import SessionValidity, parse_session_validity


class _MasqueradingValidity:
    def __init__(self, target: SessionValidity) -> None:
        self._target = target

    def __eq__(self, other: object) -> bool:
        return other == self._target


@pytest.mark.parametrize("value", ["valid", "invalid", "unavailable"])
def test_session_validity_parser_accepts_the_closed_set(
    value: object,
) -> None:
    assert parse_session_validity(value) == value


@pytest.mark.parametrize("value", [True, False, "unknown", None, object()])
def test_session_validity_parser_rejects_values_outside_the_closed_set(
    value: object,
) -> None:
    with pytest.raises(ValueError, match="invalid Session validity"):
        parse_session_validity(value)


@pytest.mark.parametrize("target", ["valid", "invalid", "unavailable"])
def test_session_validity_parser_rejects_non_string_literal_masquerades(
    target: SessionValidity,
) -> None:
    with pytest.raises(ValueError, match="invalid Session validity"):
        parse_session_validity(_MasqueradingValidity(target))


def test_host_and_broker_callbacks_share_the_session_validity_contract() -> None:
    callback = Callable[[str | None], SessionValidity]
    assert (
        get_type_hints(RuntimeHost.transport_session_validity)["return"]
        is SessionValidity
    )
    assert get_type_hints(SSEBroker.__init__)["session_is_valid"] == callback | None
    assert get_type_hints(SSEBroker.bind_session_check)["check"] == callback
    assert get_type_hints(StreamAdmissionProof)["session_valid"] is bool


@pytest.mark.parametrize("value", [True, False, "unknown", None, object()])
def test_broker_preflight_rejects_an_invalid_callback_result(
    value: object,
) -> None:
    broker = SSEBroker(
        process_instance_id="inst-test",
        snapshot=RuntimeSnapshot("inst-test", 0, "idle", None, None),
        session_is_valid=lambda _session_id: value,  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="invalid Session validity"):
        broker.preflight_session("s1")


@pytest.mark.parametrize("target", ["valid", "invalid", "unavailable"])
def test_broker_preflight_rejects_non_string_literal_masquerades(
    target: SessionValidity,
) -> None:
    broker = SSEBroker(
        process_instance_id="inst-test",
        snapshot=RuntimeSnapshot("inst-test", 0, "idle", None, None),
        session_is_valid=lambda _session_id: _MasqueradingValidity(target),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="invalid Session validity"):
        broker.preflight_session("s1")


def test_broker_preflight_preserves_the_three_public_results() -> None:
    def preflight(value: SessionValidity):
        broker = SSEBroker(
            process_instance_id="inst-test",
            snapshot=RuntimeSnapshot("inst-test", 0, "idle", None, None),
            session_is_valid=lambda _session_id: value,
        )
        return broker.preflight_session("s1")

    assert preflight("valid").session_valid is True
    assert preflight("invalid").session_valid is False
    with pytest.raises(RecordingUnavailable):
        preflight("unavailable")
