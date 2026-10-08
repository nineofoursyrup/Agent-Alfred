"""Bounded source positioning over the existing keyset readers and cursors."""

from agent_alfred.runtime import runs, sessions
from agent_alfred.runtime.cursor import (
    MalformedCursor,
    decode_cursor,
    encode_cursor,
    parse_cursor_position_int,
)
from agent_alfred.runtime.run_queries import has_inflight_chat_run


class InvalidAnchor(ValueError):
    pass


class SourceTargetUnavailable(ValueError):
    pass


def message_anchor(session_id, segment, row_id):
    return encode_cursor(
        {"v": 1, "k": "message_anchor", "s": session_id, "seg": segment, "id": row_id}
    )


def locate_session(conn, *, session_id, limit, redactor):
    target = conn.execute(
        "SELECT activity_revision FROM sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    if target is None:
        raise sessions.SessionNotFound("unknown_session")
    head = sessions.list_sessions(conn, limit=1, redactor=redactor)
    if head.non_terminal and head.non_terminal.session_id == session_id:
        return sessions.SessionInboxPage((), None, head.non_terminal), {
            "anchor": session_id,
            "session_id": session_id,
            "placement": "pinned",
        }
    pinned = head.non_terminal.session_id if head.non_terminal else None
    newer = conn.execute(
        "SELECT activity_revision FROM sessions WHERE session_id IS NOT ? "
        "AND activity_revision>? ORDER BY activity_revision ASC LIMIT ?",
        (pinned, target[0], max(0, limit - 1)),
    ).fetchall()
    upper = newer[-1][0] if newer else target[0]
    # Strict less-than reader starts directly at the chosen bounded window.
    cursor = encode_cursor({"v": 2, "k": "inbox", "ar": upper + 1})
    page = sessions.list_sessions(conn, limit=limit, redactor=redactor, cursor=cursor)
    return page, {"anchor": session_id, "session_id": session_id, "placement": "page"}


def locate_messages(
    conn, *, session_id, anchor, limit, redactor, recording_failed_run_ids=frozenset()
):
    if (
        conn.execute(
            "SELECT 1 FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        is None
    ):
        raise sessions.SessionNotFound("unknown_session")
    try:
        value = decode_cursor(anchor, version=1, kind="message_anchor")
        position = parse_cursor_position_int(value.get("id"))
        if (
            value.get("s") != session_id
            or value.get("seg") not in ("runs", "historic")
            or position < 1
        ):
            raise InvalidAnchor("invalid_anchor")
    except MalformedCursor:
        raise InvalidAnchor("invalid_anchor") from None
    row = conn.execute(
        "SELECT run_id FROM agent_log WHERE id=? AND session_id=?",
        (position, session_id),
    ).fetchone()
    if row is None:
        raise SourceTargetUnavailable("source_target_unavailable")
    segment = "historic" if row[0] is None else "runs"
    if segment != value["seg"]:
        raise InvalidAnchor("invalid_anchor")
    target = {
        "anchor": anchor,
        "session_id": session_id,
        "run_id": row[0],
        "segment": segment,
        "placement": "page",
    }
    if segment == "historic":
        if has_inflight_chat_run(
            conn,
            session_id=session_id,
            position=None,
            recording_failed_run_ids=recording_failed_run_ids,
        ):
            target["placement"] = "waiting"
            return sessions._page(
                conn,
                session_id,
                [],
                sessions._runs_cursor(session_id, None),
                redactor,
                240,
                runs_pending=True,
            ), target
        cursor = encode_cursor(
            {
                "v": 2,
                "k": "runs",
                "seg": "historic",
                "s": session_id,
                "id": position - 1,
            }
        )
    else:
        run = conn.execute(
            "SELECT activity_revision FROM runs WHERE run_id=? "
            "AND session_id=? AND purpose IN ('chat','aggregation') "
            "AND admission_state='admitted'",
            (row[0], session_id),
        ).fetchone()
        if run is None:
            raise SourceTargetUnavailable("source_target_unavailable")
        previous = conn.execute(
            "SELECT activity_revision,run_id FROM runs WHERE session_id=? "
            "AND purpose IN ('chat','aggregation') AND "
            "(activity_revision<? OR (activity_revision=? AND run_id<?)) "
            "ORDER BY activity_revision DESC,run_id DESC LIMIT 1",
            (session_id, run[0], run[0], row[0]),
        ).fetchone()
        cursor = sessions._runs_cursor(session_id, previous)
    page = sessions.open_session(
        conn,
        session_id=session_id,
        page_size=limit,
        redactor=redactor,
        cursor=cursor,
        recording_failed_run_ids=recording_failed_run_ids,
    )
    return page, target


def locate_session_run(
    conn, *, session_id, run_id, limit, redactor, recording_failed_run_ids=frozenset()
):
    if (
        conn.execute(
            "SELECT 1 FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        is None
    ):
        raise sessions.SessionNotFound("unknown_session")
    row = conn.execute(
        "SELECT activity_revision,phase FROM runs WHERE run_id=? AND session_id=? "
        "AND purpose IN ('chat','aggregation') AND admission_state='admitted'",
        (run_id, session_id),
    ).fetchone()
    if row is None or run_id in recording_failed_run_ids:
        raise SourceTargetUnavailable("unknown_run")
    # A live object has no stable historical neighbors. Return only its slot.
    if row[1] != "finished":
        limit = 1
    excluded, excluded_params = runs._excluded_runs_clause(recording_failed_run_ids)
    newer = conn.execute(
        "SELECT activity_revision,run_id FROM runs WHERE session_id=? "
        "AND purpose IN ('chat','aggregation') AND admission_state='admitted' "
        f"{excluded} AND (activity_revision>? OR (activity_revision=? AND run_id>?)) "
        "ORDER BY activity_revision ASC,run_id ASC LIMIT ?",
        (session_id, *excluded_params, row[0], row[0], run_id, limit),
    ).fetchall()
    # The extra newer row is the reader's strict upper bound. Keep the full
    # key: incrementing only the revision would re-admit larger IDs at a tie.
    cursor = (
        runs._session_runs_cursor(session_id, newer[-1])
        if len(newer) == limit
        else None
    )
    # With fewer newer neighbors, stop at the target instead of filling the
    # location window with older rows. Its next cursor still resumes history.
    page_limit = min(limit, len(newer) + 1)
    page = runs.list_session_chat_runs(
        conn,
        session_id=session_id,
        limit=page_limit,
        redactor=redactor,
        cursor=cursor,
        recording_failed_run_ids=recording_failed_run_ids,
    )
    if row[1] != "finished":
        from dataclasses import replace

        page = replace(page, next_cursor=None)
    return page, {
        "anchor": run_id,
        "session_id": session_id,
        "run_id": run_id,
        "placement": "page" if row[1] == "finished" else "pinned",
    }
