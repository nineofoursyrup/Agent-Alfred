"""Account-owned resources. Cross-account IDs enter only as reviewed parameters."""

from .render import (
    Template,
    allow,
    att,
    iam_policy,
    join,
    lambda_function,
    lambda_role,
    network,
    policy,
    ref,
    sink_egress,
    sub,
    trust,
)


def _code(t):
    t.param("CodeBucket", allowed_pattern=r"[a-z0-9][a-z0-9.-]{2,62}")
    t.param("CodeKey")


def foundation(account):
    """Owned bootstrap resources that break the A/B/C dependency cycle."""
    if account not in ("a", "b", "c"):
        raise ValueError("invalid_p2_account")
    t = Template("P2 account " + account.upper() + " bootstrap")
    bucket = t.add(
        "CodeBucket",
        "AWS::S3::Bucket",
        {
            "VersioningConfiguration": {"Status": "Enabled"},
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
            "BucketEncryption": {
                "ServerSideEncryptionConfiguration": [
                    {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                ]
            },
        },
    )
    t.out("CodeBucketName", bucket)
    if account == "a":
        t.param("BAccountId", allowed_pattern=r"[0-9]{12}")
        t.param("BAuthorityRoleArn")
        issuer_role = lambda_role(t, "IssuerRole")
        t.add(
            "SyntheticSigningKey",
            "AWS::KMS::Key",
            {
                "KeySpec": "ECC_NIST_P256",
                "KeyUsage": "SIGN_VERIFY",
                "Enabled": True,
                "EnableKeyRotation": False,
                "KeyPolicy": policy(
                    [
                        allow(
                            "kms:*",
                            "*",
                            principal={
                                "AWS": sub(
                                    "arn:${AWS::Partition}:iam::${AWS::AccountId}:root"
                                )
                            },
                        ),
                        allow("kms:Sign", "*", principal={"AWS": att("IssuerRole")}),
                        allow(
                            "kms:Verify",
                            "*",
                            principal={
                                "AWS": sub(
                                    "arn:${AWS::Partition}:iam::${BAccountId}:root"
                                )
                            },
                            condition={
                                "ArnEquals": {
                                    "aws:PrincipalArn": ref("BAuthorityRoleArn")
                                }
                            },
                        ),
                    ]
                ),
            },
        )
        t.out("IssuerRoleName", issuer_role)
        t.out("IssuerRoleArn", att("IssuerRole"))
        t.out("SigningKeyArn", att("SyntheticSigningKey"))
    if account == "b":
        role = lambda_role(t, "AuthorityRole")
        t.add(
            "IssuerDlq",
            "AWS::SQS::Queue",
            {"MessageRetentionPeriod": 1209600},
            retain=True,
        )
        queue = t.add(
            "IssuerQueue",
            "AWS::SQS::Queue",
            {
                "VisibilityTimeout": 180,
                "RedrivePolicy": {
                    "deadLetterTargetArn": att("IssuerDlq"),
                    "maxReceiveCount": 2,
                },
            },
            retain=True,
        )
        t.add(
            "WorkerRepository",
            "AWS::ECR::Repository",
            {
                "ImageScanningConfiguration": {"ScanOnPush": True},
                "ImageTagMutability": "IMMUTABLE",
                "EncryptionConfiguration": {"EncryptionType": "AES256"},
            },
        )
        t.out("AuthorityRoleName", role)
        t.out("AuthorityRoleArn", att("AuthorityRole"))
        t.out("IssuerQueueArn", att("IssuerQueue"))
        t.out("IssuerQueueUrl", queue)
        t.out("IssuerDlqUrl", ref("IssuerDlq"))
        t.out("ImageRepositoryArn", att("WorkerRepository"))
        t.out(
            "ImageRepositoryUri",
            sub(
                "${AWS::AccountId}.dkr.ecr.${AWS::Region}.amazonaws.com/"
                + "${WorkerRepository}"
            ),
        )
    return t.data


def _trail(t, name, bucket, account_id, *, data_resources=None):
    name_value = sub("p2-${AWS::StackName}-" + name)
    t.add(
        "AuditTrail",
        "AWS::CloudTrail::Trail",
        {
            "TrailName": name_value,
            "S3BucketName": bucket,
            "IsLogging": True,
            "EnableLogFileValidation": True,
            "IsMultiRegionTrail": False,
            "EventSelectors": [
                {
                    "ReadWriteType": "All",
                    "IncludeManagementEvents": True,
                    "DataResources": data_resources or [],
                }
            ],
        },
    )
    t.out(
        "AuditTrailArn",
        sub(
            "arn:${AWS::Partition}:cloudtrail:${AWS::Region}:"
            + "${AWS::AccountId}:trail/p2-${AWS::StackName}-"
            + name
        ),
    )


def account_c():
    t = Template("P2 account C independent monotonic anchor and locked audit")
    _code(t)
    for label in ("AAdminRoleArn", "BAdminRoleArn", "CAdminRoleArn"):
        t.param(
            label,
            allowed_pattern=r"arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+",
        )
    t.param(
        "AuthorityRoleArn",
        allowed_pattern=r"arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_-]+",
    )
    a_account = t.param("AAccountId", allowed_pattern=r"[0-9]{12}")
    b_account = t.param("BAccountId", allowed_pattern=r"[0-9]{12}")
    retention = t.param("RetentionDays", type_="Number", min_value=1, max_value=365)
    log_retention = t.param(
        "LogRetentionDays", type_="Number", min_value=1, max_value=365
    )
    s3_prefix = t.param("S3PrefixListId", allowed_pattern=r"pl-[0-9a-f]+")
    ddb_prefix = t.param("DynamoPrefixListId", allowed_pattern=r"pl-[0-9a-f]+")
    t.add(
        "PreflightSignerKey",
        "AWS::KMS::Key",
        {
            "KeySpec": "ECC_NIST_P256",
            "KeyUsage": "SIGN_VERIFY",
            "Enabled": True,
            "EnableKeyRotation": False,
            "KeyPolicy": policy(
                [
                    allow(
                        "kms:*",
                        "*",
                        principal={
                            "AWS": sub(
                                "arn:${AWS::Partition}:iam::${AWS::AccountId}:root"
                            )
                        },
                    ),
                    allow(
                        "kms:Sign",
                        "*",
                        principal={"AWS": ref("CAdminRoleArn")},
                    ),
                    allow(
                        "kms:Verify",
                        "*",
                        principal={"AWS": [ref("AAdminRoleArn"), ref("BAdminRoleArn")]},
                    ),
                ]
            ),
        },
        retain=True,
    )
    net = network(
        t,
        "Anchor",
        "10.84.4.0/24",
        "10.84.4.0/25",
        gateways=("s3", "dynamodb"),
        allowed_domains=(
            sub("s3.${AWS::Region}.amazonaws.com"),
            sub("dynamodb.${AWS::Region}.amazonaws.com"),
        ),
        s3_prefix=s3_prefix,
        ddb_prefix=ddb_prefix,
        log_retention=log_retention,
    )
    table = t.add(
        "AnchorTable",
        "AWS::DynamoDB::Table",
        {
            "BillingMode": "PAY_PER_REQUEST",
            "AttributeDefinitions": [{"AttributeName": "PK", "AttributeType": "S"}],
            "KeySchema": [{"AttributeName": "PK", "KeyType": "HASH"}],
            "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
        },
        retain=True,
    )
    t.data["Resources"]["AnchorS3Endpoint"]["Properties"]["PolicyDocument"] = policy(
        [
            allow("s3:ListBucketVersions", att("AuditBucket"), principal="*"),
            allow(
                [
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:GetObjectRetention",
                    "s3:PutObject",
                    "s3:PutObjectRetention",
                ],
                join("", [att("AuditBucket"), "/anchors/*"]),
                principal="*",
            ),
        ]
    )
    t.data["Resources"]["AnchorDynamodbEndpoint"]["Properties"]["PolicyDocument"] = (
        policy(
            [
                allow(
                    ["dynamodb:GetItem", "dynamodb:UpdateItem"],
                    att("AnchorTable"),
                    principal="*",
                )
            ]
        )
    )
    bucket = t.add(
        "AuditBucket",
        "AWS::S3::Bucket",
        {
            "VersioningConfiguration": {"Status": "Enabled"},
            "ObjectLockEnabled": True,
            "ObjectLockConfiguration": {
                "ObjectLockEnabled": "Enabled",
                "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Days": retention}},
            },
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
            "BucketEncryption": {
                "ServerSideEncryptionConfiguration": [
                    {
                        "ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
                    }
                ]
            },
        },
        retain=True,
    )
    role = lambda_role(t, "AnchorRole")
    function = lambda_function(
        t,
        "AnchorFunction",
        "AnchorRole",
        net,
        {
            "P2_ANCHOR_TABLE": table,
            "P2_ANCHOR_BUCKET": bucket,
            "P2_RETENTION_DAYS": retention,
        },
        handler="agent_alfred.p2.anchor.handler",
        timeout=45,
    )
    iam_policy(
        t,
        "AnchorDataPolicy",
        role,
        [
            allow(["dynamodb:GetItem", "dynamodb:UpdateItem"], att("AnchorTable")),
            allow(["s3:ListBucketVersions"], att("AuditBucket")),
            allow(
                [
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:GetObjectRetention",
                    "s3:PutObject",
                    "s3:PutObjectRetention",
                ],
                join("", [att("AuditBucket"), "/anchors/*"]),
            ),
        ],
    )
    t.add(
        "AuthorityInvokePermission",
        "AWS::Lambda::Permission",
        {
            "Action": "lambda:InvokeFunction",
            "FunctionName": function,
            "Principal": ref("AuthorityRoleArn"),
        },
    )
    trail_patterns = [
        sub(
            "arn:${AWS::Partition}:cloudtrail:${AWS::Region}:"
            + "${AWS::AccountId}:trail/p2-*-audit"
        ),
        {
            "Fn::Sub": [
                "arn:${AWS::Partition}:cloudtrail:${AWS::Region}:${External}:trail/p2-*-audit",
                {"External": a_account},
            ]
        },
        {
            "Fn::Sub": [
                "arn:${AWS::Partition}:cloudtrail:${AWS::Region}:${External}:trail/p2-*-audit",
                {"External": b_account},
            ]
        },
    ]
    t.add(
        "AuditBucketPolicy",
        "AWS::S3::BucketPolicy",
        {
            "Bucket": bucket,
            "PolicyDocument": policy(
                [
                    allow(
                        "s3:GetBucketAcl",
                        att("AuditBucket"),
                        principal={"Service": "cloudtrail.amazonaws.com"},
                        condition={"StringLike": {"aws:SourceArn": trail_patterns}},
                    ),
                    allow(
                        "s3:PutObject",
                        join("", [att("AuditBucket"), "/AWSLogs/*"]),
                        principal={"Service": "cloudtrail.amazonaws.com"},
                        condition={
                            "StringEquals": {
                                "s3:x-amz-acl": "bucket-owner-full-control"
                            },
                            "StringLike": {"aws:SourceArn": trail_patterns},
                        },
                    ),
                ]
            ),
        },
    )
    _trail(
        t,
        "audit",
        bucket,
        ref("AWS::AccountId"),
        data_resources=[
            {
                "Type": "AWS::S3::Object",
                "Values": [join("", [att("AuditBucket"), "/anchors/"])],
            },
            {"Type": "AWS::DynamoDB::Table", "Values": [att("AnchorTable")]},
            {"Type": "AWS::Lambda::Function", "Values": [att("AnchorFunction")]},
        ],
    )
    t.data["Resources"]["AuditTrail"]["DependsOn"] = "AuditBucketPolicy"
    t.out("AnchorFunctionArn", att("AnchorFunction"))
    t.out("AnchorTableArn", att("AnchorTable"))
    t.out("AnchorTableName", table)
    t.out("AuditBucketName", bucket)
    t.out("PreflightSignerKeyArn", att("PreflightSignerKey"))
    return t.data


