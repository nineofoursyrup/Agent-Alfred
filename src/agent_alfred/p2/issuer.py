"""Account A operator-only synthetic issuer and read-only proposal display."""

import base64
import html
import json
import secrets
from datetime import timedelta

from .authority import validate_submission
from .authority_handler import principal_role
from .common import (
    BoundaryError,
    check_id,
    decode,
    deployment_active,
    digest,
    encode,
    exact,
    parse_utc,
    required_env,
    synthetic_only,
    utc_now,
    within_window,
)
from .remote import PrivateApi


class SyntheticIssuer:
    def __init__(self, authority_api, signer, queue, *, now=utc_now):
        self.authority_api, self.signer, self.queue = authority_api, signer, queue
        self.now = now

    def view(self, job_id):
        view = self.authority_api.call("proposal", {"job_id": check_id(job_id)})
        exact(view, {"job_id", "state", "proposal", "batch"})
        validate_submission({"proposal": view["proposal"], "batch": view["batch"]})
        return view

    def emit(self, job_id, decision, subject, deadline=None):
        if decision not in ("ISSUE", "REVOKE"):
            raise BoundaryError("issuer_decision_invalid")
        if decision == "ISSUE":
            within_window()
        view = self.view(job_id)
        if decision == "ISSUE" and view["state"] != "PENDING":
            raise BoundaryError("issuer_decision_state_invalid")
        if decision == "REVOKE" and view["state"] not in ("ACTIVE", "SUSPENDED"):
            raise BoundaryError("issuer_decision_state_invalid")
        now = self.now()
        deadline = parse_utc(deadline) if deadline else now + timedelta(seconds=60)
        if not now < deadline <= now + timedelta(seconds=3600):
            raise BoundaryError("issuer_event_time_invalid")
        body = {
            "version": 1,
            "kind": decision,
            "job_id": job_id,
            "proposal_digest": digest(view["proposal"]),
            "event_id": secrets.token_hex(16),
            "nonce": view["proposal"]["nonce"],
            "issued_at": now.isoformat(),
            "activation_deadline": deadline.isoformat(),
            "subject": "synthetic:" + subject,
            "fee_cap": {"currency": "USD", "amount": "0", "unknown": False},
        }
        signature = self.signer(encode(body))
        envelope = {"body": body, "signature": base64.b64encode(signature).decode()}
        self.queue(encode(envelope).decode())
        return {
            "job_id": job_id,
            "event_id": body["event_id"],
            "decision": decision,
            "state": "QUEUED_SYNTHETIC_ONLY",
        }


def handler(event, _context):
    synthetic_only()
    deployment_active()
    import boto3

    try:
        operator = principal_role(event)
        if operator != required_env("P2_OPERATOR_ROLE_ARN"):
            raise BoundaryError("issuer_operator_denied")
        region = required_env("AWS_REGION")
        key_arn = required_env("P2_SYNTHETIC_KMS_ARN")
        queue_url = required_env("P2_ISSUER_QUEUE_URL")
        kms = boto3.client("kms")
        sqs = boto3.client("sqs")

        def sign(raw):
            return kms.sign(
                KeyId=key_arn,
                Message=raw,
                MessageType="RAW",
                SigningAlgorithm="ECDSA_SHA_256",
            )["Signature"]

        def send(message):
            sqs.send_message(QueueUrl=queue_url, MessageBody=message)

        issuer = SyntheticIssuer(
            PrivateApi(required_env("P2_AUTHORITY_API_BASE"), region), sign, send
        )
        method = event.get("httpMethod")
        path = event.get("path", "")
        if method == "GET" and path.endswith("/v1/proposal"):
            job_id = (event.get("queryStringParameters") or {}).get("job_id")
            view = issuer.view(job_id)
            document = html.escape(json.dumps(view, ensure_ascii=False, indent=2))
            page = (
                "<!doctype html><meta charset='utf-8'>"
                "<title>P2 synthetic proposal</title>"
                "<h1>P2 合成 proposal；不是真实用户确认</h1><pre>" + document + "</pre>"
            )
            return {
                "statusCode": 200,
                "headers": {
                    "Content-Type": "text/html; charset=utf-8",
                    "Cache-Control": "no-store",
                    "Content-Security-Policy": "default-src 'none'; style-src 'none'",
                },
                "body": page,
            }
        if method == "POST" and path.endswith(("/v1/issue", "/v1/revoke")):
            body = decode(event.get("body") or "")
            if path.endswith("/v1/issue"):
                exact(body, {"job_id", "activation_deadline"})
                result = issuer.emit(
                    body["job_id"],
                    "ISSUE",
                    operator,
                    deadline=body["activation_deadline"],
                )
            else:
                exact(body, {"job_id"})
                result = issuer.emit(body["job_id"], "REVOKE", operator)
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": encode(result).decode(),
            }
        raise BoundaryError("issuer_action_invalid")
    except BoundaryError as error:
        return {
            "statusCode": 403,
            "headers": {"Content-Type": "application/json"},
            "body": encode({"error": str(error)}).decode(),
        }
