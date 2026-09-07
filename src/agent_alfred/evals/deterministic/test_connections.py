"""Connections projection: key masking, four-state, catalog health (#34)."""

from __future__ import annotations

import json
from pathlib import Path

from agent_alfred.catalog import CatalogState
from agent_alfred.connections import CredentialOverlay, project_connections
from agent_alfred.endpoints import AuthProbe, ModelEndpoint


def _endpoint(**kwargs) -> ModelEndpoint:
    values = {
        "endpoint_id": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
    }
    values.update(kwargs)
    return ModelEndpoint(**values)


def test_long_key_exposes_last4_only() -> None:
    dto = project_connections(
        endpoints=(_endpoint(),),
        env={"OPENAI_API_KEY": "sk-abcdefgh"},
        overlay={},
        observations={},
        catalogs={},
    )
    row = dto["endpoints"][0]
    dumped = json.dumps(dto)
    assert "sk-abcdefgh" not in dumped
    assert row["key"]["last4"] == "efgh"
    assert row["key"]["masked"] is False
    assert row["key"]["configured"] is True
    assert row["observation"]["state"] == "configured_untested"


def test_short_key_is_fully_masked_and_leaks_no_characters() -> None:
    secret = "shorty"
    dto = project_connections(
        endpoints=(_endpoint(),),
        env={"OPENAI_API_KEY": secret},
        overlay={},
        observations={},
        catalogs={},
    )
    dumped = json.dumps(dto)
    assert secret not in dumped
    row = dto["endpoints"][0]
    assert row["key"]["last4"] is None
    assert row["key"]["masked"] is True
    assert row["key"]["configured"] is True
    assert "value" not in row["key"]


def test_blank_key_is_unconfigured() -> None:
    dto = project_connections(
        endpoints=(_endpoint(),),
        env={"OPENAI_API_KEY": "  "},
        overlay={},
        observations={},
        catalogs={},
    )
    row = dto["endpoints"][0]
    assert row["observation"]["state"] == "unconfigured"
    assert row["key"]["configured"] is False
    assert row["key"]["last4"] is None


def test_missing_auth_probe_declares_no_free_probe() -> None:
    dto = project_connections(
        endpoints=(_endpoint(),),
        env={"OPENAI_API_KEY": "sk-abcdefgh"},
        overlay={},
        observations={},
        catalogs={},
    )
    row = dto["endpoints"][0]
    assert row["auth_probe"]["available"] is False
    assert row["auth_probe"]["label"] == "无免费认证探针"


def test_observation_and_catalog_health_are_side_by_side() -> None:
    observation = {
        "state": "connected",
        "checked_at": "2026-08-28T12:00:00Z",
        "checked_via": "auth_probe",
        "reason": None,
    }
    catalog = CatalogState(
        health="unavailable",
        last_success_at="2026-08-28T11:00:00Z",
        last_error="http_500",
        retry_at="2026-08-28T12:01:00Z",
    )
    dto = project_connections(
        endpoints=(_endpoint(),),
        env={"OPENAI_API_KEY": "sk-abcdefgh"},
        overlay={},
        observations={"openai": observation},
        catalogs={"openai": catalog},
    )
    row = dto["endpoints"][0]
    assert row["observation"]["state"] == "connected"
    assert row["observation"]["checked_via"] == "auth_probe"
    assert row["catalog"]["health"] == "unavailable"
    assert row["catalog"]["last_success_at"] == "2026-08-28T11:00:00Z"
    assert row["catalog"]["last_error"] == "http_500"
    assert row["catalog"]["retry_at"] == "2026-08-28T12:01:00Z"
    assert row["observation"]["state"] != row["catalog"]["health"]


def test_injected_probe_endpoint_is_available_without_touching_builtins() -> None:
    from agent_alfred.endpoints import list_endpoints

    probe = AuthProbe(
        "GET", "/status", (204,), 2.0, {401: "authentication_failed"}, "bearer"
    )
    dto = project_connections(
        endpoints=(_endpoint(auth_probe=probe),),
        env={"OPENAI_API_KEY": "sk-abcdefgh"},
        overlay={},
        observations={},
        catalogs={},
    )
    assert dto["endpoints"][0]["auth_probe"]["available"] is True
    assert all(row.auth_probe is None for row in list_endpoints())


def test_dotenv_reread_follows_file_a_b_delete_without_touching_os_environ(
    tmp_path: Path, monkeypatch
) -> None:
    import os

    path = tmp_path / ".env"
    path.write_text("OPENAI_API_KEY=file-key-aaa\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    before = dict(os.environ)
    overlay = CredentialOverlay(process_env={}, dotenv_path=str(path))
    assert overlay.values()["OPENAI_API_KEY"] == "file-key-aaa"
    path.write_text("OPENAI_API_KEY=file-key-bbb\n", encoding="utf-8")
    overlay.reread()
    assert overlay.values()["OPENAI_API_KEY"] == "file-key-bbb"
    path.unlink()
    overlay.reread()
    assert overlay.values().get("OPENAI_API_KEY") is None
    assert dict(os.environ) == before


def test_process_environment_wins_over_file_and_blank_is_unconfigured(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".env"
    path.write_text("OPENAI_API_KEY=file-key-aaa\nOTHER=from-file\n", encoding="utf-8")
    overlay = CredentialOverlay(
        process_env={"OPENAI_API_KEY": "process-key-zzzz"},
        dotenv_path=str(path),
    )
    values = overlay.values()
    assert values["OPENAI_API_KEY"] == "process-key-zzzz"
    assert values["OTHER"] == "from-file"
    blank = CredentialOverlay(
        process_env={"OPENAI_API_KEY": "  "}, dotenv_path=str(path)
    )
    dto = project_connections(
        endpoints=(_endpoint(),),
        env=blank.values(),
        overlay={},
        observations={},
        catalogs={},
    )
    assert dto["endpoints"][0]["observation"]["state"] == "unconfigured"


def test_reread_keeps_the_startup_path(tmp_path: Path) -> None:
    found = tmp_path / ".env"
    found.write_text("OPENAI_API_KEY=kept-path\n", encoding="utf-8")
    other = tmp_path / "other.env"
    other.write_text("OPENAI_API_KEY=other-path\n", encoding="utf-8")
    overlay = CredentialOverlay(process_env={}, dotenv_path=str(found))
    overlay.reread()
    assert overlay.path == str(found)
    assert overlay.values()["OPENAI_API_KEY"] == "kept-path"
    assert overlay.can_reread is True
    missing = CredentialOverlay(process_env={}, dotenv_path=None)
    assert missing.can_reread is False
