"""Account-admin P2 deploy, readback, task, stop and cleanup entry.

The script has no implicit AWS mutation. Each administrator runs one phase
with their own STS identity and an explicit --execute flag. State contains
only non-secret resource IDs and hashes; never credentials or canary token.
"""

import argparse
import base64
import binascii
import hashlib
import json
import re
import secrets
import subprocess
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .stacks import account_a, account_b, account_c, foundation

BUILDERS = {
    "foundation-a": lambda: foundation("a"),
    "foundation-b": lambda: foundation("b"),
    "foundation-c": lambda: foundation("c"),
    "service-a": account_a,
    "service-b": account_b,
    "service-c": account_c,
}
ROLE = re.compile(r"arn:aws:iam::([0-9]{12}):role/[A-Za-z0-9+=,.@_/-]+\Z")
ASSUMED = re.compile(r"arn:aws:sts::([0-9]{12}):assumed-role/([^/]+)/[^/]+\Z")


def role_arn(value):
    match = ASSUMED.fullmatch(value)
    return f"arn:aws:iam::{match.group(1)}:role/{match.group(2)}" if match else value


def parse_config(path, *, require_active=True):
    config = json.loads(path.read_text())
    if (
        config.get("schema") != "p2-aws-execution-v1"
        or config.get("source_merge_sha") != "b7826a12144ec77e8656b01b02e49e0ce740f01e"
    ):
        raise ValueError("P2 config schema or baseline mismatch")
    if "<" in json.dumps(config) or "UNPROVIDED" in json.dumps(config):
        raise ValueError("P2 config has unresolved fields")
    if not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-\d", config["region"]):
        raise ValueError("invalid AWS region")
    if config["region"].startswith(("cn-", "us-gov-")):
        raise ValueError("P2 candidate currently requires the commercial AWS partition")
    accounts = config["accounts"]
    if (
        set(accounts) != {"a", "b", "c"}
        or len(set(accounts.values())) != 3
        or any(not re.fullmatch(r"[0-9]{12}", value) for value in accounts.values())
    ):
        raise ValueError("three distinct account IDs required")
    for account in accounts:
        role = config["admin_role_arns"][account]
        role_match = ROLE.fullmatch(role)
        if not role_match or role_match.group(1) != accounts[account]:
            raise ValueError("admin role/account mismatch")
        auditor = config["auditor_role_arns"][account]
        auditor_match = ROLE.fullmatch(auditor)
        if (
            not auditor_match
            or auditor_match.group(1) != accounts[account]
            or auditor == role
        ):
            raise ValueError("independent auditor role/account mismatch")
    operator = config["operator_role_arn"]
    operator_match = ROLE.fullmatch(operator)
    if (
        not operator_match
        or operator_match.group(1) != accounts["a"]
        or "/" in operator.split(":role/", 1)[1]
    ):
        raise ValueError("issuer operator role must be pathless in account A")
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,30}", config["stack_prefix"]):
        raise ValueError("invalid stack prefix")
    expiry = datetime.fromisoformat(config["expires_at_utc"].replace("Z", "+00:00"))
    if expiry.tzinfo is None or (
        require_active and expiry <= datetime.now(timezone.utc)
    ):
        raise ValueError("P2 deployment expiry required in future")
    budget = config["budget"]
    try:
        approved = Decimal(budget["approved_amount"])
        reviewed = Decimal(budget["reviewed_upper_bound"])
    except InvalidOperation as error:
        raise ValueError("approved and reviewed amount required") from error
    if (
        not approved.is_finite()
        or not reviewed.is_finite()
        or not 0 < reviewed <= approved
    ):
        raise ValueError("reviewed upper bound exceeds approved budget")
    if not re.fullmatch(r"[A-Z]{3}", budget["currency"]):
        raise ValueError("budget currency required")
    if budget["tax_included"] not in ("yes", "no"):
        raise ValueError("tax treatment required")
    if not budget["quote_source"] or budget["quote_source"].startswith("<"):
        raise ValueError("account and region cost evidence required")
    duration = int(budget["duration_hours"])
    if not 1 <= duration <= 168 or (
        require_active
        and expiry > datetime.now(timezone.utc) + timedelta(hours=duration)
    ):
        raise ValueError("expiry exceeds approved duration")
    for key in ("object_lock_days", "log_days"):
        if not 1 <= int(config["retention"][key]) <= 365:
            raise ValueError("approved retention required")
        if int(config["retention"][key]) * 24 <= duration:
            raise ValueError("retention must exceed approved deployment duration")
    if int(config["retention"]["log_days"]) not in {
        1,
        3,
        5,
        7,
        14,
        30,
        60,
        90,
        120,
        150,
        180,
        365,
    }:
        raise ValueError("unsupported CloudWatch log retention")
    return config


def load_state(path):
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def account_for(phase):
    if phase.endswith("-a"):
        return "a"
    if phase.endswith("-b"):
        return "b"
    if phase.endswith("-c"):
        return "c"
    raise ValueError("unknown phase")


def client_session(config, account, *, role="admin"):
    import boto3

    session = boto3.session.Session(region_name=config["region"])
    caller = session.client("sts").get_caller_identity()
    expected = (
        config["operator_role_arn"]
        if role == "operator"
        else config["auditor_role_arns"][account]
        if role == "auditor"
        else config["admin_role_arns"][account]
    )
    if caller["Account"] != config["accounts"][account]:
        raise ValueError("current AWS identity is not the configured independent role")
    actual = role_arn(caller["Arn"])
    if actual != expected and ASSUMED.fullmatch(caller["Arn"]):
        role_name = ASSUMED.fullmatch(caller["Arn"]).group(2)
        actual = session.client("iam").get_role(RoleName=role_name)["Role"]["Arn"]
    if actual != expected:
        raise ValueError("current AWS identity is not the configured independent role")
    return session


