"""Settings mutations over a ModelSettingsSnapshot. Assignability is ADR-0021."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from agent_alfred.endpoints import resolve_model
from agent_alfred.model import ModelRef
from agent_alfred.pricing import BILLING, UserPriceOverride
from agent_alfred.runtime.model_settings import (
    Assignments,
    ModelSettingsError,
    ModelSettingsSnapshot,
    PinRecord,
)

IMPLEMENTED_STYLES = frozenset({"anthropic", "openai"})


def is_assignable(pin: PinRecord | None, support) -> bool:
    if pin is None:
        return False
    if support.support == "supported":
        return True
    return (
        support.support == "unknown"
        and pin.wire_style_override in IMPLEMENTED_STYLES
    )


def support_label(support) -> str:
    if support.wire_style_source == "user_declared" and support.support == "unknown":
        return "未验证，按你选择的形状尝试"
    if support.support == "supported":
        return "支持"
    if support.support == "unsupported":
        return support.reason or "不支持"
    return "未知"


def pin(
    snapshot: ModelSettingsSnapshot,
    *,
    endpoint_id: str,
    model_id: str,
    wire_style_override: str | None = None,
    display_name: str | None = None,
    price_override: UserPriceOverride | None = None,
) -> ModelSettingsSnapshot:
    existing = snapshot.pin(endpoint_id, model_id)
    record = PinRecord(
        endpoint_id,
        model_id,
        wire_style_override=(
            existing.wire_style_override if existing is not None else None
        )
        if wire_style_override is None
        else wire_style_override,
        display_name=(
            existing.display_name if existing is not None else None
        )
        if display_name is None
        else display_name,
        price_override=(
            existing.price_override if existing is not None else None
        )
        if price_override is None
        else price_override,
    )
    pins = tuple(
        record if item.key == record.key else item for item in snapshot.pins
    )
    if snapshot.pin(endpoint_id, model_id) is None:
        pins = snapshot.pins + (record,)
    return replace(snapshot, pins=pins)


def unpin(
    snapshot: ModelSettingsSnapshot, *, endpoint_id: str, model_id: str
) -> ModelSettingsSnapshot:
    return replace(
        snapshot,
        pins=tuple(
            item
            for item in snapshot.pins
            if item.key != (endpoint_id, model_id)
        ),
    )


def set_style(
    snapshot: ModelSettingsSnapshot,
    *,
    endpoint_id: str,
    model_id: str,
    wire_style: str,
) -> ModelSettingsSnapshot:
    if snapshot.pin(endpoint_id, model_id) is None:
        raise ModelSettingsError("not_pinned")
    return pin(
        snapshot,
        endpoint_id=endpoint_id,
        model_id=model_id,
        wire_style_override=wire_style,
    )


def set_display_name(
    snapshot: ModelSettingsSnapshot,
    *,
    endpoint_id: str,
    model_id: str,
    display_name: str | None,
) -> ModelSettingsSnapshot:
    if snapshot.pin(endpoint_id, model_id) is None:
        raise ModelSettingsError("not_pinned")
    return pin(
        snapshot,
        endpoint_id=endpoint_id,
        model_id=model_id,
        display_name=display_name,
    )


def set_price_override(
    snapshot: ModelSettingsSnapshot,
    *,
    endpoint_id: str,
    model_id: str,
    dimension: str,
    value: Decimal | None,
) -> ModelSettingsSnapshot:
    if dimension not in {name for name, _token in BILLING}:
        raise ModelSettingsError("settings_invalid")
    current = snapshot.pin(endpoint_id, model_id)
    if current is None:
        raise ModelSettingsError("not_pinned")
    fields = {
        name: (
            current.price_override.explicit(name)
            if current.price_override is not None
            else None
        )
        for name, _token in BILLING
    }
    fields[dimension] = value
    override = (
        None
        if all(item is None for item in fields.values())
        else UserPriceOverride(**fields)
    )
    return pin(
        snapshot,
        endpoint_id=endpoint_id,
        model_id=model_id,
        price_override=override,
    )


def assign(
    snapshot: ModelSettingsSnapshot,
    *,
    slot: str,
    endpoint_id: str,
    model_id: str,
) -> ModelSettingsSnapshot:
    pin_record = snapshot.pin(endpoint_id, model_id)
    support = resolve_model(
        endpoint_id,
        model_id,
        wire_style_override=(
            None if pin_record is None else pin_record.wire_style_override
        ),
    )
    if not is_assignable(pin_record, support):
        raise ModelSettingsError("not_assignable")
    ref = ModelRef(endpoint_id, model_id)
    if slot == "primary":
        assignments = Assignments(
            primary=ref, retrieval_gate=snapshot.assignments.retrieval_gate
        )
    elif slot == "retrieval_gate":
        assignments = Assignments(
            primary=snapshot.assignments.primary, retrieval_gate=ref
        )
    else:
        raise ModelSettingsError("settings_invalid")
    return replace(snapshot, assignments=assignments)
