"""Injectable assignment snapshot provider. Not a stand-in for a config store."""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from typing import Protocol

from agent_alfred.endpoints import list_endpoints, resolve_model
from agent_alfred.model import ClientSnapshot, ModelAssignment, ModelRef
from agent_alfred.runtime.model_settings import (
    ModelSettingsError,
    ModelSettingsSnapshot,
    ModelSettingsStore,
)
from agent_alfred.settings import DEFAULT_WIRE_STYLE, Settings


class InvalidProbeTarget(Exception):
    """Inference probe named a model that is not pinned and assigned."""


class ConfigSnapshotProvider(Protocol):
    """Captured once at Run admission. Later Attempts of that Run reuse it."""

    def capture(
        self,
        *,
        stream: bool = False,
        endpoint_id: str | None = None,
        model_id: str | None = None,
    ) -> ClientSnapshot: ...


class MutableAssignmentProvider:
    """Explicit assignments + version. Tests and Host inject this seam."""

    def __init__(
        self,
        *,
        endpoint_id: str,
        model_id: str,
        wire_style: str,
        api_key: str | None,
        settings: Settings | None = None,
        retrieval_gate: ModelAssignment | None = None,
    ):
        self._lock = threading.Lock()
        self._primary = ModelAssignment(
            endpoint_id=endpoint_id, model_id=model_id, wire_style=wire_style
        )
        self._retrieval_gate = retrieval_gate
        self._api_key = api_key
        self._settings = settings or Settings()
        self._version = 1

    def capture(
        self,
        *,
        stream: bool = False,
        endpoint_id: str | None = None,
        model_id: str | None = None,
    ) -> ClientSnapshot:
        del endpoint_id, model_id
        with self._lock:
            return _snapshot(
                version=str(self._version),
                primary=self._primary,
                retrieval_gate=self._retrieval_gate,
                api_key=self._api_key,
                retrieval_gate_api_key=self._api_key,
                stream=stream,
                settings=self._settings,
            )

    def rotate_key(self, api_key: str | None) -> None:
        with self._lock:
            self._api_key = api_key
            self._version += 1

    def assign_primary(
        self,
        *,
        endpoint_id: str | None = None,
        model_id: str | None = None,
        wire_style: str | None = None,
    ) -> None:
        with self._lock:
            self._primary = ModelAssignment(
                endpoint_id=self._primary.endpoint_id
                if endpoint_id is None
                else endpoint_id,
                model_id=self._primary.model_id if model_id is None else model_id,
                wire_style=self._primary.wire_style
                if wire_style is None
                else wire_style,
            )
            self._version += 1


