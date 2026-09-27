"""Atomic ledger seam and immutable objects. Memory implementations are fixtures.

Production implementations preserve this CAS contract; an independent anchor is
committed afterwards. A partial B/C write is deliberately not recoverable into
an active grant. No full payload, response or accumulating event list is in state.
"""

from copy import deepcopy
from threading import RLock
from typing import Protocol

from ..schema import digest

ZERO = "0" * 64


class ExecutionStore(Protocol):
    def get(self, job_id): ...
    def put(self, state, event, *, old_revision, old_digest, attempt=None): ...
    def get_attempt(self, job_id, attempt_id): ...
    def list_attempts(self, job_id): ...
    def put_object(self, value): ...
    def get_object(self, identity): ...


class ExecutionAnchor(Protocol):
    def read(self, job_id): ...
    def commit(self, event): ...


class MemoryExecutionStore:
    """Synthetic-only CAS model, intentionally no persistence claim."""

    def __init__(self):
        self.states, self.events, self.attempts, self.objects = {}, {}, {}, {}
        self.lock = RLock()

    def get(self, job_id):
        with self.lock:
            state = deepcopy(self.states.get(job_id))
            if state is not None:
                previous = ZERO
                for revision in range(1, state["revision"] + 1):
                    member = self.events.get((job_id, revision))
                    if (
                        member is None
                        or member["job_id"] != job_id
                        or member["revision"] != revision
                        or member["previous_digest"] != previous
                    ):
                        raise ValueError("authority_event_unverifiable")
                    previous = digest(member)
                event = self.events.get((job_id, state["revision"]))
                if (
                    event is None
                    or digest(event) != state["event_digest"]
                    or event["state_digest"]
                    != digest({k: v for k, v in state.items() if k != "event_digest"})
                ):
                    raise ValueError("authority_state_unverifiable")
            return state

    def put(self, state, event, *, old_revision, old_digest, attempt=None):
        with self.lock:
            old = self.states.get(state["job_id"])
            if (old is None and (old_revision != 0 or old_digest != ZERO)) or (
                old is not None
                and (
                    old["revision"] != old_revision or old["event_digest"] != old_digest
                )
            ):
                raise ValueError("ledger_conflict")
            if (
                state["revision"] != old_revision + 1
                or digest(event) != state["event_digest"]
            ):
                raise ValueError("ledger_event_invalid")
            if attempt is not None:
                self.attempts[state["job_id"], attempt["attempt_id"]] = deepcopy(
                    attempt
                )
            self.events[state["job_id"], state["revision"]] = deepcopy(event)
            self.states[state["job_id"]] = deepcopy(state)

    def get_attempt(self, job_id, attempt_id):
        with self.lock:
            value = deepcopy(self.attempts.get((job_id, attempt_id)))
            if value is not None:
                events = [
                    e
                    for (j, _), e in self.events.items()
                    if j == job_id and e.get("attempt_id") == attempt_id
                ]
                if not events or max(events, key=lambda e: e["revision"])[
                    "attempt_sha256"
                ] != digest(value):
                    raise ValueError("attempt_ledger_unverifiable")
            return value

    def list_attempts(self, job_id):
        with self.lock:
            return [self.get_attempt(j, a) for (j, a) in self.attempts if j == job_id]

    def put_object(self, value):
        identity = digest(value)
        with self.lock:
            self.objects.setdefault(identity, deepcopy(value))
        return identity

    def get_object(self, identity):
        with self.lock:
            value = deepcopy(self.objects[identity])
        if digest(value) != identity:
            raise ValueError("execution_object_mismatch")
        return value


class MemoryExecutionAnchor:
    """Independent in-memory fixture object, not real trust-domain evidence."""

    def __init__(self):
        self.high = {}
        self.lock = RLock()

    def read(self, job_id):
        with self.lock:
            return deepcopy(self.high.get(job_id))

    def commit(self, event):
        with self.lock:
            prior = self.high.get(event["job_id"])
            if (
                prior is None
                and (event["revision"] != 1 or event["previous_digest"] != ZERO)
            ) or (
                prior
                and (
                    event["revision"] != prior["revision"] + 1
                    or event["previous_digest"] != prior["digest"]
                )
            ):
                raise ValueError("anchor_conflict")
            self.high[event["job_id"]] = {
                "job_id": event["job_id"],
                "revision": event["revision"],
                "digest": event["event_digest"],
            }
