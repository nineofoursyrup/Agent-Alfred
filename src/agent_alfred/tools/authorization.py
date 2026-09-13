"""Independent persisted authorization and explicit policy publication."""

import json
import threading
from dataclasses import replace

from agent_alfred.atomic_config import DiskConflict, read_bytes, write_atomic
from agent_alfred.tools import capability_identity


def decode(raw):
    value = (
        json.loads(raw)
        if raw is not None
        else {"schema_version": 1, "revision": 0, "authorizations": {}}
    )
    if (
        type(value) is not dict
        or value.get("schema_version") != 1
        or type(value.get("revision")) is not int
        or value["revision"] < 0
        or type(value.get("authorizations")) is not dict
        or any(
            type(k) is not str or v not in ("unset", "allowed", "denied")
            for k, v in value["authorizations"].items()
        )
    ):
        raise ValueError("authorization_invalid")
    return value


class ToolAuthorization:
    def __init__(self, path, registry, publish):
        self.path, self.registry, self.publish = path, registry, publish
        self.lock = threading.RLock()
        self.revision, self.values, self.fingerprint = 0, {}, None
        self.status, self.application = "ok", "pending"
        self.load()

    def load(self):
        with self.lock:
            try:
                raw, self.fingerprint = (
                    read_bytes(self.path) if self.path else (None, None)
                )
                value = decode(raw)
                self.revision, self.values = value["revision"], value["authorizations"]
                self.status = "ok"
            except OSError, ValueError, TypeError:
                self.status = "authorization_unreadable"
            self.apply()

    def check_disk_health(self):
        """Called only while admission is idle; never switch an active Run."""
        with self.lock:
            self._observe_disk()
            if self.status != "ok":
                self.registry.suspend_external(self.status)

    def _observe_disk(self):
        """Update read evidence without changing the policy of an active Run."""
        if self.path is None:
            return False, None
        try:
            raw, fingerprint = read_bytes(self.path)
            if raw is None and self.fingerprint is not None:
                raise ValueError("authorization_removed")
            value = decode(raw)
            changed = fingerprint != self.fingerprint
            return changed, value if changed else None
        except OSError, ValueError, TypeError:
            self.status, self.application = "authorization_unreadable", "pending"
            return True, None

    def prepare(self, values):
        policies = {}
        for tool in self.registry.declarations():
            policy = self.registry.policy(tool.name)
            policies[tool.name] = replace(
                policy, authorization=values.get(capability_identity(tool), "unset")
            )
        block = self.registry.external_block
        # Authorization publication owns its own failures only. Configuration
        # recovery must explicitly clear a configuration-owned suspension.
        if block in (
            "authorization_unreadable",
            "authorization_not_applied",
            "authorization_write_unconfirmed",
        ):
            block = None
        return self.registry.with_policies(policies, external_block=block)

    def apply(self, candidate=None):
        try:
            if self.status != "ok":
                raise ValueError(self.status)
            candidate = candidate or self.prepare(self.values)
            self.publish(candidate)
            self.registry, self.application = candidate, "applied"
        except BaseException as exc:
            self.application = "pending"
            if candidate is not None:
                candidate.suspend_external("authorization_not_applied")
            self.registry.suspend_external(
                self.status if self.status != "ok" else "authorization_not_applied"
            )
            if not isinstance(exc, Exception):
                raise

    def snapshot(self, *, suspend_external=False):
        with self.lock:
            disk_changed, disk = self._observe_disk()
            if self.status != "ok" and suspend_external:
                self.registry.suspend_external(self.status)
            tools = self.registry.catalog()
            retained = False
            if self.status != "ok":
                for tool in tools:
                    if tool["effect"] == "external" and tool["exposure"] != "hidden":
                        retained = True
                        tool["exposure"] = "unverified"
                        tool["reason"] = self.status
            return {
                "revision": self.revision,
                "configuration_state": self.status,
                "application_state": self.application,
                "external_change": disk_changed,
                "disk_configuration": disk,
                "authorizations": dict(self.values),
                "active_policy_retained": retained,
                "tools": tools,
            }

    def save(self, identity, authorization, expected_revision):
        with self.lock:
            if self.status != "ok":
                return {"error": {"code": self.status}, **self.snapshot()}
            if type(expected_revision) is not int or expected_revision != self.revision:
                return {
                    "error": {
                        "code": "authorization_conflict",
                        "cause": "stale_revision",
                    },
                    **self.snapshot(),
                }
            eligible = {
                capability_identity(t)
                for t in self.registry.declarations()
                if t.effect == "external"
            }
            if identity not in eligible or authorization not in (
                "unset",
                "allowed",
                "denied",
            ):
                return {"error": {"code": "invalid_input"}}
            values = {**self.values, identity: authorization}
            try:
                candidate = self.prepare(values)
                if self.path:
                    payload = json.dumps(
                        {
                            "schema_version": 1,
                            "revision": self.revision + 1,
                            "authorizations": values,
                        }
                    ).encode()
                    self.fingerprint = write_atomic(
                        self.path, payload, self.fingerprint
                    )
            except DiskConflict:
                return {
                    "error": {
                        "code": "authorization_conflict",
                        "cause": "external_change",
                    },
                    **self.snapshot(),
                }
            except BaseException as exc:
                self.application = "pending"
                self.registry.suspend_external("authorization_write_unconfirmed")
                if not isinstance(exc, Exception):
                    raise
                return {
                    "error": {"code": "authorization_write_unconfirmed"},
                    **self.snapshot(),
                }
            self.values, self.revision = values, self.revision + 1
            self.apply(candidate)
            return {"saved": True, **self.snapshot()}

    def reapply(self, expected_revision):
        with self.lock:
            if expected_revision != self.revision or self.snapshot()["external_change"]:
                return {"error": {"code": "authorization_conflict"}, **self.snapshot()}
            self.apply()
            return self.snapshot()