def outputs(client, name):
    stack = client.describe_stacks(StackName=name)["Stacks"][0]
    if stack["StackStatus"] not in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
        raise ValueError("stack not complete: " + name)
    return {row["OutputKey"]: row["OutputValue"] for row in stack.get("Outputs", [])}


def stack_snapshot(client, name):
    from botocore.exceptions import ClientError

    try:
        return client.describe_stacks(StackName=name)["Stacks"][0]
    except ClientError as error:
        if "does not exist" in str(error):
            return None
        raise


def live_controls(client, name):
    stack = stack_snapshot(client, name)
    if stack is None:
        return None
    return {
        "status": stack["StackStatus"],
        **{
            row["ParameterKey"]: row.get("ParameterValue")
            for row in stack.get("Parameters", [])
        },
    }


def require_live_stopped(client, config, phase):
    controls = live_controls(client, stack_name(config, phase))
    if controls is None:
        raise ValueError("service stack missing from live control plane")
    if controls.get("EnableTraffic") != "false" or (
        phase == "service-b" and controls.get("StopDispatch") != "true"
    ):
        raise ValueError("service traffic is still active in AWS")
    return controls


def require_staged(state, phase):
    record = state.get(phase, {})
    if (
        record.get("P2TrafficState") != "false"
        or record.get("P2NeverActivated") is not True
        or record.get("P2Closed")
        or record.get("P2StopAt")
    ):
        raise ValueError("only a never-stopped staged service may change")


def stack_name(config, phase):
    return config["stack_prefix"] + "-" + phase


def upsert_stack(client, name, template, parameters):
    from botocore.exceptions import ClientError

    try:
        prior = client.describe_stacks(StackName=name)["Stacks"][0]
    except ClientError as error:
        if "does not exist" not in str(error):
            raise
        prior = None
    rows = []
    for key, definition in template["Parameters"].items():
        if key in parameters:
            rows.append({"ParameterKey": key, "ParameterValue": str(parameters[key])})
        elif prior and any(p["ParameterKey"] == key for p in prior["Parameters"]):
            rows.append({"ParameterKey": key, "UsePreviousValue": True})
        elif "Default" not in definition:
            raise ValueError("missing stack parameter: " + key)
    kwargs = {
        "StackName": name,
        "TemplateBody": json.dumps(template, separators=(",", ":")),
        "Parameters": rows,
        "Capabilities": ["CAPABILITY_IAM"],
        "Tags": [{"Key": "p2-synthetic-only", "Value": "true"}],
    }
    if prior:
        try:
            client.update_stack(**kwargs)
        except ClientError as error:
            if "No updates are to be performed" not in str(error):
                raise
        else:
            client.get_waiter("stack_update_complete").wait(StackName=name)
    else:
        client.create_stack(**kwargs)
        client.get_waiter("stack_create_complete").wait(StackName=name)
    return outputs(client, name)


def required(state, phase, field):
    try:
        return state[phase][field]
    except KeyError as error:
        raise ValueError(f"missing independent readback {phase}.{field}") from error


def prefix_list(session, region, name):
    rows = session.client("ec2").describe_managed_prefix_lists(
        Filters=[
            {"Name": "prefix-list-name", "Values": [f"com.amazonaws.{region}.{name}"]}
        ]
    )["PrefixLists"]
    if len(rows) != 1:
        raise ValueError("region prefix list not unique: " + name)
    return rows[0]["PrefixListId"]


def firewall_fail_closed(session, stack_outputs, *, configure):
    resolver = session.client("route53resolver")
    ec2 = session.client("ec2")
    for key, vpc in stack_outputs.items():
        if not key.endswith("VpcId"):
            continue
        if configure:
            resolver.update_firewall_config(ResourceId=vpc, FirewallFailOpen="DISABLED")
        observed = resolver.get_firewall_config(ResourceId=vpc)["FirewallConfig"]
        if observed.get("FirewallFailOpen") != "DISABLED":
            raise ValueError("DNS Firewall fail-open not disabled: " + vpc)
        routes = ec2.describe_route_tables(
            Filters=[{"Name": "vpc-id", "Values": [vpc]}]
        )["RouteTables"]
        for table in routes:
            for route in table["Routes"]:
                if (
                    route.get("DestinationCidrBlock") == "0.0.0.0/0"
                    or route.get("DestinationIpv6CidrBlock") == "::/0"
                ):
                    raise ValueError("public default route found: " + vpc)


