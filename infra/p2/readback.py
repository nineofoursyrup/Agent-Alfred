"""Independent account auditor readback of deployed P2 trust controls.

Output is immutable-by-convention: an existing evidence path is refused.
Readback never calls GetSecretValue, S3 GetObject, or model endpoints.
"""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .ctl import client_session, parse_config, stack_name


def stack_resources(cf, name):
    rows = []
    for page in cf.get_paginator("list_stack_resources").paginate(StackName=name):
        rows.extend(page["StackResourceSummaries"])
    return rows


def role_readback(iam, name):
    role = iam.get_role(RoleName=name)["Role"]
    attached = []
    for page in iam.get_paginator("list_attached_role_policies").paginate(
        RoleName=name
    ):
        attached.extend(page["AttachedPolicies"])
    inline = {}
    for page in iam.get_paginator("list_role_policies").paginate(RoleName=name):
        for policy_name in page["PolicyNames"]:
            inline[policy_name] = iam.get_role_policy(
                RoleName=name, PolicyName=policy_name
            )["PolicyDocument"]
    return {
        "arn": role["Arn"],
        "assume_role_policy": role["AssumeRolePolicyDocument"],
        "permissions_boundary": role.get("PermissionsBoundary"),
        "attached_policies": attached,
        "inline_policies": inline,
    }


def safe_function(config):
    return {
        "arn": config["FunctionArn"],
        "role": config["Role"],
        "runtime": config.get("Runtime"),
        "code_sha256": config.get("CodeSha256"),
        "timeout": config.get("Timeout"),
        "vpc": config.get("VpcConfig"),
        "reserved_concurrency": config.get("ReservedConcurrentExecutions"),
        "p2_synthetic_only": config.get("Environment", {})
        .get("Variables", {})
        .get("P2_SYNTHETIC_ONLY"),
        "p2_deployment_active": config.get("Environment", {})
        .get("Variables", {})
        .get("P2_DEPLOYMENT_ACTIVE"),
        "p2_stop_dispatch": config.get("Environment", {})
        .get("Variables", {})
        .get("P2_STOP_DISPATCH"),
        "p2_probe_role": config.get("Environment", {})
        .get("Variables", {})
        .get("P2_PROBE_ROLE"),
        "p2_expires_at": config.get("Environment", {})
        .get("Variables", {})
        .get("P2_EXPIRES_AT"),
    }


def safe_task(definition):
    return {
        "task_definition_arn": definition["taskDefinitionArn"],
        "task_role_arn": definition.get("taskRoleArn"),
        "execution_role_arn": definition.get("executionRoleArn"),
        "network_mode": definition.get("networkMode"),
        "requires_compatibilities": definition.get("requiresCompatibilities"),
        "containers": [
            {
                "name": row["name"],
                "image": row["image"],
                "readonly_root_filesystem": row.get("readonlyRootFilesystem"),
                "user": row.get("user"),
                "environment_keys": sorted(
                    item["name"] for item in row.get("environment", [])
                ),
                "synthetic_only": next(
                    (
                        item["value"]
                        for item in row.get("environment", [])
                        if item["name"] == "P2_SYNTHETIC_ONLY"
                    ),
                    None,
                ),
                "package_sha256": next(
                    (
                        item["value"]
                        for item in row.get("environment", [])
                        if item["name"] == "P2_PACKAGE_SHA256"
                    ),
                    None,
                ),
                "input_sha256": next(
                    (
                        item["value"]
                        for item in row.get("environment", [])
                        if item["name"] == "P2_INPUT_SHA256"
                    ),
                    None,
                ),
                "secret_mounts_count": len(row.get("secrets", [])),
                "linux_parameters": row.get("linuxParameters"),
            }
            for row in definition.get("containerDefinitions", [])
        ],
    }


def key_grants(session, key_arn):
    return [
        {
            "grant_id": item["GrantId"],
            "grantee_principal": item["GranteePrincipal"],
            "operations": item["Operations"],
        }
        for page in session.client("kms")
        .get_paginator("list_grants")
        .paginate(KeyId=key_arn)
        for item in page.get("Grants", [])
    ]


