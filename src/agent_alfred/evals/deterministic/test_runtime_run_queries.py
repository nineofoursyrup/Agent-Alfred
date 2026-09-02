"""The shared Run query owner, exercised through its public interface."""

from __future__ import annotations

import sqlite3

from agent_alfred import schema
from agent_alfred.runtime.run_queries import (
    has_inflight_chat_run,
    page_recorded_chat_run_keys,
)


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    schema.migrate(conn)
    conn.execute(
        "INSERT INTO sessions "
        "(session_id, created_at, activity_revision) VALUES (?, ?, ?)",
        ("session-1", "2026-01-01T00:00:00Z", 1),
    )
    return conn


def _run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    purpose: str = "chat",
    phase: str = "accepted",
    revision: int,
) -> None:
    conn.execute(
        "INSERT INTO runs (run_id, purpose, gateway, phase, outcome, "
        "session_id, prompt_preview, accepted_at, started_at, finished_at, "
        "telemetry, activity_revision) "
        "VALUES (?, ?, 'web', ?, ?, 'session-1', NULL, "
        "'2026-01-01T00:00:00Z', NULL, NULL, NULL, ?)",
        (
            run_id,
            purpose,
            phase,
            "completed" if phase == "finished" else None,
            revision,
        ),
    )


def test_inflight_query_observes_phase_session_position_and_failed_projection() -> None:
    conn = _database()
    _run(conn, "accepted", revision=2)
    _run(conn, "running", phase="running", revision=3)
    _run(conn, "system", purpose="inference_probe", revision=4)
    _run(conn, "terminal", phase="finished", revision=5)

    assert has_inflight_chat_run(
        conn,
        session_id="session-1",
        position=None,
        recording_failed_run_ids=frozenset(),
    )
    assert has_inflight_chat_run(
        conn,
        session_id="session-1",
        position=(2, "accepted"),
        recording_failed_run_ids=frozenset(),
    )
    assert not has_inflight_chat_run(
        conn,
        session_id="session-1",
        position=(3, "running"),
        recording_failed_run_ids=frozenset(),
    )
    assert not has_inflight_chat_run(
        conn,
        session_id="session-1",
        position=None,
        recording_failed_run_ids=frozenset({"accepted", "running"}),
    )
    assert not has_inflight_chat_run(
        conn,
        session_id="other-session",
        position=None,
        recording_failed_run_ids=frozenset(),
    )


def test_recorded_run_key_page_is_ascending_keyset_bounded() -> None:
    conn = _database()
    for run_id, revision in (("run-b", 2), ("run-a", 2), ("run-c", 3)):
        _run(conn, run_id, phase="finished", revision=revision)
        conn.execute(
            "INSERT INTO agent_log "
            "(session_id, role, content, source, telemetry, created_at, run_id) "
            "VALUES ('session-1', 'user', '[]', 'web', NULL, "
            "'2026-01-01T00:00:00Z', ?)",
            (run_id,),
        )
    _run(conn, "no-message", phase="finished", revision=4)

    assert page_recorded_chat_run_keys(
        conn, session_id="session-1", position=None, count=2
    ) == [(2, "run-a"), (2, "run-b")]
    assert page_recorded_chat_run_keys(
        conn, session_id="session-1", position=(2, "run-a"), count=3
    ) == [(2, "run-b"), (3, "run-c")]