def parameters(config, state, phase, session):
    account = account_for(phase)
    if phase.startswith("foundation-"):
        return (
            {
                "BAccountId": config["accounts"]["b"],
                "BAuthorityRoleArn": required(
                    state, "foundation-b", "AuthorityRoleArn"
                ),
            }
            if account == "a"
            else {}
        )
    common = {
        "CodeBucket": required(state, "foundation-" + account, "CodeBucketName"),
        "CodeKey": required(state, "artifact-" + account, "CodeKey"),
        "LogRetentionDays": config["retention"]["log_days"],
    }
    if account in ("a", "b"):
        common["ExpiresAtUtc"] = config["expires_at_utc"]
    if account == "c":
        return {
            **common,
            "AAdminRoleArn": config["admin_role_arns"]["a"],
            "BAdminRoleArn": config["admin_role_arns"]["b"],
            "CAdminRoleArn": config["admin_role_arns"]["c"],
            "AuthorityRoleArn": required(state, "foundation-b", "AuthorityRoleArn"),
            "AAccountId": config["accounts"]["a"],
            "BAccountId": config["accounts"]["b"],
            "RetentionDays": config["retention"]["object_lock_days"],
            "S3PrefixListId": prefix_list(session, config["region"], "s3"),
            "DynamoPrefixListId": prefix_list(session, config["region"], "dynamodb"),
        }
    if account == "a":
        return {
            **common,
            "BAccountId": config["accounts"]["b"],
            "AuthorityApiId": required(state, "service-b", "AuthorityApiId"),
            "IssuerQueueArn": required(state, "foundation-b", "IssuerQueueArn"),
            "IssuerQueueUrl": required(state, "foundation-b", "IssuerQueueUrl"),
            "OperatorRoleArn": config["operator_role_arn"],
            "IssuerRoleArn": required(state, "foundation-a", "IssuerRoleArn"),
            "IssuerRoleName": required(state, "foundation-a", "IssuerRoleName"),
            "SigningKeyArn": required(state, "foundation-a", "SigningKeyArn"),
            "AuditBucketName": required(state, "service-c", "AuditBucketName"),
            "EnableTraffic": "false",
        }
    result = {
        **common,
        "AAccountId": config["accounts"]["a"],
        "CAccountId": config["accounts"]["c"],
        "IssuerRoleArn": required(state, "foundation-a", "IssuerRoleArn"),
        "IssuerKeyArn": required(state, "foundation-a", "SigningKeyArn"),
        "AuthorityRoleArn": required(state, "foundation-b", "AuthorityRoleArn"),
        "AuthorityRoleName": required(state, "foundation-b", "AuthorityRoleName"),
        "IssuerQueueArn": required(state, "foundation-b", "IssuerQueueArn"),
        "IssuerQueueUrl": required(state, "foundation-b", "IssuerQueueUrl"),
        "AnchorFunctionArn": required(state, "service-c", "AnchorFunctionArn"),
        "AuditBucketName": required(state, "service-c", "AuditBucketName"),
        "EnableTraffic": "false",
        "ImageUri": required(state, "image-b", "ImageUri"),
        "ImageRepositoryArn": required(state, "foundation-b", "ImageRepositoryArn"),
        "PackageSha256": required(state, "artifact-b", "PackageSha256"),
        "InputSha256": required(state, "artifact-b", "InputSha256"),
        "S3PrefixListId": prefix_list(session, config["region"], "s3"),
        "DynamoPrefixListId": prefix_list(session, config["region"], "dynamodb"),
    }
    if not state.get("service-b"):
        token = secrets.token_urlsafe(32)
        result["SyntheticSecretValue"] = token
        result["CanaryTokenSha256"] = hashlib.sha256(token.encode()).hexdigest()
    if state.get("service-a", {}).get("IssuerExecuteApiEndpointId"):
        result["AIssuerVpceId"] = required(
            state, "service-a", "IssuerExecuteApiEndpointId"
        )
    return result


def upload(session, state, account, artifact_dir):
    manifest = json.loads((artifact_dir / "manifest.json").read_text())
    archive = artifact_dir / "lambda.zip"
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual != manifest["lambda_zip_sha256"]:
        raise ValueError("lambda artifact hash mismatch")
    bucket = required(state, "foundation-" + account, "CodeBucketName")
    key = "p2/" + actual + "/lambda.zip"
    s3 = session.client("s3")
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=archive.read_bytes(),
        ServerSideEncryption="AES256",
        Metadata={"sha256": actual},
    )
    head = s3.head_object(Bucket=bucket, Key=key)
    if head.get("Metadata", {}).get("sha256") != actual:
        raise ValueError("uploaded Lambda artifact not verified")
    return {
        "CodeKey": key,
        "LambdaZipSha256": actual,
        "PackageSha256": manifest["package_sha256"],
        "InputSha256": manifest["input_sha256"],
    }


def push_image(session, state, artifact_dir):
    metadata = json.loads((artifact_dir / "manifest.json").read_text())
    if not metadata.get("image_tag"):
        raise ValueError("Docker image has not been built from a pinned base")
    repository = required(state, "foundation-b", "ImageRepositoryUri")
    ecr = session.client("ecr")
    credential = ecr.get_authorization_token()["authorizationData"][0]
    password = (
        base64.b64decode(credential["authorizationToken"]).decode().split(":", 1)[1]
    )
    registry = repository.split("/", 1)[0]
    subprocess.run(
        ["docker", "login", "--username", "AWS", "--password-stdin", registry],
        input=password,
        text=True,
        check=True,
        capture_output=True,
    )
    try:
        tag = (
            "p2-"
            + metadata["package_sha256"][:12]
            + "-"
            + metadata["input_sha256"][:12]
        )
        target = repository + ":" + tag
        subprocess.run(["docker", "tag", metadata["image_tag"], target], check=True)
        subprocess.run(["docker", "push", target], check=True)
    finally:
        subprocess.run(["docker", "logout", registry], check=True, capture_output=True)
    image = ecr.describe_images(
        repositoryName=repository.rsplit("/", 1)[1], imageIds=[{"imageTag": tag}]
    )["imageDetails"][0]
    return {"ImageUri": repository + "@" + image["imageDigest"], "ImageTag": tag}


def probe_targets(config, state):
    return {
        "secret_arn": required(state, "service-b", "SyntheticCanarySecretArn"),
        "signing_key_arn": required(state, "foundation-a", "SigningKeyArn"),
        "preflight_key_arn": required(state, "service-c", "PreflightSignerKeyArn"),
        "grant_table_name": required(state, "service-b", "GrantTableName"),
        "anchor_table_arn": required(state, "service-c", "AnchorTableArn"),
        "anchor_function_arn": required(state, "service-c", "AnchorFunctionArn"),
        "admin_role_arn": config["admin_role_arns"]["b"],
        "canary_api_base": required(state, "service-b", "CanaryApiBase"),
    }


