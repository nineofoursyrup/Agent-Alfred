"""Transport validation over the existing trusted memory executor."""

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime

from agent_alfred.memory.commands import CommandContext
from agent_alfred.memory.types import (
    CursorStaleError,
    EpisodeRecord,
    ManualOrigin,
    origin_json,
)
from agent_alfred.resource_rollback import raise_if_rollback_pending
from agent_alfred.runtime.recording import RecordingUnavailable

KINDS = ("semantic", "episodic")
RECORD_QUERY = {"kind", "mode", "text", "subject", "since", "until", "cursor"}
# Service refusals keep their own closed codes; only the HTTP status is derived.
# Any code not listed is a server-side refusal (storage, barrier, deadline): 503.
REFUSAL_STATUS = {
    "invalid_input": 400,
    "not_found": 404,
    "busy": 409,
    "version_conflict": 409,
    "duplicate_conflict": 409,
    "operation_mismatch": 409,
    "operation_unverifiable": 409,
    "scope_stale": 409,
}
UNAVAILABLE = {"unavailable", "storage_read_failed", "storage_write_failed"}
WEB_CONTEXT = CommandContext(ManualOrigin("web"), "web")


def _failure(code, **metadata):
    return {"error": {"code": code, **metadata}}


def _refusal(result):
    """HTTP status for a service refusal; other 503 causes keep a closed code."""
    error = result["error"]
    if error["code"] in REFUSAL_STATUS:
        return REFUSAL_STATUS[error["code"]], result
    if error["code"] in UNAVAILABLE:
        return 503, result
    # e.g. trace_barrier_failed: the write did not happen; the cause stays visible.
    return 503, {"error": {**error, "code": "unavailable", "reason": error["code"]}}


def _bounds(params):
    """Optional since/until instants, or the first invalid field name.

    A naive value is refused rather than guessed as server-local time.
    """
    bounds = {}
    for name in ("since", "until"):
        if name in params:
            try:
                value = datetime.fromisoformat(params[name])
            except ValueError:
                return None, name
            if value.utcoffset() is None:
                return None, name
            bounds[name] = value
    return bounds, None