def _api(
    t,
    name,
    actions,
    function_name,
    *,
    private,
    endpoints=(),
    allowed_vpce=None,
    operator_role=None,
    redeploy_condition=None,
):
    configuration = {"Types": ["PRIVATE" if private else "REGIONAL"]}
    if endpoints:
        configuration["VpcEndpointIds"] = (
            endpoints if isinstance(endpoints, dict) else list(endpoints)
        )
    properties = {
        "EndpointConfiguration": configuration,
        "Name": sub("p2-${AWS::StackName}-" + name.lower()),
    }
    if private:
        properties["Policy"] = policy(
            [
                allow(
                    "execute-api:Invoke",
                    "execute-api:/*",
                    principal="*",
                    condition={
                        "StringEquals": {
                            "aws:SourceVpce": (
                                allowed_vpce
                                if isinstance(allowed_vpce, dict)
                                else list(allowed_vpce)
                            )
                        }
                    },
                )
            ]
        )
    elif operator_role is not None:
        properties["Policy"] = policy(
            [
                allow(
                    "execute-api:Invoke",
                    "execute-api:/*",
                    principal={"AWS": operator_role},
                )
            ]
        )
    api = t.add(name + "Api", "AWS::ApiGateway::RestApi", properties)
    root = t.add(
        name + "V1",
        "AWS::ApiGateway::Resource",
        {
            "RestApiId": api,
            "ParentId": att(name + "Api", "RootResourceId"),
            "PathPart": "v1",
        },
    )
    method_names = []
    for action, method in actions:
        resource_name = name + action.title() + "Resource"
        action_resource = t.add(
            resource_name,
            "AWS::ApiGateway::Resource",
            {
                "RestApiId": api,
                "ParentId": root,
                "PathPart": action,
            },
        )
        method_name = name + action.title() + "Method"
        t.add(
            method_name,
            "AWS::ApiGateway::Method",
            {
                "RestApiId": api,
                "ResourceId": action_resource,
                "HttpMethod": method,
                "AuthorizationType": "AWS_IAM",
                "Integration": {
                    "Type": "AWS_PROXY",
                    "IntegrationHttpMethod": "POST",
                    "Uri": join(
                        "",
                        [
                            sub(
                                "arn:${AWS::Partition}:apigateway:${AWS::Region}:"
                                + "lambda:path/2015-03-31/functions/"
                            ),
                            att(function_name),
                            "/invocations",
                        ],
                    ),
                },
            },
        )
        method_names.append(method_name)
    deployment = t.add(
        name + "Deployment",
        "AWS::ApiGateway::Deployment",
        {
            "RestApiId": api,
        },
        depends=method_names,
    )
    stage_deployment = deployment
    if redeploy_condition:
        t.add(
            name + "IssuerDeployment",
            "AWS::ApiGateway::Deployment",
            {"RestApiId": api},
            depends=method_names,
            condition=redeploy_condition,
        )
        stage_deployment = {
            "Fn::If": [
                redeploy_condition,
                ref(name + "IssuerDeployment"),
                deployment,
            ]
        }
    t.add(
        name + "Stage",
        "AWS::ApiGateway::Stage",
        {
            "RestApiId": api,
            "DeploymentId": stage_deployment,
            "StageName": "p2",
            "MethodSettings": [
                {
                    "ResourcePath": "/*",
                    "HttpMethod": "*",
                    "DataTraceEnabled": False,
                    "LoggingLevel": "OFF",
                }
            ],
        },
    )
    t.add(
        name + "ApiLambdaPermission",
        "AWS::Lambda::Permission",
        {
            "FunctionName": ref(function_name),
            "Action": "lambda:InvokeFunction",
            "Principal": "apigateway.amazonaws.com",
            "SourceArn": join(
                "",
                [
                    sub(
                        "arn:${AWS::Partition}:execute-api:${AWS::Region}:"
                        + "${AWS::AccountId}:"
                    ),
                    api,
                    "/*/*/v1/*",
                ],
            ),
        },
    )
    t.out(name + "ApiId", api)
    t.out(
        name + "ApiBase",
        join(
            "",
            [
                "https://",
                api,
                sub(".execute-api.${AWS::Region}.amazonaws.com/p2"),
            ],
        ),
    )
    return api


