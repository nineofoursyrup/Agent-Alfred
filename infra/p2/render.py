"""Render three separately administered CloudFormation P2 stacks.

The templates have parameters, not account credentials or secret values. Running
this module writes JSON locally only. Each account administrator must deploy
their own template after a separate pricing/readback gate.
"""

import json
from pathlib import Path


def ref(name):
    return {"Ref": name}


def att(name, key="Arn"):
    return {"Fn::GetAtt": [name, key]}


def sub(value):
    return {"Fn::Sub": value}


def join(delimiter, values):
    return {"Fn::Join": [delimiter, values]}


def policy(statements):
    return {"Version": "2012-10-17", "Statement": statements}


def trust(service):
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": service},
                "Action": "sts:AssumeRole",
            },
        ],
    }


def allow(actions, resources, *, sid=None, condition=None, principal=None):
    row = {"Effect": "Allow", "Action": actions, "Resource": resources}
    if sid:
        row["Sid"] = sid
    if condition:
        row["Condition"] = condition
    if principal:
        row["Principal"] = principal
    return row


def deny(actions, resources, *, condition=None):
    row = {"Effect": "Deny", "Action": actions, "Resource": resources}
    if condition:
        row["Condition"] = condition
    return row


def sink_egress():
    # CloudFormation adds allow-all egress when the list is empty. TEST-NET-1
    # has no route in any P2 VPC; this explicit rule suppresses that default.
    return [
        {
            "IpProtocol": "tcp",
            "FromPort": 443,
            "ToPort": 443,
            "CidrIp": "192.0.2.0/32",
        }
    ]


class Template:
    def __init__(self, description):
        self.data = {
            "AWSTemplateFormatVersion": "2010-09-09",
            "Description": description,
            "Parameters": {},
            "Resources": {},
            "Outputs": {},
        }

    def param(
        self,
        name,
        *,
        type_="String",
        no_echo=False,
        default=None,
        allowed_pattern=None,
        min_value=None,
        max_value=None,
    ):
        row = {"Type": type_}
        if no_echo:
            row["NoEcho"] = True
        if default is not None:
            row["Default"] = default
        if allowed_pattern:
            row["AllowedPattern"] = allowed_pattern
        if min_value is not None:
            row["MinValue"] = min_value
        if max_value is not None:
            row["MaxValue"] = max_value
        self.data["Parameters"][name] = row
        return ref(name)

    def add(
        self, name, kind, properties, *, depends=None, retain=False, condition=None
    ):
        row = {"Type": kind, "Properties": properties}
        if depends:
            row["DependsOn"] = depends
        if retain:
            row["DeletionPolicy"] = "Retain"
            row["UpdateReplacePolicy"] = "Retain"
        if condition:
            row["Condition"] = condition
        self.data["Resources"][name] = row
        return ref(name)

    def out(self, name, value):
        self.data["Outputs"][name] = {"Value": value}


