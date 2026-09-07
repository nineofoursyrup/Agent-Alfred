"""Connections page DTO. Never includes full secrets; never probes."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_alfred.catalog import CatalogState
from agent_alfred.endpoints import ModelEndpoint

NO_FREE_AUTH_PROBE = "无免费认证探针"
KEY_LENGTH_FOR_LAST4 = 8


def merge_credentials(
    process_env: Mapping[str, str], overlay: Mapping[str, str]
) -> dict[str, str]:
    """Process environment wins; overlay supplies file-only keys."""
    merged = dict(overlay)
    merged.update(process_env)
    return merged


class CredentialOverlay:
    """Startup process env + the one resolved .env path. Reread never searches."""

    def __init__(self, process_env: Mapping[str, str], dotenv_path: str | None):
        self._process = dict(process_env)
        self._path = dotenv_path
        self._overlay = self._read_file()

    @property
    def path(self) -> str | None:
        return self._path

    @property
    def can_reread(self) -> bool:
        return self._path is not None

    def values(self) -> dict[str, str]:
        return merge_credentials(self._process, self._overlay)

    def reread(self) -> dict[str, str]:
        if self._path is None:
            raise RuntimeError("dotenv path was not resolved at startup")
        self._overlay = self._read_file()
        return self.values()

    def _read_file(self) -> dict[str, str]:
        if not self._path:
            return {}
        from dotenv import dotenv_values

        if not Path(self._path).is_file():
            return {}
        parsed = dotenv_values(self._path)
        return {
            key: value
            for key, value in parsed.items()
            if key is not None and value is not None
        }


def _raw_key(env: Mapping[str, str], name: str) -> str | None:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return None
    return raw


def _key_view(raw: str | None) -> dict[str, Any]:
    if raw is None:
        return {"configured": False, "last4": None, "masked": False}
    if len(raw) >= KEY_LENGTH_FOR_LAST4:
        return {"configured": True, "last4": raw[-4:], "masked": False}
    return {"configured": True, "last4": None, "masked": True}


def _observation(
    configured: bool, recorded: Mapping[str, Any] | None
) -> dict[str, Any]:
    if not configured:
        return {
            "state": "unconfigured",
            "checked_at": None,
            "checked_via": None,
            "reason": None,
        }
    if recorded is None:
        return {
            "state": "configured_untested",
            "checked_at": None,
            "checked_via": None,
            "reason": None,
        }
    return {
        "state": recorded.get("state", "configured_untested"),
        "checked_at": recorded.get("checked_at"),
        "checked_via": recorded.get("checked_via"),
        "reason": recorded.get("reason"),
    }


def _catalog_view(state: CatalogState | None) -> dict[str, Any]:
    if state is None:
        return {
            "health": "unfetched",
            "last_success_at": None,
            "last_error": None,
            "retry_at": None,
        }
    return {
        "health": state.health,
        "last_success_at": state.last_success_at,
        "last_error": state.last_error,
        "retry_at": state.retry_at,
    }


def _auth_probe_view(endpoint: ModelEndpoint) -> dict[str, Any]:
    if endpoint.auth_probe is None:
        return {"available": False, "label": NO_FREE_AUTH_PROBE}
    return {"available": True, "label": None}


def project_connections(
    *,
    endpoints: tuple[ModelEndpoint, ...],
    env: Mapping[str, str],
    overlay: Mapping[str, str],
    observations: Mapping[str, Mapping[str, Any]],
    catalogs: Mapping[str, CatalogState],
) -> dict[str, Any]:
    credentials = merge_credentials(env, overlay)
    rows = []
    for endpoint in endpoints:
        raw = _raw_key(credentials, endpoint.api_key_env)
        key = _key_view(raw)
        rows.append(
            {
                "endpoint_id": endpoint.endpoint_id,
                "base_url": endpoint.base_url,
                "catalog_url": endpoint.catalog_url,
                "api_key_env": endpoint.api_key_env,
                "key": key,
                "observation": _observation(
                    key["configured"], observations.get(endpoint.endpoint_id)
                ),
                "catalog": _catalog_view(catalogs.get(endpoint.endpoint_id)),
                "auth_probe": _auth_probe_view(endpoint),
            }
        )
    return {"endpoints": rows}
