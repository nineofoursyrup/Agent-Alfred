"""Merge Models-page candidates by (endpoint_id, model_id) with a sources set."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agent_alfred.catalog import CatalogState
from agent_alfred.endpoints import ModelEndpoint, resolve_model
from agent_alfred.runtime.model_settings import ModelSettingsSnapshot, PinRecord
from agent_alfred.settings_commands import is_assignable, support_label


def merge_candidates(
    *,
    endpoints: Sequence[ModelEndpoint],
    pins: Sequence[PinRecord],
    catalogs: Mapping[str, CatalogState],
) -> list[dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}

    def slot(endpoint_id: str, model_id: str) -> dict[str, Any]:
        key = (endpoint_id, model_id)
        found = rows.get(key)
        if found is None:
            found = {
                "endpoint_id": endpoint_id,
                "model_id": model_id,
                "sources": set(),
                "display_name": None,
            }
            rows[key] = found
        return found

    for endpoint in endpoints:
        for model_id in endpoint.models:
            item = slot(endpoint.endpoint_id, model_id)
            item["sources"].add("builtin")
    for endpoint_id, catalog in catalogs.items():
        for model in catalog.models:
            item = slot(endpoint_id, model.model_id)
            item["sources"].add("catalog")
            if item["display_name"] is None:
                item["display_name"] = model.display_name
    for pin in pins:
        item = slot(pin.endpoint_id, pin.model_id)
        item["sources"].add("pinned")
        if pin.display_name is not None:
            item["display_name"] = pin.display_name
    return list(rows.values())


def project_models_page(
    *,
    snapshot: ModelSettingsSnapshot,
    endpoints: Sequence[ModelEndpoint],
    catalogs: Mapping[str, CatalogState],
    observations: Mapping[str, Mapping[str, Any]] | None = None,
    keys: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    candidates = merge_candidates(
        endpoints=endpoints, pins=snapshot.pins, catalogs=catalogs
    )
    assigned = {
        "primary": {
            "endpoint_id": snapshot.assignments.primary.endpoint_id,
            "model_id": snapshot.assignments.primary.model_id,
        },
        "retrieval_gate": (
            None
            if snapshot.assignments.retrieval_gate is None
            else {
                "endpoint_id": snapshot.assignments.retrieval_gate.endpoint_id,
                "model_id": snapshot.assignments.retrieval_gate.model_id,
            }
        ),
    }
    groups: dict[str, dict[str, Any]] = {}
    for endpoint in endpoints:
        catalog = catalogs.get(endpoint.endpoint_id)
        recorded = (observations or {}).get(endpoint.endpoint_id)
        groups[endpoint.endpoint_id] = {
            "endpoint_id": endpoint.endpoint_id,
            "base_url": endpoint.base_url,
            "observation": {
                "state": "configured_untested"
                if recorded is None
                else recorded.get("state", "configured_untested"),
                "checked_at": (
                    None if recorded is None else recorded.get("checked_at")
                ),
                "checked_via": (
                    None if recorded is None else recorded.get("checked_via")
                ),
                "reason": None if recorded is None else recorded.get("reason"),
            },
            "catalog": {
                "health": "unfetched" if catalog is None else catalog.health,
                "last_success_at": None if catalog is None else catalog.last_success_at,
                "last_error": None if catalog is None else catalog.last_error,
                "retry_at": None if catalog is None else catalog.retry_at,
            },
            "models": [],
        }
    for row in candidates:
        pin = snapshot.pin(row["endpoint_id"], row["model_id"])
        support = resolve_model(
            row["endpoint_id"],
            row["model_id"],
            wire_style_override=None if pin is None else pin.wire_style_override,
        )
        can_assign = is_assignable(pin, support)
        disable = None
        disable_dimension = None
        if pin is not None and not can_assign:
            disable = support.reason or "model_unsupported"
            disable_dimension = "support"
        model = {
            "endpoint_id": row["endpoint_id"],
            "model_id": row["model_id"],
            "sources": sorted(row["sources"]),
            "display_name": row["display_name"],
            "pinned": pin is not None,
            "assignable": can_assign,
            "support": support.support,
            "support_basis": support.support_basis,
            "wire_style": support.wire_style,
            "wire_style_source": support.wire_style_source,
            "support_label": support_label(support),
            "reason": support.reason,
            "disable_code": disable,
            "disable_dimension": disable_dimension,
            "price_override": None
            if pin is None or pin.price_override is None
            else {
                "uncached_input": (
                    None
                    if pin.price_override.uncached_input is None
                    else str(pin.price_override.uncached_input)
                ),
                "cache_read": (
                    None
                    if pin.price_override.cache_read is None
                    else str(pin.price_override.cache_read)
                ),
                "cache_write": (
                    None
                    if pin.price_override.cache_write is None
                    else str(pin.price_override.cache_write)
                ),
                "output": (
                    None
                    if pin.price_override.output is None
                    else str(pin.price_override.output)
                ),
            },
            "probe": pin is not None
            and (
                (
                    snapshot.assignments.primary.endpoint_id,
                    snapshot.assignments.primary.model_id,
                )
                == (row["endpoint_id"], row["model_id"])
                or (
                    snapshot.assignments.retrieval_gate is not None
                    and (
                        snapshot.assignments.retrieval_gate.endpoint_id,
                        snapshot.assignments.retrieval_gate.model_id,
                    )
                    == (row["endpoint_id"], row["model_id"])
                )
            ),
        }
        raw_key = None if keys is None else keys.get(row["endpoint_id"])
        model["probe_enabled"] = bool(model["probe"] and (raw_key or "").strip())
        group = groups.get(row["endpoint_id"])
        if group is None:
            group = {
                "endpoint_id": row["endpoint_id"],
                "base_url": "",
                "observation": {
                    "state": "configured_untested",
                    "checked_at": None,
                    "checked_via": None,
                    "reason": None,
                },
                "catalog": {
                    "health": "unfetched",
                    "last_success_at": None,
                    "last_error": None,
                    "retry_at": None,
                },
                "models": [],
            }
            groups[row["endpoint_id"]] = group
        group["models"].append(model)
    return {
        "status": snapshot.status,
        "revision": snapshot.revision,
        "assignments": assigned,
        "endpoints": list(groups.values()),
    }
