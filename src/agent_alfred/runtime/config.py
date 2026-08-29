"""Injectable assignment snapshot provider. Not a stand-in for a config store."""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from typing import Protocol

from agent_alfred.model import ClientSnapshot, ModelAssignment
from agent_alfred.settings import Settings


class ConfigSnapshotProvider(Protocol):
    """Captured once at Run admission. Later Attempts of that Run reuse it."""

    def capture(self, *, stream: bool = False) -> ClientSnapshot: ...


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

    def capture(self, *, stream: bool = False) -> ClientSnapshot:
        with self._lock:
            return _snapshot(
                version=str(self._version),
                primary=self._primary,
                retrieval_gate=self._retrieval_gate,
                api_key=self._api_key,
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

    def capture(self, *, stream: bool = False) -> ClientSnapshot:
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
    )


def _read_key(
    settings: Settings, environ: Mapping[str, str] | None
) -> str | None:
    env = os.environ if environ is None else environ
    raw = env.get(settings.api_key_env)
    if raw is None or not raw.strip():
        return None
    return raw
