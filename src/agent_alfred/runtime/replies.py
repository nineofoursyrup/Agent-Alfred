"""Complete reply content, independent of recording-state publication (ADR-0029)."""

import json
import sqlite3
from dataclasses import dataclass

from agent_alfred.messages import Message, blocks_from_jsonable, message_plain_text
from agent_alfred.redact import Redactor
from agent_alfred.runtime.recording import RecordingStore, RecordingUnavailable
from agent_alfred.runtime.snapshot import RuntimeSnapshot


class ReplyUnavailable(RuntimeError):
    """The requested complete reply cannot currently be read."""


class ReplyWithheld(ReplyUnavailable):
    """Known reply text was withheld by the redaction boundary."""


class ReplyContextExpired(ReplyUnavailable):
    """The caller must synchronize with the new process before reading history."""


@dataclass(frozen=True)
class RecoveredReply:
    """Content for one exact identity, never a lifecycle patch or receipt."""

    process_instance_id: str
    session_id: str
    run_id: str
    reply_text: str | None
    skill_notice: str | None = None
    reply_disposition: str | None = None


def _redact_reply(redactor: Redactor, text: str) -> str:
    try:
        return redactor.redact_text(text)
    except Exception as exc:
        raise ReplyUnavailable("reply unavailable") from exc


def recover_reply(
    snapshot: RuntimeSnapshot,
    store: RecordingStore,
    redactor: Redactor,
    *,
    process_instance_id: str,
    session_id: str,
    run_id: str,
) -> RecoveredReply:
    """Read one immutable snapshot first, then the exact recorded row if needed.

    A memory hit never acquires the database lock. Its captured text remains
    valid if saving retires the projection before this call returns. A miss
    waits for any writer before reading the committed row; it never searches
    another Run or treats a missing row as proof of loss. Encoding/redaction
    costs grow with the full reply length, outside the database lock.
    """
    if snapshot.process_instance_id != process_instance_id:
        raise ReplyContextExpired("reply context expired")
    projection = snapshot.unrecorded_terminal_projection
    if (
        projection is not None
        and projection.session_id == session_id
        and projection.run_id == run_id
        and projection.purpose in ("chat", "aggregation")
    ):
        if projection.reply_disposition == "no_reply":
            return RecoveredReply(
                process_instance_id,
                session_id,
                run_id,
                None,
                reply_disposition="no_reply",
            )
        if projection.reply_withheld:
            if projection.reply_disposition is not None:
                raise ReplyWithheld("reply withheld")
            raise ReplyUnavailable("reply unavailable")
        if projection.reply_text is not None:
            return RecoveredReply(
                process_instance_id,
                session_id,
                run_id,
                _redact_reply(redactor, projection.reply_text),
                _redact_reply(redactor, projection.skill_notice)
                if projection.skill_notice
                else None,
                reply_disposition=projection.reply_disposition,
            )
    try:
        with store.reading() as conn:
            row = conn.execute(
                """SELECT agent_log.content, runs.telemetry FROM runs
                   LEFT JOIN agent_log ON agent_log.run_id = runs.run_id
                     AND agent_log.session_id = runs.session_id
                     AND agent_log.role = 'assistant'
                   WHERE runs.run_id = ? AND runs.session_id = ?
                     AND runs.purpose IN ('chat', 'aggregation')
                     AND runs.phase = 'finished'
                     AND runs.admission_state = 'admitted'""",
                (run_id, session_id),
            ).fetchone()
    except (sqlite3.Error, RecordingUnavailable) as exc:
        raise ReplyUnavailable("reply unavailable") from exc
    if row is None:
        raise ReplyUnavailable("reply unavailable")
    if intentional_no_reply(row[1]):
        return RecoveredReply(
            process_instance_id, session_id, run_id, None, reply_disposition="no_reply"
        )
    try:
        raw = json.loads(row[0])
        if not isinstance(raw, list) or any(
            not isinstance(block, dict)
            or (block.get("type") == "text" and not isinstance(block.get("text"), str))
            for block in raw
        ):
            raise ValueError("invalid reply blocks")
        message = Message(role="assistant", blocks=blocks_from_jsonable(raw))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ReplyUnavailable("reply unavailable") from exc
    return RecoveredReply(
        process_instance_id,
        session_id,
        run_id,
        _redact_reply(redactor, message_plain_text(message)),
        _notice_from_telemetry(row[1], redactor),
        reply_disposition=(
            "reply"
            if _memory_telemetry(row[1]).get("routing")
            else None
        ),
    )


