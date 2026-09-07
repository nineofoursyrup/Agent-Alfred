"""Models page DTO: orthogonal dimensions and assignability (#34)."""

from __future__ import annotations

from pathlib import Path

from agent_alfred.candidates import project_models_page
from agent_alfred.catalog import CatalogState
from agent_alfred.endpoints import ModelEndpoint, ModelRoute
from agent_alfred.model import ModelRef
from agent_alfred.runtime.model_settings import (
    Assignments,
    ModelSettingsSnapshot,
    PinRecord,
)
from agent_alfred.settings import DEFAULT_ENDPOINT_ID, DEFAULT_MODEL_ID


def _snapshot(pins, primary) -> ModelSettingsSnapshot:
    return ModelSettingsSnapshot(
        schema_version=1,
        revision=1,
        pins=pins,
        assignments=Assignments(primary=primary, retrieval_gate=None),
    )


def test_connected_unsupported_keeps_dimensions_apart() -> None:
    endpoint = ModelEndpoint(
        DEFAULT_ENDPOINT_ID,
        "https://opencode.ai/zen/go/v1",
        "OPENCODE_API_KEY",
        models={"grok-4.6": ModelRoute("responses", "/responses")},
    )
    snapshot = _snapshot(
        (PinRecord(DEFAULT_ENDPOINT_ID, "grok-4.6"),),
        ModelRef(DEFAULT_ENDPOINT_ID, "grok-4.6"),
    )
    page = project_models_page(
        snapshot=snapshot,
        endpoints=(endpoint,),
        catalogs={
            DEFAULT_ENDPOINT_ID: CatalogState("fresh", models=()),
        },
    )
    model = page["endpoints"][0]["models"][0]
    assert model["support"] == "unsupported"
    assert model["reason"] == "unsupported_wire_style: responses"
    assert model["assignable"] is False
    assert model["disable_dimension"] == "support"
    assert model["disable_code"]
    assert "支持" not in model["support_label"] or "不" in model["support_label"]


def test_unconfigured_supported_stays_assignable() -> None:
    endpoint = ModelEndpoint(
        DEFAULT_ENDPOINT_ID,
        "https://opencode.ai/zen/go/v1",
        "OPENCODE_API_KEY",
        models={DEFAULT_MODEL_ID: ModelRoute("openai", "/chat/completions")},
    )
    snapshot = _snapshot(
        (PinRecord(DEFAULT_ENDPOINT_ID, DEFAULT_MODEL_ID),),
        ModelRef(DEFAULT_ENDPOINT_ID, DEFAULT_MODEL_ID),
    )
    page = project_models_page(
        snapshot=snapshot, endpoints=(endpoint,), catalogs={}
    )
    model = next(
        item
        for item in page["endpoints"][0]["models"]
        if item["model_id"] == DEFAULT_MODEL_ID
    )
    assert model["support"] == "supported"
    assert model["assignable"] is True
    assert model["disable_code"] is None
    assert model["probe"] is True
    assert model["probe_enabled"] is False


def test_probe_enablement_follows_key_not_connection_state() -> None:
    endpoint = ModelEndpoint(
        DEFAULT_ENDPOINT_ID,
        "https://opencode.ai/zen/go/v1",
        "OPENCODE_API_KEY",
        models={DEFAULT_MODEL_ID: ModelRoute("openai", "/chat/completions")},
    )
    snapshot = _snapshot(
        (PinRecord(DEFAULT_ENDPOINT_ID, DEFAULT_MODEL_ID),),
        ModelRef(DEFAULT_ENDPOINT_ID, DEFAULT_MODEL_ID),
    )
    page = project_models_page(
        snapshot=snapshot,
        endpoints=(endpoint,),
        catalogs={},
        observations={
            DEFAULT_ENDPOINT_ID: {
                "state": "error",
                "checked_at": "2026-08-28T12:00:00Z",
                "checked_via": "real_run",
                "reason": "http_500",
            }
        },
        keys={DEFAULT_ENDPOINT_ID: "sk-present-key"},
    )
    model = next(
        item
        for item in page["endpoints"][0]["models"]
        if item["model_id"] == DEFAULT_MODEL_ID
    )
    assert model["assignable"] is True
    assert model["probe"] is True
    assert model["probe_enabled"] is True
    assert page["endpoints"][0]["observation"]["state"] == "error"


