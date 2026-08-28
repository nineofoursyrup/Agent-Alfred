"""Value-matching redaction for prompt_preview, events, and telemetry."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from agent_alfred.events import Notice, RunFinished, RunStarted, UnsequencedEvent
from agent_alfred.messages import message_plain_text, text_message

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "token",
        "password",
        "secret",
        "access_token",
    }
)


class Redactor:
    def __init__(self, secrets: Sequence[str], *, min_length: int = 8):
        self._min_length = min_length
        self._secrets = tuple(
            secret for secret in secrets if secret and len(secret) >= min_length
        )

    def remember(self, secret: str | None) -> None:
        if not secret or len(secret) < self._min_length:
            return
        if secret not in self._secrets:
            self._secrets = (*self._secrets, secret)

    def redact_text(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def redact_jsonable(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            for key, item in value.items():
                name = str(key)
                lowered = name.lower()
                if lowered in _SENSITIVE_KEYS:
                    out[name] = "***"
                else:
                    out[name] = self.redact_jsonable(item)
            return out
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return [self.redact_jsonable(item) for item in value]
        return value

    def redact(self, event: UnsequencedEvent) -> UnsequencedEvent:
        payload = event.payload
        if isinstance(payload, RunStarted) and payload.user_message is not None:
            original = message_plain_text(payload.user_message)
            redacted = self.redact_text(original)
            if redacted != original:
                payload = replace(
                    payload, user_message=text_message("user", redacted)
                )
        elif isinstance(payload, RunFinished):
            if payload.reply is not None:
                original = message_plain_text(payload.reply)
                redacted = self.redact_text(original)
                if redacted != original:
                    payload = replace(
                        payload, reply=text_message("assistant", redacted)
                    )
            if payload.error is not None:
                payload = replace(payload, error=self.redact_text(payload.error))
        elif isinstance(payload, Notice):
            evidence = payload.evidence
            if evidence is not None:
                payload = replace(payload, evidence=self.redact_text(evidence))
            if payload.detail:
                payload = replace(
                    payload,
                    detail=tuple(
                        (key, self.redact_text(val)) for key, val in payload.detail
                    ),
                )
        if payload is event.payload:
            return event
        return replace(event, payload=payload)
