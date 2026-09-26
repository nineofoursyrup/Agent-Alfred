"""Private non-model canary in account B. It never contacts a provider."""

import hashlib
import hmac

from .authority_handler import principal_role
from .common import (
    BoundaryError,
    check_id,
    decode,
    deployment_active,
    digest,
    encode,
    exact,
    required_env,
    synthetic_only,
    within_window,
)


def receive(payload, token_hash):
    exact(
        payload,
        {"job_id", "attempt_id", "request_digest", "descriptor", "synthetic_token"},
    )
    check_id(payload["job_id"])
    check_id(payload["attempt_id"])
    if not isinstance(payload["synthetic_token"], str):
        raise BoundaryError("canary_token_denied")
    if not hmac.compare_digest(
        hashlib.sha256(payload["synthetic_token"].encode()).hexdigest(), token_hash
    ):
        raise BoundaryError("canary_token_denied")
    descriptor = exact(
        payload["descriptor"],
        {
            "profile_id",
            "endpoint_id",
            "model_id",
            "max_tokens",
            "body",
        },
    )
    if (
        descriptor["endpoint_id"] != "p2-private-canary"
        or not descriptor["model_id"].startswith("p2-canary-")
        or digest(descriptor) != payload["request_digest"]
    ):
        raise BoundaryError("canary_payload_denied")
    exact(descriptor["body"], {"input"})
    if not isinstance(descriptor["body"]["input"], str):
        raise BoundaryError("canary_payload_denied")
    print(
        encode(
            {
                "p2_audit": "canary_received",
                "job_id": payload["job_id"],
                "attempt_id": payload["attempt_id"],
                "request_digest": payload["request_digest"],
            }
        ).decode()
    )
    # The output is synthetic and independent of user/model input.
    return {
        "kind": "P2_CANARY_RESULT",
        "attempt_id": payload["attempt_id"],
        "request_digest": payload["request_digest"],
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "output": "p2-canary-ok",
    }


def handler(event, _context):
    synthetic_only()
    deployment_active()
    within_window()
    try:
        if principal_role(event) != required_env("P2_DISPATCH_ROLE_ARN"):
            raise BoundaryError("canary_caller_denied")
        if event.get("httpMethod") != "POST" or not event.get("path", "").endswith(
            "/v1/canary"
        ):
            raise BoundaryError("canary_path_denied")
        payload = decode(event.get("body") or "")
        result = receive(payload, required_env("P2_CANARY_TOKEN_SHA256"))
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": encode(result).decode(),
        }
    except BoundaryError as error:
        return {
            "statusCode": 403,
            "headers": {"Content-Type": "application/json"},
            "body": encode({"error": str(error)}).decode(),
        }