def test_user_declared_unknown_label_does_not_say_supported() -> None:
    snapshot = _snapshot(
        (PinRecord("openai", "mystery", wire_style_override="openai"),),
        ModelRef("openai", "mystery"),
    )
    page = project_models_page(
        snapshot=snapshot,
        endpoints=(
            ModelEndpoint("openai", "https://api.openai.com/v1", "OPENAI_API_KEY"),
        ),
        catalogs={},
    )
    model = page["endpoints"][0]["models"][0]
    assert model["support"] == "unknown"
    assert model["wire_style_source"] == "user_declared"
    assert model["assignable"] is True
    assert "支持" not in model["support_label"]


def test_group_header_carries_connection_observation_separately() -> None:
    endpoint = ModelEndpoint(
        DEFAULT_ENDPOINT_ID,
        "https://opencode.ai/zen/go/v1",
        "OPENCODE_API_KEY",
        models={"grok-4.6": ModelRoute("responses", "/responses")},
    )
    snapshot = _snapshot(
        (PinRecord(DEFAULT_ENDPOINT_ID, "grok-4.6"),),
        ModelRef(DEFAULT_ENDPOINT_ID, "grok-4.6"),
    )
    page = project_models_page(
        snapshot=snapshot,
        endpoints=(endpoint,),
        catalogs={},
        observations={
            DEFAULT_ENDPOINT_ID: {
                "state": "connected",
                "checked_at": "2026-08-28T12:00:00Z",
                "checked_via": "real_run",
                "reason": None,
            }
        },
    )
    group = page["endpoints"][0]
    assert group["observation"]["state"] == "connected"
    assert group["observation"]["checked_via"] == "real_run"
    assert group["observation"]["checked_at"] == "2026-08-28T12:00:00Z"
    model = group["models"][0]
    assert model["support"] == "unsupported"
    assert model["disable_dimension"] == "support"


def test_stale_catalog_keeps_success_error_and_retry_together() -> None:
    endpoint = ModelEndpoint(
        DEFAULT_ENDPOINT_ID,
        "https://opencode.ai/zen/go/v1",
        "OPENCODE_API_KEY",
        models={DEFAULT_MODEL_ID: ModelRoute("openai", "/chat/completions")},
    )
    snapshot = _snapshot(
        (PinRecord(DEFAULT_ENDPOINT_ID, DEFAULT_MODEL_ID),),
        ModelRef(DEFAULT_ENDPOINT_ID, DEFAULT_MODEL_ID),
    )
    page = project_models_page(
        snapshot=snapshot,
        endpoints=(endpoint,),
        catalogs={
            DEFAULT_ENDPOINT_ID: CatalogState(
                "stale",
                last_success_at="2026-08-28T11:00:00Z",
                last_error="http_500",
                retry_at="2026-08-28T12:01:00Z",
            )
        },
    )
    catalog = page["endpoints"][0]["catalog"]
    assert catalog["health"] == "stale"
    assert catalog["last_success_at"] == "2026-08-28T11:00:00Z"
    assert catalog["last_error"] == "http_500"
    assert catalog["retry_at"] == "2026-08-28T12:01:00Z"


def test_never_successful_catalog_omits_success_time() -> None:
    endpoint = ModelEndpoint(
        DEFAULT_ENDPOINT_ID,
        "https://opencode.ai/zen/go/v1",
        "OPENCODE_API_KEY",
        models={DEFAULT_MODEL_ID: ModelRoute("openai", "/chat/completions")},
    )
    snapshot = _snapshot(
        (PinRecord(DEFAULT_ENDPOINT_ID, DEFAULT_MODEL_ID),),
        ModelRef(DEFAULT_ENDPOINT_ID, DEFAULT_MODEL_ID),
    )
    page = project_models_page(
        snapshot=snapshot,
        endpoints=(endpoint,),
        catalogs={
            DEFAULT_ENDPOINT_ID: CatalogState(
                "unavailable",
                last_error="http_500",
                retry_at="2026-08-28T12:01:00Z",
            )
        },
    )
    catalog = page["endpoints"][0]["catalog"]
    assert catalog["health"] == "unavailable"
    assert catalog["last_success_at"] is None
    assert catalog["last_error"] == "http_500"
    assert catalog["retry_at"] == "2026-08-28T12:01:00Z"


def test_models_group_header_script_renders_last_success_with_failure() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "ops" / "static" / "pages.js"
    ).read_text(encoding="utf-8")
    models = source.split("export function modelsPage", 1)[1]
    header = models.split("for (const model of group.models)", 1)[0]
    assert "group.catalog.last_success_at" in header
    assert "group.catalog.last_error" in header
    assert "group.catalog.retry_at" in header
