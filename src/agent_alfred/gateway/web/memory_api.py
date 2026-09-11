"""Transport validation over the existing trusted memory executor."""

import sqlite3

from agent_alfred.memory.commands import CommandContext
from agent_alfred.memory.types import ManualOrigin
from agent_alfred.resource_rollback import raise_if_rollback_pending
from agent_alfred.runtime.recording import RecordingUnavailable


class MemoryApi:
    def __init__(self, host):
        self.host = host
        self.memory = host.memory_service

    def _envelope(self, *, memory_revision=None, **values):
        return {
            "schema_version": 1,
            "process_instance_id": self.host.process_instance_id,
            "memory_revision": (
                self.memory.memory_revision
                if memory_revision is None
                else memory_revision
            ),
            **values,
        }

    def read(self, params, *, mirrors=False):
        # File reads stay outside the database lock. Bracket the complete
        # projection, including multi-file/list/operation reads, with the
        # persisted revision instead of labeling old data after the fact.
        revision = self.memory.memory_revision
        response = self._read(params, mirrors=mirrors, revision=revision)
        if self.memory.memory_revision != revision:
            return 409, {"code": "memory_changed"}
        return response

    def _read(self, params, *, mirrors, revision):
        allowed = (
            {"operation_id", "name", "preview"}
            if mirrors
            else {"operation_id", "session_id", "batch_id", "limit", "offset"}
        )
        if set(params) - allowed:
            return 400, {"code": "unexpected_fields"}
        if "operation_id" in params:
            if set(params) != {"operation_id"}:
                return 400, {"code": "unexpected_fields"}
            value = (
                self.memory.mirrors.get_operation(params["operation_id"])
                if mirrors and self.memory.mirrors is not None
                else self.memory.consolidation.get_action_result(params["operation_id"])
                if not mirrors
                else None
            )
            if mirrors and value is not None:
                value = value["receipt"]
            return (
                (404, {"code": "not_found"})
                if value is None
                else (
                    200,
                    self._envelope(
                        memory_revision=revision,
                        operation_id=params["operation_id"],
                        result=value,
                    ),
                )
            )
        if mirrors:
            if self.memory.mirrors is None:
                return 503, {"code": "mirrors_unavailable"}
            if params.get("name", "facts") not in {"facts", "episodes"} or params.get(
                "preview", "0"
            ) not in {"0", "1"}:
                return 400, {"code": "invalid_input"}
            names = [params["name"]] if "name" in params else ["facts", "episodes"]
            read = (
                self.memory.mirrors.preview
                if params.get("preview") == "1"
                else self.memory.mirrors.status
            )
            return 200, self._envelope(
                memory_revision=revision, mirrors=[read(name) for name in names]
            )
        if "batch_id" in params:
            if set(params) != {"batch_id"}:
                return 400, {"code": "unexpected_fields"}
            value = self.memory.consolidation.get_batch(params["batch_id"])
            return (
                (404, {"code": "not_found"})
                if value is None
                else (200, self._envelope(memory_revision=revision, batch=value))
            )
        try:
            limit = int(params.get("limit", "50"))
            offset = int(params.get("offset", "0"))
        except ValueError:
            return 400, {"code": "invalid_limit"}
        if not 1 <= limit <= 100 or not 0 <= offset <= 1000000:
            return 400, {"code": "invalid_limit"}
        return 200, self._envelope(
            memory_revision=revision,
            **self.memory.consolidation.list_queue(
                session_id=params.get("session_id"), limit=limit, offset=offset
            ),
        )

    def action(self, body):
        action = body.get("action")
        fields = {
            "approve": {"batch_id", "expected_revision"},
            "reject": {"batch_id", "expected_revision"},
            "retry": {"batch_id", "expected_revision"},
            "skip_oversized": {"session_id", "run_id"},
            "mirror_retry": {"name"},
            "mirror_confirm": {"name", "observation"},
        }
        if not isinstance(action, str) or action not in fields:
            return 400, {"code": "unknown_action"}
        required = fields[action] | {"schema_version", "action", "operation_id"}
        if (
            set(body) != required
            or type(body.get("schema_version")) is not int
            or body["schema_version"] != 1
        ):
            return 400, {"code": "invalid_envelope"}
        for field in required - {"schema_version", "expected_revision", "observation"}:
            if not isinstance(body[field], str) or not body[field]:
                return 400, {"code": "invalid_input"}
        if "expected_revision" in body and (
            type(body["expected_revision"]) is not int or body["expected_revision"] < 1
        ):
            return 400, {"code": "invalid_revision"}
        if "observation" in body and not isinstance(body["observation"], dict):
            return 400, {"code": "invalid_observation"}
        context = CommandContext(ManualOrigin("web"), "web")
        operation_id = body["operation_id"]
        if action.startswith("mirror_"):
            mirrors = self.memory.mirrors
            if mirrors is None:
                return 503, {"code": "mirrors_unavailable"}
            if body["name"] not in {"facts", "episodes"}:
                return 400, {"code": "invalid_name"}
            result = (
                mirrors.confirm(
                    body["name"], body["observation"], operation_id, context
                )
                if action == "mirror_confirm"
                else mirrors.retry(body["name"], context, operation_id=operation_id)
            )
        elif action == "retry":
            result = self.host.retry_consolidation(
                body["batch_id"], body["expected_revision"], operation_id=operation_id
            )
            if not isinstance(result, dict):
                if result.kind != "accepted":
                    return 409 if result.kind in {
                        "busy",
                        "run_in_progress",
                        "mutation_in_flight",
                    } else 503, {"code": result.kind}
                return 202, self._envelope(
                    operation_id=operation_id,
                    result=self.memory.consolidation.get_action_result(operation_id),
                )
        elif action == "skip_oversized":
            result = self.memory.consolidation.skip_oversized(
                body["session_id"],
                body["run_id"],
                context=context,
                operation_id=operation_id,
            )
        else:
            result = getattr(self.memory.consolidation, action)(
                body["batch_id"],
                body["expected_revision"],
                context=context,
                operation_id=operation_id,
            )
        if not action.startswith("mirror_") and not result.get("error"):
            result = self.memory.consolidation.get_action_result(operation_id) or result
        code = result.get("error", {}).get("code")
        status = (
            409
            if code
            in {
                "busy",
                "stale_revision",
                "not_found",
                "invalid_batch_state",
                "batch_invalidated",
                "operation_mismatch",
                "stale_confirmation",
                "external_conflict",
                "source_mismatch",
            }
            else 503
            if code
            in {
                "storage_write_failed",
                "storage_read_failed",
                "unavailable",
                "mirror_sync_failed",
            }
            else 400
            if code
            else 200
        )
        return status, self._envelope(operation_id=operation_id, result=result)


def serve_memory(host, *, params=None, body=None, mirrors=False):
    try:
        api = MemoryApi(host)
        return (
            api.action(body) if body is not None else api.read(params, mirrors=mirrors)
        )
    except (sqlite3.Error, RecordingUnavailable) as error:
        raise_if_rollback_pending(error)
        return 503, {"code": "storage_unavailable"}