def account_a():
    t = Template("P2 account A synthetic issuer, operator-only signing root")
    _code(t)
    t.param("BAccountId", allowed_pattern=r"[0-9]{12}")
    authority_api_id = t.param("AuthorityApiId")
    queue_arn = t.param("IssuerQueueArn")
    queue_url = t.param("IssuerQueueUrl")
    t.param(
        "OperatorRoleArn",
        allowed_pattern=r"arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_-]+",
    )
    issuer_role_arn = t.param("IssuerRoleArn")
    issuer_role_name = t.param("IssuerRoleName")
    signing_key_arn = t.param("SigningKeyArn")
    t.param("AuditBucketName")
    log_retention = t.param(
        "LogRetentionDays", type_="Number", min_value=1, max_value=365
    )
    t.param("EnableTraffic", default="false", allowed_pattern=r"true|false")
    t.param("ExpiresAtUtc")
    net = network(
        t,
        "Issuer",
        "10.84.1.0/24",
        "10.84.1.0/25",
        interfaces=("kms", "sqs", "execute-api"),
        allowed_domains=(
            sub("kms.${AWS::Region}.amazonaws.com"),
            sub("sqs.${AWS::Region}.amazonaws.com"),
            sub("*.execute-api.${AWS::Region}.amazonaws.com"),
        ),
        log_retention=log_retention,
    )
    lambda_function(
        t,
        "IssuerFunction",
        issuer_role_arn,
        net,
        {
            "P2_DEPLOYMENT_ACTIVE": ref("EnableTraffic"),
            "P2_OPERATOR_ROLE_ARN": ref("OperatorRoleArn"),
            "P2_SYNTHETIC_KMS_ARN": signing_key_arn,
            "P2_ISSUER_QUEUE_URL": queue_url,
            "P2_AUTHORITY_API_BASE": join(
                "",
                [
                    "https://",
                    authority_api_id,
                    sub(".execute-api.${AWS::Region}.amazonaws.com/p2"),
                ],
            ),
        },
        handler="agent_alfred.p2.issuer.handler",
    )
    iam_policy(
        t,
        "IssuerActionPolicy",
        issuer_role_name,
        [
            allow("kms:Sign", signing_key_arn),
            allow("sqs:SendMessage", queue_arn),
            allow(
                "execute-api:Invoke",
                join(
                    "",
                    [
                        sub("arn:${AWS::Partition}:execute-api:${AWS::Region}:"),
                        ref("BAccountId"),
                        ":",
                        authority_api_id,
                        "/p2/POST/v1/proposal",
                    ],
                ),
            ),
        ],
    )
    _api(
        t,
        "Issuer",
        [("proposal", "GET"), ("issue", "POST"), ("revoke", "POST")],
        "IssuerFunction",
        private=False,
        operator_role=ref("OperatorRoleArn"),
    )
    _trail(t, "audit", ref("AuditBucketName"), ref("AWS::AccountId"))
    t.out("IssuerRoleArn", issuer_role_arn)
    t.out("SigningKeyArn", signing_key_arn)
    return t.data


