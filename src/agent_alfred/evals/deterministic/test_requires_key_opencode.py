"""Live OpenCode Go round-trip through RuntimeHost. Skipped without the key."""

from __future__ import annotations

import pytest

from agent_alfred.messages import message_plain_text
from agent_alfred.runtime.host import SubmitRequest
from agent_alfred.settings import OPENCODE_API_KEY_ENV

pytestmark = pytest.mark.requires_key(OPENCODE_API_KEY_ENV)


def test_opencode_go_chat_through_runtime_host(tmp_path) -> None:
    from agent_alfred.wiring import build_default_host

    host = build_default_host(state_dir=tmp_path)
    host.start()
    try:
        session_id = host.create_session()
        submitted = host.submit(
            SubmitRequest(
                message="Reply with the single word pong.",
                session_id=session_id,
            )
        )
        assert submitted.kind == "accepted"
        assert submitted.run_id is not None
        result = host.wait(submitted.run_id, timeout=120)
        assert result.outcome == "completed"
        assert result.reply is not None
        text = message_plain_text(result.reply)
        assert text.strip()
        print("opencode round-trip outcome=", result.outcome, "chars=", len(text))
    finally:
        host.close()
