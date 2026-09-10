"""Versioned provider-independent input measurement and preparation failures."""

import json

from agent_alfred.messages import blocks_to_jsonable
from agent_alfred.model import NamedToolChoice, tool_schema_jsonable

INPUT_VERSION = "request-input-v1"


def serialize_input(request):
    """Only input-bearing fields belong here; credentials and generation do not."""
    return json.dumps(
        {
            "version": INPUT_VERSION,
            "system": blocks_to_jsonable(request.system or ()),
            "messages": [
                {"role": message.role, "blocks": blocks_to_jsonable(message.blocks)}
                for message in request.messages
            ],
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool_schema_jsonable(tool.input_schema),
                }
                for tool in request.tools
            ],
            "tool_choice": (
                {"name": request.tool_choice.name}
                if isinstance(request.tool_choice, NamedToolChoice)
                else request.tool_choice
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def input_characters(request):
    return len(serialize_input(request))


class InputLimitExceeded(Exception):
    """Required input cannot fit; no implicit retry or transcript compression."""

    def __init__(self, characters, limit, reserved=0):
        self.characters = characters
        self.limit = limit
        self.reserved = reserved
        super().__init__(
            f"输入超限：已有 {characters} 字符，预留 {reserved} 字符，"
            f"上限 {limit} 字符。请缩短输入或提高输入字符上限。"
        )