def run_task(session, config, state, mode, job_id, attempt_id):
    if mode not in (
        "submit",
        "invoke",
        "status",
        "finish",
        "probe",
        "probe-iam",
        "probe-executor",
        "probe-executor-iam",
    ):
        raise ValueError("invalid task mode")
    if state.get("service-b", {}).get("P2Closed") is True:
        raise ValueError("P2 environment is closed; no new task may launch")
    if state.get("service-b", {}).get("P2TrafficState") == "draining":
        raise ValueError("P2 Dispatch is stopping; no new task may launch")
    if (
        mode not in ("probe", "probe-iam", "probe-executor", "probe-executor-iam")
        and state.get("service-b", {}).get("P2TrafficState") != "true"
    ):
        raise ValueError("P2 broker is not activated")
    ecs = session.client("ecs")
    b = state["service-b"]
    task = (
        b["ExecutorTaskArn"]
        if mode in ("submit", "status", "probe-executor", "probe-executor-iam")
        else b["WorkerTaskArn"]
    )
    overrides = [{"name": "P2_RUN_MODE", "value": mode}]
    if job_id:
        overrides.append({"name": "P2_JOB_ID", "value": job_id})
    if attempt_id:
        overrides.append({"name": "P2_ATTEMPT_ID", "value": attempt_id})
    container_override = {"name": "p2-controlled", "environment": overrides}
    if mode in ("probe", "probe-iam", "probe-executor", "probe-executor-iam"):
        overrides.append(
            {
                "name": "P2_PROBE_TARGETS",
                "value": json.dumps(probe_targets(config, state)),
            }
        )
        overrides.append(
            {
                "name": "P2_PROBE_ROLE",
                "value": "executor" if mode.startswith("probe-executor") else "worker",
            }
        )
        container_override["command"] = ["python", "-m", "agent_alfred.p2.probe"]
    response = ecs.run_task(
        cluster=b["WorkerClusterName"],
        taskDefinition=task,
        count=1,
        enableExecuteCommand=False,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": [b["WorkerSubnetId"]],
                "securityGroups": [
                    b["WorkerProbeSgId"]
                    if mode in ("probe-iam", "probe-executor-iam")
                    else b["WorkerWorkloadSgId"]
                ],
                "assignPublicIp": "DISABLED",
            }
        },
        overrides={"containerOverrides": [container_override]},
    )
    if response.get("failures") or len(response.get("tasks", [])) != 1:
        raise ValueError("controlled task launch failed")
    return {"TaskArn": response["tasks"][0]["taskArn"], "Mode": mode}


def probe_dispatch(session, config, state, *, iam=False, role="dispatch"):
    if state.get("service-b", {}).get("P2TrafficState") == "draining" or state.get(
        "service-b", {}
    ).get("P2Closed"):
        raise ValueError("P2 Dispatch probe cannot run after stop")
    client = session.client("lambda")
    if role not in ("dispatch", "authority"):
        raise ValueError("invalid Lambda probe role")
    function_arn = required(
        state,
        "service-b",
        role.title() + ("IamProbeFunctionArn" if iam else "ProbeFunctionArn"),
    )
    response = client.invoke(
        FunctionName=function_arn,
        InvocationType="RequestResponse",
        Payload=json.dumps(probe_targets(config, state)).encode(),
    )
    metadata = {
        "function_arn": function_arn,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    if response.get("FunctionError") or response.get("StatusCode") != 200:
        return {
            **metadata,
            "request_id": response.get("ResponseMetadata", {}).get("RequestId"),
            "result": None,
            "invoke_error": response.get("FunctionError") or "invocation_failed",
        }
    try:
        result = json.loads(response["Payload"].read())
    except KeyError, AttributeError, TypeError, ValueError:
        return {
            **metadata,
            "request_id": response.get("ResponseMetadata", {}).get("RequestId"),
            "result": None,
            "invoke_error": "response_invalid",
        }
    if not result.get("request_id") or type(result.get("result")) is not dict:
        return {
            **metadata,
            "request_id": response.get("ResponseMetadata", {}).get("RequestId"),
            "result": None,
            "invoke_error": "response_invalid",
        }
    return {**metadata, "request_id": result["request_id"], "result": result["result"]}


def stop(session, config, state, account):
    phase = "service-" + account
    client = session.client("cloudformation")
    name = stack_name(config, phase)
    updates = {"StopDispatch": "true"} if account == "b" else {"EnableTraffic": "false"}
    updated = upsert_stack(client, name, BUILDERS[phase](), updates)
    if account == "b":
        ecs = session.client("ecs")
        cluster = updated["WorkerClusterName"]
        running = running_tasks(ecs, cluster)
        for task in running:
            ecs.stop_task(cluster=cluster, task=task, reason="P2 synthetic stop")
        for offset in range(0, len(running), 100):
            ecs.get_waiter("tasks_stopped").wait(
                cluster=cluster, tasks=running[offset : offset + 100]
            )
        assert_no_running_tasks(ecs, cluster)
        updated["P2StoppedTaskCount"] = len(running)
    return updated


def running_tasks(ecs, cluster):
    return [
        task
        for page in ecs.get_paginator("list_tasks").paginate(
            cluster=cluster, desiredStatus="RUNNING"
        )
        for task in page.get("taskArns", [])
    ]


def assert_no_running_tasks(ecs, cluster):
    if running_tasks(ecs, cluster):
        raise ValueError("controlled Fargate tasks still running")


def require_drained(session, state):
    """Leave B handling late REVOKE until the queue and grants are reconciled."""
    from agent_alfred.p2.common import decode

    if state.get("service-a") and state["service-a"].get("P2TrafficState") != "false":
        raise ValueError("A issuer ingress must be stopped before final B shutdown")
    assert_no_running_tasks(
        session.client("ecs"), required(state, "service-b", "WorkerClusterName")
    )
    sqs = session.client("sqs")
    for queue in ("IssuerQueueUrl", "IssuerDlqUrl"):
        attributes = sqs.get_queue_attributes(
            QueueUrl=required(state, "foundation-b", queue),
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesDelayed",
                "ApproximateNumberOfMessagesNotVisible",
            ],
        )["Attributes"]
        if any(int(value) != 0 for value in attributes.values()):
            raise ValueError("issuer queue not drained: " + queue)
    ddb = session.client("dynamodb")
    table = required(state, "service-b", "GrantTableName")
    token = None
    while True:
        kwargs = {
            "TableName": table,
            "ConsistentRead": True,
            "ProjectionExpression": "PK, SK, Document",
        }
        if token:
            kwargs["ExclusiveStartKey"] = token
        page = ddb.scan(**kwargs)
        for item in page.get("Items", []):
            if item["SK"]["S"] != "STATE":
                continue
            state_row = decode(item["Document"]["S"])
            if state_row["state"] == "ACTIVE":
                raise ValueError("active synthetic grant remains: " + item["PK"]["S"])
        token = page.get("LastEvaluatedKey")
        if not token:
            break


