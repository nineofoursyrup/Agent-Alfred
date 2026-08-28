"""Transport-agnostic session read side with the ADR-0027 segmented cursor.

Two public reads, both over an injected connection; callers never write SQL
and the functions never commit (the caller owns any transaction):

- :func:`list_sessions` -- the Session inbox, newest activity first, keyset
  paged on ``sessions.activity_revision`` (one of the three orthogonal
  revision numbers; never mixed with ``seq`` or ``state_revision``).
- :func:`open_session` -- one Session's messages, paged by the segmented
  cursor of ADR-0027: segment one returns the new-Run chat message pairs,
  stable-ordered by ``(activity_revision, run_id)``; once that segment is
  exhausted the cursor moves to segment two, which returns the historic
  ``run_id IS NULL`` messages, stable-ordered by ``agent_log.id``. Historic
  messages are never attributed to a fabricated Run.

The cursor is an opaque string that explicitly names its segment and position,
so a page never spans a segment boundary unmarked and reuse of a cursor
returns the same page again.
"""

from __future__ import annotations

import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from typing import Any

from agent_alfred.messages import (
    Block,
    Message,
    blocks_from_jsonable,
    message_plain_text,
)
from agent_alfred.redact import Redactor

_CURSOR_VERSION = 1
_INBOX_KIND = "inbox"
_RUNS_SEGMENT = "runs"
_HISTORIC_SEGMENT = "historic"


class SessionNotFound(ValueError):
    """The requested Session does not exist."""


class MalformedCursor(ValueError):
    """The cursor cannot be decoded or does not fit this read."""


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    created_at: str
    activity_revision: int
    title: str


@dataclass(frozen=True)
class SessionInboxPage:
    sessions: tuple[SessionSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class SessionMessage:
    role: str
    blocks: tuple[Block, ...]
    source: str
    created_at: str
    run_id: str | None
    # Historic rows may carry the legacy per-message telemetry verbatim; new
    # Runs keep telemetry in runs.telemetry, so this is None for them.
    telemetry: Any | None


@dataclass(frozen=True)
class SessionMessagesPage:
    session_id: str
    title: str
    messages: tuple[SessionMessage, ...]
    next_cursor: str | None


# --- cursor codec -----------------------------------------------------------


def _encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str, kind: str) -> dict[str, Any]:
    try:
        raw = urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        payload = json.loads(raw)
    except Exception:
        raise MalformedCursor("cursor is not a readable token") from None
    if (
        not isinstance(payload, dict)
        or payload.get("v") != _CURSOR_VERSION
        or payload.get("k") != kind
    ):
        raise MalformedCursor("cursor does not fit this read")
    return payload


# --- inbox ------------------------------------------------------------------


def list_sessions(
    conn,
    *,
    limit: int,
    cursor: str | None = None,
    redactor: Redactor | None = None,
    title_max_chars: int = 240,
) -> SessionInboxPage:
    """The Session inbox: newest persistent activity first, keyset paged."""
    if limit < 1:
        raise ValueError("limit must be >= 1")
    position: int | None = None
    if cursor is not None:
        payload = _decode_cursor(cursor, _INBOX_KIND)
        position = payload.get("ar")
        if not isinstance(position, int) or position < 0:
            raise MalformedCursor("cursor position is not an activity_revision")
    sql = (
        "SELECT session_id, created_at, activity_revision FROM sessions\n"
        "  {where} ORDER BY activity_revision DESC LIMIT ?"
    )
    where = "WHERE activity_revision < ? " if position is not None else ""
    params: tuple = (position, limit + 1) if position is not None else (limit + 1,)
    rows = conn.execute(sql.format(where=where), params).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    summaries = tuple(
        SessionSummary(
            session_id=session_id,
            created_at=created_at,
            activity_revision=revision,
            title=_session_title(
                conn, session_id, created_at, redactor, title_max_chars
            ),
        )
        for session_id, created_at, revision in rows
    )
    next_cursor = (
        _encode_cursor(
            {"v": _CURSOR_VERSION, "k": _INBOX_KIND, "ar": rows[-1][2]}
        )
        if has_more and rows
        else None
    )
    return SessionInboxPage(sessions=summaries, next_cursor=next_cursor)


