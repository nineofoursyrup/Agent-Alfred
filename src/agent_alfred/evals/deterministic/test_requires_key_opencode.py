"""Live OpenCode Go round-trip through RuntimeHost. Skipped without the key.

Deselected by default (``-m "not requires_key"``), so this never becomes a CI
requirement. It is the ticket's real-model acceptance: one real conversation
through the production wiring, ending in a redacted evidence block that can be
pasted into the issue.

Every field it prints is chosen so the block is safe to publish: the API key
is only ever read from the environment and is passed through the Redactor
before any text leaves the process; no Authorization header, no absolute path
(this process' state directory included), and no unredacted prompt or reply
body -- only lengths, counts, and the redacted previews the store keeps.

Run it with the key supplied by your shell, never by a file in the repo:

    OPENCODE_API_KEY="$OPENCODE_API_KEY" \\
      .venv/bin/pytest \\
      src/agent_alfred/evals/deterministic/test_requires_key_opencode.py \\
      -m requires_key -vv -s
"""

from __future__ import annotations

import json
import os

import pytest

from agent_alfred.messages import message_plain_text
from agent_alfred.runtime.host import SubmitRequest
from agent_alfred.settings import (
    DEFAULT_MODEL_ID,
    OPENCODE_API_KEY_ENV,
    OPENCODE_GO_BASE_URL,
)

pytestmark = pytest.mark.requires_key(OPENCODE_API_KEY_ENV)

PROMPT = "Reply with the single word pong."


def test_opencode_go_chat_through_runtime_host(tmp_path) -> None:
    from agent_alfred.wiring import build_default_host

    host = build_default_host(state_dir=tmp_path)
    host.start()
    try:
        session_id = host.create_session()
        submitted = host.submit(
            SubmitRequest(message=PROMPT, session_id=session_id)
        )
        assert submitted.kind == "accepted"
        assert submitted.run_id is not None
        result = host.wait(submitted.run_id, timeout=180)
        assert result.outcome == "completed", (
            "the live round-trip must complete. A keyed test that only says "
            "'failed' is unusable: the two real-world blockers -- a dead "
            "endpoint and an unfunded account -- look identical from here, "
            "so the wire's own answer is reported. "
            f"outcome={result.outcome} error={result.error} "
            f"steps={result.step_count} {_wire_answer(host, result)}"
        )
        assert result.reply is not None
        text = message_plain_text(result.reply)
        assert text.strip()

        evidence = _collect_evidence(
            host,
            tmp_path,
            run_id=submitted.run_id,
            session_id=session_id,
            outcome=result.outcome,
            reply_text=text,
        )
        print("\n--- opencode-go live evidence ---")
        print(json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=False))
        print("--- end evidence ---")

        assert evidence["endpoint"]["base_url"] == OPENCODE_GO_BASE_URL
        assert evidence["model"]["model_id"] == DEFAULT_MODEL_ID
        assert evidence["submission"]["kind"] == "accepted"
        assert evidence["run"]["outcome"] == "completed"
        assert evidence["run"]["reply_chars"] > 0
        assert evidence["attempts"]["count"] >= 1
        assert evidence["trace"]["bundle_published"] is True
        assert evidence["trace"]["payload_names"][-1] == "run.finished"
        assert evidence["persistence"]["agent_log_rows"] == 2
        assert evidence["restart"]["session_messages"] == 2
        # The one thing that must never appear anywhere in the block.
        key = os.environ[OPENCODE_API_KEY_ENV]
        assert key not in json.dumps(evidence)
    finally:
        host.close()