def auditor_readback(config, account, path, *, after=None):
    if path is None:
        raise ValueError("fresh independent auditor readback required: " + account)
    raw = path.read_bytes()
    digest_value = hashlib.sha256(raw).hexdigest()
    recorded = path.with_suffix(path.suffix + ".sha256")
    if not raw or not recorded.exists() or recorded.read_text().strip() != digest_value:
        raise ValueError("independent auditor readback hash mismatch")
    record = json.loads(raw)
    now = datetime.now(timezone.utc)
    observed = datetime.fromisoformat(record["at"])
    if (
        observed.tzinfo is None
        or observed > now
        or now - observed > timedelta(hours=1)
        or after is not None
        and observed < after
        or record.get("account") != config["accounts"][account]
        or record.get("region") != config["region"]
        or record.get("auditor_role") != config["auditor_role_arns"][account]
    ):
        raise ValueError("independent auditor readback identity or freshness mismatch")
    return record


def preflight_payload(config_path, path, expected_sha256):
    if path is None or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or ""):
        raise ValueError("independently reviewed preflight receipt and SHA256 required")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("preflight receipt differs from reviewed SHA256")
    receipt = json.loads(raw)
    checked_at = datetime.fromisoformat(receipt["checked_at"])
    now = datetime.now(timezone.utc)
    if (
        checked_at.tzinfo is None
        or checked_at > now
        or now - checked_at > timedelta(hours=1)
        or receipt.get("config_sha256")
        != hashlib.sha256(config_path.read_bytes()).hexdigest()
        or receipt.get("preflight_boundary") != "PASS_SYNTHETIC_CANARY_ONLY"
        or receipt.get("control_plane") != "PASS"
        or receipt.get("network_probes") != "PASS"
        or receipt.get("identity_probes") != "PASS_TESTED_PATHS_ONLY"
        or receipt.get("anchor_table_route_probe") != "PASS_ENDPOINT_DENIAL_ONLY"
        or receipt.get("identity_privilege_expansion") != "PENDING_REAL_EVIDENCE"
        or receipt.get("unverified_identity_paths")
        != [
            "C_anchor_table_role_IAM_direct_write",
            "C_anchor_S3_direct_write",
            "IAM_role_trust_or_policy_mutation",
            "ECS_RunTask_and_PassRole",
            "task_definition_mutation",
            "VPC_SG_DNS_policy_mutation",
        ]
        or not re.fullmatch(r"[0-9a-f]{64}", receipt.get("candidate_sha256", ""))
        or set(receipt.get("readback_sha256", {})) != {"a", "b", "c"}
        or any(
            re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in receipt["readback_sha256"].values()
        )
        or set(receipt.get("probe_sha256", {}))
        != {
            "worker",
            "executor",
            "dispatch",
            "authority",
            "worker_iam",
            "executor_iam",
            "dispatch_iam",
            "authority_iam",
        }
        or any(
            re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in receipt["probe_sha256"].values()
        )
    ):
        raise ValueError("preflight is stale, incomplete or for a different config")
    return receipt


def preflight_signature_message(signature):
    fields = {
        key: signature[key]
        for key in (
            "receipt_sha256",
            "key_arn",
            "signer_role_arn",
            "signed_at",
            "signing_algorithm",
        )
    }
    return hashlib.sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    ).digest()


def require_preflight(
    session, config, config_path, state, path, expected_sha256, signature_path
):
    receipt = preflight_payload(config_path, path, expected_sha256)
    if signature_path is None:
        raise ValueError("C administrator preflight signature required")
    signature = json.loads(signature_path.read_text())
    key_arn = required(state, "service-c", "PreflightSignerKeyArn")
    if (
        signature.get("receipt_sha256") != expected_sha256
        or signature.get("key_arn") != key_arn
        or signature.get("signer_role_arn") != config["admin_role_arns"]["c"]
        or signature.get("signing_algorithm") != "ECDSA_SHA_256"
        or not key_arn.startswith(
            f"arn:aws:kms:{config['region']}:{config['accounts']['c']}:key/"
        )
    ):
        raise ValueError("preflight signature is not bound to independent C key")
    signed_at = datetime.fromisoformat(signature["signed_at"])
    if (
        signed_at.tzinfo is None
        or signed_at < datetime.fromisoformat(receipt["checked_at"])
        or signed_at > datetime.now(timezone.utc)
    ):
        raise ValueError("preflight signature chronology invalid")
    try:
        signature_bytes = base64.b64decode(signature["signature"], validate=True)
    except (KeyError, ValueError, binascii.Error) as error:
        raise ValueError("preflight signature encoding invalid") from error
    verified = session.client("kms").verify(
        KeyId=key_arn,
        Message=preflight_signature_message(signature),
        MessageType="DIGEST",
        Signature=signature_bytes,
        SigningAlgorithm="ECDSA_SHA_256",
    )
    if verified.get("SignatureValid") is not True or verified.get("KeyId") != key_arn:
        raise ValueError("C administrator preflight signature not verified")
    return receipt


