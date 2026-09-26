"""Validate independent control-plane readbacks and negative probe evidence.

This gate deliberately cannot declare real P2 PASS from offline templates.
Rollback, revoke races and unknown-send scenarios also require separate
actual job/anchor/canary evidence listed in README before a final claim.
"""

import argparse
import base64
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .ctl import parse_config

FORBIDDEN_RUNTIME_ACTIONS = {
    "iam:passrole",
    "sts:assumerole",
    "kms:sign",
    "secretsmanager:getsecretvalue",
    "dynamodb:putitem",
    "dynamodb:updateitem",
    "lambda:invokefunction",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def role(readback, arn):
    for entry in readback["roles"].values():
        if entry["arn"] == arn:
            return entry
    raise ValueError("role not in independent readback: " + arn)


def resource_id(readback, logical_id):
    matches = [
        row["physical_id"]
        for stack in readback["stacks"].values()
        for row in stack["resources"]
        if row["logical_id"] == logical_id
    ]
    if len(matches) != 1 or not matches[0]:
        raise ValueError("P2 resource readback missing: " + logical_id)
    return matches[0]


def policy_statements_equal(actual, expected):
    def normalized(rows):
        values = []
        for row in rows:
            value = {key: item for key, item in row.items() if key != "Sid"}
            if isinstance(value.get("Resource"), list) and len(value["Resource"]) == 1:
                value["Resource"] = value["Resource"][0]
            principal = value.get("Principal")
            if isinstance(principal, dict) and isinstance(principal.get("AWS"), list):
                value["Principal"] = {**principal, "AWS": sorted(principal["AWS"])}
            condition = value.get("Condition")
            if isinstance(condition, dict):
                equals = condition.get("StringEquals")
                if isinstance(equals, dict) and isinstance(
                    equals.get("aws:SourceVpce"), list
                ):
                    value["Condition"] = {
                        **condition,
                        "StringEquals": {
                            **equals,
                            "aws:SourceVpce": sorted(equals["aws:SourceVpce"]),
                        },
                    }
            values.append(json.dumps(value, sort_keys=True))
        return sorted(values)

    return normalized(actual["Statement"]) == normalized(expected)


def api_resource_policy_exact(
    policy, config, account, api_id, principal, condition=None
):
    statement = {
        "Effect": "Allow",
        "Action": "execute-api:Invoke",
        "Resource": (
            f"arn:aws:execute-api:{config['region']}:"
            f"{config['accounts'][account]}:{api_id}/*"
        ),
        "Principal": principal,
    }
    if condition is not None:
        statement["Condition"] = condition
    return policy_statements_equal(policy, [statement])


def egress_targets(readback, group_id):
    group = next(
        row for row in readback["security_groups"] if row["GroupId"] == group_id
    )
    targets = set()
    for rule in group["IpPermissionsEgress"]:
        targets.update(("cidr", row["CidrIp"]) for row in rule.get("IpRanges", []))
        targets.update(
            ("sg", row["GroupId"]) for row in rule.get("UserIdGroupPairs", [])
        )
        targets.update(
            ("prefix", row["PrefixListId"]) for row in rule.get("PrefixListIds", [])
        )
    return targets


def allow_statements(entry):
    for document in entry["inline_policies"].values():
        for statement in document.get("Statement", []):
            if statement.get("Effect") != "Allow":
                continue
            require(
                "Action" in statement
                and "Resource" in statement
                and "NotAction" not in statement
                and "NotResource" not in statement
                and "NotPrincipal" not in statement,
                "inline IAM allow uses an unbounded alternate selector",
            )
            yield statement


def actions(entry):
    found = set()
    for statement in allow_statements(entry):
        value = statement.get("Action", [])
        found.update(
            item.lower() for item in (value if isinstance(value, list) else [value])
        )
    return found


def allowed_resources(entry):
    grants = {}
    for statement in allow_statements(entry):
        names = statement.get("Action", [])
        names = names if isinstance(names, list) else [names]
        targets = statement.get("Resource", [])
        targets = targets if isinstance(targets, list) else [targets]
        for name in names:
            grants.setdefault(name, set()).update(targets)
    return grants


def invoke_resources(entry):
    result = set()
    for document in entry["inline_policies"].values():
        for statement in document.get("Statement", []):
            listed = statement.get("Action", [])
            listed = listed if isinstance(listed, list) else [listed]
            if statement.get("Effect") != "Allow" or "execute-api:Invoke" not in listed:
                continue
            resource = statement["Resource"]
            result.update(resource if isinstance(resource, list) else [resource])
    return result


def _probe(probe, label, *, role_name, iam):
    if "probe_result" in probe:
        require(
            probe.get("last_status") == "STOPPED"
            and probe.get("containers")
            and all(item.get("exit_code") == 0 for item in probe["containers"]),
            label + " worker probe task did not stop cleanly",
        )
        probe = probe["probe_result"] or {}
    if "result" in probe:
        probe = probe["result"]
    identity_checks = {
        "issuer_sign",
        "preflight_sign",
        "admin_assume",
    }
    if role_name != "authority":
        identity_checks |= {"grant_read", "grant_write", "anchor_invoke"}
    if role_name != "dispatch":
        identity_checks.add("secret_read")
    network_checks = {
        "public_ipv4",
        "public_ipv6",
        "public_dns",
        "direct_dns",
        "proxy_bypass",
    }
    endpoint_checks = {"anchor_table_write"}
    expected = identity_checks | endpoint_checks | network_checks
    if role_name != "dispatch":
        expected.add("canary_direct")
    require(
        set(probe) == expected | {"all_blocked"} and probe["all_blocked"] is True,
        label + " probe evidence incomplete",
    )
    for name in identity_checks:
        allowed = (
            {"AWS_ACCESS_DENIED"} if iam else {"AWS_ACCESS_DENIED", "NETWORK_BLOCKED"}
        )
        require(
            probe[name]["category"] in allowed,
            label + " protected identity action was not denied: " + name,
        )
    for name in endpoint_checks:
        allowed = (
            {"AWS_ACCESS_DENIED"} if iam else {"AWS_ACCESS_DENIED", "NETWORK_BLOCKED"}
        )
        require(
            probe[name]["category"] in allowed,
            label + " protected endpoint route was not denied: " + name,
        )
    for name in network_checks:
        require(
            probe[name]["category"] == "NETWORK_BLOCKED",
            label + " public egress was not network-blocked: " + name,
        )
    if role_name != "dispatch":
        require(
            probe["canary_direct"]["category"] in ("GATEWAY_DENIED", "NETWORK_BLOCKED"),
            label + " unauthorized canary application access",
        )


def check_artifacts(config, records, state, candidate):
    """Bind actual running bytes to the separately reviewed candidate."""
    manifest = candidate["lambda_artifact"]
    require(
        candidate["baseline_sha"]
        == manifest["source_merge_sha"]
        == config["source_merge_sha"],
        "artifact baseline differs from approved merge",
    )
    require(
        isinstance(manifest.get("base_image"), str)
        and re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", manifest["base_image"])
        is not None
        and bool(manifest.get("image_tag")),
        "reviewed candidate lacks a complete digest-pinned image build",
    )
    expected_code = base64.b64encode(
        bytes.fromhex(manifest["lambda_zip_sha256"])
    ).decode("ascii")
    for account, record in records.items():
        artifact = state["artifact-" + account]
        require(
            artifact["LambdaZipSha256"] == manifest["lambda_zip_sha256"]
            and artifact["PackageSha256"] == manifest["package_sha256"]
            and artifact["InputSha256"] == manifest["input_sha256"],
            "uploaded Lambda artifact differs from reviewed candidate",
        )
        require(record["functions"], "no deployed Lambda function read back")
        for function in record["functions"].values():
            require(
                function["code_sha256"] == expected_code,
                "running Lambda code differs from reviewed candidate",
            )
    b = records["b"]
    image = state["image-b"]
    repository = b["stacks"]["foundation-b"]["outputs"]["ImageRepositoryUri"]
    expected_tag = (
        "p2-" + manifest["package_sha256"][:12] + "-" + manifest["input_sha256"][:12]
    )
    require(
        image["ImageTag"] == expected_tag
        and re.fullmatch(
            re.escape(repository) + r"@sha256:[0-9a-f]{64}", image["ImageUri"]
        )
        is not None
        and candidate.get("deployed_image_uri") == image["ImageUri"],
        "pushed image digest differs from independently reviewed candidate",
    )
    digest = image["ImageUri"].rsplit("@", 1)[1]
    require(
        any(
            item["imageDigest"] == digest and expected_tag in item.get("imageTags", [])
            for item in b["ecr_images"]
        ),
        "independent ECR image readback differs from deployment record",
    )
    require(b["task_definitions"], "no worker task definition read back")
    for task in b["task_definitions"].values():
        for container in task["containers"]:
            require(
                container["image"] == image["ImageUri"]
                and container["package_sha256"] == manifest["package_sha256"]
                and container["input_sha256"] == manifest["input_sha256"],
                "running worker image or package differs from reviewed candidate",
            )


def check(
    config,
    a,
    b,
    c,
    worker_probe,
    executor_probe,
    dispatch_probe,
    authority_probe,
    worker_iam_probe,
    executor_iam_probe,
    dispatch_iam_probe,
    authority_iam_probe,
    state,
    candidate,
):
    records = {"a": a, "b": b, "c": c}
    check_artifacts(config, records, state, candidate)
    now = datetime.now(timezone.utc)
    for record in records.values():
        observed = datetime.fromisoformat(record["at"])
        require(
            observed.tzinfo is not None
            and observed <= now
            and now - observed <= timedelta(hours=1),
            "independent readback is stale or lacks a timezone",
        )
    for account in ("a", "b"):
        require(
            records[account]["stacks"]["service-" + account]["traffic_parameter"]
            == "false",
            "preflight must run before A/B traffic activation",
        )
    for account, record in records.items():
        require(
            record["account"] == config["accounts"][account],
            "account readback mismatch",
        )
        require(record["region"] == config["region"], "region readback mismatch")
        require(
            record["auditor_role"] == config["auditor_role_arns"][account],
            "auditor readback mismatch",
        )
        gateway_endpoints = {
            item["VpcEndpointId"]: item.get("PrefixListId")
            for item in record["endpoints"]
            if item["VpcEndpointType"] == "Gateway"
        }
        for stack in record["stacks"].values():
            require(
                stack["status"] in ("CREATE_COMPLETE", "UPDATE_COMPLETE"),
                "stack incomplete or rolled back",
            )
        for vpc in record["vpcs"]:
            require(
                vpc["dns_firewall"].get("FirewallFailOpen") == "DISABLED",
                "DNS firewall can fail open",
            )
            associations = record["dns_associations"].get(vpc["id"], [])
            require(
                len(associations) == 1
                and associations[0]["Status"] == "COMPLETE"
                and associations[0]["Priority"] == 100,
                "P2 VPC DNS firewall association missing or shadowed",
            )
            rules = record["dns_rules"].get(associations[0]["FirewallRuleGroupId"], [])
            require(
                len(rules) == 2
                and {(rule["Priority"], rule["Action"]) for rule in rules}
                == {(10, "ALLOW"), (100, "BLOCK")},
                "P2 VPC DNS firewall rules changed",
            )
            allow_rule = next(rule for rule in rules if rule["Action"] == "ALLOW")
            block_rule = next(rule for rule in rules if rule["Action"] == "BLOCK")
            allowed_domains = record["dns_domain_lists"].get(
                allow_rule["FirewallDomainListId"], []
            )
            require(
                allowed_domains
                and all(
                    domain.endswith(".amazonaws.com") and domain != "*"
                    for domain in allowed_domains
                )
                and record["dns_domain_lists"].get(block_rule["FirewallDomainListId"])
                == ["*"],
                "P2 VPC DNS allow/block domain lists changed",
            )
            for table in vpc["route_tables"]:
                for route in table["Routes"]:
                    require(
                        route.get("GatewayId") == "local"
                        or (
                            route.get("GatewayId") in gateway_endpoints
                            and route.get("DestinationPrefixListId")
                            == gateway_endpoints[route["GatewayId"]]
                        ),
                        "unexpected route or public egress path",
                    )
        known_groups = {row["GroupId"] for row in record["security_groups"]}
        known_prefixes = set(gateway_endpoints.values())
        for group in record["security_groups"]:
            found_sink = False
            for rule in group.get("IpPermissionsEgress", []):
                require(
                    rule.get("IpProtocol") == "tcp"
                    and rule.get("FromPort") == 443
                    and rule.get("ToPort") == 443,
                    "security group egress not restricted to TCP 443",
                )
                ranges = [row["CidrIp"] for row in rule.get("IpRanges", [])]
                if ranges:
                    require(
                        ranges == ["192.0.2.0/32"]
                        and not rule.get("Ipv6Ranges")
                        and not rule.get("UserIdGroupPairs")
                        and not rule.get("PrefixListIds"),
                        "security group permits CIDR egress",
                    )
                    found_sink = True
                else:
                    require(not rule.get("Ipv6Ranges"), "IPv6 egress present")
                    destinations = rule.get("UserIdGroupPairs", [])
                    prefixes = rule.get("PrefixListIds", [])
                    require(
                        bool(destinations) != bool(prefixes)
                        and all(
                            row.get("GroupId") in known_groups for row in destinations
                        )
                        and all(
                            row.get("PrefixListId") in known_prefixes
                            for row in prefixes
                        ),
                        "security group egress target outside P2 boundary",
                    )
            require(found_sink, "security group default-egress suppression missing")
        for trail in record["trails"].values():
            require(trail["status"].get("IsLogging") is True, "CloudTrail not logging")
            require(
                not trail["status"].get("LatestDeliveryError"),
                "CloudTrail delivery error",
            )
            require(
                trail["status"].get("LatestDeliveryTime") is not None,
                "CloudTrail delivery not yet observed",
            )
            selectors = trail["selectors"].get("EventSelectors", [])
            require(
                any(
                    row.get("IncludeManagementEvents") is True
                    and row.get("ReadWriteType") == "All"
                    for row in selectors
                ),
                "CloudTrail management event selector missing",
            )
            if account == "c":
                c_outputs = record["stacks"]["service-c"]["outputs"]
                partition = ":".join(c_outputs["AnchorTableArn"].split(":")[:2])
                expected_data = {
                    (
                        "AWS::S3::Object",
                        partition
                        + ":s3:::"
                        + c_outputs["AuditBucketName"]
                        + "/anchors/",
                    ),
                    ("AWS::DynamoDB::Table", c_outputs["AnchorTableArn"]),
                    ("AWS::Lambda::Function", c_outputs["AnchorFunctionArn"]),
                }
                actual_data = {
                    (resource["Type"], value)
                    for row in selectors
                    for resource in row.get("DataResources", [])
                    for value in resource.get("Values", [])
                }
                require(
                    expected_data <= actual_data,
                    "C anchor data event selector missing",
                )
        require(record["trails"], "account audit trail missing")
        require(
            record["flow_logs"]
            and all(
                item.get("DeliverLogsStatus") == "SUCCESS"
                for item in record["flow_logs"]
            ),
            "VPC Flow Log delivery not verified",
        )
        for label in ("deployer", "auditor"):
            expected = (
                config["admin_role_arns"][account]
                if label == "deployer"
                else config["auditor_role_arns"][account]
            )
            require(
                record["management_roles"][label]["arn"] == expected,
                "management role readback mismatch",
            )
        for function in record["functions"].values():
            require(
                function["p2_synthetic_only"] == "1"
                and (
                    account == "c"
                    or function["p2_expires_at"] == config["expires_at_utc"]
                ),
                "synthetic or expiry guard missing from Lambda",
            )
            entry = role(record, function["role"])
            require(
                {row["PolicyArn"] for row in entry["attached_policies"]}
                == {
                    "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
                }
                and entry["assume_role_policy"]["Statement"]
                == [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "lambda.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }
                ],
                "Lambda role attached policy or trust changed",
            )
            denials = [
                statement
                for document in entry["inline_policies"].values()
                for statement in document.get("Statement", [])
                if statement.get("Effect") == "Deny"
            ]
            require(
                len(denials) == 1
                and set(denials[0]) == {"Effect", "Action", "Resource", "Condition"}
                and denials[0]["Resource"] == "*"
                and set(denials[0]["Action"])
                == {
                    "ec2:CreateNetworkInterface",
                    "ec2:DeleteNetworkInterface",
                    "ec2:DescribeNetworkInterfaces",
                    "ec2:DescribeSubnets",
                    "ec2:DetachNetworkInterface",
                    "ec2:AssignPrivateIpAddresses",
                    "ec2:UnassignPrivateIpAddresses",
                }
                and denials[0]["Condition"]
                == {
                    "ArnLike": {
                        "lambda:SourceFunctionArn": (
                            f"arn:aws:lambda:{config['region']}:"
                            f"{config['accounts'][account]}:function:p2-*"
                        )
                    }
                },
                "Lambda code ENI management deny changed",
            )
    b_outputs = b["stacks"]["service-b"]["outputs"]
    a_outputs = a["stacks"]["service-a"]["outputs"]
    worker_s3 = next(
        item
        for item in b["endpoints"]
        if item["VpcEndpointId"] == b_outputs["WorkerS3EndpointId"]
    )
    require(
        egress_targets(b, b_outputs["WorkerWorkloadSgId"])
        == {
            ("cidr", "192.0.2.0/32"),
            ("sg", resource_id(b, "WorkerEndpointSg")),
            ("prefix", worker_s3["PrefixListId"]),
        },
        "worker runtime egress exceeds ECR/log/API boundary",
    )
    require(
        egress_targets(b, b_outputs["DispatchSgId"])
        == {
            ("cidr", "192.0.2.0/32"),
            ("sg", resource_id(b, "BrokerApiEndpointSg")),
            ("sg", resource_id(b, "BrokerSecretEndpointSg")),
        },
        "Dispatch runtime egress exceeds canary/secret boundary",
    )
    require(
        egress_targets(b, resource_id(b, "CanarySg")) == {("cidr", "192.0.2.0/32")},
        "canary application gained egress",
    )
    endpoint_policy = json.loads(worker_s3["PolicyDocument"])
    require(
        len(endpoint_policy["Statement"]) == 1
        and endpoint_policy["Statement"][0]["Effect"] == "Allow"
        and endpoint_policy["Statement"][0]["Principal"] == "*"
        and endpoint_policy["Statement"][0]["Action"] == "s3:GetObject"
        and endpoint_policy["Statement"][0]["Resource"]
        == f"arn:aws:s3:::prod-{config['region']}-starport-layer-bucket/*",
        "worker S3 egress can reach a non-ECR bucket",
    )
    for endpoint in b["endpoints"]:
        service = endpoint["ServiceName"].rsplit(".", 1)[-1]
        if endpoint["VpcEndpointType"] != "Interface" or service not in {
            "kms",
            "lambda",
            "secretsmanager",
            "sts",
        }:
            continue
        statements = json.loads(endpoint["PolicyDocument"])["Statement"]
        require(
            len(statements) == 1
            and statements[0]["Effect"] == "Allow"
            and statements[0]["Action"] == "*"
            and statements[0]["Resource"] == "*"
            and statements[0]["Principal"] == "*",
            "IAM probe endpoint policy can mask identity denial",
        )
    for endpoint_id in (
        b_outputs["WorkerProbeDynamoEndpointId"],
        b_outputs["BrokerDynamodbEndpointId"],
    ):
        endpoint = next(
            row for row in b["endpoints"] if row["VpcEndpointId"] == endpoint_id
        )
        statements = json.loads(endpoint["PolicyDocument"])["Statement"]
        require(
            len(statements) == 1
            and statements[0]["Effect"] == "Allow"
            and set(statements[0]["Action"]) == {"dynamodb:GetItem", "dynamodb:PutItem"}
            and statements[0]["Resource"]
            == b["grant_table"][b_outputs["GrantTableName"]]["TableArn"]
            and statements[0]["Principal"] == "*",
            "DynamoDB endpoint policy can mask identity denial",
        )
    for api_id in (b_outputs["AuthorityApiId"], b_outputs["CanaryApiId"]):
        require(
            "PRIVATE" in b["apis"][api_id]["endpoint_configuration"]["types"],
            "broker API not private",
        )
        require(
            b["apis"][api_id]["stage"].get("deploymentId"),
            "broker API stage lacks a deployment",
        )
    issuer_deployment = next(
        row["physical_id"]
        for row in b["stacks"]["service-b"]["resources"]
        if row["logical_id"] == "AuthorityIssuerDeployment"
    )
    require(
        b["apis"][b_outputs["AuthorityApiId"]]["stage"]["deploymentId"]
        == issuer_deployment,
        "issuer endpoint policy has not been redeployed",
    )
    require(
        set(
            b["apis"][b_outputs["AuthorityApiId"]]["endpoint_configuration"][
                "vpcEndpointIds"
            ]
        )
        == {
            b_outputs["WorkerExecuteApiEndpointId"],
            b_outputs["BrokerExecuteApiEndpointId"],
            a_outputs["IssuerExecuteApiEndpointId"],
        },
        "issuer endpoint association mismatch",
    )
    authority_policy = json.loads(b["apis"][b_outputs["AuthorityApiId"]]["policy"])
    require(
        api_resource_policy_exact(
            authority_policy,
            config,
            "b",
            b_outputs["AuthorityApiId"],
            "*",
            {
                "StringEquals": {
                    "aws:SourceVpce": [
                        b_outputs["WorkerExecuteApiEndpointId"],
                        b_outputs["BrokerExecuteApiEndpointId"],
                        a_outputs["IssuerExecuteApiEndpointId"],
                    ]
                }
            },
        ),
        "Authority API resource policy gained an extra grant",
    )
    issuer_policy = json.loads(a["apis"][a_outputs["IssuerApiId"]]["policy"])
    require(
        api_resource_policy_exact(
            issuer_policy,
            config,
            "a",
            a_outputs["IssuerApiId"],
            {"AWS": config["operator_role_arn"]},
        ),
        "public issuer API principal mismatch",
    )
    canary_policy = json.loads(b["apis"][b_outputs["CanaryApiId"]]["policy"])
    require(
        api_resource_policy_exact(
            canary_policy,
            config,
            "b",
            b_outputs["CanaryApiId"],
            "*",
            {"StringEquals": {"aws:SourceVpce": [b_outputs["BrokerApiVpceId"]]}},
        ),
        "Canary API resource policy exceeds Dispatch endpoint",
    )
    require(
        "REGIONAL"
        in a["apis"][a_outputs["IssuerApiId"]]["endpoint_configuration"]["types"],
        "issuer API not regional",
    )
    for label in ("WorkerRoleArn", "ExecutorRoleArn"):
        entry = role(b, b_outputs[label])
        require(
            not entry["attached_policies"], "runtime role has attached managed policy"
        )
        granted = actions(entry)
        require(
            not granted.intersection(FORBIDDEN_RUNTIME_ACTIONS),
            "runtime role gained protected action",
        )
        require(
            all("*" not in item for item in granted), "runtime role wildcard action"
        )
        paths = (
            ("invoke", "finish", "status")
            if label == "WorkerRoleArn"
            else ("submit", "status")
        )
        expected = {
            f"arn:aws:execute-api:{config['region']}:{config['accounts']['b']}:"
            f"{b_outputs['AuthorityApiId']}/p2/POST/v1/{path}"
            for path in paths
        }
        require(
            invoke_resources(entry) == expected,
            "runtime role API resource exceeds bounded methods",
        )
        require(
            allowed_resources(entry) == {"execute-api:Invoke": expected},
            "runtime role has an unrelated allow",
        )
        statements = entry["assume_role_policy"]["Statement"]
        require(
            len(statements) == 1
            and statements[0]["Principal"] == {"Service": "ecs-tasks.amazonaws.com"},
            "runtime role trust not ECS-only",
        )
    dispatch = role(b, b_outputs["DispatchRoleArn"])
    require(
        {row["PolicyArn"] for row in dispatch["attached_policies"]}
        == {"arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"},
        "Dispatch attached policy changed",
    )
    require(
        allowed_resources(dispatch)
        == {
            "secretsmanager:GetSecretValue": {b_outputs["SyntheticCanarySecretArn"]},
            "execute-api:Invoke": {
                f"arn:aws:execute-api:{config['region']}:{config['accounts']['b']}:"
                f"{b_outputs['AuthorityApiId']}/p2/POST/v1/preflight",
                f"arn:aws:execute-api:{config['region']}:{config['accounts']['b']}:"
                f"{b_outputs['CanaryApiId']}/p2/POST/v1/canary",
            },
        },
        "Dispatch allow set exceeds fixed secret and API methods",
    )
    require(
        dispatch["assume_role_policy"]["Statement"]
        == [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
        "Dispatch trust not Lambda-only",
    )
    authority = role(b, b_outputs["AuthorityRoleArn"])
    require(
        {row["PolicyArn"] for row in authority["attached_policies"]}
        == {"arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"},
        "Authority attached policy changed",
    )
    require(
        allowed_resources(authority)
        == {
            "dynamodb:GetItem": {
                b["grant_table"][b_outputs["GrantTableName"]]["TableArn"]
            },
            "dynamodb:PutItem": {
                b["grant_table"][b_outputs["GrantTableName"]]["TableArn"]
            },
            "sqs:ReceiveMessage": {b_outputs["IssuerQueueArn"]},
            "sqs:DeleteMessage": {b_outputs["IssuerQueueArn"]},
            "sqs:GetQueueAttributes": {b_outputs["IssuerQueueArn"]},
            "kms:Verify": {a["stacks"]["foundation-a"]["outputs"]["SigningKeyArn"]},
            "lambda:InvokeFunction": {
                c["stacks"]["service-c"]["outputs"]["AnchorFunctionArn"],
                b_outputs["DispatchFunctionArn"],
            },
        },
        "Authority allow set exceeds grant/anchor/dispatch boundary",
    )
    issuer = role(a, a["stacks"]["foundation-a"]["outputs"]["IssuerRoleArn"])
    require(
        allowed_resources(issuer)
        == {
            "kms:Sign": {a["stacks"]["foundation-a"]["outputs"]["SigningKeyArn"]},
            "sqs:SendMessage": {b_outputs["IssuerQueueArn"]},
            "execute-api:Invoke": {
                f"arn:aws:execute-api:{config['region']}:{config['accounts']['b']}:"
                f"{b_outputs['AuthorityApiId']}/p2/POST/v1/proposal"
            },
        }
        and not issuer["permissions_boundary"],
        "A issuer allow set exceeds signing, queue and proposal",
    )
    require(
        {row["PolicyArn"] for row in issuer["attached_policies"]}
        == {"arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"}
        and issuer["assume_role_policy"]["Statement"]
        == [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
        "A issuer attached policy or trust changed",
    )
    c_outputs = c["stacks"]["service-c"]["outputs"]
    anchor_function = c["functions"][c_outputs["AnchorFunctionArn"]]
    anchor_role = role(c, anchor_function["role"])
    require(
        allowed_resources(anchor_role)
        == {
            "dynamodb:GetItem": {c_outputs["AnchorTableArn"]},
            "dynamodb:UpdateItem": {c_outputs["AnchorTableArn"]},
            "s3:ListBucketVersions": {f"arn:aws:s3:::{c_outputs['AuditBucketName']}"},
            **{
                action: {f"arn:aws:s3:::{c_outputs['AuditBucketName']}/anchors/*"}
                for action in (
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:GetObjectRetention",
                    "s3:PutObject",
                    "s3:PutObjectRetention",
                )
            },
        }
        and not anchor_role["permissions_boundary"],
        "C anchor allow set exceeds high-water and locked prefix",
    )
    require(
        {row["PolicyArn"] for row in anchor_role["attached_policies"]}
        == {"arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"}
        and anchor_role["assume_role_policy"]["Statement"]
        == [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
        "C anchor attached policy or trust changed",
    )
    anchor_policy = c["anchor_resource_policy"]
    require(
        policy_statements_equal(
            anchor_policy,
            [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": b_outputs["AuthorityRoleArn"]},
                    "Action": "lambda:InvokeFunction",
                    "Resource": c_outputs["AnchorFunctionArn"],
                }
            ],
        ),
        "C anchor invocation not limited to B Authority",
    )
    key_policy = a["signing_key_policy"]
    require(
        policy_statements_equal(
            key_policy,
            [
                {
                    "Effect": "Allow",
                    "Action": "kms:*",
                    "Resource": "*",
                    "Principal": {
                        "AWS": f"arn:aws:iam::{config['accounts']['a']}:root"
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": "kms:Sign",
                    "Resource": "*",
                    "Principal": {
                        "AWS": a["stacks"]["foundation-a"]["outputs"]["IssuerRoleArn"]
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": "kms:Verify",
                    "Resource": "*",
                    "Principal": {
                        "AWS": f"arn:aws:iam::{config['accounts']['b']}:root"
                    },
                    "Condition": {
                        "ArnEquals": {"aws:PrincipalArn": b_outputs["AuthorityRoleArn"]}
                    },
                },
            ],
        ),
        "A signing key policy changed",
    )
    require(a["signing_key_grants"] == [], "A signing key has an unreviewed grant")
    preflight_policy = c["preflight_signer_key_policy"]
    require(
        policy_statements_equal(
            preflight_policy,
            [
                {
                    "Effect": "Allow",
                    "Action": "kms:*",
                    "Resource": "*",
                    "Principal": {
                        "AWS": f"arn:aws:iam::{config['accounts']['c']}:root"
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": "kms:Sign",
                    "Resource": "*",
                    "Principal": {"AWS": config["admin_role_arns"]["c"]},
                },
                {
                    "Effect": "Allow",
                    "Action": "kms:Verify",
                    "Resource": "*",
                    "Principal": {
                        "AWS": [
                            config["admin_role_arns"]["a"],
                            config["admin_role_arns"]["b"],
                        ]
                    },
                },
            ],
        ),
        "C preflight signing key policy gained an unauthorized grant",
    )
    require(
        c["preflight_signer_key_grants"] == [],
        "C preflight signing key has an unreviewed grant",
    )
    for task in b["task_definitions"].values():
        for container in task["containers"]:
            require(
                re.search(r"@sha256:[0-9a-f]{64}$", container["image"]) is not None,
                "task image not digest pinned",
            )
            require(
                container["readonly_root_filesystem"] is True
                and container["user"] == "10001"
                and container["synthetic_only"] == "1"
                and container["secret_mounts_count"] == 0,
                "task writable, root or secret mounted",
            )
    lock = c.get("object_lock", {}).get("ObjectLockConfiguration", {})
    rule = lock.get("Rule", {}).get("DefaultRetention", {})
    require(
        lock.get("ObjectLockEnabled") == "Enabled"
        and rule.get("Mode") == "COMPLIANCE"
        and rule.get("Days") == int(config["retention"]["object_lock_days"]),
        "independent Object Lock retention mismatch",
    )
    if "synthetic_job" in b or "synthetic_anchor" in c:
        job, anchor = b["synthetic_job"], c["synthetic_anchor"]
        require(
            (job["job_id"], job["revision"], job["event_digest"])
            == (anchor["job_id"], anchor["revision"], anchor["digest"]),
            "B state and C high-water disagree",
        )
    for evidence, task_arn, role_arn, expected_sg in (
        (
            worker_probe,
            b_outputs["WorkerTaskArn"],
            b_outputs["WorkerRoleArn"],
            b_outputs["WorkerWorkloadSgId"],
        ),
        (
            worker_iam_probe,
            b_outputs["WorkerTaskArn"],
            b_outputs["WorkerRoleArn"],
            b_outputs["WorkerProbeSgId"],
        ),
        (
            executor_probe,
            b_outputs["ExecutorTaskArn"],
            b_outputs["ExecutorRoleArn"],
            b_outputs["WorkerWorkloadSgId"],
        ),
        (
            executor_iam_probe,
            b_outputs["ExecutorTaskArn"],
            b_outputs["ExecutorRoleArn"],
            b_outputs["WorkerProbeSgId"],
        ),
    ):
        require(
            evidence["task_definition_arn"] == task_arn
            and evidence["task_role_arn"] == role_arn
            and evidence["launch"]["security_groups"] == [expected_sg]
            and evidence["launch"]["subnets"] == [b_outputs["WorkerSubnetId"]]
            and evidence["launch"]["assign_public_ip"] == "DISABLED",
            "task probe did not run as the bounded role/network",
        )
    for function_arn, role_arn, expected_sg, probe_role in (
        (
            b_outputs["DispatchProbeFunctionArn"],
            b_outputs["DispatchRoleArn"],
            b_outputs["DispatchSgId"],
            "dispatch",
        ),
        (
            b_outputs["DispatchIamProbeFunctionArn"],
            b_outputs["DispatchRoleArn"],
            b_outputs["BrokerProbeSgId"],
            "dispatch",
        ),
        (
            b_outputs["AuthorityProbeFunctionArn"],
            b_outputs["AuthorityRoleArn"],
            b_outputs["BrokerWorkloadSgId"],
            "authority",
        ),
        (
            b_outputs["AuthorityIamProbeFunctionArn"],
            b_outputs["AuthorityRoleArn"],
            b_outputs["BrokerProbeSgId"],
            "authority",
        ),
    ):
        function = b["functions"][function_arn]
        require(
            function["role"] == role_arn
            and function["vpc"]["SecurityGroupIds"] == [expected_sg]
            and function["p2_probe_role"] == probe_role,
            "Lambda probe did not use the bounded role/network",
        )
    for evidence, function_arn in (
        (dispatch_probe, b_outputs["DispatchProbeFunctionArn"]),
        (dispatch_iam_probe, b_outputs["DispatchIamProbeFunctionArn"]),
        (authority_probe, b_outputs["AuthorityProbeFunctionArn"]),
        (authority_iam_probe, b_outputs["AuthorityIamProbeFunctionArn"]),
    ):
        require(
            evidence.get("source") == "B_AUDITOR_LAMBDA_LOG_READBACK"
            and evidence.get("account") == config["accounts"]["b"]
            and evidence.get("region") == config["region"]
            and evidence.get("auditor_role") == config["auditor_role_arns"]["b"]
            and evidence.get("function_arn") == function_arn
            and isinstance(evidence.get("request_id"), str)
            and bool(evidence["request_id"])
            and isinstance(evidence.get("log_event_id"), str)
            and bool(evidence["log_event_id"])
            and isinstance(evidence.get("log_stream_name"), str)
            and bool(evidence["log_stream_name"])
            and re.fullmatch(r"[0-9a-f]{64}", evidence.get("invocation_sha256", ""))
            is not None,
            "Lambda probe lacks independent B auditor log binding",
        )
    now_ms = int(now.timestamp() * 1000)
    for evidence in (
        worker_probe,
        worker_iam_probe,
        executor_probe,
        executor_iam_probe,
    ):
        require(
            now_ms - 3_600_000 <= evidence.get("probe_log_timestamp", -1) <= now_ms,
            "task negative probe is stale",
        )
    for evidence in (
        dispatch_probe,
        dispatch_iam_probe,
        authority_probe,
        authority_iam_probe,
    ):
        require(
            now_ms - 3_600_000 <= evidence.get("log_timestamp", -1) <= now_ms,
            "Lambda negative probe is stale",
        )
    for evidence, name, iam in (
        (worker_probe, "worker", False),
        (worker_iam_probe, "worker", True),
        (executor_probe, "executor", False),
        (executor_iam_probe, "executor", True),
        (dispatch_probe, "dispatch", False),
        (dispatch_iam_probe, "dispatch", True),
        (authority_probe, "authority", False),
        (authority_iam_probe, "authority", True),
    ):
        _probe(evidence, name, role_name=name, iam=iam)
    return {
        "control_plane": "PASS",
        "network_probes": "PASS",
        "identity_probes": "PASS_TESTED_PATHS_ONLY",
        "anchor_table_route_probe": "PASS_ENDPOINT_DENIAL_ONLY",
        "unverified_identity_paths": [
            "C_anchor_table_role_IAM_direct_write",
            "C_anchor_S3_direct_write",
            "IAM_role_trust_or_policy_mutation",
            "ECS_RunTask_and_PassRole",
            "task_definition_mutation",
            "VPC_SG_DNS_policy_mutation",
        ],
        "identity_privilege_expansion": "PENDING_REAL_EVIDENCE",
        "scenario_faults": "PENDING_REAL_EVIDENCE",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--candidate-sha256", required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    for name in (
        "config",
        "a",
        "b",
        "c",
        "worker-probe",
        "executor-probe",
        "dispatch-probe",
        "authority-probe",
        "worker-iam-probe",
        "executor-iam-probe",
        "dispatch-iam-probe",
        "authority-iam-probe",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), "fresh preflight output path required")
    config = parse_config(args.config, require_active=False)
    candidate_raw = args.candidate.read_bytes()
    require(
        re.fullmatch(r"[0-9a-f]{64}", args.candidate_sha256) is not None
        and hashlib.sha256(candidate_raw).hexdigest() == args.candidate_sha256,
        "candidate digest differs from independently reviewed digest",
    )
    candidate = json.loads(candidate_raw)
    state = json.loads(args.state.read_text())
    files = {
        name: json.loads(getattr(args, name.replace("-", "_")).read_text())
        for name in (
            "a",
            "b",
            "c",
            "worker-probe",
            "executor-probe",
            "dispatch-probe",
            "authority-probe",
            "worker-iam-probe",
            "executor-iam-probe",
            "dispatch-iam-probe",
            "authority-iam-probe",
        )
    }
    receipt = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "candidate_sha256": args.candidate_sha256,
        "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
        "readback_sha256": {
            account: hashlib.sha256(getattr(args, account).read_bytes()).hexdigest()
            for account in "abc"
        },
        "probe_sha256": {
            label: hashlib.sha256(files_path.read_bytes()).hexdigest()
            for label, files_path in (
                ("worker", args.worker_probe),
                ("executor", args.executor_probe),
                ("dispatch", args.dispatch_probe),
                ("authority", args.authority_probe),
                ("worker_iam", args.worker_iam_probe),
                ("executor_iam", args.executor_iam_probe),
                ("dispatch_iam", args.dispatch_iam_probe),
                ("authority_iam", args.authority_iam_probe),
            )
        },
    }
    try:
        receipt.update(
            check(
                config,
                files["a"],
                files["b"],
                files["c"],
                files["worker-probe"],
                files["executor-probe"],
                files["dispatch-probe"],
                files["authority-probe"],
                files["worker-iam-probe"],
                files["executor-iam-probe"],
                files["dispatch-iam-probe"],
                files["authority-iam-probe"],
                state,
                candidate,
            )
        )
        receipt["preflight_boundary"] = "PASS_SYNTHETIC_CANARY_ONLY"
    except Exception as error:
        receipt["preflight_boundary"] = "FAIL"
        receipt["failure_type"] = type(error).__name__
        if isinstance(error, ValueError):
            receipt["failure"] = str(error)
    raw = json.dumps(receipt, sort_keys=True, indent=2).encode() + b"\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as output:
        output.write(raw)
    args.output.with_suffix(args.output.suffix + ".sha256").write_text(
        hashlib.sha256(raw).hexdigest() + "\n"
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "preflight_boundary": receipt["preflight_boundary"],
            }
        )
    )
    if receipt["preflight_boundary"] != "PASS_SYNTHETIC_CANARY_ONLY":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
