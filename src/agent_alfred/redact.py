"""Value-matching redaction for prompt_preview, events, and telemetry."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import fields, is_dataclass, replace
from decimal import Decimal
from typing import Any

from agent_alfred.events import (
    AttemptAborted,
    AttemptCommitted,
    AttemptStarted,
    BlockDelta,
    BlockStarted,
    BlockStopped,
    GateEvaluated,
    GraphFinished,
    GraphStarted,
    NodeAborted,
    NodeFinished,
    NodeSkipped,
    NodeStarted,
    Notice,
    PathCaptured,
    PathStage,
    RawToolArgumentFragment,
    RunFinished,
    RunStarted,
    StepFinished,
    StepStarted,
    ToolFinished,
    ToolProgress,
    ToolStarted,
    UnsequencedEvent,
    WaveSettled,
)
from agent_alfred.messages import (
    Message,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from agent_alfred.model import ModelError, ModelRef, Usage

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

_EVENT_PAYLOAD_TYPES = (
    RunStarted,
    RunFinished,
    StepStarted,
    ToolStarted,
    ToolFinished,
    ToolProgress,
    StepFinished,
    AttemptStarted,
    AttemptCommitted,
    AttemptAborted,
    BlockStarted,
    BlockDelta,
    BlockStopped,
    Notice,
    GateEvaluated,
    GraphStarted, GraphFinished, NodeStarted, NodeFinished, NodeSkipped, NodeAborted,
    PathCaptured, PathStage, WaveSettled,
)


class Redactor:
    def __init__(
        self,
        secrets: Sequence[str],
        *,
        min_length: int = 8,
        on_change=None,
        approved: bool = False,
    ):
        self._min_length = min_length
        self._lock = threading.RLock()
        self._on_change = on_change
        self._version = 1
        if approved:
            self._secrets = tuple(secret for secret in secrets if secret)
        else:
            self._secrets = tuple(
                secret for secret in secrets if secret and len(secret) >= min_length
            )

    @contextmanager
    def protection_guard(self):
        """Linearize protected send admission with credential registration."""
        with self._lock:
            yield

    @property
    def min_length(self) -> int:
        return self._min_length

    @property
    def secrets(self) -> tuple[str, ...]:
        with self._lock:
            return self._secrets

    @property
    def protection_version(self) -> str:
        with self._lock:
            return str(self._version)

    def remember(self, secret: str | None, *, credential: bool = False) -> None:
        """Only known credential inputs may bypass the ordinary length guard."""
        if not secret or (not credential and len(secret) < self._min_length):
            return
        changed = False
        with self._lock:
            if secret not in self._secrets:
                self._secrets = (*self._secrets, secret)
                self._version += 1
                changed = True
        if changed and self._on_change is not None:
            self._on_change()

    def redact_text(self, text: str) -> str:
        with self._lock:
            secrets = self._secrets
        for secret in secrets:
            text = text.replace(secret, "***")
        return text

    def redact_jsonable(self, value: Any) -> Any:
        if value is None or isinstance(value, (int, float, bool, Decimal)):
            return value
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
        raise TypeError(f"unredactable {type(value).__name__}")

    def redact(self, event: UnsequencedEvent) -> UnsequencedEvent:
        if not isinstance(event.payload, _EVENT_PAYLOAD_TYPES):
            raise TypeError(
                f"unknown event payload {type(event.payload).__name__}"
            )
        payload = self._redact_value(event.payload)
        if payload is event.payload:
            return event
        return replace(event, payload=payload)

    def _redact_value(self, value: Any) -> Any:
        if value is None or isinstance(value, (int, float, bool, Decimal)):
            return value
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, ModelRef):
            return value
        if isinstance(value, Message):
            blocks = tuple(self._redact_value(block) for block in value.blocks)
            if blocks == value.blocks:
                return value
            return Message(role=value.role, blocks=blocks)
        if isinstance(value, TextBlock):
            text = self.redact_text(value.text)
            return value if text == value.text else replace(value, text=text)
        if isinstance(value, ThinkingBlock):
            text = self.redact_text(value.text)
            return value if text == value.text else replace(value, text=text)
        if isinstance(value, RawToolArgumentFragment):
            return replace(
                value,
                call_id=self.redact_text(value.call_id)
                if value.call_id is not None
                else None,
                name=self.redact_text(value.name) if value.name is not None else None,
                raw=self.redact_text(value.raw),
            )
        if isinstance(value, ToolCallBlock):
            redacted_input = self.redact_jsonable(dict(value.input))
            name = self.redact_text(value.name)
            if redacted_input == value.input and name == value.name:
                return value
            return replace(value, input=redacted_input, name=name)
        if isinstance(value, ToolResultBlock):
            content = tuple(self._redact_value(block) for block in value.content)
            if content == value.content:
                return value
            return replace(value, content=content)
        if isinstance(value, Usage):
            raw = self.redact_jsonable(value.raw)
            return value if raw == value.raw else replace(value, raw=raw)
        if isinstance(value, ModelError):
            excerpt = value.body_excerpt
            if excerpt is None:
                return value
            redacted = self.redact_text(excerpt)
            if redacted == excerpt:
                return value
            return replace(value, body_excerpt=redacted)
        if is_dataclass(value) and not isinstance(value, type):
            updates: dict[str, Any] = {}
            for field in fields(value):
                if field.name in ("name", "trace_policy"):
                    continue
                current = getattr(value, field.name)
                if field.name.lower() in _SENSITIVE_KEYS:
                    redacted = "***"
                else:
                    redacted = self._redact_value(current)
                if redacted != current:
                    updates[field.name] = redacted
            return replace(value, **updates) if updates else value
        if isinstance(value, Mapping):
            return self.redact_jsonable(value)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            if (
                isinstance(value, tuple)
                and len(value) == 2
                and isinstance(value[0], str)
                and value[0].lower() in _SENSITIVE_KEYS
            ):
                return (value[0], "***")
            items = [self._redact_value(item) for item in value]
            if isinstance(value, tuple):
                return tuple(items)
            return items
        raise TypeError(f"unredactable {type(value).__name__}")