def snapshot(session, config, account, job_id=None):
    from botocore.exceptions import ClientError

    cf = session.client("cloudformation")
    ec2 = session.client("ec2")
    iam = session.client("iam")
    result = {
        "account": config["accounts"][account],
        "region": config["region"],
        "auditor_role": config["auditor_role_arns"][account],
        "at": datetime.now(timezone.utc).isoformat(),
        "stacks": {},
    }
    all_resources = []
    for phase in ("foundation-" + account, "service-" + account):
        name = stack_name(config, phase)
        try:
            stack = cf.describe_stacks(StackName=name)["Stacks"][0]
        except ClientError as error:
            if "does not exist" not in str(error):
                raise
            continue
        resources = stack_resources(cf, name)
        all_resources.extend(resources)
        result["stacks"][phase] = {
            "status": stack["StackStatus"],
            "outputs": {
                row["OutputKey"]: row["OutputValue"] for row in stack.get("Outputs", [])
            },
            "resources": [
                {
                    "logical_id": row["LogicalResourceId"],
                    "type": row["ResourceType"],
                    "physical_id": row.get("PhysicalResourceId"),
                    "status": row["ResourceStatus"],
                }
                for row in resources
            ],
            "traffic_parameter": next(
                (
                    p.get("ParameterValue")
                    for p in stack.get("Parameters", [])
                    if p["ParameterKey"] == "EnableTraffic"
                ),
                None,
            ),
        }
    by_type = {}
    for row in all_resources:
        if row.get("PhysicalResourceId"):
            by_type.setdefault(row["ResourceType"], []).append(
                row["PhysicalResourceId"]
            )
    role_names = by_type.get("AWS::IAM::Role", [])
    result["roles"] = {name: role_readback(iam, name) for name in role_names}
    result["management_roles"] = {
        label: role_readback(iam, arn.rsplit("/", 1)[1])
        for label, arn in (
            ("deployer", config["admin_role_arns"][account]),
            ("auditor", config["auditor_role_arns"][account]),
        )
    }
    groups = by_type.get("AWS::EC2::SecurityGroup", [])
    result["security_groups"] = (
        ec2.describe_security_groups(GroupIds=groups)["SecurityGroups"]
        if groups
        else []
    )
    vpcs = by_type.get("AWS::EC2::VPC", [])
    result["vpcs"] = []
    resolver = session.client("route53resolver")
    for vpc in vpcs:
        routes = ec2.describe_route_tables(
            Filters=[{"Name": "vpc-id", "Values": [vpc]}]
        )["RouteTables"]
        dns = resolver.get_firewall_config(ResourceId=vpc)["FirewallConfig"]
        result["vpcs"].append({"id": vpc, "route_tables": routes, "dns_firewall": dns})
    endpoints = by_type.get("AWS::EC2::VPCEndpoint", [])
    result["endpoints"] = (
        ec2.describe_vpc_endpoints(VpcEndpointIds=endpoints)["VpcEndpoints"]
        if endpoints
        else []
    )
    result["flow_logs"] = (
        ec2.describe_flow_logs(FlowLogIds=by_type.get("AWS::EC2::FlowLog", []))[
            "FlowLogs"
        ]
        if by_type.get("AWS::EC2::FlowLog")
        else []
    )
    result["dns_rules"] = {}
    for group in by_type.get("AWS::Route53Resolver::FirewallRuleGroup", []):
        rows = []
        token = None
        while True:
            kwargs = {"FirewallRuleGroupId": group}
            if token:
                kwargs["NextToken"] = token
            page = resolver.list_firewall_rules(**kwargs)
            rows.extend(page["FirewallRules"])
            token = page.get("NextToken")
            if not token:
                break
        result["dns_rules"][group] = rows
    result["dns_domain_lists"] = {}
    for item in by_type.get("AWS::Route53Resolver::FirewallDomainList", []):
        domains = []
        token = None
        while True:
            kwargs = {"FirewallDomainListId": item}
            if token:
                kwargs["NextToken"] = token
            page = resolver.list_firewall_domains(**kwargs)
            domains.extend(page["Domains"])
            token = page.get("NextToken")
            if not token:
                break
        result["dns_domain_lists"][item] = domains
    result["dns_associations"] = {}
    for vpc in vpcs:
        associations = []
        token = None
        while True:
            kwargs = {"VpcId": vpc}
            if token:
                kwargs["NextToken"] = token
            page = resolver.list_firewall_rule_group_associations(**kwargs)
            associations.extend(page["FirewallRuleGroupAssociations"])
            token = page.get("NextToken")
            if not token:
                break
        result["dns_associations"][vpc] = associations
    result["functions"] = {}
    lam = session.client("lambda")
    for name in by_type.get("AWS::Lambda::Function", []):
        function = safe_function(lam.get_function_configuration(FunctionName=name))
        result["functions"][function["arn"]] = function
    if account == "c" and result.get("stacks", {}).get("service-c", {}).get(
        "outputs", {}
    ).get("AnchorFunctionArn"):
        anchor_arn = result["stacks"]["service-c"]["outputs"]["AnchorFunctionArn"]
        result["anchor_resource_policy"] = json.loads(
            lam.get_policy(FunctionName=anchor_arn)["Policy"]
        )
    result["apis"] = {}
    gateway = session.client("apigateway")
    for api_id in by_type.get("AWS::ApiGateway::RestApi", []):
        api = gateway.get_rest_api(restApiId=api_id)
        result["apis"][api_id] = {
            "endpoint_configuration": api.get("endpointConfiguration"),
            "policy": api.get("policy"),
            "stage": gateway.get_stage(restApiId=api_id, stageName="p2"),
        }
    result["trails"] = {}
    trail_client = session.client("cloudtrail")
    for arn in by_type.get("AWS::CloudTrail::Trail", []):
        result["trails"][arn] = {
            "status": trail_client.get_trail_status(Name=arn),
            "selectors": trail_client.get_event_selectors(TrailName=arn),
        }
    if account == "b":
        ecs = session.client("ecs")
        repository = (
            result.get("stacks", {})
            .get("foundation-b", {})
            .get("outputs", {})
            .get("ImageRepositoryArn")
        )
        result["ecr_images"] = []
        if repository:
            result["ecr_images"] = [
                {
                    "imageDigest": item["imageDigest"],
                    "imageTags": item.get("imageTags", []),
                }
                for page in session.client("ecr")
                .get_paginator("describe_images")
                .paginate(repositoryName=repository.rsplit("/", 1)[-1])
                for item in page.get("imageDetails", [])
            ]
        result["task_definitions"] = {
            arn: safe_task(
                ecs.describe_task_definition(taskDefinition=arn)["taskDefinition"]
            )
            for arn in by_type.get("AWS::ECS::TaskDefinition", [])
        }
        result["grant_table"] = {
            name: session.client("dynamodb").describe_table(TableName=name)["Table"]
            for name in by_type.get("AWS::DynamoDB::Table", [])
        }
        result["synthetic_secret_metadata"] = {
            arn: session.client("secretsmanager").describe_secret(SecretId=arn)
            for arn in by_type.get("AWS::SecretsManager::Secret", [])
        }
        if job_id and result.get("stacks", {}).get("service-b", {}).get(
            "outputs", {}
        ).get("GrantTableName"):
            from agent_alfred.p2.store import DynamoJobStore

            grant_table = result["stacks"]["service-b"]["outputs"]["GrantTableName"]
            state = DynamoJobStore(session.client("dynamodb"), grant_table).get(job_id)
            result["synthetic_job"] = {
                "job_id": job_id,
                "revision": state["revision"],
                "event_digest": state["event_digest"],
                "state": state["state"],
                "send_states": [row["send_state"] for row in state["requests"]],
            }
    if account == "c":
        s3 = session.client("s3")
        for bucket in by_type.get("AWS::S3::Bucket", []):
            if bucket == result.get("stacks", {}).get("service-c", {}).get(
                "outputs", {}
            ).get("AuditBucketName"):
                result["object_lock"] = s3.get_object_lock_configuration(Bucket=bucket)
        result["anchor_table"] = {
            name: session.client("dynamodb").describe_table(TableName=name)["Table"]
            for name in by_type.get("AWS::DynamoDB::Table", [])
        }
        if job_id and result.get("stacks", {}).get("service-c", {}).get(
            "outputs", {}
        ).get("AnchorTableName"):
            from agent_alfred.p2.anchor import Anchor

            service = result["stacks"]["service-c"]["outputs"]
            anchor = Anchor(
                session.client("dynamodb"),
                session.client("s3"),
                service["AnchorTableName"],
                service["AuditBucketName"],
                int(config["retention"]["object_lock_days"]),
            )
            result["synthetic_anchor"] = anchor.read(job_id)
    if account == "a" and result.get("stacks", {}).get("foundation-a", {}).get(
        "outputs", {}
    ).get("SigningKeyArn"):
        key_arn = result["stacks"]["foundation-a"]["outputs"]["SigningKeyArn"]
        result["signing_key_policy"] = json.loads(
            session.client("kms").get_key_policy(KeyId=key_arn, PolicyName="default")[
                "Policy"
            ]
        )
        result["signing_key_grants"] = key_grants(session, key_arn)
    if account == "c" and result.get("stacks", {}).get("service-c", {}).get(
        "outputs", {}
    ).get("PreflightSignerKeyArn"):
        key_arn = result["stacks"]["service-c"]["outputs"]["PreflightSignerKeyArn"]
        result["preflight_signer_key_policy"] = json.loads(
            session.client("kms").get_key_policy(KeyId=key_arn, PolicyName="default")[
                "Policy"
            ]
        )
        result["preflight_signer_key_grants"] = key_grants(session, key_arn)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", choices=["a", "b", "c"], required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--job-id")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("evidence output already exists")
    config = parse_config(args.config, require_active=False)
    session = client_session(config, args.account, role="auditor")
    record = snapshot(session, config, args.account, job_id=args.job_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(record, indent=2, sort_keys=True, default=str).encode() + b"\n"
    args.output.write_bytes(raw)
    args.output.with_suffix(args.output.suffix + ".sha256").write_text(
        hashlib.sha256(raw).hexdigest() + "\n"
    )
    print(args.output)


if __name__ == "__main__":
    main()