def _ecs_role(t, name):
    return t.add(
        name,
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": trust("ecs-tasks.amazonaws.com"),
        },
    )


def _task(
    t,
    name,
    task_role,
    execution_role,
    image_uri,
    package_sha,
    input_sha,
    api_base,
    log_group,
    mode,
):
    return t.add(
        name,
        "AWS::ECS::TaskDefinition",
        {
            "Family": sub("p2-${AWS::StackName}-" + name.lower()),
            "Cpu": "256",
            "Memory": "512",
            "NetworkMode": "awsvpc",
            "RequiresCompatibilities": ["FARGATE"],
            "RuntimePlatform": {
                "CpuArchitecture": "X86_64",
                "OperatingSystemFamily": "LINUX",
            },
            "TaskRoleArn": att(task_role),
            "ExecutionRoleArn": att(execution_role),
            "ContainerDefinitions": [
                {
                    "Name": "p2-controlled",
                    "Image": image_uri,
                    "Essential": True,
                    "ReadonlyRootFilesystem": True,
                    "User": "10001",
                    "Environment": [
                        {"Name": "P2_SYNTHETIC_ONLY", "Value": "1"},
                        {"Name": "P2_PACKAGE_SHA256", "Value": package_sha},
                        {"Name": "P2_INPUT_SHA256", "Value": input_sha},
                        {"Name": "P2_INPUT_PATH", "Value": "/opt/p2/input.json"},
                        {"Name": "P2_AUTHORITY_API_BASE", "Value": api_base},
                        {"Name": "P2_RUN_MODE", "Value": mode},
                        {"Name": "AWS_REGION", "Value": ref("AWS::Region")},
                        {"Name": "PYTHONDONTWRITEBYTECODE", "Value": "1"},
                    ],
                    "LogConfiguration": {
                        "LogDriver": "awslogs",
                        "Options": {
                            "awslogs-group": log_group,
                            "awslogs-region": ref("AWS::Region"),
                            "awslogs-stream-prefix": name.lower(),
                        },
                    },
                }
            ],
        },
    )