def require_a_stopped(config, path, *, after=None):
    record = auditor_readback(config, "a", path, after=after)
    service = record.get("stacks", {}).get("service-a")
    if service is None:
        return record
    if service.get("traffic_parameter") != "false":
        raise ValueError("A issuer traffic is not stopped in auditor readback")
    issuer = [
        row["physical_id"]
        for row in service.get("resources", [])
        if row["logical_id"] == "IssuerFunction" and row.get("physical_id")
    ]
    if len(issuer) != 1 or not any(
        function.get("arn", "").endswith(":function:" + issuer[0])
        and function.get("p2_deployment_active") == "false"
        for function in record.get("functions", {}).values()
    ):
        raise ValueError("A issuer function is not stopped in auditor readback")
    return record


def record_phase_result(state, phase, result):
    if result.get("cleanup_status") == "DELETE_FAILED":
        state.setdefault("cleanup_attempts", []).append(
            {
                "phase": phase,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                **result,
            }
        )
        return
    if (
        phase.startswith("service-")
        and result.get("cleanup_status") != "DELETED"
        and state.get(phase, {}).get("P2CreatedAt")
    ):
        result.setdefault("P2CreatedAt", state[phase]["P2CreatedAt"])
    if result.get("cleanup_status") == "DELETED" and phase in state:
        result = {**result, "prior_state": state[phase]}
    state[phase] = result


def require_ab_absent(config, state, a_path, b_path):
    for account, path in (("a", a_path), ("b", b_path)):
        phase = "service-" + account
        service = state.get(phase)
        if service and service.get("cleanup_status") != "DELETED":
            raise ValueError("A/B service lacks successful deletion receipt")
        after_text = (
            service.get("deleted_at")
            if service
            else state.get("service-c", {}).get("P2CreatedAt")
        )
        if not after_text:
            raise ValueError(
                "A/B deletion or C creation timestamp missing; "
                "independent recovery required"
            )
        record = auditor_readback(
            config,
            account,
            path,
            after=datetime.fromisoformat(after_text),
        )
        if datetime.now(timezone.utc) - datetime.fromisoformat(
            record["at"]
        ) > timedelta(minutes=5):
            raise ValueError("A/B deletion readback is not recent")
        if "service-" + account in record.get("stacks", {}):
            raise ValueError("A/B service still exists in auditor readback")


def emergency_evidence(config, state, readback_path, first_failure_path):
    """Bind an auditor readback and first failure before shutting a broken B plane."""
    if not readback_path or not first_failure_path:
        raise ValueError("emergency stop requires auditor readback and first failure")
    raw_readback = readback_path.read_bytes()
    raw_failure = first_failure_path.read_bytes()
    if not raw_readback or not raw_failure:
        raise ValueError("emergency evidence cannot be empty")
    recorded = readback_path.with_suffix(readback_path.suffix + ".sha256")
    readback_hash = hashlib.sha256(raw_readback).hexdigest()
    if not recorded.exists() or recorded.read_text().strip() != readback_hash:
        raise ValueError("independent auditor readback hash mismatch")
    observation = json.loads(raw_readback)
    if (
        observation.get("auditor_role") != config["auditor_role_arns"]["b"]
        or observation.get("account") != config["accounts"]["b"]
        or observation.get("region") != config["region"]
    ):
        raise ValueError("independent B auditor readback mismatch")
    stop_at = datetime.fromisoformat(state["service-b"]["P2StopAt"])
    observed_at = datetime.fromisoformat(observation["at"])
    if observed_at < stop_at or observed_at > datetime.now(timezone.utc):
        raise ValueError("auditor readback predates P2 stop")
    if not any(
        function.get("p2_stop_dispatch") == "true"
        for function in observation.get("functions", {}).values()
        if function.get("arn") == state["service-b"]["AuthorityFunctionArn"]
    ):
        raise ValueError("auditor did not observe stopped Dispatch")
    return {
        "readback_sha256": readback_hash,
        "first_failure_sha256": hashlib.sha256(raw_failure).hexdigest(),
        "first_failure_path": str(first_failure_path),
        "unresolved_liability": True,
    }


def empty_code_bucket(session, bucket):
    from botocore.exceptions import ClientError

    s3 = session.client("s3")
    paginator = s3.get_paginator("list_object_versions")
    try:
        entries = [
            {"Key": item["Key"], "VersionId": item["VersionId"]}
            for page in paginator.paginate(Bucket=bucket)
            for item in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]
        ]
        for offset in range(0, len(entries), 1000):
            result = s3.delete_objects(
                Bucket=bucket, Delete={"Objects": entries[offset : offset + 1000]}
            )
            if result.get("Errors"):
                raise ValueError("dedicated code bucket cleanup failed")
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "NoSuchBucket":
            raise


def empty_repository(session, arn):
    from botocore.exceptions import ClientError

    name = arn.rsplit("/", 1)[-1]
    ecr = session.client("ecr")
    try:
        images = [
            image
            for page in ecr.get_paginator("list_images").paginate(repositoryName=name)
            for image in page.get("imageIds", [])
        ]
        for offset in range(0, len(images), 100):
            result = ecr.batch_delete_image(
                repositoryName=name, imageIds=images[offset : offset + 100]
            )
            if result.get("failures"):
                raise ValueError("dedicated ECR image cleanup failed")
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "RepositoryNotFoundException":
            raise


