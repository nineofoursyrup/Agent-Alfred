"""Rich content-block messages. The internal baseline (ADR-0002)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

Role = Literal["user", "assistant"]


@dataclass(frozen=True)
class TextBlock:
    text: str


@dataclass(frozen=True)
class ThinkingBlock:
    text: str
    signature: str | None = None


@dataclass(frozen=True)
class ToolCallBlock:
    id: str
    name: str
    input: Mapping[str, Any]


@dataclass(frozen=True)
class ToolResultBlock:
    call_id: str
    content: Sequence[TextBlock]
    is_error: bool = False


Block = TextBlock | ThinkingBlock | ToolCallBlock | ToolResultBlock


class MessageError(ValueError):
    """Raised when a Message would occupy an illegal state."""


@dataclass(frozen=True)
class Message:
    role: Role
    blocks: tuple[Block, ...]

    def __post_init__(self) -> None:
        if self.role == "user":
            for block in self.blocks:
                if isinstance(block, (ToolCallBlock, ThinkingBlock)):
                    raise MessageError(
                        "user messages cannot contain tool_call or thinking blocks"
                    )
            _require_tool_results_first(self.blocks)
        elif self.role == "assistant":
            for block in self.blocks:
                if isinstance(block, ToolResultBlock):
                    raise MessageError(
                        "assistant messages cannot contain tool_result blocks"
                    )
        else:
            raise MessageError(f"unknown role {self.role!r}")


def _require_tool_results_first(blocks: Sequence[Block]) -> None:
    seen_text = False
    for block in blocks:
        if isinstance(block, ToolResultBlock):
            if seen_text:
                raise MessageError(
                    "tool_result blocks must come before any text in a user message"
                )
        elif isinstance(block, TextBlock):
            seen_text = True


def text_message(role: Role, text: str) -> Message:
    return Message(role=role, blocks=(TextBlock(text),))


def message_plain_text(message: Message) -> str:
    parts = [block.text for block in message.blocks if isinstance(block, TextBlock)]
    return "".join(parts)


def blocks_to_jsonable(blocks: Sequence[Block]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, TextBlock):
            out.append({"type": "text", "text": block.text})
        elif isinstance(block, ThinkingBlock):
            out.append(
                {
                    "type": "thinking",
                    "text": block.text,
                    "signature": block.signature,
                }
            )
        elif isinstance(block, ToolCallBlock):
            out.append(
                {
                    "type": "tool_call",
                    "id": block.id,
                    "name": block.name,
                    "input": dict(block.input),
                }
            )
        elif isinstance(block, ToolResultBlock):
            out.append(
                {
                    "type": "tool_result",
                    "call_id": block.call_id,
                    "content": [
                        {"type": "text", "text": part.text}
                        for part in block.content
                    ],
                    "is_error": block.is_error,
                }
            )
        else:
            raise MessageError(f"unknown block type {type(block)!r}")
    return out


def blocks_from_jsonable(raw: Sequence[Mapping[str, Any]]) -> tuple[Block, ...]:
    blocks: list[Block] = []
    for item in raw:
        kind = item.get("type")
        if kind == "text":
            blocks.append(TextBlock(text=str(item.get("text", ""))))
        elif kind == "thinking":
            signature = item.get("signature")
            blocks.append(
                ThinkingBlock(
                    text=str(item.get("text", "")),
                    signature=None if signature is None else str(signature),
                )
            )
        elif kind == "tool_call":
            blocks.append(
                ToolCallBlock(
                    id=str(item.get("id", "")),
                    name=str(item.get("name", "")),
                    input=dict(item.get("input") or {}),
                )
            )
        elif kind == "tool_result":
            content_raw = item.get("content") or []
            content = tuple(
                TextBlock(text=str(part.get("text", "")))
                for part in content_raw
                if isinstance(part, Mapping)
            )
            blocks.append(
                ToolResultBlock(
                    call_id=str(item.get("call_id", "")),
                    content=content,
                    is_error=bool(item.get("is_error", False)),
                )
            )
        else:
            raise MessageError(f"unknown block type {kind!r}")
    return tuple(blocks)
