"""Strict wire format and fail-closed configuration for P2 services."""

import hashlib
import json
import os
import re
from datetime import datetime, timezone

MAX_WIRE_BYTES = 256 * 1024
ZERO_DIGEST = "0" * 64
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[a-zA-Z0-9_-]{1,100}\Z")


class BoundaryError(ValueError):
    """A request or trust assertion cannot safely proceed."""


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BoundaryError("duplicate_json_key")
        result[key] = value
    return result


def _constant(_):
    raise BoundaryError("non_finite_json")


def decode(raw):
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, bytes) or len(raw) > MAX_WIRE_BYTES:
        raise BoundaryError("invalid_wire_size")
    try:
        return json.loads(raw, object_pairs_hook=_unique, parse_constant=_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise BoundaryError("invalid_json") from error


def encode(value):
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise BoundaryError("invalid_wire_value") from error
    if len(raw) > MAX_WIRE_BYTES:
        raise BoundaryError("invalid_wire_size")
    return raw


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def check_digest(value):
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise BoundaryError("invalid_digest")
    return value


def check_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise BoundaryError("invalid_identity")
    return value


def exact(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise BoundaryError("invalid_wire_shape")
    return value


def required_env(name):
    value = os.environ.get(name)
    if not value or value.startswith("<") or "UNPROVIDED" in value:
        raise BoundaryError("p2_configuration_missing:" + name)
    return value


def synthetic_only():
    if os.environ.get("P2_SYNTHETIC_ONLY") != "1":
        raise BoundaryError("p2_synthetic_guard_required")


def deployment_active():
    if os.environ.get("P2_DEPLOYMENT_ACTIVE") != "true":
        raise BoundaryError("p2_deployment_not_activated")


def within_window():
    if utc_now() >= parse_utc(required_env("P2_EXPIRES_AT")):
        raise BoundaryError("p2_deployment_expired")


def dispatch_allowed():
    if os.environ.get("P2_STOP_DISPATCH") != "false":
        raise BoundaryError("p2_dispatch_stopped")
    within_window()


def utc_now():
    return datetime.now(timezone.utc)


def parse_utc(value):
    if not isinstance(value, str):
        raise BoundaryError("invalid_time")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise BoundaryError("invalid_time") from error
    if result.tzinfo is None:
        raise BoundaryError("invalid_time")
    return result.astimezone(timezone.utc)


def lambda_json(client, function_arn, body):
    """Synchronous only; a lost response is uncertain, never safe to replay."""
    response = client.invoke(
        FunctionName=function_arn,
        InvocationType="RequestResponse",
        Payload=encode(body),
    )
    if response.get("FunctionError") or response.get("StatusCode") != 200:
        raise BoundaryError("remote_state_unverifiable")
    return decode(response["Payload"].read())
