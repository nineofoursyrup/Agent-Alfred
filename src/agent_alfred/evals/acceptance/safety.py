"""Reject secret-bearing evidence before any persistent or judge boundary."""

import os

from agent_alfred.redact import Redactor

SENSITIVE = {
    "api_key",
    "secret",
    "password",
    "access_token",
    "authorization_header",
    "cookie",
    "credential",
    "token",
}


def ensure_safe(value, *, secrets=()):
    known = list(secrets)
    known.extend(
        v
        for k, v in os.environ.items()
        if any(word in k.lower() for word in ("key", "secret", "token", "password"))
        and v
    )
    redactor = Redactor(known, approved=True)

    def visit(item):
        if isinstance(item, str):
            if redactor.redact_text(item) != item:
                raise ValueError("sensitive_data")
        elif isinstance(item, dict):
            for key, content in item.items():
                if key.lower() in SENSITIVE:
                    raise ValueError("sensitive_data")
                visit(key)
                visit(content)
        elif isinstance(item, list):
            for content in item:
                visit(content)
        elif item is not None and type(item) not in (bool, int, float):
            raise ValueError("unsafe_structure")

    visit(value)


def credential_secrets(values):
    """CredentialOverlay also holds non-secret environment settings."""
    from agent_alfred.endpoints import list_endpoints

    names = {endpoint.api_key_env for endpoint in list_endpoints()}
    return tuple(
        value
        for key, value in values.items()
        if value
        and (
            key in names
            or any(
                word in key.lower()
                for word in ("secret", "token", "password", "api_key")
            )
        )
    )
