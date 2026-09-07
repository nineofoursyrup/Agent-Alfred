"""Durable model pins and assignments. Host-exclusive; ADR-0022 writes."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from agent_alfred.model import ModelRef
from agent_alfred.pricing import BILLING, UserPriceOverride
from agent_alfred.settings import DEFAULT_ENDPOINT_ID, DEFAULT_MODEL_ID

CURRENT_SCHEMA_VERSION = 1
SettingsStatus = Literal["ok", "settings_invalid", "settings_schema_newer"]


class ModelSettingsError(Exception):
    """A settings load or mutate that must not write."""

    def __init__(self, code: str, cause: str | None = None):
        super().__init__(code if cause is None else f"{code}:{cause}")
        self.code = code
        self.cause = cause


@dataclass(frozen=True)
class PinRecord:
    endpoint_id: str
    model_id: str
    wire_style_override: str | None = None
    price_override: UserPriceOverride | None = None
    display_name: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.endpoint_id, self.model_id)


@dataclass(frozen=True)
class Assignments:
    primary: ModelRef
    retrieval_gate: ModelRef | None = None


@dataclass(frozen=True)
class ModelSettingsSnapshot:
    schema_version: int
    revision: int
    pins: tuple[PinRecord, ...]
    assignments: Assignments
    status: SettingsStatus = "ok"

    def pin(self, endpoint_id: str, model_id: str) -> PinRecord | None:
        key = (endpoint_id, model_id)
        for record in self.pins:
            if record.key == key:
                return record
        return None


@dataclass(frozen=True)
class _Fingerprint:
    exists: bool
    digest: str | None


def _first_snapshot() -> ModelSettingsSnapshot:
    primary = ModelRef(DEFAULT_ENDPOINT_ID, DEFAULT_MODEL_ID)
    return ModelSettingsSnapshot(
        schema_version=CURRENT_SCHEMA_VERSION,
        revision=0,
        pins=(PinRecord(DEFAULT_ENDPOINT_ID, DEFAULT_MODEL_ID),),
        assignments=Assignments(primary=primary, retrieval_gate=None),
    )


def _pin_keys(pins: tuple[PinRecord, ...]) -> set[tuple[str, str]]:
    return {record.key for record in pins}


def _ref_key(ref: ModelRef) -> tuple[str, str]:
    return (ref.endpoint_id, ref.model_id)


def _validate_snapshot(snapshot: ModelSettingsSnapshot) -> None:
    keys = _pin_keys(snapshot.pins)
    if len(keys) != len(snapshot.pins):
        raise ModelSettingsError("settings_invalid")
    if _ref_key(snapshot.assignments.primary) not in keys:
        raise ModelSettingsError("settings_invalid")
    gate = snapshot.assignments.retrieval_gate
    if gate is not None and _ref_key(gate) not in keys:
        raise ModelSettingsError("settings_invalid")


def _decimal_field(raw: object) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise ModelSettingsError("settings_invalid")
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ModelSettingsError("settings_invalid") from exc
    if value < 0 or not value.is_finite():
        raise ModelSettingsError("settings_invalid")
    return value


def _parse_price_override(raw: object) -> UserPriceOverride | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ModelSettingsError("settings_invalid")
    fields: dict[str, Decimal | None] = {}
    for dimension, _token in BILLING:
        if dimension not in raw:
            continue
        value = raw[dimension]
        if value is None:
            continue
        fields[dimension] = _decimal_field(value)
    if not fields:
        return None
    return UserPriceOverride(**fields)


def _parse_ref(raw: object) -> ModelRef:
    if not isinstance(raw, dict):
        raise ModelSettingsError("settings_invalid")
    endpoint_id = raw.get("endpoint_id")
    model_id = raw.get("model_id")
    if type(endpoint_id) is not str or not endpoint_id:
        raise ModelSettingsError("settings_invalid")
    if type(model_id) is not str or not model_id:
        raise ModelSettingsError("settings_invalid")
    return ModelRef(endpoint_id, model_id)


def _parse_pin(raw: object) -> PinRecord:
    if not isinstance(raw, dict):
        raise ModelSettingsError("settings_invalid")
    endpoint_id = raw.get("endpoint_id")
    model_id = raw.get("model_id")
    if type(endpoint_id) is not str or not endpoint_id:
        raise ModelSettingsError("settings_invalid")
    if type(model_id) is not str or not model_id:
        raise ModelSettingsError("settings_invalid")
    style = raw.get("wire_style_override")
    if style is not None and (type(style) is not str or not style):
        raise ModelSettingsError("settings_invalid")
    name = raw.get("display_name")
    if name is not None and type(name) is not str:
        raise ModelSettingsError("settings_invalid")
    return PinRecord(
        endpoint_id,
        model_id,
        wire_style_override=style,
        price_override=_parse_price_override(raw.get("price_override")),
        display_name=name,
    )


def _parse_payload(payload: dict[str, Any]) -> ModelSettingsSnapshot:
    schema_version = payload.get("schema_version")
    if type(schema_version) is not int:
        raise ModelSettingsError("settings_invalid")
    if schema_version > CURRENT_SCHEMA_VERSION:
        raise ModelSettingsError("settings_schema_newer")
    if schema_version != CURRENT_SCHEMA_VERSION:
        raise ModelSettingsError("settings_invalid")
    revision = payload.get("revision")
    if type(revision) is not int or revision < 1:
        raise ModelSettingsError("settings_invalid")
    raw_pins = payload.get("pins")
    if not isinstance(raw_pins, list) or not raw_pins:
        raise ModelSettingsError("settings_invalid")
    pins = tuple(_parse_pin(item) for item in raw_pins)
    raw_assignments = payload.get("assignments")
    if not isinstance(raw_assignments, dict):
        raise ModelSettingsError("settings_invalid")
    primary = _parse_ref(raw_assignments.get("primary"))
    raw_gate = raw_assignments.get("retrieval_gate")
    gate = None if raw_gate is None else _parse_ref(raw_gate)
    snapshot = ModelSettingsSnapshot(
        schema_version=schema_version,
        revision=revision,
        pins=pins,
        assignments=Assignments(primary=primary, retrieval_gate=gate),
    )
    _validate_snapshot(snapshot)
    return snapshot


def _dump_price(override: UserPriceOverride | None) -> dict[str, str] | None:
    if override is None:
        return None
    body = {
        dimension: str(value)
        for dimension, _token in BILLING
        if (value := override.explicit(dimension)) is not None
    }
    return body or None


def _dump_ref(ref: ModelRef) -> dict[str, str]:
    return {"endpoint_id": ref.endpoint_id, "model_id": ref.model_id}


def _dump_pin(pin: PinRecord) -> dict[str, Any]:
    body: dict[str, Any] = {
        "endpoint_id": pin.endpoint_id,
        "model_id": pin.model_id,
    }
    if pin.wire_style_override is not None:
        body["wire_style_override"] = pin.wire_style_override
    if pin.display_name is not None:
        body["display_name"] = pin.display_name
    prices = _dump_price(pin.price_override)
    if prices is not None:
        body["price_override"] = prices
    return body


def _dump_snapshot(snapshot: ModelSettingsSnapshot) -> bytes:
    payload = {
        "schema_version": snapshot.schema_version,
        "revision": snapshot.revision,
        "pins": [_dump_pin(pin) for pin in snapshot.pins],
        "assignments": {
            "primary": _dump_ref(snapshot.assignments.primary),
            "retrieval_gate": (
                None
                if snapshot.assignments.retrieval_gate is None
                else _dump_ref(snapshot.assignments.retrieval_gate)
            ),
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _assigned_keys(assignments: Assignments) -> set[tuple[str, str]]:
    keys = {_ref_key(assignments.primary)}
    if assignments.retrieval_gate is not None:
        keys.add(_ref_key(assignments.retrieval_gate))
    return keys


class ModelSettingsStore:
    """Load and mutate the settings file. Callers see snapshot + mutate only."""

    def __init__(self, path: Path, *, clock=None):
        del clock
        self._path = Path(path)
        self._lock = threading.RLock()
        self._snapshot: ModelSettingsSnapshot | None = None
        self._fingerprint = _Fingerprint(False, None)

    def load(self) -> ModelSettingsSnapshot:
        with self._lock:
            return self._load_locked()

    def snapshot(self) -> ModelSettingsSnapshot:
        with self._lock:
            if self._snapshot is None:
                return self._load_locked()
            return self._snapshot

    def mutate(
        self,
        expected_revision: int,
        transform: Callable[[ModelSettingsSnapshot], ModelSettingsSnapshot],
    ) -> ModelSettingsSnapshot:
        with self._lock:
            current = (
                self._snapshot
                if self._snapshot is not None
                else self._load_locked()
            )
            if current.status != "ok":
                raise ModelSettingsError(current.status)
            if expected_revision != current.revision:
                raise ModelSettingsError("settings_conflict", "stale_revision")
            disk = self._read_fingerprint()
            if disk != self._fingerprint:
                raise ModelSettingsError("settings_conflict", "external_change")
            proposed = transform(current)
            removed = _pin_keys(current.pins) - _pin_keys(proposed.pins)
            if removed & _assigned_keys(current.assignments):
                raise ModelSettingsError("pin_assigned")
            _validate_snapshot(proposed)
            next_snapshot = replace(
                proposed,
                schema_version=CURRENT_SCHEMA_VERSION,
                revision=current.revision + 1,
                status="ok",
            )
            _validate_snapshot(next_snapshot)
            payload = _dump_snapshot(next_snapshot)
            self._fingerprint = self._write_atomic(payload, self._fingerprint)
            self._snapshot = next_snapshot
            return next_snapshot

    def _load_locked(self) -> ModelSettingsSnapshot:
        raw, fingerprint = self._read_bytes()
        self._fingerprint = fingerprint
        if raw is None:
            self._snapshot = _first_snapshot()
            return self._snapshot
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ModelSettingsError("settings_invalid")
            snapshot = _parse_payload(payload)
        except ModelSettingsError as exc:
            status: SettingsStatus = (
                "settings_schema_newer"
                if exc.code == "settings_schema_newer"
                else "settings_invalid"
            )
            snapshot = replace(_first_snapshot(), status=status)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            snapshot = replace(_first_snapshot(), status="settings_invalid")
        self._snapshot = snapshot
        return snapshot

    def _read_bytes(self) -> tuple[bytes | None, _Fingerprint]:
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return None, _Fingerprint(False, None)
        digest = hashlib.sha256(raw).hexdigest()
        return raw, _Fingerprint(True, digest)

    def _read_fingerprint(self) -> _Fingerprint:
        _, fingerprint = self._read_bytes()
        return fingerprint

    def _write_atomic(self, data: bytes, expected: _Fingerprint) -> _Fingerprint:
        directory = self._path.parent
        tmp = directory / f".{self._path.name}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written == 0:
                    raise OSError("zero-byte settings write")
                view = view[written:]
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        os.close(fd)
        current = self._read_fingerprint()
        if current != expected:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise ModelSettingsError("settings_conflict", "external_change")
        try:
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return _Fingerprint(True, hashlib.sha256(data).hexdigest())
