"""Run tools adapt the shared memory commands without acquiring another lease."""

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from agent_alfred.memory.commands import CommandContext
from agent_alfred.memory.types import ToolOrigin
from agent_alfred.messages import TextBlock
from agent_alfred.resource_rollback import raise_if_rollback_pending
from agent_alfred.tools import Tool, ToolFailure, ToolSuccess
from agent_alfred.tools.calendar import object_schema, string_schema


class MemoryTools:
    def __init__(self, service):
        self._service = service

    def declarations(self):
        kind_schema = {"type": "string", "enum": ["semantic", "episodic"]}
        return (
            Tool(
                "get_memory",
                "Read one memory and its current version.",
                object_schema(
                    {"kind": kind_schema, "id": string_schema()}, ("kind", "id")
                ),
                self.get,
                "local_read",
            ),
            Tool(
                "query_memory",
                "Find explicit memory targets; clarify ambiguous matches.",
                object_schema(
                    {"kind": kind_schema, "text": string_schema()}, ("kind", "text")
                ),
                self.query,
                "local_read",
            ),
            Tool(
                "save_fact",
                "Save a fact only when the user explicitly asks to save it.",
                object_schema(
                    {"subject": string_schema(), "fact": string_schema()},
                    ("subject", "fact"),
                ),
                self.save,
                "local_write",
            ),
            Tool(
                "update_memory",
                "Update one memory using its observed version.",
                object_schema(
                    {
                        "kind": {"type": "string", "enum": ["semantic", "episodic"]},
                        "id": string_schema(),
                        "expected_version": {"type": "integer", "minimum": 1},
                        **{
                            key: string_schema()
                            for key in (
                                "subject",
                                "fact",
                                "summary",
                                "occurred_at",
                                "occurred_until",
                            )
                        },
                    },
                    ("kind", "id", "expected_version"),
                ),
                self.update,
                "local_write",
            ),
            Tool(
                "delete_memory",
                "Delete one explicit memory ID and version.",
                object_schema(
                    {
                        "kind": {"type": "string", "enum": ["semantic", "episodic"]},
                        "id": string_schema(),
                        "expected_version": {"type": "integer", "minimum": 1},
                    },
                    ("kind", "id", "expected_version"),
                ),
                self.delete,
                "local_write",
            ),
        )

    def get(self, args, context):
        context.checkpoint()
        record = self._service.get(args["kind"], args["id"])
        if record is None:
            return ToolFailure("execution_error", (), memory_code="not_found")
        return ToolSuccess(
            (TextBlock(json.dumps(record, default=record_json, ensure_ascii=False)),)
        )

    def query(self, args, context):
        context.checkpoint()
        page = self._service.get_records(
            kind=args["kind"], mode="search", text=args["text"]
        )
        return ToolSuccess(
            (TextBlock(json.dumps(page, default=record_json, ensure_ascii=False)),)
        )

    def update(self, args, context):
        return self.command(
            "update",
            args["kind"],
            {
                key: value
                for key, value in args.items()
                if key not in ("kind", "expected_version")
            },
            context,
            args["expected_version"],
        )

    def save(self, args, context):
        return self.command("save", "semantic", dict(args), context)

    def delete(self, args, context):
        return self.command(
            "delete",
            args["kind"],
            {"id": args["id"]},
            context,
            args["expected_version"],
        )

    def command(self, action, kind, payload, context, expected_version=None):
        operation_id = uuid5(
            NAMESPACE_URL,
            json.dumps([context.run_id, context.step_index, context.call_id]),
        ).hex
        context.checkpoint()
        try:
            receipt = self._service.execute(
                {
                    "schema_version": 1,
                    "operation_id": operation_id,
                    "kind": kind,
                    "action": action,
                    "payload": payload,
                    "expected_version": expected_version,
                },
                CommandContext(
                    origin=ToolOrigin(context.call_id),
                    source=context.source,
                    run_id=context.run_id,
                    session_id=context.session_id,
                    call_id=context.call_id,
                    permission=context.permission,
                    checkpoint=context.checkpoint,
                    meter_success=(
                        lambda conn, op: context.metering.atomic_success(
                            context, conn, op
                        )
                    )
                    if context.metering is not None
                    else None,
                ),
            )
        except Exception as exc:
            raise_if_rollback_pending(exc)
            try:
                receipt = self._service.get_operation(operation_id)
            except Exception as read_error:
                raise_if_rollback_pending(read_error)
                receipt = None
            if receipt is None:
                return ToolFailure(
                    "execution_error",
                    (),
                    stop_reason="memory_result_unverified",
                    operation_id=operation_id,
                )
        if "error" in receipt:
            code = receipt["error"]["code"]
            tool_code = (
                "timeout"
                if code == "deadline_exceeded"
                else "invalid_input"
                if code == "invalid_input"
                else "unavailable"
                if code == "unavailable"
                else "execution_error"
            )
            return ToolFailure(tool_code, (), memory_code=code)
        if action == "delete":
            try:
                progress = self._service.forgetting.get_forgetting(operation_id)
                receipt["cleanup_state"] = (
                    None if progress is None else progress.get("state")
                )
            except Exception as exc:
                raise_if_rollback_pending(exc)
                receipt["cleanup_state"] = "unavailable"
        return ToolSuccess(
            (TextBlock(json.dumps(receipt, ensure_ascii=False)),),
            stop_reason="memory_delete_boundary" if action == "delete" else None,
            operation_id=operation_id,
        )


def record_json(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError("unsupported memory record value")