def _record_json(kind, record, provenance=None):
    value = {
        "kind": kind,
        "id": record.id,
        "record_version": record.record_version,
        "origin": origin_json(record.origin),
        "last_change_origin": origin_json(record.last_change_origin),
        "created_at": record.created_at.isoformat(),
        "modified_at": record.modified_at.isoformat(),
        "human_protected": record.human_protected,
    }
    if isinstance(record, EpisodeRecord):
        value.update(
            summary=record.summary,
            occurred_at=record.occurred_at.isoformat(),
            occurred_until=None
            if record.occurred_until is None
            else record.occurred_until.isoformat(),
        )
    else:
        value.update(subject=record.subject, fact=record.fact)
    if provenance is not None:
        value["provenance"] = provenance
    return value


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
            mirrors = []
            for name in names:
                status = {**read(name), "name": name}
                if "confirmation" in status:
                    # The observed file identity holds integers beyond 2**53
                    # (inode, ctime ns) that a browser number would round; the
                    # page returns this opaque text unchanged instead.
                    status["confirmation_token"] = json.dumps(
                        status["confirmation"], separators=(",", ":")
                    )
                mirrors.append(status)
            return 200, self._envelope(memory_revision=revision, mirrors=mirrors)
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
        if isinstance(body.get("observation"), str):
            try:
                body = {**body, "observation": json.loads(body["observation"])}
            except ValueError:
                return 400, {"code": "invalid_observation"}
        if "observation" in body and not isinstance(body["observation"], dict):
            return 400, {"code": "invalid_observation"}
        context = WEB_CONTEXT
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

    # -- Issue 46 Memory page: records, receipts, forgetting and statistics --

    def records(self, params):
        # Unknown names come from the client and are never echoed back.
        if set(params) - RECORD_QUERY - {"page_size"}:
            return 400, _failure("invalid_input")
        if params.get("kind") not in KINDS:
            return 400, _failure("invalid_input", field="kind")
        try:
            page_size = int(params.get("page_size", "25"))
        except ValueError:
            page_size = 0
        if not 1 <= page_size <= 100:
            return 400, _failure("invalid_input", field="page_size")
        bounds, invalid = _bounds(params)
        if invalid is not None:
            return 400, _failure("invalid_input", field=invalid)
        query = {
            name: params[name]
            for name in RECORD_QUERY - {"since", "until"}
            if name in params
        }
        try:
            page = self.memory.get_records(page_size=page_size, **query, **bounds)
        except CursorStaleError:
            return 409, _failure("cursor_stale")
        except ValueError:
            return 400, _failure("invalid_input")
        return 200, self._envelope(
            memory_revision=page.read_revision,
            read_revision=page.read_revision,
            next_cursor=page.next_cursor,
            records=[_record_json(params["kind"], item) for item in page.records],
        )

    def record(self, params):
        if set(params) != {"kind", "id"}:
            return 400, _failure("invalid_input")
        if params["kind"] not in KINDS:
            return 400, _failure("invalid_input", field="kind")
        # The record and its provenance are two reads; bracket them with the
        # persisted revision so a changed record is never labeled as current.
        revision = self.memory.memory_revision
        record = self.memory.get(params["kind"], params["id"])
        provenance = (
            None
            if record is None
            else self.memory.get_provenance(
                params["kind"], params["id"], record.record_version
            )
        )
        if self.memory.memory_revision != revision:
            return 409, _failure("memory_changed")
        if record is None:
            return 404, _failure("not_found", memory_revision=revision)
        return 200, self._envelope(
            memory_revision=revision,
            record=_record_json(params["kind"], record, provenance),
        )

    def state(self, params):
        if params:
            return 400, _failure("invalid_input")
        return 200, self._envelope()

    def command(self, body):
        result = self.memory.execute(body, WEB_CONTEXT)
        if "error" in result:
            return _refusal(result)
        return 200, self._envelope(
            operation_id=result["operation_id"],
            result=result,
            forgetting=self.memory.forgetting.get_forgetting(result["operation_id"])
            if result["status"] == "deleted"
            else None,
        )

    def operation(self, params):
        if set(params) != {"operation_id"}:
            return 400, _failure("invalid_input")
        operation_id = params["operation_id"]
        revision = self.memory.memory_revision
        receipt = self.memory.get_operation(operation_id)
        if receipt is None:
            # Unknown is not failure: an unacknowledged write stays unconfirmed.
            return 404, _failure("not_found")
        forgetting = self.memory.forgetting.get_forgetting(operation_id)
        scopes = (
            None
            if forgetting is None or "error" in forgetting
            else self.memory.forgetting.list_scopes(operation_id)
        )
        if self.memory.memory_revision != revision:
            return 409, _failure("memory_changed")
        return 200, self._envelope(
            memory_revision=revision,
            operation_id=operation_id,
            result=receipt,
            forgetting=forgetting,
            scopes=scopes,
        )

    def forget_action(self, body):
        fields = {
            "retry_cleanup": {"operation_id"},
            "resolve_scope": {"operation_id", "action_id", "scopes"},
        }
        action = body.get("action")
        if not isinstance(action, str) or action not in fields:
            return 400, _failure("invalid_input", field="action")
        if (
            set(body) != fields[action] | {"schema_version", "action"}
            or type(body["schema_version"]) is not int
            or body["schema_version"] != 1
            or not isinstance(body["operation_id"], str)
            or not body["operation_id"]
        ):
            return 400, _failure("invalid_input")
        operation_id = body["operation_id"]
        forgetting = self.memory.forgetting
        if action == "retry_cleanup":
            receipt = None
            result = forgetting.retry_cleanup(operation_id, WEB_CONTEXT)
        else:
            # Only the server's scope snapshots are confirmable; members never
            # travel from the client.
            receipt = result = forgetting.resolve_scope(
                {name: body[name] for name in fields[action]}, WEB_CONTEXT
            )
        if "error" in result:
            return _refusal(result)
        return 200, self._envelope(
            operation_id=operation_id,
            action=action,
            result=receipt,
            forgetting=result
            if action == "retry_cleanup"
            else forgetting.get_forgetting(operation_id),
        )

    def statistics(self, params):
        if set(params) - {"since", "until", "session_id"}:
            return 400, _failure("invalid_input")
        bounds, invalid = _bounds(params)
        if invalid is not None:
            return 400, _failure("invalid_input", field=invalid)
        try:
            statistics = self.memory.statistics(
                **bounds, session_id=params.get("session_id")
            )
        except ValueError:
            return 400, _failure("invalid_input")
        return 200, self._envelope(statistics=statistics)

    def skills(self, params):
        if params:
            return 400, _failure("invalid_input")
        catalog = self.host.skill_catalog
        if catalog is None:
            return 503, _failure("unavailable")
        return 200, {
            "schema_version": 1,
            "skills": [asdict(meta) for meta in catalog.list()],
        }

    def skill(self, params):
        catalog = self.host.skill_catalog
        if catalog is None:
            return 503, _failure("unavailable")
        if set(params) != {"name"}:
            return 400, _failure("invalid_input")
        # Names resolve only through the startup index; nothing joins a path.
        meta = next(
            (item for item in catalog.list() if item.name == params["name"]), None
        )
        if meta is None:
            return 404, _failure("not_found")
        return 200, {
            "schema_version": 1,
            "skill": {**asdict(meta), "body": catalog.load(meta.name)},
        }


# Reads and writes are separate tables: a write handler is reachable only by
# the guarded POST path, never by a GET that skips the CSRF check.
PAGE_READS = {
    "/api/memory/records": MemoryApi.records,
    "/api/memory/record": MemoryApi.record,
    "/api/memory/state": MemoryApi.state,
    "/api/memory/operations": MemoryApi.operation,
    "/api/memory/statistics": MemoryApi.statistics,
    "/api/memory/skills": MemoryApi.skills,
    "/api/memory/skill": MemoryApi.skill,
}
PAGE_WRITES = {
    "/api/memory/commands": MemoryApi.command,
    "/api/memory/forget/actions": MemoryApi.forget_action,
}


def serve_memory_read(host, path, params):
    try:
        return PAGE_READS[path](MemoryApi(host), params)
    except (sqlite3.Error, RecordingUnavailable) as error:
        raise_if_rollback_pending(error)
        return 503, _failure("storage_read_failed")


def serve_memory_write(host, path, body):
    try:
        return PAGE_WRITES[path](MemoryApi(host), body)
    except (sqlite3.Error, RecordingUnavailable) as error:
        raise_if_rollback_pending(error)
        return 503, _failure("storage_write_failed")


def serve_memory(host, *, params=None, body=None, mirrors=False):
    try:
        api = MemoryApi(host)
        return (
            api.action(body) if body is not None else api.read(params, mirrors=mirrors)
        )
    except (sqlite3.Error, RecordingUnavailable) as error:
        raise_if_rollback_pending(error)
        return 503, {"code": "storage_unavailable"}
