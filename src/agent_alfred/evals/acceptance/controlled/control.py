"""Versioned 120-second control channel with persistent idempotency receipts.

This is separate from p2.remote's private eight-second canary protocol. Only the
authenticated server owns an Authority. A retransmitted envelope can read its
old receipt; it never calls invoke a second time, even after process loss.
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from functools import partial
from urllib.parse import urlsplit

import httpx2 as httpx

from agent_alfred.resource_rollback import (
    OwnedResource,
    RollbackSlot,
    capture_call_result,
)

from ..materials import strict_json
from ..schema import digest, hash_value
from ..supplement_decisions import instant
from .contract import MAX_CONTROL_BYTES, exact, integer, text
from .persistence import MAX_OBJECT_BYTES, bounded
from .response_resources import owned_response

CONTROL_CONTRACT = "V1-CONTROLLED-CONTROL"
CONTROL_MARGIN_SECONDS = 30


def control_request(*, request_id, job_id, action, arguments, deadline):
    value = {
        "contract": CONTROL_CONTRACT,
        "version": 1,
        "request_id": text(request_id),
        "job_id": text(job_id),
        "action": action,
        "arguments": deepcopy(arguments),
        "deadline": deadline.isoformat(),
    }
    validate_request(value)
    return value


def validate_request(value):
    exact(value, "contract version request_id job_id action arguments deadline")
    if (
        value["contract"] != CONTROL_CONTRACT
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise ValueError("control_contract_invalid")
    text(value["request_id"])
    text(value["job_id"])
    instant(value["deadline"])
    action, arguments = value["action"], value["arguments"]
    if action == "invoke":
        exact(arguments, "operation_id attempt_id descriptor")
        text(arguments["operation_id"])
        text(arguments["attempt_id"])
        exact(arguments["descriptor"], "prepared_ref payload_sha256 timeout_seconds")
        integer(arguments["descriptor"]["timeout_seconds"], minimum=1, maximum=120)
        hash_value(arguments["descriptor"]["prepared_ref"])
        hash_value(arguments["descriptor"]["payload_sha256"])
    elif action == "finish":
        exact(arguments, "operation_id")
        text(arguments["operation_id"])
    else:
        raise ValueError("control_action_invalid")
    bounded(value, MAX_CONTROL_BYTES)


class ControlService:
    """Server entry; principal comes from transport authentication, never JSON."""

    def __init__(self, authority):
        self.authority = authority

    def call(self, request, *, principal):
        validate_request(request)
        auth = self.authority
        if principal != auth.worker:
            raise ValueError("worker_identity_mismatch")
        job, request_id = request["job_id"], request["request_id"]
        existing = auth.persistence_call(
            job, auth.store.control_status, job, request_id
        )
        if existing is not None:
            if existing["request_sha256"] != digest(request):
                raise ValueError("control_id_reused")
            return existing
        state = auth.persistence_call(job, auth.store.get, job)
        if state is None:
            raise ValueError("job_unknown")
        # A replacement/retired handler may read existing receipts, but must not
        # claim new work. This preliminary B read grants nothing: the claimed
        # action still verifies both domains immediately below and before send.
        auth._active(state)
        claimed, receipt = auth.persistence_call(
            job, auth.store.claim_control, job, request_id, request
        )
        if not claimed:
            # This read is deliberately permitted after STOP or deadline. It
            # cannot create an Attempt, revive permission, or refresh a timeout.
            return receipt
        try:
            state = auth._verified(job)
            auth._commit(
                state,
                "CONTROL_RECEIVED",
                previous=state,
                detail={"request_id": request_id, "request_sha256": digest(request)},
            )
            now = auth.now()
            remaining = min(
                (instant(request["deadline"]) - now).total_seconds(),
                (
                    instant(state["started_at"]) + timedelta(seconds=10800) - now
                ).total_seconds(),
            )
            if remaining < 1:
                raise ValueError("control_deadline_before_dispatch")
            args = request["arguments"]
            if request["action"] == "invoke":
                descriptor = {
                    **args["descriptor"],
                    "timeout_seconds": min(
                        args["descriptor"]["timeout_seconds"], int(remaining)
                    ),
                }
                result = auth.invoke(
                    job, args["operation_id"], args["attempt_id"], descriptor
                )
            else:
                result = auth.finish(job, args["operation_id"])
            outcome = {"ok": True, "result": result, "error": None}
        except BaseException as error:
            # Keep the original error in a safe, bounded receipt. The durable
            # Authority separately records integrity failures and their chain.
            outcome = {
                "ok": False,
                "result": None,
                "error": {"type": type(error).__name__, "message": str(error)},
            }
        receipt = auth.persistence_call(
            job, auth.store.complete_control, job, request_id, request, outcome
        )
        try:
            state = auth._verified(job)
            auth._commit(
                state,
                "CONTROL_COMPLETED",
                previous=state,
                detail={"request_id": request_id, "result_ref": receipt["result_ref"]},
            )
        except Exception:
            # Completion receipt stays readable; a failed anchor is a separate
            # durable stop, not a reason to re-run the action.
            pass
        return receipt

    def lookup(self, job_id, request_id, *, principal):
        if principal != self.authority.worker:
            raise ValueError("worker_identity_mismatch")
        return self.authority.persistence_call(
            job_id, self.authority.store.control_status, job_id, request_id
        )

    def reconcile(self, request, *, principal):
        """Complete a lost control receipt only from the original terminal facts."""
        validate_request(request)
        auth = self.authority
        if principal != auth.controller:
            raise ValueError("controller_identity_required")
        job, request_id = request["job_id"], request["request_id"]
        receipt = auth.persistence_call(job, auth.store.control_status, job, request_id)
        if receipt is None or receipt["request_sha256"] != digest(request):
            raise ValueError("control_request_unverifiable")
        if receipt["state"] == "COMPLETED":
            return receipt
        status = auth.status(job)
        arguments = request["arguments"]
        if request["action"] == "invoke":
            row = next(
                (
                    row
                    for row in status["attempts"]
                    if row["attempt_id"] == arguments["attempt_id"]
                ),
                None,
            )
            if row is None or row["send_state"] != "SETTLED":
                return receipt
            if (
                row["operation_id"] != arguments["operation_id"]
                or row["prepared_ref"] != arguments["descriptor"]["prepared_ref"]
                or row["payload_sha256"] != arguments["descriptor"]["payload_sha256"]
            ):
                raise ValueError("control_attempt_mismatch")
            result = {
                "attempt": row,
                "response": auth.persistence_call(
                    job, auth.store.get_object, row["response_ref"]
                ),
            }
        else:
            if arguments["operation_id"] not in status["state"]["finished_operations"]:
                return receipt
            result = status
        receipt = auth.persistence_call(
            job,
            auth.store.complete_control,
            job,
            request_id,
            request,
            {"ok": True, "result": result, "error": None},
        )
        auth._commit(
            status["state"],
            "CONTROL_RECONCILED",
            previous=status["state"],
            detail={"request_id": request_id, "result_ref": receipt["result_ref"]},
        )
        return receipt

    def read_result(self, identity, *, principal, job_id=None):
        # This endpoint should be scoped by its authenticated job at deployment;
        # it exposes control receipts only, never arbitrary material objects.
        if principal != self.authority.worker:
            raise ValueError("worker_identity_mismatch")
        hash_value(identity)
        # job_id comes from the authenticated endpoint scope, never from an
        # untrusted result body. Legacy unscoped audit reads infer no ownership.
        result = self.authority.persistence_call(
            job_id, self.authority.store.get_object, identity
        )
        exact(result, "ok result error")
        return result


class ControlDeliveryUnknown(RuntimeError):
    def __init__(self, request_id, *, cancelled=False, reason="timeout"):
        super().__init__(
            "control_cancelled_may_have_sent"
            if cancelled
            else "control_" + reason + "_may_have_sent"
        )
        self.request_id = request_id
        self.delivery = "UNKNOWN"
        self.possibly_sent = True
        self.cancelled = cancelled
        self.reason = "cancelled" if cancelled else reason


class ControlResultUnavailable(RuntimeError):
    """Delivery is proven; only retrieval of that immutable result is incomplete."""

    def __init__(self, request_id, receipt, *, reason, cancelled=False):
        super().__init__("control_completed_result_" + reason)
        self.request_id = request_id
        self.receipt = deepcopy(receipt)
        self.delivery = "COMPLETED"
        self.result_state = "UNREAD"
        self.possibly_sent = True
        self.cancelled = cancelled
        self.reason = reason


class ControlClient:
    """Caller retains the exact envelope for explicit retry/readback."""

    def __init__(self, transport, *, now=None):
        self.transport = transport
        self.now = now or (lambda: datetime.now(UTC))

    def call(self, request):
        validate_request(request)
        # A retry may be after the Attempt deadline; a short receipt read can
        # still return its already committed result. The server never reruns it.
        remaining = max(
            0, min(120, (instant(request["deadline"]) - self.now()).total_seconds())
        )
        timeout = remaining + CONTROL_MARGIN_SECONDS
        try:
            receipt = self.transport.exchange(request, timeout=timeout)
        except (TimeoutError, httpx.TimeoutException) as error:
            raise ControlDeliveryUnknown(request["request_id"]) from error
        except (ConnectionError, httpx.TransportError) as error:
            raise ControlDeliveryUnknown(
                request["request_id"], reason="connection_lost"
            ) from error
        except httpx.HTTPStatusError as error:
            if error.response.status_code >= 500:
                raise ControlDeliveryUnknown(
                    request["request_id"], reason="remote_error"
                ) from error
            raise
        except KeyboardInterrupt as error:
            raise ControlDeliveryUnknown(
                request["request_id"], cancelled=True
            ) from error
        if receipt["request_sha256"] != digest(request):
            raise ValueError("control_receipt_mismatch")
        if receipt["state"] == "PENDING":
            return {"state": "PENDING", "possibly_sent": True, "result": None}
        if receipt["state"] != "COMPLETED" or not receipt["result_ref"]:
            raise ValueError("control_receipt_unverifiable")
        return self.read_completed(request, receipt)

    def read_completed(self, request, receipt):
        """Explicit read-only retry; does not POST or create another Attempt."""
        validate_request(request)
        if (
            receipt["request_sha256"] != digest(request)
            or receipt["state"] != "COMPLETED"
            or not receipt["result_ref"]
        ):
            raise ValueError("control_receipt_unverifiable")
        try:
            value = self.transport.read_result(
                receipt["result_ref"], timeout=CONTROL_MARGIN_SECONDS
            )
            if digest(value) != receipt["result_ref"]:
                raise ValueError("control_result_mismatch")
        except BaseException as error:
            cancelled = isinstance(error, KeyboardInterrupt)
            if isinstance(error, (TimeoutError, httpx.TimeoutException)):
                reason = "timeout"
            elif isinstance(error, (ConnectionError, httpx.TransportError)):
                reason = "connection_lost"
            elif isinstance(error, httpx.HTTPStatusError):
                reason = "remote_error"
            elif cancelled:
                reason = "cancelled"
            elif isinstance(error, Exception):
                reason = "unverifiable"
            else:
                raise
            raise ControlResultUnavailable(
                request["request_id"], receipt, reason=reason, cancelled=cancelled
            ) from error
        return {"state": "COMPLETED", **value}


class _ControlResponseStream(httpx.SyncByteStream):
    """Keep the leaf close progress when HTTPX has already set is_closed."""

    def __init__(self, stream):
        self._owned = OwnedResource()
        self._owned.publish(stream)

    def __iter__(self):
        yield from self._owned.resource

    def close(self):
        return self._owned.close()


def _retain_control_stream(response):
    if not response.is_closed and not isinstance(
        response.stream, _ControlResponseStream
    ):
        capture_call_result(
            response, "stream", partial(_ControlResponseStream, response.stream)
        )


def _close_control_response(response):
    # An interruption can arrive before the with-body installs this wrapper.
    # Adopt the leaf before the very first cleanup can set is_closed.
    _retain_control_stream(response)
    if not response.is_closed:
        response.close()
    if isinstance(response.stream, _ControlResponseStream):
        return response.stream.close()


class HTTPControlTransport:
    """Fixed authenticated HTTPS control endpoint; caller installs the client.

    The installed client is a control identity, never the provider credential.
    The server must derive principal from TLS/authentication and scope result
    reads to that caller's job. No automatic application retry is performed.
    """

    def __init__(self, url, client):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("control_endpoint_invalid")
        self.url, self.client = url.rstrip("/"), client
        self._cleanup = RollbackSlot()

    def retry_cleanup(self):
        """Close only retained responses; never repeat a control operation."""
        return self._cleanup.retry()

    def _read(self, method, url, *, timeout, limit, content=None):
        request = self.client.build_request(
            method, url, content=content, timeout=timeout
        )
        with owned_response(
            self._cleanup,
            partial(self.client.send, request, stream=True, follow_redirects=False),
            close=_close_control_response,
        ) as response:
            _retain_control_stream(response)
            response.raise_for_status()
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > limit:
                    raise ValueError("control_response_capacity_exceeded")
        return strict_json(bytes(raw), limit=limit)

    def exchange(self, request, *, timeout):
        return self._read(
            "POST",
            self.url + "/control",
            timeout=timeout,
            limit=MAX_CONTROL_BYTES,
            content=bounded(request, MAX_CONTROL_BYTES),
        )

    def read_result(self, identity, *, timeout):
        hash_value(identity)
        return self._read(
            "GET",
            self.url + "/results/" + identity,
            timeout=timeout,
            limit=MAX_OBJECT_BYTES,
        )
