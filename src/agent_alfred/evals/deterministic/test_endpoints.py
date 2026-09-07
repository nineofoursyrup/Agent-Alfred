"""Endpoint declarations and effective support through public read seams."""

from dataclasses import FrozenInstanceError

import pytest


def test_six_read_only_endpoints_keep_the_two_opencode_destinations_distinct():
    from agent_alfred.endpoints import list_endpoints

    rows = {row.endpoint_id: row for row in list_endpoints()}
    assert set(rows) == {
        "anthropic",
        "openai",
        "xai",
        "deepseek",
        "opencode",
        "opencode-go",
    }
    assert rows["opencode"].base_url == "https://opencode.ai/zen/v1"
    assert rows["opencode-go"].base_url == "https://opencode.ai/zen/go/v1"
    assert rows["opencode"].api_key_env == "OPENCODE_API_KEY"
    assert rows["opencode-go"].api_key_env == "OPENCODE_API_KEY"
    with pytest.raises(FrozenInstanceError):
        rows["opencode"].base_url = "changed"
    with pytest.raises(TypeError):
        rows["opencode"].models["new"] = None


def test_opencode_rows_read_destination_and_key_from_one_source():
    from agent_alfred.endpoints import list_endpoints
    from agent_alfred.settings import OPENCODE_API_KEY_ENV, OPENCODE_GO_BASE_URL

    rows = {row.endpoint_id: row for row in list_endpoints()}
    assert rows["opencode-go"].base_url == OPENCODE_GO_BASE_URL
    assert rows["opencode"].api_key_env == OPENCODE_API_KEY_ENV
    assert rows["opencode-go"].api_key_env == OPENCODE_API_KEY_ENV


@pytest.mark.parametrize(
    "model,override,style,support,basis,source",
    [
        (
            "deepseek-v4-flash",
            None,
            "openai",
            "supported",
            "builtin_table",
            "builtin_table",
        ),
        (
            "qwen3.7-max",
            None,
            "anthropic",
            "supported",
            "builtin_table",
            "builtin_table",
        ),
        (
            "grok-4.6",
            None,
            "responses",
            "unsupported",
            "builtin_table",
            "builtin_table",
        ),
        ("unlisted", None, None, "unknown", None, None),
        ("unlisted", "openai", "openai", "unknown", None, "user_declared"),
        (
            "deepseek-v4-flash",
            "openai",
            "openai",
            "supported",
            "builtin_table",
            "user_declared",
        ),
        (
            "deepseek-v4-flash",
            "anthropic",
            "anthropic",
            "unknown",
            None,
            "user_declared",
        ),
        (
            "grok-4.6",
            "openai",
            "openai",
            "unsupported",
            "builtin_table",
            "user_declared",
        ),
    ],
)
def test_effective_support_never_borrows_evidence_from_another_shape(
    model,
    override,
    style,
    support,
    basis,
    source,
):
    from agent_alfred.endpoints import resolve_model

    result = resolve_model("opencode-go", model, wire_style_override=override)
    assert (
        result.wire_style,
        result.support,
        result.support_basis,
        result.wire_style_source,
    ) == (style, support, basis, source)
    if model == "grok-4.6":
        assert result.reason == "unsupported_wire_style: responses"


@pytest.mark.parametrize(
    "missing", ["method", "path", "success_statuses", "timeout_s", "error_mapping"]
)
def test_probe_requires_all_five_fields_and_production_declares_none(missing):
    from agent_alfred.endpoints import auth_probe, list_endpoints

    spec = dict(
        method="GET",
        path="/test-only-auth",
        success_statuses=(204,),
        timeout_s=2.0,
        error_mapping={401: "authentication_failed"},
    )
    assert auth_probe(spec) is not None
    del spec[missing]
    assert auth_probe(spec) is None
    assert all(row.auth_probe is None for row in list_endpoints())


def test_opencode_exposes_source_and_one_auth_scheme_per_route():
    from agent_alfred.endpoints import list_endpoints

    for row in list_endpoints():
        if row.endpoint_id in ("opencode", "opencode-go"):
            assert row.auth_scheme("anthropic") == "x-api-key"
            assert row.auth_scheme("openai") == "bearer"
            assert "337fd144d2ba144743368f78d9579a99cce175bd" in row.auth_evidence
            assert "未实测" in row.auth_evidence