def _memory_telemetry(encoded):
    try:
        value = json.loads(encoded) if encoded else {}
    except ValueError, TypeError:
        return {}
    if not isinstance(value, dict) or not isinstance(value.get("memory"), dict):
        return {}
    return value["memory"]


def _notice_from_telemetry(encoded, redactor):
    skills = _memory_telemetry(encoded).get("skills")
    value = skills.get("notice") if isinstance(skills, dict) else None
    return _redact_reply(redactor, value) if isinstance(value, str) else None


def intentional_no_reply(encoded):
    """Only an explicit successful NoAction is an intentional missing answer."""
    memory = _memory_telemetry(encoded)
    aggregation = memory.get("aggregation")
    if (
        isinstance(aggregation, dict)
        and aggregation.get("graph_result") == "NoAction"
        and aggregation.get("reply_disposition") == "no_reply"
    ):
        return True
    routing = memory.get("routing")
    return (
        isinstance(routing, dict)
        and routing.get("graph_result") == "NoAction"
        and routing.get("reply_disposition") == "no_reply"
        and routing.get("reason_code") == "user_requested_no_reply"
    )


class ReplyTargetUnavailable(ReplyUnavailable):
    """The exact identity is absent, belongs elsewhere, or has no chat purpose."""


def locate_record(
    snapshot, store, redactor, *, process_instance_id, session_id, run_id
):
    """One exact formal record, independent from ordinary history pagination."""
    from agent_alfred.runtime.cursor import encode_cursor
    from agent_alfred.runtime.runs import _stored_message
    from agent_alfred.runtime.telemetry import finalization_metadata

    if snapshot.process_instance_id != process_instance_id:
        raise ReplyContextExpired("reply context expired")
    projection = snapshot.unrecorded_terminal_projection
    matched = (
        projection is not None
        and projection.run_id == run_id
        and projection.session_id == session_id
        and projection.purpose in ("chat", "aggregation")
    )
    # A projection hit must not acquire the lock held by the saving writer.
    if matched:
        reply = recover_reply(
            snapshot,
            store,
            redactor,
            process_instance_id=process_instance_id,
            session_id=session_id,
            run_id=run_id,
        )
        preview = (
            redactor.redact_text(projection.prompt_preview)
            if projection.prompt_preview is not None
            else None
        )
        values = {
            "purpose": projection.purpose,
            "activity_revision": None,
            "created_at": None,
            "source": "unrecorded_projection",
            "recording_state": projection.recording_state,
            "recording_source": "host_state",
            "state_revision": snapshot.state_revision,
            "user": {
                "availability": "preview" if preview is not None else "unavailable",
                "blocks": None,
                "preview": preview,
            },
        }
    else:
        try:
            with store.reading() as conn:
                row = conn.execute(
                    "SELECT purpose,phase,activity_revision,telemetry,admission_state "
                    "FROM runs WHERE run_id=? AND session_id=?",
                    (run_id, session_id),
                ).fetchone()
                if (
                    row is None
                    or row[0] not in ("chat", "aggregation")
                    or row[4] != "admitted"
                ):
                    raise ReplyTargetUnavailable("reply target unavailable")
                user = conn.execute(
                    "SELECT content,created_at FROM agent_log WHERE run_id=? "
                    "AND session_id=? AND role='user' ORDER BY id LIMIT 1",
                    (run_id, session_id),
                ).fetchone()
            reply = recover_reply(
                snapshot,
                store,
                redactor,
                process_instance_id=process_instance_id,
                session_id=session_id,
                run_id=run_id,
            )
            user_message = _stored_message("user", user[0], redactor) if user else None
        except (
            sqlite3.Error,
            RecordingUnavailable,
            ValueError,
            TypeError,
            AttributeError,
        ) as exc:
            raise ReplyUnavailable("reply unavailable") from exc
        metadata = finalization_metadata(row[3], phase=row[1], purpose=row[0])
        values = {
            "purpose": row[0],
            "activity_revision": row[2],
            "created_at": user[1] if user else None,
            "source": "recorded_pair",
            "recording_state": metadata["recording_state"],
            "recording_source": metadata["recording_source"],
            "user": {
                "availability": "full" if user_message is not None else "unavailable",
                "blocks": user_message,
                "preview": None,
            },
        }
    return {
        "process_instance_id": process_instance_id,
        "session_id": session_id,
        "run_id": run_id,
        "item_key": encode_cursor(
            {"v": 1, "k": "run_pair", "s": session_id, "r": run_id}
        ),
        "reply_text": reply.reply_text,
        "reply_disposition": reply.reply_disposition,
        "skill_notice": reply.skill_notice,
        "history_contiguous": False,
        **values,
    }
