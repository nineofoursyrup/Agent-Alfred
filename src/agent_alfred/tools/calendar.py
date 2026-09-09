"""Calendar commands: the service owns business and ledger in one transaction."""

import hashlib
import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from agent_alfred.clock import format_instant
from agent_alfred.messages import TextBlock
from agent_alfred.tools import Tool, ToolFailure, ToolSuccess


def object_schema(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def string_schema():
    return {"type": "string", "minLength": 1}


def parse_instant(value):
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("Time must include a UTC offset.")
    return result.astimezone(UTC)


class CalendarTools:
    def __init__(self, store, clock):
        self._store = store
        self._clock = clock

    def declarations(self):
        return (
            Tool(
                "create_event",
                "Create a local calendar entry; time requires UTC offset.",
                object_schema(
                    {
                        name: string_schema()
                        for name in (
                            "title",
                            "starts_at",
                            "ends_at",
                            "iana_time_zone",
                            "participants",
                            "notes",
                        )
                    },
                    ("title", "starts_at"),
                ),
                self.create,
                "local_write",
            ),
            Tool(
                "query_events",
                "Query local calendar entries by absolute time.",
                object_schema({"since": string_schema(), "until": string_schema()}),
                self.query,
                "local_read",
            ),
        )

    def create(self, args, context):
        context.checkpoint()
        try:
            starts = parse_instant(args["starts_at"])
            ends = parse_instant(args["ends_at"]) if "ends_at" in args else None
            if ends is not None and ends < starts:
                raise ValueError("ends_at must not precede starts_at")
            if "iana_time_zone" in args:
                ZoneInfo(args["iana_time_zone"])
        except ValueError, KeyError:
            return ToolFailure(
                "invalid_input",
                (TextBlock("Use valid offset-aware times and an IANA time zone."),),
            )
        from agent_alfred.tools.files import operation_id

        op = operation_id(context)
        identity = hashlib.sha256(
            json.dumps(dict(args), sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        if self._store.transaction_in_progress:
            return ToolFailure(
                "execution_error", (TextBlock("Caller transaction is active."),)
            )
        now = format_instant(self._clock.wall_utc())
        with self._store.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute(
                "SELECT fingerprint,receipt FROM local_tool_operations "
                "WHERE operation_id=?",
                (op,),
            ).fetchone()
            if old:
                if old[0] != identity:
                    return ToolFailure(
                        "execution_error", (TextBlock("Operation parameter mismatch."),)
                    )
                return ToolSuccess((TextBlock(old[1]),))
            context.checkpoint()
            cursor = conn.execute(
                "INSERT INTO calendar_entries(title,starts_at,ends_at,iana_time_zone,"
                "participants,notes,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    args["title"],
                    starts.isoformat(),
                    ends.isoformat() if ends else None,
                    args.get("iana_time_zone"),
                    args.get("participants"),
                    args.get("notes"),
                    now,
                ),
            )
            entry_id = cursor.lastrowid
            fingerprint = hashlib.sha256(
                json.dumps(
                    {"title": args["title"], "starts_at": starts.isoformat()},
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            conn.execute(
                "INSERT INTO tool_ledger(tool_name,fingerprint,effect,status,call_id,"
                "run_id,session_id,summary,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "create_event",
                    fingerprint,
                    "local_write",
                    "succeeded",
                    context.call_id,
                    context.run_id,
                    context.session_id,
                    "Created calendar entry",
                    now,
                ),
            )
            receipt = json.dumps({"id": entry_id, "status": "created"})
            conn.execute(
                "INSERT INTO local_tool_operations VALUES (?,?,?)",
                (op, identity, receipt),
            )
            context.checkpoint()
            conn.commit()
        return ToolSuccess(
            (TextBlock(json.dumps({"id": entry_id, "status": "created"})),)
        )

    def query(self, args, context):
        context.checkpoint()
        try:
            since = parse_instant(args["since"]) if "since" in args else None
            until = parse_instant(args["until"]) if "until" in args else None
            if since and until and until < since:
                raise ValueError("inverted range")
        except ValueError:
            return ToolFailure("invalid_input", (TextBlock("Use offset-aware times."),))
        with self._store.reading() as conn:
            rows = conn.execute(
                "SELECT id,title,starts_at,ends_at,iana_time_zone,"
                "participants,notes FROM calendar_entries"
            ).fetchall()
        matching = []
        for row in rows:
            context.checkpoint()
            instant = parse_instant(row[2])
            if (since is None or instant >= since) and (
                until is None or instant <= until
            ):
                matching.append(row)
        matching.sort(key=lambda row: (parse_instant(row[2]), row[0]))
        keys = (
            "id",
            "title",
            "starts_at",
            "ends_at",
            "iana_time_zone",
            "participants",
            "notes",
        )
        return ToolSuccess(
            (
                TextBlock(
                    json.dumps(
                        [dict(zip(keys, row, strict=True)) for row in matching],
                        ensure_ascii=False,
                    )
                ),
            )
        )