def _session_title(
    conn,
    session_id: str,
    created_at: str,
    redactor: Redactor | None,
    limit: int,
) -> str:
    """Prompt preview of the earliest chat Run, else the first historic user
    message's user-visible text (redacted, limited), else the created-at
    fallback. Derived fresh on every read; nothing is stored."""
    run = conn.execute(
        """SELECT prompt_preview FROM runs
           WHERE session_id = ? AND purpose = 'chat'
           ORDER BY activity_revision ASC, run_id ASC LIMIT 1""",
        (session_id,),
    ).fetchone()
    if run is not None and run[0]:
        return run[0]
    try:
        row = conn.execute(
            """SELECT content FROM agent_log
               WHERE session_id = ? AND role = 'user'
               ORDER BY id ASC LIMIT 1""",
            (session_id,),
        ).fetchone()
        if row is not None:
            blocks = blocks_from_jsonable(json.loads(row[0]))
            text = message_plain_text(Message(role="user", blocks=blocks))
            if text:
                if redactor is not None:
                    text = redactor.redact_text(text)
                return _limit_text(text, limit)
    except Exception:
        pass
    return f"新会话 · {created_at}"


def _limit_text(text: str, limit: int) -> str:
    if limit < 1:
        raise ValueError("title_max_chars must be >= 1")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# --- one Session, segmented (ADR-0027) --------------------------------------


