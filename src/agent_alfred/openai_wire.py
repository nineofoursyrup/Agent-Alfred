"""Pure OpenAI message encoding shared by transport and restricted IPC.

No provider SDK or TLS module is imported to project a ModelRequest.
"""

import json

from agent_alfred.messages import ThinkingBlock, ToolCallBlock, ToolResultBlock
from agent_alfred.model import ModelRequest


def to_wire_messages(request: ModelRequest) -> list[dict]:
    messages = []
    if request.system:
        messages.append(
            {"role": "system", "content": "\n\n".join(b.text for b in request.system)}
        )
    for message in request.messages:
        texts = []
        calls = []
        reasoning = []
        for block in message.blocks:
            if isinstance(block, ToolResultBlock):
                content = "\n".join(b.text for b in block.content)
                if block.is_error:
                    content = '{"ok":false}\n' + content
                messages.append(
                    {"role": "tool", "tool_call_id": block.call_id, "content": content}
                )
            elif isinstance(block, ToolCallBlock):
                calls.append(
                    {
                        "id": block.id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": json.dumps(dict(block.input)),
                        },
                    }
                )
            elif isinstance(block, ThinkingBlock):
                reasoning.append(block.text)
            else:
                texts.append(block.text)
        if texts or calls or reasoning or not message.blocks:
            row = {"role": message.role, "content": "\n".join(texts) or None}
            if calls:
                row["tool_calls"] = calls
            if reasoning:
                row["reasoning_content"] = "\n".join(reasoning)
            messages.append(row)
    return messages

