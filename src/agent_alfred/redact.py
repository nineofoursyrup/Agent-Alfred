"""Value-matching redaction for prompt_preview and event text."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from agent_alfred.events import RunFinished, RunStarted, UnsequencedEvent
from agent_alfred.messages import message_plain_text, text_message


class Redactor:
    def __init__(self, secrets: Sequence[str], *, min_length: int = 8):
        self._secrets = tuple(
            secret for secret in secrets if secret and len(secret) >= min_length
        )

    def redact_text(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def redact(self, event: UnsequencedEvent) -> UnsequencedEvent:
        payload = event.payload
        if isinstance(payload, RunStarted) and payload.user_message is not None:
            original = message_plain_text(payload.user_message)
            redacted = self.redact_text(original)
            if redacted != original:
                payload = replace(
                    payload, user_message=text_message("user", redacted)
                )
        elif isinstance(payload, RunFinished) and payload.reply is not None:
            original = message_plain_text(payload.reply)
            redacted = self.redact_text(original)
            if redacted != original:
                payload = replace(
                    payload, reply=text_message("assistant", redacted)
                )
        if payload is event.payload:
            return event
        return replace(event, payload=payload)
