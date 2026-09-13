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


class ReplyContextExpired(ReplyUnavailable):
    """The caller must synchronize with the new process before reading history."""


@dataclass(frozen=True)
class RecoveredReply:
    """Content for one exact identity, never a lifecycle patch or receipt."""

    process_instance_id: str
    session_id: str
    run_id: str
    reply_text: str
    skill_notice: str | None = None


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
        and projection.purpose == "chat"
    ):
        if projection.reply_withheld:
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
            )
    try:
        with store.reading() as conn:
            row = conn.execute(
                """SELECT agent_log.content, runs.telemetry FROM runs
                   JOIN agent_log ON agent_log.run_id = runs.run_id
                     AND agent_log.session_id = runs.session_id
                   WHERE runs.run_id = ? AND runs.session_id = ?
                     AND runs.purpose = 'chat' AND runs.phase = 'finished'
                     AND runs.admission_state = 'admitted'
                     AND agent_log.role = 'assistant'""",
                (run_id, session_id),
            ).fetchone()
    except (sqlite3.Error, RecordingUnavailable) as exc:
        raise ReplyUnavailable("reply unavailable") from exc
    if row is None:
        raise ReplyUnavailable("reply unavailable")
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
    )


def _notice_from_telemetry(encoded, redactor):
    value = (
        json.loads(encoded).get("memory", {}).get("skills", {}).get("notice")
        if encoded
        else None
    )
    return _redact_reply(redactor, value) if isinstance(value, str) else None