def account_b():
    t = Template("P2 account B protected Authority, Dispatch, canary, and worker")
    _code(t)
    t.param("AAccountId", allowed_pattern=r"[0-9]{12}")
    c_account = t.param("CAccountId", allowed_pattern=r"[0-9]{12}")
    t.param("IssuerRoleArn")
    t.param("IssuerKeyArn")
    authority_role_arn = t.param("AuthorityRoleArn")
    authority_role_name = t.param("AuthorityRoleName")
    queue_arn = t.param("IssuerQueueArn")
    queue_url = t.param("IssuerQueueUrl")
    t.param("AnchorFunctionArn")
    t.param("AIssuerVpceId", default="")
    t.param("AuditBucketName")
    log_retention = t.param(
        "LogRetentionDays", type_="Number", min_value=1, max_value=365
    )
    t.param("EnableTraffic", default="false", allowed_pattern=r"true|false")
    t.param("StopDispatch", default="true", allowed_pattern=r"true|false")
    t.param("ExpiresAtUtc")
    t.param("SyntheticSecretValue", no_echo=True)
    t.param("CanaryTokenSha256", allowed_pattern=r"[0-9a-f]{64}")
    image_uri = t.param("ImageUri", allowed_pattern=r".+@sha256:[0-9a-f]{64}")
    package_sha = t.param("PackageSha256", allowed_pattern=r"[0-9a-f]{64}")
    input_sha = t.param("InputSha256", allowed_pattern=r"[0-9a-f]{64}")
    image_repo = t.param("ImageRepositoryArn")
    s3_prefix = t.param("S3PrefixListId", allowed_pattern=r"pl-[0-9a-f]+")
    ddb_prefix = t.param("DynamoPrefixListId", allowed_pattern=r"pl-[0-9a-f]+")
    t.data["Conditions"] = {
        "TrafficEnabled": {"Fn::Equals": [ref("EnableTraffic"), "true"]},
        "HasIssuerVpce": {"Fn::Not": [{"Fn::Equals": [ref("AIssuerVpceId"), ""]}]},
    }
    worker_net = network(
        t,
        "Worker",
        "10.84.2.0/24",
        "10.84.2.0/25",
        interfaces=("ecr.api", "ecr.dkr", "logs", "execute-api"),
        gateways=("s3",),
        s3_prefix=s3_prefix,
        allowed_domains=(
            sub("api.ecr.${AWS::Region}.amazonaws.com"),
            sub("*.dkr.ecr.${AWS::Region}.amazonaws.com"),
            sub("logs.${AWS::Region}.amazonaws.com"),
            sub("*.execute-api.${AWS::Region}.amazonaws.com"),
            sub("s3.${AWS::Region}.amazonaws.com"),
            sub("*.s3.${AWS::Region}.amazonaws.com"),
            sub("kms.${AWS::Region}.amazonaws.com"),
            sub("secretsmanager.${AWS::Region}.amazonaws.com"),
            sub("lambda.${AWS::Region}.amazonaws.com"),
            sub("sts.${AWS::Region}.amazonaws.com"),
            sub("dynamodb.${AWS::Region}.amazonaws.com"),
        ),
        log_retention=log_retention,
    )
    broker_net = network(
        t,
        "Broker",
        "10.84.3.0/24",
        "10.84.3.0/25",
        interfaces=("sqs", "lambda", "kms"),
        gateways=("dynamodb",),
        ddb_prefix=ddb_prefix,
        allowed_domains=(
            sub("sqs.${AWS::Region}.amazonaws.com"),
            sub("lambda.${AWS::Region}.amazonaws.com"),
            sub("kms.${AWS::Region}.amazonaws.com"),
            sub("secretsmanager.${AWS::Region}.amazonaws.com"),
            sub("*.execute-api.${AWS::Region}.amazonaws.com"),
            sub("dynamodb.${AWS::Region}.amazonaws.com"),
            sub("sts.${AWS::Region}.amazonaws.com"),
        ),
        log_retention=log_retention,
    )
    dispatch_subnet = t.add(
        "DispatchSubnet",
        "AWS::EC2::Subnet",
        {
            "VpcId": broker_net["vpc"],
            "CidrBlock": "10.84.3.128/25",
            "MapPublicIpOnLaunch": False,
            "AvailabilityZone": {"Fn::Select": [0, {"Fn::GetAZs": ""}]},
        },
    )
    t.add(
        "DispatchRouteAssociation",
        "AWS::EC2::SubnetRouteTableAssociation",
        {
            "SubnetId": dispatch_subnet,
            "RouteTableId": ref("BrokerRoute"),
        },
    )
    dispatch_sg = t.add(
        "DispatchSg",
        "AWS::EC2::SecurityGroup",
        {
            "GroupDescription": "P2 dispatch separate egress",
            "VpcId": broker_net["vpc"],
            "SecurityGroupEgress": sink_egress(),
        },
    )
    canary_sg = t.add(
        "CanarySg",
        "AWS::EC2::SecurityGroup",
        {
            "GroupDescription": "P2 canary no application egress",
            "VpcId": broker_net["vpc"],
            "SecurityGroupEgress": sink_egress(),
        },
    )
    private_api_sg = t.add(
        "BrokerApiEndpointSg",
        "AWS::EC2::SecurityGroup",
        {
            "GroupDescription": "P2 broker private REST endpoint",
            "VpcId": broker_net["vpc"],
            "SecurityGroupEgress": sink_egress(),
            "SecurityGroupIngress": [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 443,
                    "ToPort": 443,
                    "SourceSecurityGroupId": dispatch_sg,
                },
            ],
        },
    )
    secret_sg = t.add(
        "BrokerSecretEndpointSg",
        "AWS::EC2::SecurityGroup",
        {
            "GroupDescription": "P2 secret endpoint Dispatch only",
            "VpcId": broker_net["vpc"],
            "SecurityGroupEgress": sink_egress(),
            "SecurityGroupIngress": [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 443,
                    "ToPort": 443,
                    "SourceSecurityGroupId": dispatch_sg,
                }
            ],
        },
    )
    broker_probe_sg = t.add(
        "BrokerProbeSg",
        "AWS::EC2::SecurityGroup",
        {
            "GroupDescription": "P2 Dispatch identity probe with protected endpoints",
            "VpcId": broker_net["vpc"],
            "SecurityGroupEgress": sink_egress(),
        },
    )
    for name, target in (
        ("Broker", broker_net["endpoint_sg"]),
        ("Secret", secret_sg),
    ):
        t.add(
            "BrokerProbe" + name + "Ingress",
            "AWS::EC2::SecurityGroupIngress",
            {
                "GroupId": target,
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "SourceSecurityGroupId": broker_probe_sg,
            },
        )
        t.add(
            "BrokerProbe" + name + "Egress",
            "AWS::EC2::SecurityGroupEgress",
            {
                "GroupId": broker_probe_sg,
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "DestinationSecurityGroupId": target,
            },
        )
    t.add(
        "BrokerProbeDynamoEgress",
        "AWS::EC2::SecurityGroupEgress",
        {
            "GroupId": broker_probe_sg,
            "IpProtocol": "tcp",
            "FromPort": 443,
            "ToPort": 443,
            "DestinationPrefixListId": ddb_prefix,
        },
    )
    broker_api_vpce = t.add(
        "BrokerExecuteApiEndpoint",
        "AWS::EC2::VPCEndpoint",
        {
            "VpcId": broker_net["vpc"],
            "VpcEndpointType": "Interface",
            "ServiceName": sub("com.amazonaws.${AWS::Region}.execute-api"),
            "PrivateDnsEnabled": True,
            "SubnetIds": [broker_net["subnet"]],
            "SecurityGroupIds": [private_api_sg],
        },
    )
    t.add(
        "BrokerSecretsEndpoint",
        "AWS::EC2::VPCEndpoint",
        {
            "VpcId": broker_net["vpc"],
            "VpcEndpointType": "Interface",
            "ServiceName": sub("com.amazonaws.${AWS::Region}.secretsmanager"),
            "PrivateDnsEnabled": True,
            "SubnetIds": [dispatch_subnet],
            "SecurityGroupIds": [secret_sg],
        },
    )
    t.add(
        "BrokerProbeStsEndpoint",
        "AWS::EC2::VPCEndpoint",
        {
            "VpcId": broker_net["vpc"],
            "VpcEndpointType": "Interface",
            "ServiceName": sub("com.amazonaws.${AWS::Region}.sts"),
            "PrivateDnsEnabled": True,
            "SubnetIds": [broker_net["subnet"]],
            "SecurityGroupIds": [broker_net["endpoint_sg"]],
        },
    )
    for name, target in (
        ("DispatchApiEgress", private_api_sg),
        ("DispatchSecretEgress", secret_sg),
    ):
        t.add(
            name,
            "AWS::EC2::SecurityGroupEgress",
            {
                "GroupId": dispatch_sg,
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "DestinationSecurityGroupId": target,
            },
        )
    table = t.add(
        "GrantTable",
        "AWS::DynamoDB::Table",
        {
            "BillingMode": "PAY_PER_REQUEST",
            "AttributeDefinitions": [
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            "KeySchema": [
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
        },
        retain=True,
    )
    worker_probe_sg = t.add(
        "WorkerProbeSg",
        "AWS::EC2::SecurityGroup",
        {
            "GroupDescription": "P2 same-role IAM probe private endpoints",
            "VpcId": worker_net["vpc"],
            "SecurityGroupEgress": sink_egress(),
        },
    )
    worker_probe_endpoint_sg = t.add(
        "WorkerProbeEndpointSg",
        "AWS::EC2::SecurityGroup",
        {
            "GroupDescription": "P2 IAM probe endpoint reachable only from probe tasks",
            "VpcId": worker_net["vpc"],
            "SecurityGroupEgress": sink_egress(),
            "SecurityGroupIngress": [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 443,
                    "ToPort": 443,
                    "SourceSecurityGroupId": worker_probe_sg,
                }
            ],
        },
    )
    t.add(
        "WorkerProbeInfrastructureIngress",
        "AWS::EC2::SecurityGroupIngress",
        {
            "GroupId": worker_net["endpoint_sg"],
            "IpProtocol": "tcp",
            "FromPort": 443,
            "ToPort": 443,
            "SourceSecurityGroupId": worker_probe_sg,
        },
    )
    for name, target in (
        ("Infrastructure", worker_net["endpoint_sg"]),
        ("Protected", worker_probe_endpoint_sg),
    ):
        t.add(
            "WorkerProbe" + name + "Egress",
            "AWS::EC2::SecurityGroupEgress",
            {
                "GroupId": worker_probe_sg,
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "DestinationSecurityGroupId": target,
            },
        )
    for name, prefix in (("S3", s3_prefix), ("Dynamo", ddb_prefix)):
        t.add(
            "WorkerProbe" + name + "GatewayEgress",
            "AWS::EC2::SecurityGroupEgress",
            {
                "GroupId": worker_probe_sg,
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "DestinationPrefixListId": prefix,
            },
        )
    for service in ("kms", "secretsmanager", "lambda", "sts"):
        t.add(
            "WorkerProbe" + service.title() + "Endpoint",
            "AWS::EC2::VPCEndpoint",
            {
                "VpcId": worker_net["vpc"],
                "VpcEndpointType": "Interface",
                "ServiceName": sub("com.amazonaws.${AWS::Region}." + service),
                "PrivateDnsEnabled": True,
                "SubnetIds": [worker_net["subnet"]],
                "SecurityGroupIds": [worker_probe_endpoint_sg],
            },
        )
    worker_probe_ddb = t.add(
        "WorkerProbeDynamoEndpoint",
        "AWS::EC2::VPCEndpoint",
        {
            "VpcId": worker_net["vpc"],
            "VpcEndpointType": "Gateway",
            "ServiceName": sub("com.amazonaws.${AWS::Region}.dynamodb"),
            "RouteTableIds": [ref("WorkerRoute")],
            "PolicyDocument": policy(
                [
                    allow(
                        ["dynamodb:GetItem", "dynamodb:PutItem"],
                        att("GrantTable"),
                        principal="*",
                    )
                ]
            ),
        },
    )
    t.data["Resources"]["WorkerS3Endpoint"]["Properties"]["PolicyDocument"] = policy(
        [
            allow(
                "s3:GetObject",
                sub(
                    "arn:${AWS::Partition}:s3:::prod-${AWS::Region}-starport-layer-bucket/*"
                ),
                principal="*",
            )
        ]
    )
    t.data["Resources"]["BrokerDynamodbEndpoint"]["Properties"]["PolicyDocument"] = (
        policy(
            [
                allow(
                    ["dynamodb:GetItem", "dynamodb:PutItem"],
                    att("GrantTable"),
                    principal="*",
                )
            ]
        )
    )
    t.add(
        "IssuerQueuePolicy",
        "AWS::SQS::QueuePolicy",
        {
            "Queues": [queue_url],
            "PolicyDocument": policy(
                [
                    allow(
                        "sqs:SendMessage",
                        queue_arn,
                        principal={
                            "AWS": sub("arn:${AWS::Partition}:iam::${AAccountId}:root")
                        },
                        condition={
                            "ArnEquals": {"aws:PrincipalArn": ref("IssuerRoleArn")}
                        },
                    )
                ]
            ),
        },
    )
    t.add(
        "SyntheticCanarySecret",
        "AWS::SecretsManager::Secret",
        {
            "Description": "Synthetic P2 canary token; never a model credential",
            "SecretString": ref("SyntheticSecretValue"),
        },
    )
    authority_role = authority_role_name
    dispatch_role = lambda_role(t, "DispatchRole")
    lambda_role(t, "CanaryRole")
    worker_role = _ecs_role(t, "WorkerRole")
    executor_role = _ecs_role(t, "ExecutorRole")
    execution_role = _ecs_role(t, "TaskExecutionRole")
    authority_function = lambda_function(
        t,
        "AuthorityFunction",
        authority_role_arn,
        broker_net,
        {
            "P2_DEPLOYMENT_ACTIVE": ref("EnableTraffic"),
            "P2_STOP_DISPATCH": ref("StopDispatch"),
            "P2_GRANT_TABLE": table,
            "P2_ANCHOR_FUNCTION_ARN": ref("AnchorFunctionArn"),
            "P2_DISPATCH_FUNCTION_ARN": sub(
                "arn:${AWS::Partition}:lambda:${AWS::Region}:${AWS::AccountId}:"
                + "function:p2-${AWS::StackName}-dispatchfunction"
            ),
            "P2_ISSUER_KMS_ARN": ref("IssuerKeyArn"),
            "P2_ISSUER_QUEUE_ARN": queue_arn,
            "P2_WORKER_ROLE_ARN": att("WorkerRole"),
            "P2_EXECUTOR_ROLE_ARN": att("ExecutorRole"),
            "P2_ISSUER_ROLE_ARN": ref("IssuerRoleArn"),
            "P2_DISPATCH_ROLE_ARN": att("DispatchRole"),
        },
        handler="agent_alfred.p2.authority_handler.handler",
        memory=512,
        timeout=90,
    )
    dispatch_net = {**broker_net, "subnet": dispatch_subnet, "sg": dispatch_sg}
    canary_net = {**broker_net, "subnet": dispatch_subnet, "sg": canary_sg}
    # The REST API IDs are referenced by Dispatch after API resources are
    # created. Their Methods depend on the functions, not vice versa.
    lambda_function(
        t,
        "DispatchFunction",
        "DispatchRole",
        dispatch_net,
        {
            "P2_DEPLOYMENT_ACTIVE": ref("EnableTraffic"),
            "P2_STOP_DISPATCH": ref("StopDispatch"),
            "P2_AUTHORITY_API_BASE": join(
                "",
                [
                    "https://",
                    ref("AuthorityApi"),
                    sub(".execute-api.${AWS::Region}.amazonaws.com/p2"),
                ],
            ),
            "P2_CANARY_API_BASE": join(
                "",
                [
                    "https://",
                    ref("CanaryApi"),
                    sub(".execute-api.${AWS::Region}.amazonaws.com/p2"),
                ],
            ),
            "P2_SYNTHETIC_SECRET_ARN": ref("SyntheticCanarySecret"),
        },
        handler="agent_alfred.p2.dispatch.handler",
        timeout=25,
    )
    lambda_function(
        t,
        "CanaryFunction",
        "CanaryRole",
        canary_net,
        {
            "P2_DEPLOYMENT_ACTIVE": ref("EnableTraffic"),
            "P2_DISPATCH_ROLE_ARN": att("DispatchRole"),
            "P2_CANARY_TOKEN_SHA256": ref("CanaryTokenSha256"),
        },
        handler="agent_alfred.p2.canary.handler",
        timeout=10,
    )
    lambda_function(
        t,
        "DispatchProbeFunction",
        "DispatchRole",
        dispatch_net,
        {"P2_PROBE_ONLY": "1", "P2_PROBE_ROLE": "dispatch"},
        handler="agent_alfred.p2.probe.handler",
    )
    lambda_function(
        t,
        "DispatchIamProbeFunction",
        "DispatchRole",
        {**broker_net, "sg": broker_probe_sg},
        {"P2_PROBE_ONLY": "1", "P2_PROBE_ROLE": "dispatch"},
        handler="agent_alfred.p2.probe.handler",
    )
    lambda_function(
        t,
        "AuthorityProbeFunction",
        authority_role_arn,
        broker_net,
        {"P2_PROBE_ONLY": "1", "P2_PROBE_ROLE": "authority"},
        handler="agent_alfred.p2.probe.handler",
    )
    lambda_function(
        t,
        "AuthorityIamProbeFunction",
        authority_role_arn,
        {**broker_net, "sg": broker_probe_sg},
        {"P2_PROBE_ONLY": "1", "P2_PROBE_ROLE": "authority"},
        handler="agent_alfred.p2.probe.handler",
    )
    authority_api = _api(
        t,
        "Authority",
        [
            ("submit", "POST"),
            ("invoke", "POST"),
            ("finish", "POST"),
            ("status", "POST"),
            ("proposal", "POST"),
            ("preflight", "POST"),
        ],
        "AuthorityFunction",
        private=True,
        endpoints={
            "Fn::If": [
                "HasIssuerVpce",
                [
                    worker_net["endpoints"]["execute-api"],
                    broker_api_vpce,
                    ref("AIssuerVpceId"),
                ],
                [worker_net["endpoints"]["execute-api"], broker_api_vpce],
            ]
        },
        allowed_vpce={
            "Fn::If": [
                "HasIssuerVpce",
                [
                    worker_net["endpoints"]["execute-api"],
                    broker_api_vpce,
                    ref("AIssuerVpceId"),
                ],
                [worker_net["endpoints"]["execute-api"], broker_api_vpce],
            ]
        },
        redeploy_condition="HasIssuerVpce",
    )
    canary_api = _api(
        t,
        "Canary",
        [("canary", "POST")],
        "CanaryFunction",
        private=True,
        endpoints=(broker_api_vpce,),
        allowed_vpce=(broker_api_vpce,),
    )
    iam_policy(
        t,
        "AuthorityDataPolicy",
        authority_role,
        [
            allow(["dynamodb:GetItem", "dynamodb:PutItem"], att("GrantTable")),
            allow(
                ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
                queue_arn,
            ),
            allow("kms:Verify", ref("IssuerKeyArn")),
            allow(
                "lambda:InvokeFunction",
                [ref("AnchorFunctionArn"), att("DispatchFunction")],
            ),
        ],
    )
    iam_policy(
        t,
        "DispatchDataPolicy",
        dispatch_role,
        [
            allow("secretsmanager:GetSecretValue", ref("SyntheticCanarySecret")),
            allow(
                "execute-api:Invoke",
                [
                    join(
                        "",
                        [
                            sub(
                                "arn:${AWS::Partition}:execute-api:${AWS::Region}:"
                                + "${AWS::AccountId}:"
                            ),
                            authority_api,
                            "/p2/POST/v1/preflight",
                        ],
                    ),
                    join(
                        "",
                        [
                            sub(
                                "arn:${AWS::Partition}:execute-api:${AWS::Region}:"
                                + "${AWS::AccountId}:"
                            ),
                            canary_api,
                            "/p2/POST/v1/canary",
                        ],
                    ),
                ],
            ),
        ],
    )

    def api_arns(paths):
        return [
            join(
                "",
                [
                    sub(
                        "arn:${AWS::Partition}:execute-api:${AWS::Region}:"
                        + "${AWS::AccountId}:"
                    ),
                    authority_api,
                    "/p2/POST/v1/" + path,
                ],
            )
            for path in paths
        ]

    iam_policy(
        t,
        "WorkerApiPolicy",
        worker_role,
        [allow("execute-api:Invoke", api_arns(("invoke", "finish", "status")))],
    )
    iam_policy(
        t,
        "ExecutorApiPolicy",
        executor_role,
        [allow("execute-api:Invoke", api_arns(("submit", "status")))],
    )
    log_group = t.add(
        "TaskLogGroup",
        "AWS::Logs::LogGroup",
        {
            "LogGroupName": sub("/p2/${AWS::StackName}/tasks"),
            "RetentionInDays": log_retention,
        },
    )
    iam_policy(
        t,
        "TaskExecutionPolicy",
        execution_role,
        [
            allow("ecr:GetAuthorizationToken", "*"),
            allow(["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"], image_repo),
            allow(["logs:CreateLogStream", "logs:PutLogEvents"], att("TaskLogGroup")),
        ],
    )
    t.add(
        "WorkerCluster",
        "AWS::ECS::Cluster",
        {
            "ClusterName": sub("p2-${AWS::StackName}-controlled"),
            "ClusterSettings": [{"Name": "containerInsights", "Value": "enabled"}],
        },
    )
    api_base = join(
        "",
        [
            "https://",
            authority_api,
            sub(".execute-api.${AWS::Region}.amazonaws.com/p2"),
        ],
    )
    _task(
        t,
        "ExecutorTask",
        "ExecutorRole",
        "TaskExecutionRole",
        image_uri,
        package_sha,
        input_sha,
        api_base,
        log_group,
        "submit",
    )
    _task(
        t,
        "WorkerTask",
        "WorkerRole",
        "TaskExecutionRole",
        image_uri,
        package_sha,
        input_sha,
        api_base,
        log_group,
        "invoke",
    )
    t.add(
        "IssuerEventMapping",
        "AWS::Lambda::EventSourceMapping",
        {
            "EventSourceArn": queue_arn,
            "FunctionName": authority_function,
            "BatchSize": 1,
            "Enabled": True,
        },
        condition="TrafficEnabled",
        depends="AuthorityDataPolicy",
    )
    _trail(t, "audit", ref("AuditBucketName"), ref("AWS::AccountId"))
    t.out("AuthorityRoleArn", authority_role_arn)
    t.out("DispatchRoleArn", att("DispatchRole"))
    t.out("WorkerRoleArn", att("WorkerRole"))
    t.out("ExecutorRoleArn", att("ExecutorRole"))
    t.out("IssuerQueueArn", queue_arn)
    t.out("IssuerQueueUrl", queue_url)
    t.out("WorkerClusterName", ref("WorkerCluster"))
    t.out("ExpectedCAccountId", c_account)
    t.out("WorkerTaskArn", ref("WorkerTask"))
    t.out("ExecutorTaskArn", ref("ExecutorTask"))
    t.out("TaskLogGroupName", log_group)
    t.out("BrokerApiVpceId", broker_api_vpce)
    t.out("AuthorityFunctionArn", att("AuthorityFunction"))
    t.out("DispatchFunctionArn", att("DispatchFunction"))
    t.out("DispatchProbeFunctionArn", att("DispatchProbeFunction"))
    t.out("DispatchIamProbeFunctionArn", att("DispatchIamProbeFunction"))
    t.out("AuthorityProbeFunctionArn", att("AuthorityProbeFunction"))
    t.out("AuthorityIamProbeFunctionArn", att("AuthorityIamProbeFunction"))
    t.out("WorkerProbeSgId", worker_probe_sg)
    t.out("WorkerProbeDynamoEndpointId", worker_probe_ddb)
    t.out("BrokerProbeSgId", broker_probe_sg)
    t.out("BrokerWorkloadSgId", broker_net["sg"])
    t.out("DispatchSgId", dispatch_sg)
    t.out("GrantTableName", table)
    t.out("SyntheticCanarySecretArn", ref("SyntheticCanarySecret"))
    t.out(
        "CanaryApiBase",
        join(
            "",
            [
                "https://",
                canary_api,
                sub(".execute-api.${AWS::Region}.amazonaws.com/p2"),
            ],
        ),
    )
    return t.data
