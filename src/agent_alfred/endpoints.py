"""Read-only destinations. Model routes, never endpoint names, select a codec."""

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from agent_alfred.settings import OPENCODE_API_KEY_ENV, OPENCODE_GO_BASE_URL
from agent_alfred.support_overrides import SupportOverrides


@dataclass(frozen=True)
class ModelRoute:
    style: str
    path: str


@dataclass(frozen=True)
class AuthProbe:
    method: str
    path: str
    success_statuses: tuple[int, ...]
    timeout_s: float
    error_mapping: Mapping[int, str]
    auth_scheme: Literal["bearer", "x-api-key"]
    static_headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "success_statuses", tuple(self.success_statuses))
        object.__setattr__(
            self, "error_mapping", MappingProxyType(dict(self.error_mapping))
        )
        object.__setattr__(
            self, "static_headers", MappingProxyType(dict(self.static_headers))
        )


def auth_probe(spec: Mapping | None) -> AuthProbe | None:
    """An incomplete executable declaration means no probe; never perform IO."""
    if not spec:
        return None
    required = (
        "method",
        "path",
        "success_statuses",
        "timeout_s",
        "error_mapping",
        "auth_scheme",
    )
    if any(not spec.get(key) for key in required):
        return None
    timeout = spec["timeout_s"]
    static_headers = spec.get("static_headers") or {}
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or timeout <= 0
        or spec["method"] not in ("GET", "HEAD", "POST")
        or spec["auth_scheme"] not in ("bearer", "x-api-key")
        or not isinstance(spec["path"], str)
        or not spec["path"].startswith(("/", "https://"))
        or spec["path"].startswith("//")
        or not isinstance(spec["error_mapping"], Mapping)
        or not isinstance(static_headers, Mapping)
        or any(
            not isinstance(name, str)
            or not name
            or not isinstance(value, str)
            or "{" in value
            or "}" in value
            for name, value in static_headers.items()
        )
    ):
        return None
    statuses = spec["success_statuses"]
    if not isinstance(statuses, (list, tuple)) or any(
        type(code) is not int or not 200 <= code < 300 for code in statuses
    ):
        return None
    if any(
        type(code) is not int
        or not 400 <= code <= 599
        or not isinstance(reason, str)
        or not reason
        for code, reason in spec["error_mapping"].items()
    ):
        return None
    return AuthProbe(
        spec["method"],
        spec["path"],
        spec["success_statuses"],
        spec["timeout_s"],
        spec["error_mapping"],
        spec["auth_scheme"],
        static_headers,
    )


@dataclass(frozen=True)
class ModelEndpoint:
    endpoint_id: str
    base_url: str
    api_key_env: str
    models: Mapping[str, ModelRoute] = field(default_factory=dict)
    catalog_url: str | None = None
    auth_probe: AuthProbe | None = None
    auth_evidence: str | None = None

    def auth_scheme(self, style: str) -> str:
        # Authentication belongs to the route, not the endpoint name.
        return {"anthropic": "x-api-key", "openai": "bearer"}[style]

    def __post_init__(self):
        object.__setattr__(self, "models", MappingProxyType(dict(self.models)))


# Approved representative routes from provider-protocol-diff.md §10.1.
_OPENCODE_MODELS = {
    "deepseek-v4-flash": ModelRoute("openai", "/chat/completions"),
    "qwen3.7-max": ModelRoute("anthropic", "/messages"),
    "grok-4.6": ModelRoute("responses", "/responses"),
}

_OPENCODE_AUTH_EVIDENCE = (
    "官方源码 337fd144d2ba144743368f78d9579a99cce175bd；"
    "messages=x-api-key；chat/completions=Bearer；未实测，线上版本未确认"
)

_BUILTIN = (
    ModelEndpoint("anthropic", "https://api.anthropic.com/v1", "ANTHROPIC_API_KEY"),
    ModelEndpoint("openai", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    ModelEndpoint("xai", "https://api.x.ai/v1", "XAI_API_KEY"),
    ModelEndpoint("deepseek", "https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    ModelEndpoint(
        "opencode",
        "https://opencode.ai/zen/v1",
        OPENCODE_API_KEY_ENV,
        _OPENCODE_MODELS,
        auth_evidence=_OPENCODE_AUTH_EVIDENCE,
    ),
    ModelEndpoint(
        "opencode-go",
        OPENCODE_GO_BASE_URL,
        OPENCODE_API_KEY_ENV,
        _OPENCODE_MODELS,
        auth_evidence=_OPENCODE_AUTH_EVIDENCE,
    ),
)


def list_endpoints() -> tuple[ModelEndpoint, ...]:
    return _BUILTIN


@dataclass(frozen=True)
class EffectiveSupport:
    wire_style: str | None
    support: Literal["supported", "unsupported", "unknown"]
    support_basis: Literal["builtin_table", "probe_evidence"] | None
    wire_style_source: Literal["builtin_table", "user_declared"] | None
    reason: str | None = None


def resolve_model(
    endpoint_id: str,
    model_id: str,
    *,
    wire_style_override: str | None = None,
    endpoints: tuple[ModelEndpoint, ...] | None = None,
    overrides: SupportOverrides | None = None,
) -> EffectiveSupport:
    rows = list_endpoints() if endpoints is None else endpoints
    endpoint = next((e for e in rows if e.endpoint_id == endpoint_id), None)
    route = endpoint.models.get(model_id) if endpoint is not None else None
    declared = route.style if route else None
    style = wire_style_override if wire_style_override is not None else declared
    source = (
        "user_declared"
        if wire_style_override is not None
        else "builtin_table"
        if declared is not None
        else None
    )
    if declared == "responses" or style == "responses":
        return EffectiveSupport(
            style,
            "unsupported",
            "builtin_table" if declared == "responses" else None,
            source,
            "unsupported_wire_style: responses",
        )
    evidence = (
        overrides.get(endpoint_id, model_id, style) if overrides and style else None
    )
    if evidence is not None:
        return EffectiveSupport(
            style, "unsupported", "probe_evidence", source, evidence.reason
        )
    if declared in ("openai", "anthropic") and style == declared:
        return EffectiveSupport(style, "supported", "builtin_table", source)
    return EffectiveSupport(style, "unknown", None, source)
