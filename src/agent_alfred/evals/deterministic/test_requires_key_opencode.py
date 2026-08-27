"""Live OpenCode Go round-trip. Skipped unless OPENCODE_API_KEY is set."""

from __future__ import annotations

import os

import pytest

from agent_alfred.settings import OPENCODE_API_KEY_ENV

pytestmark = pytest.mark.requires_key(OPENCODE_API_KEY_ENV)


def test_opencode_go_chat_completions_round_trip() -> None:
    from openai import OpenAI

    from agent_alfred.settings import DEFAULT_MODEL_ID, OPENCODE_GO_BASE_URL

    key = os.environ[OPENCODE_API_KEY_ENV]
    client = OpenAI(base_url=OPENCODE_GO_BASE_URL, api_key=key)
    response = client.chat.completions.create(
        model=DEFAULT_MODEL_ID,
        messages=[{"role": "user", "content": "Reply with the single word pong."}],
        max_tokens=16,
        timeout=60,
    )
    text = response.choices[0].message.content or ""
    assert text.strip()
    assert response.id
