"""Account B credential-bearing Dispatch; only the fixed private canary target."""

from .common import (
    BoundaryError,
    check_digest,
    check_id,
    deployment_active,
    digest,
    dispatch_allowed,
    exact,
    required_env,
    synthetic_only,
)
from .remote import PrivateApi


class Dispatch:
    def __init__(self, authority_api, canary_api, secret_reader):
        self.authority_api = authority_api
        self.canary_api = canary_api
        self.secret_reader = secret_reader

    def send(self, request):
        exact(request, {"job_id", "attempt_id", "role", "request_digest", "descriptor"})
        check_id(request["job_id"])
        check_id(request["attempt_id"])
        check_digest(request["request_digest"])
        descriptor = exact(
            request["descriptor"],
            {
                "profile_id",
                "endpoint_id",
                "model_id",
                "max_tokens",
                "body",
            },
        )
        if digest(descriptor) != request["request_digest"]:
            raise BoundaryError("dispatch_payload_mismatch")
        if descriptor["endpoint_id"] != "p2-private-canary":
            raise BoundaryError("dispatch_target_denied")
        if (
            self.authority_api.call(
                "preflight",
                {
                    "job_id": request["job_id"],
                    "attempt_id": request["attempt_id"],
                    "request_digest": request["request_digest"],
                },
            ).get("allowed")
            is not True
        ):
            raise BoundaryError("send_intent_unverifiable")
        token = self.secret_reader()
        if not isinstance(token, str) or not token:
            raise BoundaryError("synthetic_secret_unavailable")
        result = self.canary_api.call(
            "canary",
            {
                "job_id": request["job_id"],
                "attempt_id": request["attempt_id"],
                "request_digest": request["request_digest"],
                "descriptor": descriptor,
                "synthetic_token": token,
            },
        )
        # No retry: a lost response may mean that the canary saw the request.
        return result


def handler(event, _context):
    synthetic_only()
    deployment_active()
    dispatch_allowed()
    import boto3

    region = required_env("AWS_REGION")
    secret_arn = required_env("P2_SYNTHETIC_SECRET_ARN")
    secrets_client = boto3.client("secretsmanager")

    def read_secret():
        return secrets_client.get_secret_value(SecretId=secret_arn)["SecretString"]

    service = Dispatch(
        PrivateApi(required_env("P2_AUTHORITY_API_BASE"), region),
        PrivateApi(required_env("P2_CANARY_API_BASE"), region),
        read_secret,
    )
    return service.send(event)