def network(
    t,
    prefix,
    cidr,
    subnet_cidr,
    *,
    interfaces=(),
    gateways=(),
    allowed_domains=(),
    s3_prefix=None,
    ddb_prefix=None,
    log_retention=None,
):
    """One AZ, no IGW/NAT/IPv6, explicit egress to private AWS endpoints."""
    vpc = t.add(
        prefix + "Vpc",
        "AWS::EC2::VPC",
        {
            "CidrBlock": cidr,
            "EnableDnsSupport": True,
            "EnableDnsHostnames": True,
            "Tags": [{"Key": "p2-owner", "Value": prefix}],
        },
    )
    subnet = t.add(
        prefix + "Subnet",
        "AWS::EC2::Subnet",
        {
            "VpcId": vpc,
            "CidrBlock": subnet_cidr,
            "MapPublicIpOnLaunch": False,
            "AvailabilityZone": {"Fn::Select": [0, {"Fn::GetAZs": ""}]},
        },
    )
    route = t.add(prefix + "Route", "AWS::EC2::RouteTable", {"VpcId": vpc})
    t.add(
        prefix + "RouteAssociation",
        "AWS::EC2::SubnetRouteTableAssociation",
        {
            "SubnetId": subnet,
            "RouteTableId": route,
        },
    )
    workload_sg = t.add(
        prefix + "WorkloadSg",
        "AWS::EC2::SecurityGroup",
        {
            "GroupDescription": prefix + " workload private egress only",
            "VpcId": vpc,
            "SecurityGroupEgress": sink_egress(),
        },
    )
    endpoint_sg = t.add(
        prefix + "EndpointSg",
        "AWS::EC2::SecurityGroup",
        {
            "GroupDescription": prefix + " endpoint 443 from workload",
            "VpcId": vpc,
            "SecurityGroupEgress": sink_egress(),
            "SecurityGroupIngress": [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 443,
                    "ToPort": 443,
                    "SourceSecurityGroupId": workload_sg,
                }
            ],
        },
    )
    t.add(
        prefix + "EndpointEgress",
        "AWS::EC2::SecurityGroupEgress",
        {
            "GroupId": workload_sg,
            "IpProtocol": "tcp",
            "FromPort": 443,
            "ToPort": 443,
            "DestinationSecurityGroupId": endpoint_sg,
        },
    )
    endpoints = {}
    for service in interfaces:
        name = prefix + service.title().replace("-", "").replace(".", "") + "Endpoint"
        endpoints[service] = t.add(
            name,
            "AWS::EC2::VPCEndpoint",
            {
                "VpcId": vpc,
                "VpcEndpointType": "Interface",
                "ServiceName": sub("com.amazonaws.${AWS::Region}." + service),
                "PrivateDnsEnabled": True,
                "SubnetIds": [subnet],
                "SecurityGroupIds": [endpoint_sg],
            },
        )
    for service in gateways:
        name = prefix + service.title() + "Endpoint"
        endpoints[service] = t.add(
            name,
            "AWS::EC2::VPCEndpoint",
            {
                "VpcId": vpc,
                "VpcEndpointType": "Gateway",
                "ServiceName": sub("com.amazonaws.${AWS::Region}." + service),
                "RouteTableIds": [route],
            },
        )
        prefix_list = s3_prefix if service == "s3" else ddb_prefix
        if prefix_list is None:
            raise ValueError("gateway_prefix_list_required")
        t.add(
            prefix + service.title() + "GatewayEgress",
            "AWS::EC2::SecurityGroupEgress",
            {
                "GroupId": workload_sg,
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "DestinationPrefixListId": prefix_list,
            },
        )
    allowed = t.add(
        prefix + "AllowedDns",
        "AWS::Route53Resolver::FirewallDomainList",
        {
            "Name": "p2-" + prefix.lower() + "-allowed",
            "Domains": list(allowed_domains),
        },
    )
    all_domains = t.add(
        prefix + "AllDns",
        "AWS::Route53Resolver::FirewallDomainList",
        {
            "Name": "p2-" + prefix.lower() + "-all",
            "Domains": ["*"],
        },
    )
    rules = t.add(
        prefix + "DnsRules",
        "AWS::Route53Resolver::FirewallRuleGroup",
        {
            "Name": "p2-" + prefix.lower() + "-deny-default",
            "FirewallRules": [
                {"Action": "ALLOW", "FirewallDomainListId": allowed, "Priority": 10},
                {
                    "Action": "BLOCK",
                    "BlockResponse": "NXDOMAIN",
                    "FirewallDomainListId": all_domains,
                    "Priority": 100,
                },
            ],
        },
    )
    t.add(
        prefix + "DnsAssociation",
        "AWS::Route53Resolver::FirewallRuleGroupAssociation",
        {
            "Name": "p2-" + prefix.lower() + "-association",
            "FirewallRuleGroupId": rules,
            "VpcId": vpc,
            "Priority": 100,
            "MutationProtection": "DISABLED",
        },
    )
    t.add(
        prefix + "FlowLogGroup",
        "AWS::Logs::LogGroup",
        {
            "LogGroupName": sub("/p2/${AWS::StackName}/" + prefix.lower() + "/flow"),
            "RetentionInDays": log_retention,
        },
    )
    t.add(
        prefix + "FlowRole",
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": trust("vpc-flow-logs.amazonaws.com"),
            "Policies": [
                {
                    "PolicyName": "write-flow-only",
                    "PolicyDocument": policy(
                        [
                            allow(
                                [
                                    "logs:CreateLogStream",
                                    "logs:PutLogEvents",
                                    "logs:DescribeLogStreams",
                                ],
                                att(prefix + "FlowLogGroup"),
                            ),
                            allow("logs:DescribeLogGroups", "*"),
                        ]
                    ),
                }
            ],
        },
    )
    t.add(
        prefix + "FlowLog",
        "AWS::EC2::FlowLog",
        {
            "ResourceType": "VPC",
            "ResourceId": vpc,
            "TrafficType": "ALL",
            "LogDestinationType": "cloud-watch-logs",
            "LogGroupName": ref(prefix + "FlowLogGroup"),
            "DeliverLogsPermissionArn": att(prefix + "FlowRole"),
        },
    )
    t.out(prefix + "VpcId", vpc)
    t.out(prefix + "SubnetId", subnet)
    t.out(prefix + "WorkloadSgId", workload_sg)
    for service, endpoint in endpoints.items():
        t.out(
            prefix + service.title().replace("-", "").replace(".", "") + "EndpointId",
            endpoint,
        )
    return {
        "vpc": vpc,
        "subnet": subnet,
        "sg": workload_sg,
        "endpoint_sg": endpoint_sg,
        "endpoints": endpoints,
    }


