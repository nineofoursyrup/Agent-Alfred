"""Transport-agnostic Run read side: the runs page and the MainBar pairs.

Two reads, both over an injected connection, neither of which commits:

- :func:`list_runs` -- the runs page. Filtered by purpose, keyset paged on
  ``(activity_revision, run_id)``, with the one non-terminal Run returned
  *separately* so the client can pin it above the page and fold it away by
  ``run_id`` rather than showing it twice.
- :func:`mainbar_pairs` -- the MainBar's initial load: the unique user /
  assistant message pair of each **recorded** chat Run, newest activity
  first, 25 by default.

Both sort on ``activity_revision`` and nothing else. It is the persistent
activity clock's number and it is the only one of the three revisions that
orders *stored* state; ``seq`` and ``state_revision`` are in-process numbers
and comparing any of the three to another is meaningless (CONTEXT.md).

"Recorded" is decided here by the only thing that can decide it (ADR-0024):
the finalizing transaction wrote the Run's phase and its message rows in one
commit, so a finished Run that *has* message rows is recorded and one that
has none is not. Nothing in the event stream, the trace or the replay ring is
consulted -- all three can be ahead of a database that never committed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from agent_alfred.messages import Message, blocks_from_jsonable, message_plain_text
from agent_alfred.redact import Redactor
from agent_alfred.runtime.cursor import (
    MalformedCursor,
)
from agent_alfred.runtime.cursor import (
    decode_cursor as _decode_cursor,
)
from agent_alfred.runtime.cursor import (
    encode_cursor as _encode_cursor,
)
from agent_alfred.runtime.sessions import SessionNotFound

_CURSOR_VERSION = 1
_RUNS_KIND = "runs"
_MAINBAR_KIND = "mainbar"
_SESSION_RUNS_KIND = "session_runs"

# The runs page's filter is closed to three values (#30): a Run is either a
# conversation, a system run, or it is included because the filter is "all".
CHAT_FILTER = "chat"
SYSTEM_FILTER = "system"
ALL_FILTER = "all"
RUN_FILTERS = (CHAT_FILTER, SYSTEM_FILTER, ALL_FILTER)

DEFAULT_RUN_PAGE_SIZE = 25
DEFAULT_MAINBAR_LIMIT = 25

_TERMINAL_PHASE = "finished"


class UnknownRunFilter(ValueError):
    """The requested filter is not one of the three closed values."""


# ``MalformedCursor`` is the shared codec's exception, re-exported under this
# module's name so a caller keeps one name for one failure.


@dataclass(frozen=True)
class RunSummary:
    """One row of the runs page. Never carries a reply.

    No reply text and no telemetry live here: the runs page shows what a Run
    *is* and where it got to. Its content is the run detail's job, and the
    MainBar's pair is a different read with a different shape.
    """

    run_id: str
    purpose: str
    # Which shelf this Run belongs to, decided by the server. The client
    # renders this rather than re-deriving it, so an unknown purpose is
    # handled once, here, instead of in every surface that lists Runs.
    filter: str
    # False for a purpose this build does not recognise. The value is still
    # returned verbatim -- a client that shows "unknown" next to a real value
    # is honest, one that shows nothing is hiding something.
    purpose_known: bool
    session_id: str | None
    gateway: str
    entry_surface_id: str | None
    prompt_preview: str | None
    phase: str
    outcome: str | None
    accepted_at: str
    started_at: str | None
    finished_at: str | None
    activity_revision: int


@dataclass(frozen=True)
class RunPage:
    filter: str
    runs: tuple[RunSummary, ...]
    # The one Run that is not terminal yet, if this filter has one. It is not
    # in ``runs``: an unfinished Run keeps taking new activity revisions, so
    # paging over it would move it under the cursor. Pinning it and folding
    # by ``run_id`` is what keeps it from appearing twice.
    non_terminal: RunSummary | None
    next_cursor: str | None


@dataclass(frozen=True)
class MainBarPair:
    """The unique user/assistant pair of one recorded chat Run.

    Either side may be missing: an interrupted Run produces no assistant
    message by contract, and a pair with nothing on either side is not a pair
    anybody is shown.
    """

    run_id: str
    activity_revision: int
    session_id: str | None
    created_at: str | None
    user_message: Message | None
    assistant_message: Message | None


@dataclass(frozen=True)
class MainBarPage:
    pairs: tuple[MainBarPair, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class SessionChatRun:
    """One admitted chat Run, as a Session's own group shows it.

    The row is what the database holds about the Run -- its phase, its times,
    and, only for a Run whose recording committed, the one final reply. A Run
    still running has no reply yet and its row says so by carrying none.
    """

    run_id: str
    phase: str
    outcome: str | None
    accepted_at: str
    started_at: str | None
    finished_at: str | None
    activity_revision: int
    reply_preview: str | None
    reply_source: str | None


@dataclass(frozen=True)
class SessionChatRunsPage:
    session_id: str
    runs: tuple[SessionChatRun, ...]
    next_cursor: str | None


def classify_purpose(purpose: str) -> tuple[str, bool]:
    """Which shelf a purpose belongs to, and whether we recognise it.

    ``chat`` is the conversation shelf and everything else is a system run --
    including purposes this build has never heard of. The alternative, hiding
    an unknown value, would make a system Run invisible rather than oddly
    named.
    """
    from agent_alfred.schema import PURPOSES

    if purpose == CHAT_FILTER:
        return CHAT_FILTER, True
    return SYSTEM_FILTER, purpose in PURPOSES


# --- cursor codec -----------------------------------------------------------
#
# The envelope -- canonical JSON, URL-safe base64, the malformed exception --
# is the shared codec's (``runtime.cursor``); this read owns only its
# version, its kinds, and its position fields.


def _decode_read_cursor(cursor: str, kind: str) -> dict[str, Any]:
    return _decode_cursor(cursor, version=_CURSOR_VERSION, kind=kind)


def _position(payload: dict[str, Any]) -> tuple[int, str] | None:
    ar = payload.get("ar")
    run_key = payload.get("r")
    if ar is None and run_key is None:
        return None
    if not isinstance(ar, int) or not isinstance(run_key, str):
        raise MalformedCursor("cursor position is malformed")
    return (ar, run_key)


def _runs_cursor(position: tuple[int, str] | None) -> str:
    payload: dict[str, Any] = {"v": _CURSOR_VERSION, "k": _RUNS_KIND}
    if position is not None:
        payload["ar"] = position[0]
        payload["r"] = position[1]
    return _encode_cursor(payload)


def _mainbar_cursor(session_id: str, position: tuple[int, str] | None) -> str:
    payload: dict[str, Any] = {
        "v": _CURSOR_VERSION,
        "k": _MAINBAR_KIND,
        "s": session_id,
    }
    if position is not None:
        payload["ar"] = position[0]
        payload["r"] = position[1]
    return _encode_cursor(payload)


# --- the runs page ----------------------------------------------------------

_COLUMNS = """run_id, purpose, session_id, gateway, entry_surface_id,
              prompt_preview, phase, outcome, accepted_at, started_at,
              finished_at, activity_revision"""


def _row_to_summary(row) -> RunSummary:
    purpose = row[1]
    shelf, known = classify_purpose(purpose)
    preview = row[5]
    return RunSummary(
        run_id=row[0],
        purpose=purpose,
        filter=shelf,
        purpose_known=known,
        session_id=row[2],
        gateway=row[3],
        entry_surface_id=row[4],
        prompt_preview=preview,
        phase=row[6],
        outcome=row[7],
        accepted_at=row[8],
        started_at=row[9],
        finished_at=row[10],
        activity_revision=row[11],
    )


def _non_terminal_run(conn, purpose_clause: str) -> RunSummary | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM runs\n"
        f"  WHERE phase != ? {purpose_clause}\n"
        "  ORDER BY activity_revision DESC, run_id DESC LIMIT 1",
        (_TERMINAL_PHASE,),
    ).fetchone()
    return None if row is None else _row_to_summary(row)


def list_runs(
    conn,
    *,
    redactor: Redactor,
    filter: str = ALL_FILTER,
    limit: int = DEFAULT_RUN_PAGE_SIZE,
    cursor: str | None = None,
) -> RunPage:
    """The runs page: terminal Runs newest first, plus the pinned live Run."""
    if filter not in RUN_FILTERS:
        raise UnknownRunFilter(f"unknown run filter {filter!r}")
    if limit < 1:
        raise ValueError("limit must be >= 1")
    position: tuple[int, str] | None = None
    if cursor is not None:
        position = _position(_decode_read_cursor(cursor, _RUNS_KIND))

    if filter == CHAT_FILTER:
        purpose_clause = "AND purpose = 'chat'"
    elif filter == SYSTEM_FILTER:
        purpose_clause = "AND purpose != 'chat'"
    else:
        purpose_clause = ""

    # Only terminal Runs are paged. A non-terminal Run keeps acquiring new
    # activity revisions, so leaving it in the page would let the cursor
    # walk past it and show it again -- the duplicate the pin exists to
    # prevent.
    beyond = (
        "AND (activity_revision < ?"
        " OR (activity_revision = ? AND run_id < ?))\n"
        if position is not None
        else ""
    )
    params: tuple[Any, ...]
    params = (_TERMINAL_PHASE,)
    if position is not None:
        params += (position[0], position[0], position[1])
    params += (limit + 1,)
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM runs\n"
        f"  WHERE phase = ? {purpose_clause}\n  {beyond}"
        "  ORDER BY activity_revision DESC, run_id DESC LIMIT ?",
        params,
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    summaries = tuple(
        _redact_summary(_row_to_summary(row), redactor) for row in rows
    )
    next_cursor = (
        _runs_cursor((rows[-1][11], rows[-1][0])) if has_more and rows else None
    )
    return RunPage(
        filter=filter,
        runs=summaries,
        non_terminal=_redact_summary(
            _non_terminal_run(conn, purpose_clause), redactor
        ),
        next_cursor=next_cursor,
    )


def _redact_summary(
    summary: RunSummary | None, redactor: Redactor
) -> RunSummary | None:
    """Re-redact the preview on the way out.

    It was redacted when the Run was accepted; doing it again here is
    defence in depth under ADR-0003, and it is idempotent -- a remembered
    secret is already a marker, which no later pass can re-match.
    """
    if summary is None or summary.prompt_preview is None:
        return summary
    return replace(summary, prompt_preview=redactor.redact_text(
        summary.prompt_preview
    ))


def locate_run(
    conn,
    *,
    run_id: str,
    redactor: Redactor,
    limit: int = DEFAULT_RUN_PAGE_SIZE,
) -> RunPage | None:
    """The page a deep link should open on for one Run.

    The server decides the filter -- a deep link into a system Run must not
    land on the conversation shelf and show "no results" -- and returns a
    page positioned so the Run is the *last* item, so continuing from the
    returned cursor walks forward without repeating it.
    """
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        return None
    summary = _row_to_summary(row)
    if summary.phase != _TERMINAL_PHASE:
        # It is the pinned Run, not a page row: it has no stable position to
        # page to.
        return RunPage(
            filter=summary.filter,
            runs=(),
            non_terminal=_redact_summary(summary, redactor),
            next_cursor=None,
        )
    if summary.filter == CHAT_FILTER:
        purpose_clause = "AND purpose = 'chat'"
    else:
        purpose_clause = "AND purpose != 'chat'"
    # The newest ``limit - 1`` terminal Runs that are newer than the target,
    # taken nearest-first and then turned back around, so the page ends on
    # the target itself. Bounded either way: a deep link into the ten
    # thousandth Run still returns one page, not ten thousand rows.
    older = conn.execute(
        f"SELECT {_COLUMNS} FROM runs\n"
        "  WHERE phase = ? "
        + purpose_clause
        + "\n  AND (activity_revision > ?"
        " OR (activity_revision = ? AND run_id > ?))\n"
        "  ORDER BY activity_revision ASC, run_id ASC LIMIT ?",
        (
            _TERMINAL_PHASE,
            summary.activity_revision,
            summary.activity_revision,
            run_id,
            max(0, limit - 1),
        ),
    ).fetchall()
    runs = [_redact_summary(_row_to_summary(r), redactor) for r in reversed(older)]
    runs.append(_redact_summary(summary, redactor))
    # The cursor sits on the target, so continuing from here walks forward
    # into whatever came after it -- and never repeats it.
    beyond = conn.execute(
        "SELECT 1 FROM runs\n"
        "  WHERE phase = ? "
        + purpose_clause
        + "\n  AND (activity_revision < ?"
        " OR (activity_revision = ? AND run_id < ?)) LIMIT 1",
        (_TERMINAL_PHASE, summary.activity_revision, summary.activity_revision, run_id),
    ).fetchone()
    return RunPage(
        filter=summary.filter,
        runs=tuple(runs),
        non_terminal=_redact_summary(_non_terminal_run(conn, purpose_clause), redactor),
        next_cursor=(
            _runs_cursor((summary.activity_revision, run_id))
            if beyond is not None
            else None
        ),
    )


# --- the MainBar pairs ------------------------------------------------------


def mainbar_pairs(
    conn,
    *,
    session_id: str,
    redactor: Redactor,
    limit: int = DEFAULT_MAINBAR_LIMIT,
    cursor: str | None = None,
) -> MainBarPage:
    """The unique message pair of each recorded chat Run of one Session.

    Each tab's MainBar is that tab's Session's only conversation, so the
    Session is part of the question, not a post-filter: the SQL carries it,
    and the cursor names it, so a page minted by Session A answers nothing
    for Session B.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    exists = conn.execute(
        "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if exists is None:
        raise SessionNotFound(f"no such session: {session_id!r}")
    position: tuple[int, str] | None = None
    if cursor is not None:
        payload = _decode_read_cursor(cursor, _MAINBAR_KIND)
        if payload.get("s") != session_id:
            raise MalformedCursor("cursor belongs to a different session")
        position = _position(payload)
    beyond = (
        "AND (runs.activity_revision < ?"
        " OR (runs.activity_revision = ? AND runs.run_id < ?))\n"
        if position is not None
        else ""
    )
    params: tuple[Any, ...] = (_TERMINAL_PHASE, session_id)
    if position is not None:
        params += (position[0], position[0], position[1])
    params += (limit + 1,)
    # EXISTS on agent_log is what makes "recorded" a database fact: the
    # finalize transaction wrote the phase and the messages together, so a
    # finished Run with rows is a Run whose recording committed.
    rows = conn.execute(
        "SELECT runs.run_id, runs.session_id, runs.activity_revision\n"
        "  FROM runs\n"
        "  WHERE runs.phase = ? AND runs.purpose = 'chat'\n"
        "    AND runs.session_id = ?\n"
        "    AND EXISTS (SELECT 1 FROM agent_log\n"
        "                WHERE agent_log.run_id = runs.run_id)\n"
        f"  {beyond}"
        "  ORDER BY runs.activity_revision DESC, runs.run_id DESC LIMIT ?",
        params,
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    pairs = tuple(
        MainBarPair(
            run_id=run_id,
            activity_revision=revision,
            session_id=session_id,
            created_at=_created_at(conn, run_id),
            user_message=_message(conn, run_id, "user", redactor),
            assistant_message=_message(conn, run_id, "assistant", redactor),
        )
        for run_id, session_id, revision in rows
    )
    next_cursor = (
        _mainbar_cursor(session_id, (rows[-1][2], rows[-1][0]))
        if has_more and rows
        else None
    )
    return MainBarPage(pairs=pairs, next_cursor=next_cursor)


def _created_at(conn, run_id: str) -> str | None:
    """The time text of this Run's *first* message row.

    Ordered by ``id``, not by ``MIN(created_at)``: ``agent_log.created_at``
    was never a strict instant (ADR-0027), so a lexicographic minimum can
    name a row that is not the earliest one. ``id`` is the only column here
    that is genuinely monotonic.
    """
    row = conn.execute(
        "SELECT created_at FROM agent_log WHERE run_id = ? ORDER BY id ASC LIMIT 1",
        (run_id,),
    ).fetchone()
    return None if row is None else row[0]


def _message(
    conn, run_id: str, role: str, redactor: Redactor
) -> Message | None:
    row = conn.execute(
        "SELECT content FROM agent_log WHERE run_id = ? AND role = ?",
        (run_id, role),
    ).fetchone()
    if row is None:
        return None
    # The same central pass the session view uses, and the same reason: the
    # user message was stored verbatim, so this read is the last chance to
    # stop a secret reaching a surface (ADR-0003).
    parsed = json.loads(row[0])
    parsed = redactor.redact_jsonable(parsed)
    return Message(role=role, blocks=tuple(blocks_from_jsonable(parsed)))


# --- one Session's chat Runs -------------------------------------------------

DEFAULT_REPLY_PREVIEW_CHARS = 240


def list_session_chat_runs(
    conn,
    *,
    session_id: str,
    limit: int,
    redactor: Redactor,
    cursor: str | None = None,
    reply_max_chars: int = DEFAULT_REPLY_PREVIEW_CHARS,
) -> SessionChatRunsPage:
    """One Session's admitted chat Runs, newest activity first, keyset paged.

    The inbox returns Session summaries and the messages read returns message
    bodies; this is the group between them -- one row per Run the Session has
    been admitted, whether or not it has recorded yet, so a running Run is a
    row too instead of a hole in the group. Recorded is still only ever the
    database's own fact (ADR-0024), decided by the same finalize transaction
    that the MainBar reads; a reply is shown only when that fact holds.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if reply_max_chars < 1:
        raise ValueError("reply_max_chars must be >= 1")
    exists = conn.execute(
        "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if exists is None:
        raise SessionNotFound(f"no such session: {session_id!r}")
    position: tuple[int, str] | None = None
    if cursor is not None:
        payload = _decode_read_cursor(cursor, _SESSION_RUNS_KIND)
        if payload.get("s") != session_id:
            raise MalformedCursor("cursor belongs to a different session")
        position = _position(payload)
    beyond = (
        "AND (runs.activity_revision < ?"
        " OR (runs.activity_revision = ? AND runs.run_id < ?))\n"
        if position is not None
        else ""
    )
    params: tuple[Any, ...] = (session_id,)
    if position is not None:
        params += (position[0], position[0], position[1])
    params += (limit + 1,)
    rows = conn.execute(
        "SELECT runs.run_id, runs.phase, runs.outcome, runs.accepted_at,\n"
        "       runs.started_at, runs.finished_at, runs.activity_revision\n"
        "  FROM runs\n"
        "  WHERE runs.session_id = ? AND runs.purpose = 'chat'\n"
        f"  {beyond}"
        "  ORDER BY runs.activity_revision DESC, runs.run_id DESC LIMIT ?",
        params,
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    entries = tuple(
        SessionChatRun(
            run_id=run_id,
            phase=phase,
            outcome=outcome,
            accepted_at=accepted_at,
            started_at=started_at,
            finished_at=finished_at,
            activity_revision=revision,
            **_final_reply(conn, run_id, redactor, reply_max_chars),
        )
        for (
            run_id,
            phase,
            outcome,
            accepted_at,
            started_at,
            finished_at,
            revision,
        ) in rows
    )
    next_cursor = (
        _session_runs_cursor(session_id, (rows[-1][6], rows[-1][0]))
        if has_more and rows
        else None
    )
    return SessionChatRunsPage(
        session_id=session_id, runs=entries, next_cursor=next_cursor
    )


def _session_runs_cursor(session_id: str, position: tuple[int, str] | None) -> str:
    payload: dict[str, Any] = {
        "v": _CURSOR_VERSION,
        "k": _SESSION_RUNS_KIND,
        "s": session_id,
    }
    if position is not None:
        payload["ar"] = position[0]
        payload["r"] = position[1]
    return _encode_cursor(payload)


def _final_reply(
    conn, run_id: str, redactor: Redactor, max_chars: int
) -> dict[str, Any]:
    """The one final reply of a recorded Run, as ``reply_*`` fields.

    The finalize transaction wrote the phase and the messages together, so
    the last assistant row is the reply and nothing else may stand in for it
    -- a running Run has no rows and gets no summary. Redacted before it is
    previewed (ADR-0003), truncated after, so a marker is never cut.
    """
    row = conn.execute(
        "SELECT content, source FROM agent_log"
        " WHERE run_id = ? AND role = 'assistant'"
        " ORDER BY id DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if row is None:
        return {"reply_preview": None, "reply_source": None}
    parsed = json.loads(row[0])
    parsed = redactor.redact_jsonable(parsed)
    text = message_plain_text(
        Message(role="assistant", blocks=tuple(blocks_from_jsonable(parsed)))
    )
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return {"reply_preview": text if text else None, "reply_source": row[1]}