def open_session(
    conn,
    *,
    session_id: str,
    page_size: int,
    cursor: str | None = None,
    redactor: Redactor | None = None,
    title_max_chars: int = 240,
) -> SessionMessagesPage:
    """One Session's messages under the ADR-0027 segmented cursor.

    Page size counts items: Run message pairs in segment one, single historic
    messages in segment two. When segment one is exhausted the same page
    continues into segment two, so no empty intermediate pages exist; the
    returned cursor always names the segment (and position) the next page
    starts from, and re-using a cursor returns the same page again.
    """
    if page_size < 1:
        raise ValueError("page_size must be >= 1")
    exists = conn.execute(
        "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if exists is None:
        raise SessionNotFound(f"no such session: {session_id!r}")

    in_runs_segment = True
    runs_position: tuple[int, str] | None = None
    historic_position: int | None = None
    if cursor is not None:
        payload = _decode_cursor(cursor, _RUNS_SEGMENT)
        segment = payload.get("seg")
        if segment == _RUNS_SEGMENT:
            ar = payload.get("ar")
            run_key = payload.get("r")
            if not isinstance(ar, int) or not isinstance(run_key, str):
                raise MalformedCursor("runs cursor position is malformed")
            runs_position = (ar, run_key)
        elif segment == _HISTORIC_SEGMENT:
            last_id = payload.get("id")
            if not isinstance(last_id, int) or last_id < 0:
                raise MalformedCursor("historic cursor position is malformed")
            in_runs_segment = False
            historic_position = last_id
        else:
            raise MalformedCursor("unknown cursor segment")

    messages: list[SessionMessage] = []
    remaining = page_size

    if in_runs_segment:
        keys = _page_run_keys(conn, session_id, runs_position, remaining + 1)
        taken = keys[:remaining]
        for activity_revision, run_id in taken:
            messages.extend(_run_messages(conn, run_id))
            runs_position = (activity_revision, run_id)
            remaining -= 1
        if len(keys) > len(taken):
            # The page ends inside segment one; segment two comes later.
            return _page(
                conn,
                session_id,
                messages,
                _encode_cursor(
                    {
                        "v": _CURSOR_VERSION,
                        "k": _RUNS_SEGMENT,
                        "seg": _RUNS_SEGMENT,
                        "ar": runs_position[0],
                        "r": runs_position[1],
                    }
                ),
                redactor,
                title_max_chars,
            )
        # Segment one is exhausted; this page continues into segment two.

    next_cursor = _historic_tail(
        conn, session_id, historic_position, messages, remaining
    )
    return _page(
        conn, session_id, messages, next_cursor, redactor, title_max_chars
    )


def _historic_tail(
    conn,
    session_id: str,
    historic_position: int | None,
    messages: list[SessionMessage],
    remaining: int,
) -> str | None:
    """Fill the page from segment two and decide whether more of it remains.

    ``remaining == 0`` here means segment one filled the page exactly and is
    exhausted: the next page starts at segment two's current position, but
    only if it actually has a row to return -- a trailing empty page is never
    emitted.
    """
    if remaining > 0:
        rows = _page_historic(
            conn, session_id, historic_position, remaining + 1
        )
        taken = rows[:remaining]
        for row in taken:
            messages.append(_historic_message(row))
            historic_position = row[0]
        if len(rows) <= len(taken):
            return None
    else:
        historic_position = historic_position or 0
        has_more = conn.execute(
            """SELECT 1 FROM agent_log
               WHERE session_id = ? AND run_id IS NULL AND id > ? LIMIT 1""",
            (session_id, historic_position),
        ).fetchone()
        if has_more is None:
            return None
    return _encode_cursor(
        {
            "v": _CURSOR_VERSION,
            "k": _RUNS_SEGMENT,
            "seg": _HISTORIC_SEGMENT,
            "id": historic_position,
        }
    )


def _page(
    conn,
    session_id: str,
    messages: list[SessionMessage],
    next_cursor: str | None,
    redactor: Redactor | None,
    title_max_chars: int,
) -> SessionMessagesPage:
    return SessionMessagesPage(
        session_id=session_id,
        title=_session_title(
            conn,
            session_id,
            _created_at(conn, session_id),
            redactor,
            title_max_chars,
        ),
        messages=tuple(messages),
        next_cursor=next_cursor,
    )


def _created_at(conn, session_id: str) -> str:
    row = conn.execute(
        "SELECT created_at FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    return "" if row is None else row[0]


def _page_run_keys(
    conn, session_id: str, position: tuple[int, str] | None, count: int
) -> list[tuple[int, str]]:
    """Chat Runs of this Session that already carry messages, keyset paged.

    Runs are only written into the message pair at finalize, in the same
    transaction that stamps activity_revision, so an in-flight Run has no
    messages and no key -- and being the only in-flight Run it also holds the
    highest key, so it can only appear in a later page, never be skipped by
    one. The agent_log unique index guarantees at most one pair per Run.
    """
    sql = """
        SELECT runs.activity_revision, runs.run_id FROM runs
        WHERE runs.session_id = ?
          AND runs.purpose = 'chat'
          AND EXISTS (
            SELECT 1 FROM agent_log WHERE agent_log.run_id = runs.run_id
          )
          {keyset}
        ORDER BY runs.activity_revision ASC, runs.run_id ASC
        LIMIT ?
    """
    if position is None:
        rows = conn.execute(sql.format(keyset=""), (session_id, count)).fetchall()
    else:
        rows = conn.execute(
            sql.format(
                keyset=(
                    "AND (runs.activity_revision > ?"
                    " OR (runs.activity_revision = ? AND runs.run_id > ?))\n"
                )
            ),
            (session_id, position[0], position[0], position[1], count),
        ).fetchall()
    return [(row[0], row[1]) for row in rows]


def _run_messages(conn, run_id: str) -> list[SessionMessage]:
    rows = conn.execute(
        """SELECT role, content, source, telemetry, created_at, run_id
           FROM agent_log WHERE run_id = ? ORDER BY id ASC""",
        (run_id,),
    ).fetchall()
    return [
        SessionMessage(
            role=role,
            blocks=blocks_from_jsonable(json.loads(content)),
            source=source,
            created_at=created_at,
            run_id=run_id,
            telemetry=None if telemetry is None else json.loads(telemetry),
        )
        for role, content, source, telemetry, created_at, run_id in rows
    ]


def _page_historic(conn, session_id: str, after_id: int | None, count: int):
    sql = """
        SELECT id, role, content, source, telemetry, created_at, run_id
        FROM agent_log
        WHERE session_id = ? AND run_id IS NULL {after}
        ORDER BY id ASC LIMIT ?
    """
    if after_id is None:
        return conn.execute(sql.format(after=""), (session_id, count)).fetchall()
    return conn.execute(
        sql.format(after="AND id > ? "), (session_id, after_id, count)
    ).fetchall()


def _historic_message(row) -> SessionMessage:
    row_id, role, content, source, telemetry, created_at, run_id = row
    del row_id
    return SessionMessage(
        role=role,
        blocks=blocks_from_jsonable(json.loads(content)),
        source=source,
        created_at=created_at,
        run_id=run_id,
        telemetry=None if telemetry is None else json.loads(telemetry),
    )
