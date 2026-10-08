"""New Dashboard reads: strict identities, existing DTOs and secret-free errors."""

import sqlite3
from datetime import datetime, timedelta

from agent_alfred.memory.queries import CountsUnavailable
from agent_alfred.memory.types import CursorStaleError
from agent_alfred.runtime.accounting import AccountingError
from agent_alfred.runtime.recording import RecordingUnavailable
from agent_alfred.runtime.replies import (
    ReplyContextExpired,
    ReplyTargetUnavailable,
    ReplyUnavailable,
    ReplyWithheld,
)
from agent_alfred.runtime.sessions import SessionNotFound
from agent_alfred.runtime.source_locations import InvalidAnchor, SourceTargetUnavailable

READS = {
    "/api/overview/period": ({"range", "timezone"}, set()),
    "/api/overview/memory-counts": ({"expected_memory_revision"}, set()),
    "/api/overview/recent-runs": (set(), set()),
    "/api/sessions/locate": ({"session_id"}, {"limit"}),
    "/api/sessions/messages/locate": ({"session_id", "anchor"}, {"page_size"}),
    "/api/sessions/runs/locate": ({"session_id", "run_id"}, {"limit"}),
    "/api/mainbar/locate": ({"session_id", "run_id"}, set()),
}


def read(host, path, params, *, cancelled=None):
    from agent_alfred.gateway.web.api import (
        _block_json,
        _inbox_payload,
        _messages_payload,
        _page_size,
        _run_json,
        _session_runs_payload,
    )

    required, optional = READS[path]
    for key in ("process_instance_id", *sorted(required)):
        if key not in params:
            return 400, {"code": f"missing_{key}"}
    if set(params) - required - optional - {"process_instance_id"}:
        return 400, {"code": "invalid_input"}
    if params["process_instance_id"] != host.process_instance_id:
        return 409, {
            "code": "reply_context_expired"
            if path == "/api/mainbar/locate"
            else "process_context_expired"
        }
    try:
        if path == "/api/overview/period":
            result = host.overview_period(
                {key: params[key] for key in required}, cancelled=cancelled
            )
        elif path == "/api/overview/memory-counts":
            raw = params["expected_memory_revision"]
            # Decimal ASCII only, before conversion; revision fits SQLite's integer.
            normalized = raw.lstrip("0")
            if (
                not raw
                or not raw.isascii()
                or not raw.isdecimal()
                or len(normalized) > 19
            ):
                return 400, {"code": "invalid_input"}
            revision = int(normalized or "0")
            result = host.overview_memory_counts(revision)
        elif path == "/api/overview/recent-runs":
            page = host.list_runs(filter="all", limit=6)
            result = {
                "display_limit": 5,
                "runs": [
                    {
                        key: value
                        for key, value in _run_json(run).items()
                        if key != "prompt_preview"
                    }
                    for run in page.runs
                ],
            }
        elif path == "/api/mainbar/locate":
            result = host.locate_mainbar(
                **{
                    key: params[key]
                    for key in ("process_instance_id", "session_id", "run_id")
                }
            )
            message = result["user"]["blocks"]
            if message is not None:
                result["user"]["blocks"] = [
                    _block_json(block) for block in message.blocks
                ]
        else:
            kind, project, target = {
                "/api/sessions/locate": ("session", _inbox_payload, {}),
                "/api/sessions/messages/locate": (
                    "messages",
                    _messages_payload,
                    {"anchor": params.get("anchor")},
                ),
                "/api/sessions/runs/locate": (
                    "session_run",
                    _session_runs_payload,
                    {"run_id": params.get("run_id")},
                ),
            }[path]
            page, location = host.locate_source(
                kind,
                session_id=params["session_id"],
                limit=_page_size(
                    params, "page_size" if kind == "messages" else "limit"
                ),
                **target,
            )
            result = {**project(page), "target": location}
        observed = host.read_observation()
        result = {**result, **observed}
        if path.startswith("/api/overview/") and "expires_at" not in result:
            result["expires_at"] = (
                datetime.fromisoformat(observed["observed_at"]) + timedelta(minutes=15)
            ).isoformat()
        if path == "/api/overview/memory-counts":
            if host.memory_service.memory_revision != result["memory_revision"]:
                return 409, {"code": "memory_changed"}
        return 200, result
    except ReplyContextExpired:
        return 409, {"code": "reply_context_expired"}
    except ReplyTargetUnavailable:
        return 404, {"code": "reply_target_unavailable"}
    except ReplyWithheld:
        return 503, {"code": "reply_withheld", "reply_disposition": "reply_withheld"}
    except ReplyUnavailable:
        return 503, {"code": "reply_unavailable"}
    except CursorStaleError:
        return 409, {"code": "memory_changed"}
    except CountsUnavailable:
        return 503, {"code": "counts_unavailable"}
    except SessionNotFound:
        return 404, {"code": "unknown_session"}
    except InvalidAnchor:
        return 400, {"code": "invalid_anchor"}
    except SourceTargetUnavailable as exc:
        return 404, {"code": str(exc)}
    except AccountingError as exc:
        code = str(exc)
        if code in ("invalid_range", "invalid_timezone", "invalid_input"):
            return 400, {"code": code}
        return (
            (429, {"code": "read_quota"})
            if code == "snapshot_quota"
            else (503, {"code": "read_unavailable"})
        )
    except sqlite3.Error, RecordingUnavailable, ValueError, TypeError:
        return 503, {"code": "read_unavailable"}