def delete_stack(session, config, phase):
    client = session.client("cloudformation")
    name = stack_name(config, phase)
    stack = stack_snapshot(client, name)
    if stack is None:
        raise ValueError("stack already absent; independent residual readback required")
    prior_outputs = {
        row["OutputKey"]: row["OutputValue"] for row in stack.get("Outputs", [])
    }
    resources = [
        {
            "logical_id": row["LogicalResourceId"],
            "physical_id": row.get("PhysicalResourceId"),
            "status": row["ResourceStatus"],
            "type": row["ResourceType"],
        }
        for page in client.get_paginator("list_stack_resources").paginate(
            StackName=name
        )
        for row in page["StackResourceSummaries"]
    ]
    physical = {row["logical_id"]: row["physical_id"] for row in resources}
    retained = {
        "service-c": ("AuditBucket", "AnchorTable", "PreflightSignerKey"),
        "service-b": ("GrantTable",),
        "foundation-b": ("IssuerQueue", "IssuerDlq"),
    }
    evidence = {
        "stack": name,
        "prior_status": stack["StackStatus"],
        "prior_outputs": prior_outputs,
        "prior_resources": resources,
        "retained_resources": [
            physical[key] for key in retained.get(phase, ()) if physical.get(key)
        ],
    }
    try:
        if phase.startswith("foundation-"):
            bucket = physical.get("CodeBucket") or prior_outputs.get("CodeBucketName")
            if bucket:
                empty_code_bucket(session, bucket)
            if phase == "foundation-b":
                repo = physical.get("WorkerRepository") or prior_outputs.get(
                    "ImageRepositoryArn"
                )
                if repo:
                    empty_repository(session, repo)
        client.delete_stack(StackName=name)
        client.get_waiter("stack_delete_complete").wait(StackName=name)
    except Exception as error:
        evidence["cleanup_status"] = "DELETE_FAILED"
        evidence["cleanup_error"] = type(error).__name__ + ": " + str(error)
        return evidence
    evidence["cleanup_status"] = "DELETED"
    evidence["deleted_stack"] = name
    evidence["deleted_at"] = datetime.now(timezone.utc).isoformat()
    return evidence


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "phase",
        choices=[
            *BUILDERS,
            "upload-a",
            "upload-b",
            "upload-c",
            "push-image",
            "run-task",
            "probe-dispatch",
            "probe-dispatch-iam",
            "probe-authority",
            "probe-authority-iam",
            "stop-a",
            "stop-b",
            "finalize-stop-b",
            "emergency-finalize-stop-b",
            "allow-issuer-b",
            "activate-a",
            "activate-b",
            "delete-service-a",
            "delete-service-b",
            "delete-service-c",
            "delete-foundation-a",
            "delete-foundation-b",
            "delete-foundation-c",
        ],
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, default=Path("build/p2"))
    parser.add_argument(
        "--mode",
        choices=[
            "submit",
            "invoke",
            "status",
            "finish",
            "probe",
            "probe-iam",
            "probe-executor",
            "probe-executor-iam",
        ],
    )
    parser.add_argument("--job-id")
    parser.add_argument("--attempt-id")
    parser.add_argument("--auditor-readback", type=Path)
    parser.add_argument("--a-auditor-readback", type=Path)
    parser.add_argument("--b-auditor-readback", type=Path)
    parser.add_argument("--first-failure", type=Path)
    parser.add_argument("--probe-output", type=Path)
    parser.add_argument("--preflight-result", type=Path)
    parser.add_argument("--preflight-sha256")
    parser.add_argument("--preflight-signature", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    active_phase = not args.phase.startswith(
        ("stop-", "finalize-stop-", "emergency-finalize-stop-", "delete-")
    )
    config = parse_config(args.config, require_active=active_phase)
    state = load_state(args.state)
    account = (
        "b"
        if args.phase
        in (
            "push-image",
            "run-task",
            "probe-dispatch",
            "probe-dispatch-iam",
            "probe-authority",
            "probe-authority-iam",
        )
        else account_for(args.phase)
    )
    if not args.execute:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "phase": args.phase,
                    "account": config["accounts"][account],
                    "region": config["region"],
                }
            )
        )
        return
    if args.phase in (
        "probe-dispatch",
        "probe-dispatch-iam",
        "probe-authority",
        "probe-authority-iam",
    ) and (args.probe_output is None or args.probe_output.exists()):
        raise ValueError("fresh --probe-output is required before Lambda probe")
    session = client_session(config, account)
    if args.phase in BUILDERS:
        cf = session.client("cloudformation")
        if args.phase.startswith("service-"):
            if state.get(args.phase):
                raise ValueError(
                    "service stack already recorded; use explicit lifecycle phase"
                )
            controls = live_controls(cf, stack_name(config, args.phase))
            if controls is not None:
                raise ValueError(
                    "service stack already exists; independent recovery required"
                )
        result = upsert_stack(
            cf,
            stack_name(config, args.phase),
            BUILDERS[args.phase](),
            parameters(config, state, args.phase, session),
        )
        if args.phase.startswith("service-"):
            if account in ("a", "b"):
                require_live_stopped(cf, config, args.phase)
                result["P2TrafficState"] = "false"
                result["P2NeverActivated"] = True
            result["P2CreatedAt"] = datetime.now(timezone.utc).isoformat()
            state[args.phase] = result
            save_state(args.state, state)
            firewall_fail_closed(session, result, configure=True)
    elif args.phase.startswith("upload-"):
        result = upload(session, state, account, args.artifact_dir)
        args.phase = "artifact-" + account
    elif args.phase == "push-image":
        result = push_image(session, state, args.artifact_dir)
        args.phase = "image-b"
    elif args.phase == "run-task":
        result = run_task(
            session, config, state, args.mode, args.job_id, args.attempt_id
        )
        args.phase = "task-" + result["TaskArn"].rsplit("/", 1)[1]
    elif args.phase in (
        "probe-dispatch",
        "probe-dispatch-iam",
        "probe-authority",
        "probe-authority-iam",
    ):
        result = probe_dispatch(
            session,
            config,
            state,
            iam=args.phase.endswith("-iam"),
            role="authority"
            if args.phase.startswith("probe-authority")
            else "dispatch",
        )
        raw = json.dumps(result, sort_keys=True, indent=2).encode() + b"\n"
        args.probe_output.parent.mkdir(parents=True, exist_ok=True)
        with args.probe_output.open("xb") as output:
            output.write(raw)
        args.probe_output.with_suffix(args.probe_output.suffix + ".sha256").write_text(
            hashlib.sha256(raw).hexdigest() + "\n"
        )
        args.phase += "-" + (result.get("request_id") or "invalid")
    elif args.phase == "allow-issuer-b":
        require_staged(state, "service-b")
        require_live_stopped(session.client("cloudformation"), config, "service-b")
        result = upsert_stack(
            session.client("cloudformation"),
            stack_name(config, "service-b"),
            account_b(),
            {
                "AIssuerVpceId": required(
                    state, "service-a", "IssuerExecuteApiEndpointId"
                )
            },
        )
        require_live_stopped(session.client("cloudformation"), config, "service-b")
        result["P2TrafficState"] = "false"
        result["P2NeverActivated"] = True
        args.phase = "service-b"
    elif args.phase.startswith("activate-"):
        require_preflight(
            session,
            config,
            args.config,
            state,
            args.preflight_result,
            args.preflight_sha256,
            args.preflight_signature,
        )
        phase = "service-" + account
        require_staged(state, phase)
        require_live_stopped(session.client("cloudformation"), config, phase)
        if account == "b" and not state.get("service-a", {}).get(
            "IssuerExecuteApiEndpointId"
        ):
            raise ValueError("issuer endpoint readback required before activation")
        if (
            account == "a"
            and state.get("service-b", {}).get("P2TrafficState") != "true"
        ):
            raise ValueError("broker activation readback required first")
        firewall_fail_closed(session, state[phase], configure=False)
        result = upsert_stack(
            session.client("cloudformation"),
            stack_name(config, phase),
            BUILDERS[phase](),
            {
                "EnableTraffic": "true",
                **({"StopDispatch": "false"} if account == "b" else {}),
            },
        )
        result["P2TrafficState"] = "true"
        result["P2NeverActivated"] = False
        args.phase = phase
    elif args.phase.startswith("stop-"):
        result = stop(session, config, state, account)
        result["P2TrafficState"] = "draining" if account == "b" else "false"
        if account == "b":
            result["P2StopAt"] = (
                state.get("service-b", {}).get("P2StopAt")
                or datetime.now(timezone.utc).isoformat()
            )
        else:
            result["P2Closed"] = True
        args.phase = "service-" + account
    elif args.phase == "finalize-stop-b":
        if state.get("service-b", {}).get("P2TrafficState") != "draining":
            raise ValueError("stop Dispatch before final B shutdown")
        require_a_stopped(
            config,
            args.a_auditor_readback,
            after=datetime.fromisoformat(state["service-b"]["P2StopAt"]),
        )
        require_drained(session, state)
        result = upsert_stack(
            session.client("cloudformation"),
            stack_name(config, "service-b"),
            account_b(),
            {"EnableTraffic": "false", "StopDispatch": "true"},
        )
        result["P2TrafficState"] = "false"
        result["P2Closed"] = True
        result["P2StopAt"] = state["service-b"]["P2StopAt"]
        args.phase = "service-b"
    elif args.phase == "emergency-finalize-stop-b":
        if state.get("service-b", {}).get("P2TrafficState") != "draining":
            raise ValueError("stop Dispatch before emergency B shutdown")
        require_a_stopped(
            config,
            args.a_auditor_readback,
            after=datetime.fromisoformat(state["service-b"]["P2StopAt"]),
        )
        incident = emergency_evidence(
            config, state, args.auditor_readback, args.first_failure
        )
        assert_no_running_tasks(
            session.client("ecs"), required(state, "service-b", "WorkerClusterName")
        )
        result = upsert_stack(
            session.client("cloudformation"),
            stack_name(config, "service-b"),
            account_b(),
            {"EnableTraffic": "false", "StopDispatch": "true"},
        )
        result["P2TrafficState"] = "false"
        result["P2Closed"] = True
        result["P2StopAt"] = state["service-b"]["P2StopAt"]
        result["P2Incident"] = incident
        args.phase = "service-b"
    else:
        target = args.phase.removeprefix("delete-")
        cf = session.client("cloudformation")
        if target in ("service-a", "service-b"):
            controls = require_live_stopped(cf, config, target)
            if target == "service-b":
                b_epoch = state.get("service-b", {}).get("P2StopAt") or state.get(
                    "service-b", {}
                ).get("P2CreatedAt")
                require_a_stopped(
                    config,
                    args.a_auditor_readback,
                    after=datetime.fromisoformat(b_epoch) if b_epoch else None,
                )
            if controls["status"] in ("CREATE_COMPLETE", "UPDATE_COMPLETE") and (
                state.get(target, {}).get("P2TrafficState") != "false"
                or not (
                    state.get(target, {}).get("P2Closed") is True
                    or state.get(target, {}).get("P2NeverActivated") is True
                )
            ):
                raise ValueError("finalize stop before deleting a complete service")
            if target == "service-b" and controls["status"] in (
                "CREATE_COMPLETE",
                "UPDATE_COMPLETE",
            ):
                assert_no_running_tasks(
                    session.client("ecs"), required(state, target, "WorkerClusterName")
                )
                if state[target].get("P2NeverActivated") is True:
                    require_drained(session, state)
        if (
            target.startswith("foundation-")
            and stack_snapshot(cf, stack_name(config, "service-" + account)) is not None
        ):
            raise ValueError("delete service stack before foundation")
        if target == "service-c":
            require_ab_absent(
                config, state, args.a_auditor_readback, args.b_auditor_readback
            )
        result = delete_stack(session, config, target)
        args.phase = target
    record_phase_result(state, args.phase, result)
    save_state(args.state, state)
    print(
        json.dumps(
            {
                "phase": args.phase,
                "account": config["accounts"][account],
                "result": result,
            },
            sort_keys=True,
        )
    )
    if result.get("cleanup_status") == "DELETE_FAILED":
        raise SystemExit(2)
    if result.get("function_arn") and (
        not isinstance(result.get("result"), dict)
        or result["result"].get("all_blocked") is not True
    ):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
