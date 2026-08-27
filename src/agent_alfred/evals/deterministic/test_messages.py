"""Lock the Message validating constructor."""

from __future__ import annotations

import pytest

from agent_alfred.messages import (
    Message,
    MessageError,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    text_message,
)


def test_user_message_rejects_thinking_and_tool_call_blocks() -> None:
    with pytest.raises(MessageError):
        Message(role="user", blocks=(ThinkingBlock(text="secret"),))
    with pytest.raises(MessageError):
        Message(
            role="user",
            blocks=(ToolCallBlock(id="c1", name="x", input={}),),
        )


def test_assistant_message_rejects_tool_result_blocks() -> None:
    with pytest.raises(MessageError):
        Message(
            role="assistant",
            blocks=(
                ToolResultBlock(call_id="c1", content=(TextBlock("ok"),)),
            ),
        )


def test_tool_results_must_precede_text_in_a_user_message() -> None:
    with pytest.raises(MessageError, match="before any text"):
        Message(
            role="user",
            blocks=(
                TextBlock("here"),
                ToolResultBlock(call_id="c1", content=(TextBlock("ok"),)),
            ),
        )
    ok = Message(
        role="user",
        blocks=(
            ToolResultBlock(call_id="c1", content=(TextBlock("ok"),)),
            TextBlock("thanks"),
        ),
    )
    assert ok.role == "user"


def test_text_message_helper() -> None:
    msg = text_message("assistant", "hello")
    assert msg.blocks == (TextBlock("hello"),)
