"""Asynchronous MCP operations retain the real Host mutation lease until done."""

import threading
import time
import uuid
from copy import deepcopy

from agent_alfred.atomic_config import read_bytes
from agent_alfred.mcp.config import read, resolve
from agent_alfred.resource_rollback import (
    ResumableRollback,
    thread_exit_confirmed,
    thread_start_effect_happened,
)


class MCPControl:
    def __init__(self, host):
        self.host = host
        self.bridge = host._mcp
        self.lock = threading.RLock()
        self.records = {}
        self.thread = None
        self.token = None
        self.token_state = None
        self.token_time = 0

    def disk(self):
        try:
            directory = self.bridge.directory
            return read_bytes(directory / "mcp.json")[1] if directory else None
        except OSError, ValueError:
            return "unreadable"

    def preview(self):
        with self.lock:
            self.expire()
            state = (self.host.process_instance_id, self.bridge.revision, self.disk())
            if (
                self.token is None
                or state != self.token_state
                or time.monotonic() - self.token_time >= 600
            ):
                self.token, self.token_state = uuid.uuid4().hex, state
                self.token_time = time.monotonic()
            return {
                "token": self.token,
                "disk_fingerprint": state[2],
                "operation": deepcopy(list(self.records.values())[-1]["public"])
                if self.records
                else None,
            }

    def expire(self):
        completed = [(key, r) for key, r in self.records.items() if r["done"].is_set()]
        for key, record in completed:
            if time.monotonic() - record["time"] >= 600 or len(self.records) > 256:
                del self.records[key]

    def submit(self, body):
        if (
            not isinstance(body, dict)
            or set(body) != {"action", "operation_id", "token", "server_key"}
            or body["action"] not in ("apply", "reconnect", "cleanup")
            or not isinstance(body["operation_id"], str)
            or not 1 <= len(body["operation_id"]) <= 128
            or not isinstance(body["token"], str)
            or (
                body["server_key"] is not None
                and not isinstance(body["server_key"], str)
            )
        ):
            return {"error": {"code": "invalid_input"}}
        with self.lock:
            self.expire()
            previous = self.records.get(body["operation_id"])
            if previous:
                if previous["body"] != body:
                    return {"error": {"code": "operation_conflict"}}
                return deepcopy(previous["public"])
        owner = ResumableRollback()
        refusal = self.host.try_begin_mutation(owner=owner)
        if refusal:
            return {"error": {"code": refusal}}
        try:
            with self.lock:
                return self.accept(body, owner)
        finally:
            if (
                self.thread is None
                or getattr(self.thread, "mcp_owner", None) is not owner
                or not thread_start_effect_happened(self.thread)
            ):
                with self.lock:
                    record = self.records.get(body["operation_id"])
                    if record is not None and record["body"] == body:
                        record["public"].update(status="failed", error="start_failed")
                        record["done"].set()
                self.host.end_mutation(owner=owner)

    def accept(self, body, owner):
        try:
            state = (
                self.host.process_instance_id,
                self.bridge.revision,
                self.disk(),
            )
            if (
                body["token"] != self.token
                or state != self.token_state
                or time.monotonic() - self.token_time >= 600
            ):
                return {"error": {"code": "mcp_conflict"}}
            if body["action"] != "apply" and body["server_key"] not in self.bridge.rows:
                return {"error": {"code": "invalid_server"}}
            candidate, fingerprint = None, None
            if body["action"] == "apply":
                try:
                    candidate, fingerprint = read(
                        self.bridge.directory, self.bridge.env, self.bridge.redactor
                    )
                except ValueError, OSError, TypeError:
                    self.bridge.error = "configuration_invalid"
                    return {"error": {"code": "configuration_invalid"}}
                if fingerprint != state[2]:
                    return {"error": {"code": "mcp_conflict"}}
            elif body["action"] == "reconnect":
                try:
                    key = body["server_key"]
                    candidate = dict(self.bridge.config)
                    candidate[key] = resolve(
                        key,
                        candidate[key].raw,
                        self.bridge.env,
                        self.bridge.directory,
                        self.bridge.redactor,
                    )
                except ValueError, OSError, TypeError:
                    return {"error": {"code": "configuration_invalid"}}
            record = {
                "body": deepcopy(body),
                "done": threading.Event(),
                "time": time.monotonic(),
                "public": {
                    "operation_id": body["operation_id"],
                    "status": "running",
                    "process_instance_id": self.host.process_instance_id,
                },
            }
            self.records[body["operation_id"]] = record
            self.token = None
            thread = threading.Thread(
                target=self.run,
                args=(record, owner, candidate, fingerprint),
                name="mcp-control",
                daemon=True,
            )
            self.thread = thread
            thread.mcp_owner = owner
            thread.start()
        except BaseException:
            raise
        else:
            return deepcopy(record["public"])

    def run(self, record, owner, candidate, fingerprint):
        error = None
        body = record["body"]
        try:
            if body["action"] == "cleanup":
                row = self.bridge.rows[body["server_key"]]
                if (
                    self.bridge.resources is not None
                    and not self.bridge.resources.recovered(body["server_key"])
                ):
                    row.update(state="error", reason="resource_ownership_unconfirmed")
                elif row["session"] is not None and not row["session"].close():
                    row.update(state="error", reason="cleanup_incomplete")
                else:
                    row.update(
                        state="configured_untested",
                        reason="reconnect_required",
                        cleanup_incomplete=False,
                    )
                self.bridge.revision += 1
            else:
                # Resource work is outside the view lock. The Host lease still
                # excludes all Runs and competing mutations until publication.
                self.bridge.apply(
                    candidate,
                    (body["server_key"],) if body["action"] == "reconnect" else (),
                )
                self.bridge.config = candidate
                if body["action"] == "apply":
                    self.bridge.config_fingerprint = fingerprint
                self.bridge.error = None
            with self.host._configuration_lock:
                self.host._publish_mcp()
        except BaseException:
            self.bridge.pause("publication_unconfirmed")
            error = "mcp_operation_failed"
        finally:
            # No worker publication can occur after this final state. Residual
            # sessions stay on the bridge's rows and are owned by Host.close.
            with self.lock:
                record["public"].update(status="failed" if error else "completed")
                if error:
                    record["public"]["error"] = error
                record["time"] = time.monotonic()
            self.host.end_mutation(owner=owner)
            record["done"].set()

    def get(self, identity):
        with self.lock:
            self.expire()
            record = self.records.get(identity)
            return (
                deepcopy(record["public"])
                if record
                else {"error": {"code": "operation_not_found"}}
            )

    def wait(self, identity, timeout):
        with self.lock:
            record = self.records.get(identity)
        return record is not None and record["done"].wait(timeout)

    def close(self, deadline):
        thread = self.thread
        if thread is None or not thread_start_effect_happened(thread):
            return True
        return thread_exit_confirmed(thread, max(0, deadline - time.monotonic()))
