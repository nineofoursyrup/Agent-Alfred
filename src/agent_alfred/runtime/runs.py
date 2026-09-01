"""Transport-agnostic Run read side: the runs page and the MainBar items.

Two reads, both over an injected connection, neither of which commits:

- :func:`list_runs` -- the runs page. Filtered by purpose, keyset paged on
  ``(activity_revision, run_id)``, with the one non-terminal Run returned
  *separately* so the client can pin it above the page and fold it away by
  ``run_id`` rather than showing it twice.
- :func:`mainbar_pairs` -- the MainBar's initial load: **recorded** chat Run
  pairs newest-first, followed by historic messages without fabricated Runs.

Run-backed items sort on ``activity_revision`` and nothing else. It is the
persistent activity clock's number and it is the only one of the three
revisions that orders *stored* state; ``seq`` and ``state_revision`` are
in-process numbers and comparing any of the three to another is meaningless
(CONTEXT.md).

"Recorded" is decided here by the only thing that can decide it (ADR-0024):
the finalizing transaction wrote the Run's phase and its message rows in one
commit, so a finished Run that *has* message rows is recorded and one that
has none is not. Nothing in the event stream, the trace or the replay ring is
consulted -- all three can be ahead of a database that never committed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, NamedTuple, TypeAlias

from agent_alfred.messages import Message, blocks_from_jsonable, message_plain_text
from agent_alfred.outcomes import RunOutcome
from agent_alfred.redact import Redactor
from agent_alfred.run_phases import (
    TERMINAL_RUN_PHASE,
    RunPhase,
    parse_run_lifecycle_pair,
)
from agent_alfred.runtime.cursor import (
    MalformedCursor,
    parse_cursor_position_int,
)
from agent_alfred.runtime.cursor import (
    decode_cursor as _decode_cursor,
)
from agent_alfred.runtime.cursor import (
    encode_cursor as _encode_cursor,
)
from agent_alfred.runtime.sessions import SessionNotFound

_CURSOR_VERSION = 1
_MAINBAR_CURSOR_VERSION = 2
_RUNS_KIND = "runs"
_MAINBAR_KIND = "mainbar"
_SESSION_RUNS_KIND = "session_runs"
_RUNS_SEGMENT = "runs"
_HISTORIC_SEGMENT = "historic"

# The runs page's filter is closed to three values (#30): a Run is either a
# conversation, a system run, or it is included because the filter is "all".
CHAT_FILTER = "chat"
SYSTEM_FILTER = "system"
ALL_FILTER = "all"
RUN_FILTERS = (CHAT_FILTER, SYSTEM_FILTER, ALL_FILTER)

DEFAULT_RUN_PAGE_SIZE = 25
DEFAULT_MAINBAR_LIMIT = 25

_TERMINAL_PHASE = TERMINAL_RUN_PHASE


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
    phase: RunPhase
    outcome: RunOutcome | None
    accepted_at: str
    started_at: str | None
    finished_at: str | None
    activity_revision: int

    def __post_init__(self) -> None:
        phase, outcome = parse_run_lifecycle_pair(self.phase, self.outcome)
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "outcome", outcome)


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
class MainBarRunPair:
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


# Compatibility name for callers that still describe the Run arm as a pair.
MainBarPair = MainBarRunPair


@dataclass(frozen=True)
class MainBarHistoricMessage:
    """One pre-Run message, kept distinct from a recorded Run pair."""

    message: Message
    source: str
    created_at: str

    @property
    def run_id(self) -> None:
        """Historic messages can never be attributed to a Run."""
        return None


MainBarItem: TypeAlias = MainBarRunPair | MainBarHistoricMessage


@dataclass(frozen=True)
class MainBarPage:
    items: tuple[MainBarItem, ...]
    next_cursor: str | None

    @property
    def pairs(self) -> tuple[MainBarRunPair, ...]:
        """Read-only compatibility projection of the recorded Run arm."""
        return tuple(
            item for item in self.items if isinstance(item, MainBarRunPair)
        )


@dataclass(frozen=True)
class SessionChatRun:
    """One admitted chat Run, as a Session's own group shows it.

    The row is what the database holds about the Run -- its phase, its times,
    and, only for a Run whose recording committed, the one final reply. A Run
    still running has no reply yet and its row says so by carrying none.
    """

    run_id: str
    phase: RunPhase
    outcome: RunOutcome | None
    accepted_at: str
    started_at: str | None
    finished_at: str | None
    activity_revision: int
    reply_preview: str | None
    reply_source: str | None

    def __post_init__(self) -> None:
        phase, outcome = parse_run_lifecycle_pair(self.phase, self.outcome)
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "outcome", outcome)


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
    if not isinstance(run_key, str):
        raise MalformedCursor("cursor position is malformed")
    return (parse_cursor_position_int(ar), run_key)


def _runs_cursor(position: tuple[int, str] | None) -> str:
    payload: dict[str, Any] = {"v": _CURSOR_VERSION, "k": _RUNS_KIND}
    if position is not None:
        payload["ar"] = position[0]
        payload["r"] = position[1]
    return _encode_cursor(payload)


def _mainbar_runs_cursor(
    session_id: str, position: tuple[int, str] | None
) -> str:
    payload: dict[str, Any] = {
        "v": _MAINBAR_CURSOR_VERSION,
        "k": _MAINBAR_KIND,
        "seg": _RUNS_SEGMENT,
        "s": session_id,
    }
    if position is not None:
        payload["ar"] = position[0]
        payload["r"] = position[1]
    return _encode_cursor(payload)


def _mainbar_historic_cursor(session_id: str, position: int) -> str:
    return _encode_cursor(
        {
            "v": _MAINBAR_CURSOR_VERSION,
            "k": _MAINBAR_KIND,
            "seg": _HISTORIC_SEGMENT,
            "s": session_id,
            "id": position,
        }
    )


# --- the runs page ----------------------------------------------------------

_COLUMNS = """run_id, purpose, session_id, gateway, entry_surface_id,
              prompt_preview, phase, outcome, accepted_at, started_at,
              finished_at, activity_revision"""


class _RunRow(NamedTuple):
    run_id: str
    purpose: str
    session_id: str | None
    gateway: str
    entry_surface_id: str | None
    prompt_preview: str | None
    phase: object
    outcome: object
    accepted_at: str
    started_at: str | None
    finished_at: str | None
    activity_revision: int


def _row_to_summary(raw_row) -> RunSummary:
    row = _RunRow(*raw_row)
    purpose = row.purpose
    shelf, known = classify_purpose(purpose)
    phase, outcome = parse_run_lifecycle_pair(row.phase, row.outcome)
    return RunSummary(
        run_id=row.run_id,
        purpose=purpose,
        filter=shelf,
        purpose_known=known,
        session_id=row.session_id,
        gateway=row.gateway,
        entry_surface_id=row.entry_surface_id,
        prompt_preview=row.prompt_preview,
        phase=phase,
        outcome=outcome,
        accepted_at=row.accepted_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        activity_revision=row.activity_revision,
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
    rows = [_RunRow(*row) for row in rows[:limit]]
    summaries = tuple(
        _redact_summary(_row_to_summary(row), redactor) for row in rows
    )
    next_cursor = (
        _runs_cursor((rows[-1].activity_revision, rows[-1].run_id))
        if has_more and rows
        else None
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


class _MainBarRunRow(NamedTuple):
    run_id: str
    session_id: str | None
    activity_revision: int


def mainbar_pairs(
    conn,
    *,
    session_id: str,
    redactor: Redactor,
    limit: int = DEFAULT_MAINBAR_LIMIT,
    cursor: str | None = None,
) -> MainBarPage:
    """The recorded Run pairs then historic messages of one Session.

    Each tab's MainBar is that tab's Session's only conversation, so the
    Session is part of the question, not a post-filter: the SQL carries it,
    and the cursor names it, so a page minted by Session A answers nothing
    for Session B. Page size counts Run pairs and historic single messages;
    when the Run segment is exhausted, any room in that same page is filled
    from the historic segment so no empty transition page is produced.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    in_runs_segment = True
    runs_position: tuple[int, str] | None = None
    historic_position: int | None = None
    if cursor is not None:
        payload = _decode_cursor(
            cursor, version=_MAINBAR_CURSOR_VERSION, kind=_MAINBAR_KIND
        )
        if payload.get("s") != session_id:
            raise MalformedCursor("cursor belongs to a different session")
        segment = payload.get("seg")
        if segment == _RUNS_SEGMENT:
            runs_position = _position(payload)
        elif segment == _HISTORIC_SEGMENT:
            last_id = parse_cursor_position_int(payload.get("id"))
            in_runs_segment = False
            historic_position = last_id
        else:
            raise MalformedCursor("unknown cursor segment")
    exists = conn.execute(
        "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if exists is None:
        raise SessionNotFound(f"no such session: {session_id!r}")

    items: list[MainBarItem] = []
    remaining = limit
    if in_runs_segment:
        rows = _mainbar_run_rows(
            conn, session_id, runs_position, remaining + 1
        )
        taken = rows[:remaining]
        items.extend(
            MainBarRunPair(
                run_id=row.run_id,
                activity_revision=row.activity_revision,
                session_id=row.session_id,
                created_at=_created_at(conn, row.run_id),
                user_message=_message(conn, row.run_id, "user", redactor),
                assistant_message=_message(
                    conn, row.run_id, "assistant", redactor
                ),
            )
            for row in taken
        )
        remaining -= len(taken)
        if taken:
            last = taken[-1]
            runs_position = (last.activity_revision, last.run_id)
        if len(rows) > len(taken):
            return MainBarPage(
                items=tuple(items),
                next_cursor=_mainbar_runs_cursor(
                    session_id, runs_position
                ),
            )

    historic_rows = _mainbar_historic_rows(
        conn, session_id, historic_position, remaining + 1
    )
    taken_historic = historic_rows[:remaining]
    for row in taken_historic:
        row_id, role, content, source, created_at = row
        items.append(
            MainBarHistoricMessage(
                message=_stored_message(role, content, redactor),
                source=source,
                created_at=created_at,
            )
        )
        historic_position = row_id
    if len(historic_rows) > len(taken_historic):
        if not taken_historic and in_runs_segment:
            # No historic id has been consumed yet, so there is no honest
            # historic keyset position to sign. Replaying the exhausted Run
            # position starts the next page at the newest historic row.
            return MainBarPage(
                items=tuple(items),
                next_cursor=_mainbar_runs_cursor(session_id, runs_position),
            )
        return MainBarPage(
            items=tuple(items),
            next_cursor=_mainbar_historic_cursor(
                session_id, historic_position or 0
            ),
        )
    return MainBarPage(items=tuple(items), next_cursor=None)


def _mainbar_run_rows(
    conn,
    session_id: str,
    position: tuple[int, str] | None,
    count: int,
) -> list[_MainBarRunRow]:
    beyond = (
        "AND (runs.activity_revision < ?"
        " OR (runs.activity_revision = ? AND runs.run_id < ?))\n"
        if position is not None
        else ""
    )
    params: tuple[Any, ...] = (_TERMINAL_PHASE, session_id)
    if position is not None:
        params += (position[0], position[0], position[1])
    params += (count,)
    # EXISTS on agent_log is what makes "recorded" a database fact: the
    # finalize transaction wrote the phase and the messages together, so a
    # finished Run with rows is a Run whose recording committed.
    return [
        _MainBarRunRow(*row)
        for row in conn.execute(
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
    ]


def _mainbar_historic_rows(
    conn, session_id: str, before_id: int | None, count: int
):
    sql = """
        SELECT id, role, content, source, created_at
        FROM agent_log
        WHERE session_id = ? AND run_id IS NULL {before}
        ORDER BY id DESC LIMIT ?
    """
    if before_id is None:
        return conn.execute(sql.format(before=""), (session_id, count)).fetchall()
    return conn.execute(
        sql.format(before="AND id < ? "), (session_id, before_id, count)
    ).fetchall()


def _stored_message(role: str, content: str, redactor: Redactor) -> Message:
    parsed = json.loads(content)
    parsed = redactor.redact_jsonable(parsed)
    return Message(role=role, blocks=tuple(blocks_from_jsonable(parsed)))


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
    return _stored_message(role, row[0], redactor)


# --- one Session's chat Runs -------------------------------------------------

DEFAULT_REPLY_PREVIEW_CHARS = 240


class _SessionChatRunRow(NamedTuple):
    run_id: str
    phase: object
    outcome: object
    accepted_at: str
    started_at: str | None
    finished_at: str | None
    activity_revision: int


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
    position: tuple[int, str] | None = None
    if cursor is not None:
        payload = _decode_read_cursor(cursor, _SESSION_RUNS_KIND)
        if payload.get("s") != session_id:
            raise MalformedCursor("cursor belongs to a different session")
        position = _position(payload)
    exists = conn.execute(
        "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if exists is None:
        raise SessionNotFound(f"no such session: {session_id!r}")
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
    rows = [_SessionChatRunRow(*row) for row in rows[:limit]]
    entries_list: list[SessionChatRun] = []
    for row in rows:
        phase, outcome = parse_run_lifecycle_pair(row.phase, row.outcome)
        entries_list.append(
            SessionChatRun(
                run_id=row.run_id,
                phase=phase,
                outcome=outcome,
                accepted_at=row.accepted_at,
                started_at=row.started_at,
                finished_at=row.finished_at,
                activity_revision=row.activity_revision,
                **_final_reply(conn, row.run_id, redactor, reply_max_chars),
            )
        )
    entries = tuple(entries_list)
    next_cursor = (
        _session_runs_cursor(
            session_id, (rows[-1].activity_revision, rows[-1].run_id)
        )
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
