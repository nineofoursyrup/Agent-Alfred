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
    set_display_name,
    set_price_override,
    support_label,
    unpin,
)


@pytest.mark.parametrize("field", ["display_name", "price_override"])
def test_clearing_persisted_pin_field_survives_reopening(tmp_path, field):
    from agent_alfred.pricing import UserPriceOverride

    path = tmp_path / "model_settings.json"
    store = ModelSettingsStore(path)
    store.load()
    target = dict(endpoint_id=DEFAULT_ENDPOINT_ID, model_id=DEFAULT_MODEL_ID)
    saved = store.mutate(
        0,
        lambda snap: pin(
            snap,
            **target,
            display_name="已保存名称",
            price_override=UserPriceOverride(output=Decimal("0")),
        ),
    )
    # An omitted argument still means keep the saved value.
    saved = store.mutate(saved.revision, lambda snap: pin(snap, **target))
    assert saved.pin(**target).display_name == "已保存名称"
    assert saved.pin(**target).price_override.output == Decimal("0")
    store.mutate(
        saved.revision,
        lambda snap: (
            set_display_name(snap, **target, display_name=None)
            if field == "display_name"
            else set_price_override(snap, **target, dimension="output", value=None)
        ),
    )
    reopened = ModelSettingsStore(path).load().pin(**target)
    assert getattr(reopened, field) is None
    if field == "display_name":
        assert reopened.price_override.output == Decimal("0")
    else:
        assert reopened.display_name == "已保存名称"


def test_assignable_formula_ignores_connection_and_keeps_unknown() -> None:
    pinned = PinRecord(
        DEFAULT_ENDPOINT_ID, DEFAULT_MODEL_ID, wire_style_override="openai"
    )
    unknown = resolve_model("openai", "mystery", wire_style_override="openai")
    assert unknown.support == "unknown"
    assert unknown.wire_style_source == "user_declared"
    assert is_assignable(pinned, unknown) is True
    assert "支持" not in support_label(unknown)
    unsupported = resolve_model(
        DEFAULT_ENDPOINT_ID, "grok-4.6", wire_style_override=None
    )
    assert unsupported.support == "unsupported"
    assert (
        is_assignable(PinRecord(DEFAULT_ENDPOINT_ID, "grok-4.6"), unsupported) is False
    )
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


def test_http_clear_reopens_store_and_reprices_only_new_snapshots(tmp_path):
    import json
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from agent_alfred.connections import CredentialOverlay
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
    from agent_alfred.pricing import PriceQuote
    from agent_alfred.settings import OPENCODE_API_KEY_ENV
    from agent_alfred.wiring import build_dashboard

    class Prices:
        def quote(self, endpoint_id, model_id, dimension):
            return PriceQuote(Decimal("2"), "catalog")

    class Factory(ScriptedModelFactory):
        def catalog_prices(self):
            return Prices()

    class MeteredModel(ScriptedModel):
        def respond(self, request, *, events=None, deadline=None):
            return ModelResult(
                (
                    AttemptRecord(
                        "clear-probe",
                        False,
                        "committed",
                        Usage(
                            uncached_input_tokens=1000,
                            output_tokens=1000,
                        ),
                        model=request.model,
                    ),
                ),
                ModelResponse((TextBlock("offline probe"),), "end_turn", request.model),
                None,
            )

    factory = Factory(MeteredModel([]))
    dashboard = build_dashboard(
        state_dir=tmp_path,
        port=free_loopback_port(),
        factory=factory,
        credentials=CredentialOverlay({OPENCODE_API_KEY_ENV: "fixture-only"}, None),
    )
    dashboard.start()
    target = dict(endpoint_id=DEFAULT_ENDPOINT_ID, model_id=DEFAULT_MODEL_ID)

    def request(path, body=None):
        headers = {"Content-Type": "application/json"}
        if body is not None:
            headers["x-agent-alfred-csrf"] = dashboard.csrf_token
        call = Request(
            f"http://127.0.0.1:{dashboard.port}{path}",
            headers=headers,
            data=None if body is None else json.dumps(body).encode(),
        )
        try:
            response = urlopen(call, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def save(op, **values):
        _, view = request("/api/models")
        status, body = request(
            "/api/settings",
            dict(
                op=op,
                expected_revision=view["revision"],
                **target,
                **values,
            ),
        )
        assert status == 200, body
        return body

    try:
        save("display", display_name="持久旧值")
        save("price", dimension="uncached_input", value="4")
        saved = save("price", dimension="output", value="0")
        status, admission = request(
            "/api/runs",
            dict(
                purpose="inference_probe",
                message="probe",
                **target,
            ),
        )
        assert status == 202
        import time

        deadline = time.monotonic() + 5
        while dashboard.host.snapshot().coordinator_state != "idle":
            assert time.monotonic() < deadline, "probe did not finish recording"
            time.sleep(0.01)
        assert factory.snapshots[-1].model_id == DEFAULT_MODEL_ID
        old = dashboard.host.accounting_snapshot({"range": "all", "timezone": "UTC"})
        detail = dashboard.host.accounting_detail(
            old["snapshot_id"], admission["run_id"]
        )
        assert old["summary"]["estimated_usd"] == "0.004"
        assert detail["run"]["attempts"][0]["attempt_id"] == "clear-probe"
        save("display", display_name=None)
        save("price", dimension="uncached_input", value=None)
        partial = (
            ModelSettingsStore(tmp_path / "model_settings.json").load().pin(**target)
        )
        assert partial.display_name is None
        assert partial.price_override.uncached_input is None
        assert partial.price_override.output == Decimal("0")
        midway = dashboard.host.accounting_snapshot({"range": "all", "timezone": "UTC"})
        assert midway["summary"]["estimated_usd"] == "0.002"
        save("price", dimension="output", value=None)
        reopened = (
            ModelSettingsStore(tmp_path / "model_settings.json").load().pin(**target)
        )
        assert reopened.display_name is None
        assert reopened.price_override is None
        _, reread = request("/api/models")
        row = next(
            model
            for group in reread["endpoints"]
            for model in group["models"]
            if model["endpoint_id"] == DEFAULT_ENDPOINT_ID
            and model["model_id"] == DEFAULT_MODEL_ID
        )
        assert row["display_name"] is None
        assert row["price_override"] is None
        status, conflict = request(
            "/api/settings",
            dict(
                op="display",
                expected_revision=saved["revision"],
                **target,
                display_name="stale",
            ),
        )
        assert status == 409
        assert conflict == {"code": "settings_conflict", "cause": "stale_revision"}
        new = dashboard.host.accounting_snapshot({"range": "all", "timezone": "UTC"})
        assert new["summary"]["estimated_usd"] == "0.004"
        assert new["price_version"] != old["price_version"]
        assert (
            dashboard.host.accounting_detail(old["snapshot_id"], admission["run_id"])
            == detail
        )
    finally:
        assert dashboard.close()
