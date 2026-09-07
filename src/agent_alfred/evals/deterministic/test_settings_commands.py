"""Models commands, assignability, and override evidence (#34)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from agent_alfred.endpoints import resolve_model
from agent_alfred.runtime.model_settings import ModelSettingsStore, PinRecord
from agent_alfred.settings import DEFAULT_ENDPOINT_ID, DEFAULT_MODEL_ID
from agent_alfred.settings_commands import (
    assign,
    is_assignable,
    pin,
    set_price_override,
    support_label,
    unpin,
)


def test_assignable_formula_ignores_connection_and_keeps_unknown() -> None:
    pinned = PinRecord(
        DEFAULT_ENDPOINT_ID, DEFAULT_MODEL_ID, wire_style_override="openai"
    )
    unknown = resolve_model(
        "openai", "mystery", wire_style_override="openai"
    )
    assert unknown.support == "unknown"
    assert unknown.wire_style_source == "user_declared"
    assert is_assignable(pinned, unknown) is True
    assert "支持" not in support_label(unknown)
    unsupported = resolve_model(
        DEFAULT_ENDPOINT_ID, "grok-4.6", wire_style_override=None
    )
    assert unsupported.support == "unsupported"
    assert is_assignable(
        PinRecord(DEFAULT_ENDPOINT_ID, "grok-4.6"), unsupported
    ) is False
    assert is_assignable(None, unknown) is False


def test_pin_style_price_and_assign_round_trip(tmp_path: Path) -> None:
    store = ModelSettingsStore(tmp_path / "model_settings.json")
    snapshot = store.load()
    snapshot = pin(
        snapshot,
        endpoint_id="openai",
        model_id="gpt-test",
        wire_style_override="openai",
    )
    snapshot = set_price_override(
        snapshot,
        endpoint_id="openai",
        model_id="gpt-test",
        dimension="output",
        value=Decimal("0"),
    )
    snapshot = assign(
        snapshot, slot="primary", endpoint_id="openai", model_id="gpt-test"
    )
    written = store.mutate(0, lambda _: snapshot)
    loaded = ModelSettingsStore(tmp_path / "model_settings.json").load()
    gpt = loaded.pin("openai", "gpt-test")
    assert gpt is not None
    assert gpt.wire_style_override == "openai"
    assert gpt.price_override is not None
    assert gpt.price_override.output == Decimal("0")
    assert gpt.price_override.uncached_input is None
    assert loaded.assignments.primary.model_id == "gpt-test"
    assert written.revision == 1


def test_unpin_of_assigned_primary_is_rejected(tmp_path: Path) -> None:
    from agent_alfred.runtime.model_settings import ModelSettingsError

    store = ModelSettingsStore(tmp_path / "model_settings.json")
    store.load()
    with pytest.raises(ModelSettingsError) as caught:
        store.mutate(
            0,
            lambda snap: unpin(
                snap, endpoint_id=DEFAULT_ENDPOINT_ID, model_id=DEFAULT_MODEL_ID
            ),
        )
    assert caught.value.code == "pin_assigned"


def test_explicit_zero_override_reaches_run_evidence(tmp_path: Path) -> None:
    from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
        free_loopback_port,
    )
    from agent_alfred.messages import TextBlock
    from agent_alfred.model import (
        AttemptRecord,
        ModelResponse,
        ModelResult,
        ScriptedModel,
        ScriptedModelFactory,
        Usage,
    )
    from agent_alfred.runtime.host import SubmitRequest
    from agent_alfred.wiring import build_dashboard

    class PricedModel(ScriptedModel):
        def respond(self, request, *, events=None, deadline=None):
            del deadline
            from agent_alfred.events import AttemptCommitted, AttemptStarted

            if events is not None:
                events.emit(AttemptStarted(attempt_id="a1", model=request.model))
                events.emit(
                    AttemptCommitted(
                        attempt_id="a1",
                        blocks=(TextBlock("pong"),),
                        usage=Usage(
                            uncached_input_tokens=10,
                            output_tokens=5,
                        ),
                    )
                )
            return ModelResult(
                attempts=(
                    AttemptRecord(
                        attempt_id="a1",
                        streamed=False,
                        outcome="committed",
                        usage=Usage(
                            uncached_input_tokens=10,
                            output_tokens=5,
                        ),
                    ),
                ),
                response=ModelResponse(
                    blocks=(TextBlock("pong"),),
                    stop_reason="end_turn",
                    model=request.model,
                ),
                final_error=None,
            )

    dashboard = build_dashboard(
        state_dir=tmp_path,
        port=free_loopback_port(),
        factory=ScriptedModelFactory(PricedModel([])),
    )
    dashboard.start()
    host = dashboard.host
    try:
        assert host.try_begin_mutation() is None
        host.mutate_model_settings(
            0,
            lambda snap: set_price_override(
                set_price_override(
                    snap,
                    endpoint_id=DEFAULT_ENDPOINT_ID,
                    model_id=DEFAULT_MODEL_ID,
                    dimension="uncached_input",
                    value=Decimal("1"),
                ),
                endpoint_id=DEFAULT_ENDPOINT_ID,
                model_id=DEFAULT_MODEL_ID,
                dimension="output",
                value=Decimal("0"),
            ),
        )
        host.end_mutation()
        submitted = host.submit(
            SubmitRequest(message="hello", session_id=host.create_session())
        )
        host.wait(submitted.run_id)
        evidence = host.read_run_evidence(
            submitted.run_id, trace_root=tmp_path / "traces"
        )
        assert evidence is not None
        components = evidence["attempts"][0]["cost"].get("price_components", [])
        by_dim = {row["dimension"]: row for row in components}
        assert by_dim["output"]["source"] == "user_override"
        assert by_dim["output"]["amount"] == "0"
        assert by_dim["uncached_input"]["source"] == "user_override"
    finally:
        dashboard.close()
