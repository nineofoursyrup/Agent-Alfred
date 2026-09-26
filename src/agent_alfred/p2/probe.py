"""P2 negative boundary probe, run under the actual worker/Dispatch task role.

Never prints a secret or successful response body. Any unexpected permission
is a hard failure. The supplied ARNs are resource identifiers, not secrets.
"""

import json
import os
import socket
import urllib.error
import urllib.request

from .common import BoundaryError, exact, synthetic_only

TARGETS = {
    "secret_arn",
    "signing_key_arn",
    "preflight_key_arn",
    "grant_table_name",
    "anchor_table_arn",
    "anchor_function_arn",
    "admin_role_arn",
    "canary_api_base",
}


def _outcome(call):
    from botocore.exceptions import (
        ClientError,
        ConnectTimeoutError,
        EndpointConnectionError,
    )

    try:
        call()
    except ClientError as error:
        error_type = type(error).__name__
        code = error.response.get("Error", {}).get("Code", "")
        category = (
            "AWS_ACCESS_DENIED"
            if code
            in (
                "AccessDenied",
                "AccessDeniedException",
                "UnauthorizedOperation",
                "NotAuthorized",
                "NotAuthorizedException",
            )
            else "OTHER_ERROR"
        )
    except (ConnectTimeoutError, EndpointConnectionError, OSError) as error:
        error_type = type(error).__name__
        category = "NETWORK_BLOCKED"
    except BoundaryError as error:
        error_type = type(error).__name__
        category = {
            "private_api_rejected:403": "GATEWAY_DENIED",
            "private_api_state_unknown": "NETWORK_BLOCKED",
        }.get(str(error), "OTHER_ERROR")
    except Exception as error:
        error_type = type(error).__name__
        category = "OTHER_ERROR"
    else:
        return {"blocked": False, "category": "OPEN", "error_type": None}
    return {
        "blocked": category
        in ("AWS_ACCESS_DENIED", "NETWORK_BLOCKED", "GATEWAY_DENIED"),
        "category": category,
        "error_type": error_type,
    }


def run(targets, *, role="worker", aws_session=None):
    synthetic_only()
    exact(targets, TARGETS)
    if role not in ("worker", "executor", "authority", "dispatch"):
        raise BoundaryError("probe_role_invalid")
    if aws_session is None:
        import boto3

        aws_session = boto3.session.Session()
    from botocore.config import Config

    config = Config(
        connect_timeout=2, read_timeout=2, retries={"total_max_attempts": 1}
    )

    def client(service):
        if service == "sts":
            return aws_session.client(
                service,
                endpoint_url="https://sts."
                + os.environ["AWS_REGION"]
                + ".amazonaws.com",
                config=config,
            )
        return aws_session.client(service, config=config)

    checks = {
        "issuer_sign": lambda: client("kms").sign(
            KeyId=targets["signing_key_arn"],
            Message=b"p2-negative-probe",
            MessageType="RAW",
            SigningAlgorithm="ECDSA_SHA_256",
        ),
        "preflight_sign": lambda: client("kms").sign(
            KeyId=targets["preflight_key_arn"],
            Message=b"p2-negative-probe",
            MessageType="RAW",
            SigningAlgorithm="ECDSA_SHA_256",
        ),
        "admin_assume": lambda: client("sts").assume_role(
            RoleArn=targets["admin_role_arn"], RoleSessionName="p2-negative-probe"
        ),
        "public_ipv4": lambda: socket.create_connection(
            ("1.1.1.1", 443), timeout=2
        ).close(),
        "public_dns": lambda: socket.getaddrinfo("example.com", 443),
        "direct_dns": lambda: _direct_dns(),
        "public_ipv6": lambda: socket.create_connection(
            ("2606:4700:4700::1111", 443), timeout=2
        ).close(),
        "proxy_bypass": lambda: _proxy_probe(),
    }
    if role != "authority":
        checks["grant_read"] = lambda: client("dynamodb").get_item(
            TableName=targets["grant_table_name"],
            Key={"PK": {"S": "p2-negative-probe"}, "SK": {"S": "STATE"}},
            ConsistentRead=True,
        )
        checks["grant_write"] = lambda: client("dynamodb").put_item(
            TableName=targets["grant_table_name"],
            Item={"PK": {"S": "p2-negative-probe"}, "SK": {"S": "STATE"}},
            ConditionExpression="attribute_not_exists(PK)",
        )
        checks["anchor_invoke"] = lambda: client("lambda").invoke(
            FunctionName=targets["anchor_function_arn"],
            InvocationType="RequestResponse",
            Payload=b"{}",
        )
    checks["anchor_table_write"] = lambda: client("dynamodb").update_item(
        TableName=targets["anchor_table_arn"],
        Key={"PK": {"S": "p2-negative-probe"}},
        UpdateExpression="SET probe = :value",
        ConditionExpression="attribute_exists(PK) AND attribute_not_exists(PK)",
        ExpressionAttributeValues={":value": {"S": "synthetic"}},
    )
    if role != "dispatch":
        from .remote import PrivateApi

        checks["secret_read"] = lambda: client("secretsmanager").get_secret_value(
            SecretId=targets["secret_arn"]
        )
        checks["canary_direct"] = lambda: PrivateApi(
            targets["canary_api_base"], os.environ["AWS_REGION"]
        ).call("canary", {})
    result = {name: _outcome(operation) for name, operation in checks.items()}
    result["all_blocked"] = all(row["blocked"] for row in result.values())
    return result


def _direct_dns():
    packet = bytes.fromhex("123401000001000000000000076578616d706c6503636f6d0000010001")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        connection.settimeout(2)
        connection.sendto(packet, ("8.8.8.8", 53))
        connection.recvfrom(512)


def _proxy_probe():
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"https": "http://1.1.1.1:8080"})
    )
    with opener.open("https://example.com", timeout=2):
        return None


def main():
    targets = json.loads(os.environ["P2_PROBE_TARGETS"])
    result = run(targets, role=os.environ.get("P2_PROBE_ROLE", "worker"))
    print(json.dumps(result, sort_keys=True))
    if not result["all_blocked"]:
        raise BoundaryError("p2_negative_probe_failed")


def handler(event, _context):
    if os.environ.get("P2_PROBE_ONLY") != "1":
        raise BoundaryError("probe_handler_disabled")
    result = run(event, role=os.environ.get("P2_PROBE_ROLE", ""))
    request_id = _context.aws_request_id
    print(
        json.dumps(
            {
                "p2_audit": "lambda_negative_probe",
                "request_id": request_id,
                "result": result,
            },
            sort_keys=True,
        )
    )
    return {"request_id": request_id, "result": result}


if __name__ == "__main__":
    main()
