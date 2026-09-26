"""AWS Lambda entry for account B's private Authority API and SQS issuer events."""

import base64
import re

from .authority import (
    Authority,
    AwsIssuerVerifier,
    LambdaAnchorClient,
    LambdaDispatchClient,
)
from .common import (
    BoundaryError,
    decode,
    deployment_active,
    dispatch_allowed,
    encode,
    exact,
    required_env,
    synthetic_only,
)
from .store import DynamoJobStore

_PATH = re.compile(
    r"(?:/[a-zA-Z0-9_-]+)?/v1/(submit|invoke|finish|status|proposal|preflight)\Z"
)
_STS_ROLE = re.compile(r"arn:aws:sts::(\d{12}):assumed-role/([^/]+)/[^/]+\Z")


def principal_role(event):
    context = event.get("requestContext") or {}
    identity = context.get("identity") or {}
    arn = identity.get("userArn")
    if not isinstance(arn, str):
        raise BoundaryError("caller_identity_unverifiable")
    match = _STS_ROLE.fullmatch(arn)
    if match:
        return f"arn:aws:iam::{match.group(1)}:role/{match.group(2)}"
    if re.fullmatch(r"arn:aws:iam::\d{12}:role/[a-zA-Z0-9+=,.@_-]+", arn):
        return arn
    raise BoundaryError("caller_identity_unverifiable")


def service():
    import boto3
    from botocore.config import Config

    # A lost synchronous response can mean Dispatch already sent. SDK retries
    # would turn that uncertainty into a second physical send.
    lambda_client = boto3.client(
        "lambda",
        config=Config(
            connect_timeout=3, read_timeout=28, retries={"total_max_attempts": 1}
        ),
    )
    return Authority(
        DynamoJobStore(boto3.client("dynamodb"), required_env("P2_GRANT_TABLE")),
        LambdaAnchorClient(lambda_client, required_env("P2_ANCHOR_FUNCTION_ARN")),
        LambdaDispatchClient(lambda_client, required_env("P2_DISPATCH_FUNCTION_ARN")),
        AwsIssuerVerifier(boto3.client("kms"), required_env("P2_ISSUER_KMS_ARN")),
        worker_principal=required_env("P2_WORKER_ROLE_ARN"),
    )


def route(api, action, body, caller, allowed):
    expected = allowed[action]
    if caller not in expected:
        raise BoundaryError("caller_role_denied")
    if action == "submit":
        return api.submit_job(body, caller)
    if action == "invoke":
        exact(body, {"job_id", "role", "attempt_id", "request_descriptor"})
        return api.invoke(
            body["job_id"],
            body["role"],
            body["attempt_id"],
            body["request_descriptor"],
            caller,
        )
    if action == "finish":
        exact(body, {"job_id", "role"})
        return api.finish(body["job_id"], body["role"], caller)
    if action == "status":
        exact(body, {"job_id"})
        return api.status(body["job_id"], caller)
    if action == "proposal":
        exact(body, {"job_id"})
        return api.proposal_view(body["job_id"])
    if action == "preflight":
        return api.preflight(body)
    raise BoundaryError("authority_action_invalid")


def handler(event, _context):
    synthetic_only()
    deployment_active()
    api = service()
    if type(event) is dict and "Records" in event:
        records = event["Records"]
        if type(records) is not list or len(records) != 1:
            raise BoundaryError("issuer_queue_batch_invalid")
        record = records[0]
        if record.get("eventSource") != "aws:sqs" or record.get(
            "eventSourceARN"
        ) != required_env("P2_ISSUER_QUEUE_ARN"):
            raise BoundaryError("issuer_queue_unverifiable")
        envelope = decode(record["body"])
        if envelope.get("body", {}).get("kind") == "ISSUE":
            dispatch_allowed()
        return api.issuer_event(envelope)
    try:
        if type(event) is not dict or event.get("httpMethod") != "POST":
            raise BoundaryError("authority_method_invalid")
        match = _PATH.fullmatch(event.get("path", ""))
        if not match:
            raise BoundaryError("authority_path_invalid")
        action = match.group(1)
        if action in ("submit", "invoke", "preflight"):
            dispatch_allowed()
        caller = principal_role(event)
        allowed = {
            "submit": {required_env("P2_EXECUTOR_ROLE_ARN")},
            "invoke": {required_env("P2_WORKER_ROLE_ARN")},
            "finish": {required_env("P2_WORKER_ROLE_ARN")},
            "status": {
                required_env("P2_EXECUTOR_ROLE_ARN"),
                required_env("P2_WORKER_ROLE_ARN"),
            },
            "proposal": {required_env("P2_ISSUER_ROLE_ARN")},
            "preflight": {required_env("P2_DISPATCH_ROLE_ARN")},
        }
        raw = event.get("body") or ""
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw, validate=True)
        result = route(api, action, decode(raw), caller, allowed)
        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Cache-Control": "no-store",
            },
            "body": encode(result).decode(),
        }
    except BoundaryError as error:
        return {
            "statusCode": 403,
            "headers": {
                "Content-Type": "application/json",
                "Cache-Control": "no-store",
            },
            "body": encode({"error": str(error)}).decode(),
        }