def _collect_evidence(
    host,
    state_dir,
    *,
    run_id: str,
    session_id: str,
    outcome: str,
    reply_text: str,
) -> dict:
    """Assemble the publishable facts about one live Run."""
    conn = host._conn
    row = conn.execute(
        """SELECT purpose, phase, outcome, started_at, finished_at, telemetry,
                  prompt_preview
             FROM runs WHERE run_id = ?""",
        (run_id,),
    ).fetchone()
    telemetry = json.loads(row[5]) if row[5] else {"attempts": []}
    attempts = telemetry.get("attempts", [])

    logs = conn.execute(
        "SELECT role, source FROM agent_log WHERE run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall()

    bundle = _published_bundle(state_dir)
    payload_names: list[str] = []
    trace_lines = 0
    if bundle is not None:
        for line in (bundle / "trace.jsonl").read_text(
            encoding="utf-8"
        ).splitlines():
            if not line.strip():
                continue
            trace_lines += 1
            payload_names.append(json.loads(line).get("payload_name", "?"))

    restart = _reread_after_restart(state_dir, session_id)

    return {
        "endpoint": {
            "endpoint_id": host.settings.endpoint_id,
            "base_url": OPENCODE_GO_BASE_URL,
            "api_key_env": OPENCODE_API_KEY_ENV,
            "api_key_present": True,
        },
        "model": {
            "model_id": host.settings.model_id,
            "wire_style": host.settings.wire_style,
        },
        "submission": {"kind": "accepted", "run_id": run_id[:8] + "…"},
        "run": {
            "purpose": row[0],
            "phase": row[1],
            "outcome": row[2],
            "settled_outcome": outcome,
            "started_at": row[3],
            "finished_at": row[4],
            "prompt_preview": row[6],
            "prompt_chars": len(PROMPT),
            "reply_chars": len(reply_text),
            "reply_excerpt": _redacted(host, reply_text)[:120],
        },
        "attempts": {
            "count": len(attempts),
            "attempt_ids": [attempt["attempt_id"][:8] + "…" for attempt in attempts],
            "outcomes": [attempt["outcome"] for attempt in attempts],
            "output_tokens": [
                attempt["usage"]["output_tokens"] for attempt in attempts
            ],
            "total_input_tokens": [
                attempt["usage"]["total_input_tokens"] for attempt in attempts
            ],
        },
        "trace": {
            "trace_incomplete": telemetry.get("trace_incomplete"),
            "bundle_published": bundle is not None,
            "bundle_dir_name": None if bundle is None else bundle.name,
            "trace_lines": trace_lines,
            "payload_names": payload_names,
        },
        "persistence": {
            "run_row_phase": row[1],
            "agent_log_rows": len(logs),
            "agent_log_roles": [entry[0] for entry in logs],
            "agent_log_sources": sorted({entry[1] for entry in logs}),
        },
        "restart": restart,
    }


def _reread_after_restart(state_dir, session_id: str) -> dict:
    """A fresh Host over the same state directory is the restart."""
    from agent_alfred.wiring import build_default_host

    restarted = build_default_host(state_dir=state_dir)
    restarted.start()
    try:
        page = restarted.open_session(session_id, page_size=10)
        return {
            "session_id": page.session_id[:8] + "…",
            "title": page.title,
            "session_messages": len(page.messages),
            "roles": [message.role for message in page.messages],
        }
    finally:
        restarted.close()


def _published_bundle(state_dir):
    traces = state_dir / "traces"
    if not traces.is_dir():
        return None
    published = [
        path
        for date_dir in sorted(traces.iterdir())
        for path in sorted(date_dir.iterdir())
        if path.is_dir() and not path.name.startswith(".")
    ]
    return published[-1] if published else None


def _redacted(host, text: str) -> str:
    """Route text through the Host's own Redactor before it is printed."""
    try:
        return host._redactor.redact_text(text)
    except Exception:  # noqa: BLE001 - fail closed, print nothing
        return "<redaction failed; text withheld>"


def _wire_answer(host, result) -> str:
    """What the endpoint itself said about the failed request, redacted.

    ``outcome='failed'`` covers an unreachable host, a rejected key, and an
    empty balance; only the wire's own status code and body tell them apart,
    and that pair is what decides whether to re-run or to top up.
    """
    errors: list[str] = []
    for model_result in getattr(result, "model_results", ()) or ():
        final_error = getattr(model_result, "final_error", None)
        if final_error is None:
            continue
        errors.append(
            "status={} code={} body={}".format(
                getattr(final_error, "status_code", None),
                getattr(final_error, "code", None),
                _redacted(host, str(getattr(final_error, "body_excerpt", None))),
            )
        )
    return ("wire=[" + "; ".join(errors) + "]") if errors else "wire=[no ModelError]"