def lambda_role(t, name):
    return t.add(
        name,
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": trust("lambda.amazonaws.com"),
            "ManagedPolicyArns": [
                sub(
                    "arn:${AWS::Partition}:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
                )
            ],
            "Policies": [
                {
                    "PolicyName": "deny-function-code-eni-management",
                    "PolicyDocument": policy(
                        [
                            deny(
                                [
                                    "ec2:CreateNetworkInterface",
                                    "ec2:DeleteNetworkInterface",
                                    "ec2:DescribeNetworkInterfaces",
                                    "ec2:DescribeSubnets",
                                    "ec2:DetachNetworkInterface",
                                    "ec2:AssignPrivateIpAddresses",
                                    "ec2:UnassignPrivateIpAddresses",
                                ],
                                "*",
                                condition={
                                    "ArnLike": {
                                        "lambda:SourceFunctionArn": sub(
                                            "arn:${AWS::Partition}:lambda:${AWS::Region}:"
                                            "${AWS::AccountId}:function:p2-*"
                                        )
                                    }
                                },
                            )
                        ]
                    ),
                }
            ],
        },
    )


def iam_policy(t, name, role, statements):
    return t.add(
        name,
        "AWS::IAM::Policy",
        {
            "PolicyName": sub("p2-${AWS::StackName}-" + name.lower()),
            "Roles": [role],
            "PolicyDocument": policy(statements),
        },
    )


def lambda_function(
    t, name, role, net, environment, *, handler, memory=256, timeout=30
):
    function_name = sub("p2-${AWS::StackName}-" + name.lower())
    t.add(
        name + "LogGroup",
        "AWS::Logs::LogGroup",
        {
            "LogGroupName": sub("/aws/lambda/p2-${AWS::StackName}-" + name.lower()),
            "RetentionInDays": ref("LogRetentionDays"),
        },
    )
    return t.add(
        name,
        "AWS::Lambda::Function",
        {
            "FunctionName": function_name,
            "Runtime": "python3.14",
            "Handler": handler,
            "Role": att(role) if isinstance(role, str) else role,
            "Code": {"S3Bucket": ref("CodeBucket"), "S3Key": ref("CodeKey")},
            "Timeout": timeout,
            "MemorySize": memory,
            "VpcConfig": {
                "SubnetIds": [net["subnet"]],
                "SecurityGroupIds": [net["sg"]],
            },
            "Environment": {
                "Variables": {
                    "P2_SYNTHETIC_ONLY": "1",
                    **(
                        {"P2_EXPIRES_AT": ref("ExpiresAtUtc")}
                        if "ExpiresAtUtc" in t.data["Parameters"]
                        else {}
                    ),
                    **environment,
                }
            },
            "ReservedConcurrentExecutions": 2,
        },
    )


def main():
    from .stacks import account_a, account_b, account_c, foundation

    root = Path(__file__).resolve().parent / "generated"
    root.mkdir(exist_ok=True)
    for name, builder in (
        ("account-a", account_a),
        ("account-b", account_b),
        ("account-c", account_c),
        ("foundation-a", lambda: foundation("a")),
        ("foundation-b", lambda: foundation("b")),
        ("foundation-c", lambda: foundation("c")),
    ):
        output = root / (name + ".json")
        output.write_text(json.dumps(builder(), indent=2, ensure_ascii=False) + "\n")
        print(output)


if __name__ == "__main__":
    main()