class SettingsBackedSnapshotProvider:
    """Reads current Settings + credentials. Version bumps when they change.

    This is the production seam until an assignment store exists. It does
    not pretend a dict inside RuntimeHost is the assignment model.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        environ: Mapping[str, str] | None = None,
    ):
        self._settings = settings
        self._environ = environ
        self._lock = threading.Lock()
        self._version = 0
        self._last_fingerprint: object = object()

    def capture(
        self,
        *,
        stream: bool = False,
        endpoint_id: str | None = None,
        model_id: str | None = None,
    ) -> ClientSnapshot:
        del endpoint_id, model_id
        key = _read_key(self._settings, self._environ)
        fingerprint = (
            self._settings.endpoint_id,
            self._settings.model_id,
            self._settings.wire_style,
            key,
            self._settings.stream_fallback,
            self._settings.overall_deadline_s,
            self._settings.per_attempt_timeout_s,
        )
        with self._lock:
            if fingerprint != self._last_fingerprint:
                self._version += 1
                self._last_fingerprint = fingerprint
            version = str(self._version)
        return _snapshot(
            version=version,
            primary=ModelAssignment(
                endpoint_id=self._settings.endpoint_id,
                model_id=self._settings.model_id,
                wire_style=self._settings.wire_style,
            ),
            retrieval_gate=None,
            api_key=key,
            stream=stream,
            settings=self._settings,
        )


def _snapshot(
    *,
    version: str,
    primary: ModelAssignment,
    retrieval_gate: ModelAssignment | None,
    api_key: str | None,
    stream: bool,
    settings: Settings,
    retrieval_gate_api_key: str | None = None,
) -> ClientSnapshot:
    return ClientSnapshot(
        config_version=version,
        primary=primary,
        retrieval_gate=retrieval_gate,
        api_key=api_key,
        stream=stream,
        stream_fallback=settings.stream_fallback,
        overall_deadline_s=settings.overall_deadline_s,
        per_attempt_timeout_s=settings.per_attempt_timeout_s,
        retrieval_gate_api_key=retrieval_gate_api_key,
    )


def _read_key(
    settings: Settings, environ: Mapping[str, str] | None
) -> str | None:
    env = os.environ if environ is None else environ
    raw = env.get(settings.api_key_env)
    if raw is None or not raw.strip():
        return None
    return raw


def _style_for(ref: ModelRef, snapshot: ModelSettingsSnapshot) -> str:
    pin = snapshot.pin(ref.endpoint_id, ref.model_id)
    override = None if pin is None else pin.wire_style_override
    support = resolve_model(
        ref.endpoint_id, ref.model_id, wire_style_override=override
    )
    return support.wire_style or DEFAULT_WIRE_STYLE


def _assignment_for(ref: ModelRef, snapshot: ModelSettingsSnapshot) -> ModelAssignment:
    return ModelAssignment(
        endpoint_id=ref.endpoint_id,
        model_id=ref.model_id,
        wire_style=_style_for(ref, snapshot),
    )


def _key_for(endpoint_id: str, environ: Mapping[str, str] | None) -> str | None:
    env = os.environ if environ is None else environ
    endpoint = next(
        (row for row in list_endpoints() if row.endpoint_id == endpoint_id),
        None,
    )
    name = endpoint.api_key_env if endpoint is not None else None
    if name is None:
        return None
    raw = env.get(name)
    if raw is None or not raw.strip():
        return None
    return raw


class StoreBackedSnapshotProvider:
    """Admission capture from the Host-exclusive ModelSettingsStore."""

    def __init__(
        self,
        store: ModelSettingsStore,
        settings: Settings,
        *,
        environ: Mapping[str, str] | None = None,
    ):
        self._store = store
        self._settings = settings
        self._environ = environ
        self._lock = threading.Lock()
        self._version = 0
        self._last_fingerprint: object = object()

    def capture(
        self,
        *,
        stream: bool = False,
        endpoint_id: str | None = None,
        model_id: str | None = None,
    ) -> ClientSnapshot:
        snapshot = self._store.snapshot()
        if snapshot.status != "ok":
            raise ModelSettingsError(snapshot.status)
        primary = _assignment_for(snapshot.assignments.primary, snapshot)
        gate_ref = snapshot.assignments.retrieval_gate
        retrieval_gate = (
            None if gate_ref is None else _assignment_for(gate_ref, snapshot)
        )
        invoked = primary
        if endpoint_id is not None or model_id is not None:
            if type(endpoint_id) is not str or type(model_id) is not str:
                raise InvalidProbeTarget
            target = ModelRef(endpoint_id, model_id)
            assigned = {snapshot.assignments.primary}
            if snapshot.assignments.retrieval_gate is not None:
                assigned.add(snapshot.assignments.retrieval_gate)
            if target not in assigned or snapshot.pin(endpoint_id, model_id) is None:
                raise InvalidProbeTarget
            invoked = _assignment_for(target, snapshot)
        key = _key_for(invoked.endpoint_id, self._environ)
        gate_key = None if retrieval_gate is None else _key_for(
            retrieval_gate.endpoint_id, self._environ
        )
        fingerprint = (
            snapshot.revision,
            snapshot.status,
            invoked,
            retrieval_gate,
            gate_key,
            key,
            self._settings.stream_fallback,
            self._settings.overall_deadline_s,
            self._settings.per_attempt_timeout_s,
        )
        with self._lock:
            if fingerprint != self._last_fingerprint:
                self._version += 1
                self._last_fingerprint = fingerprint
            version = str(self._version)
        return _snapshot(
            version=version,
            primary=invoked,
            retrieval_gate_api_key=gate_key,
            retrieval_gate=retrieval_gate,
            api_key=key,
            stream=stream,
            settings=self._settings,
        )

    def bind_environ(self, environ: Mapping[str, str] | None) -> None:
        self._environ = environ
