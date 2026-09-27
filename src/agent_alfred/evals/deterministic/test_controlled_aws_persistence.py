"""Pure stub SDK calls against botocore shapes, no session or credential lookup."""

from copy import deepcopy
from io import BytesIO

import pytest
from botocore.exceptions import ClientError
from botocore.loaders import Loader
from botocore.model import ServiceModel
from botocore.validate import validate_parameters

from agent_alfred.evals.acceptance.controlled.aws_persistence import (
    AwsExecutionAnchor,
    AwsExecutionStore,
)
from agent_alfred.evals.acceptance.controlled_persistence import (
    PersistentControlledAuthority,
)

from .test_controlled_execution import fixture, prepare


class DynamoStub:
    def __init__(self):
        self.model = ServiceModel(Loader().load_service_model("dynamodb", "service-2"))
        self.rows, self.calls = {}, []

    def validate(self, operation, args):
        validate_parameters(args, self.model.operation_model(operation).input_shape)
        self.calls.append((operation, deepcopy(args)))

    def get_item(self, **args):
        self.validate("GetItem", args)
        assert args["ConsistentRead"] is True
        key = (args["TableName"], args["Key"]["PK"]["S"], args["Key"]["SK"]["S"])
        return {"Item": deepcopy(self.rows[key])} if key in self.rows else {}

    def query(self, **args):
        self.validate("Query", args)
        assert args["ConsistentRead"] is True
        values = args["ExpressionAttributeValues"]
        part, prefix = values[":partition"]["S"], values.get(":prefix", {}).get("S", "")
        after = args.get("ExclusiveStartKey", {}).get("SK", {}).get("S", "")
        rows = [
            deepcopy(row)
            for (table, pk, sk), row in sorted(self.rows.items())
            if table == args["TableName"]
            and pk == part
            and sk.startswith(prefix)
            and sk > after
        ]
        page = {"Items": rows[:3]}
        if len(rows) > 3:
            page["LastEvaluatedKey"] = {key: rows[2][key] for key in ("PK", "SK")}
        return page

    def transact_write_items(self, **args):
        self.validate("TransactWriteItems", args)
        pending = []
        for item in args["TransactItems"]:
            put = item["Put"]
            row = put["Item"]
            key = (put["TableName"], row["PK"]["S"], row["SK"]["S"])
            current = self.rows.get(key)
            expected = (
                put.get("ExpressionAttributeValues", {}).get(":expected", {}).get("S")
            )
            if (current["Digest"]["S"] if current else None) != expected:
                raise ClientError(
                    {
                        "Error": {"Code": "TransactionCanceledException"},
                        "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
                    },
                    "TransactWriteItems",
                )
            pending.append((key, deepcopy(row)))
        for key, row in pending:
            self.rows[key] = row
        return {}


class S3Stub:
    def __init__(self):
        self.model = ServiceModel(Loader().load_service_model("s3", "service-2"))
        self.rows, self.bodies, self.calls = {}, [], []
        self.fail_anchor_write = False

    def validate(self, operation, args):
        validate_parameters(args, self.model.operation_model(operation).input_shape)
        self.calls.append((operation, args))

    def put_object(self, **args):
        self.validate("PutObject", args)
        assert args["IfNoneMatch"] == "*" and args["ObjectLockMode"] == "COMPLIANCE"
        if self.fail_anchor_write and args["Key"].startswith("anchors/"):
            raise OSError("fixture S3 anchor unavailable")
        key = (args["Bucket"], args["Key"])
        if key in self.rows:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.rows[key] = {
            "version": "fixture-version",
            "raw": args["Body"],
            "until": args["ObjectLockRetainUntilDate"],
        }
        return {"VersionId": "fixture-version"}

    def get_object(self, **args):
        self.validate("GetObject", args)
        row = self.rows.get((args["Bucket"], args["Key"]))
        if row is None:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        if "VersionId" in args:
            assert args["VersionId"] == row["version"]
        body = BytesIO(row["raw"])
        self.bodies.append(body)
        return {"Body": body, "VersionId": row["version"]}

    def get_object_retention(self, **args):
        self.validate("GetObjectRetention", args)
        row = self.rows[args["Bucket"], args["Key"]]
        assert args["VersionId"] == row["version"]
        return {"Retention": {"Mode": "COMPLIANCE", "RetainUntilDate": row["until"]}}

    def list_object_versions(self, **args):
        self.validate("ListObjectVersions", args)
        versions = [
            {"Key": key, "VersionId": row["version"]}
            for (bucket, key), row in sorted(self.rows.items())
            if bucket == args["Bucket"]
            and key.startswith(args["Prefix"])
            and key > args.get("KeyMarker", "")
        ]
        result = {"Versions": versions[:2], "IsTruncated": len(versions) > 2}
        if len(versions) > 2:
            result.update(
                NextKeyMarker=versions[1]["Key"],
                NextVersionIdMarker=versions[1]["VersionId"],
            )
        return result


def aws_fixture(tmp_path):
    f = fixture(tmp_path)
    b, c, s3 = DynamoStub(), DynamoStub(), S3Stub()
    f["ledger"] = AwsExecutionStore(
        dynamodb=b,
        s3=s3,
        table="authority-fixture",
        bucket="authority-fixture",
        retention_days=1,
        now=f["clock"],
    )
    f["anchor"] = AwsExecutionAnchor(
        dynamodb=c,
        s3=s3,
        table="anchor-fixture",
        bucket="anchor-fixture",
        retention_days=1,
        now=f["clock"],
    )
    f["authority"] = PersistentControlledAuthority(
        **{
            key: value
            for key, value in vars(f["authority"]).items()
            if key not in ("store", "anchor")
        },
        store=f["ledger"],
        anchor=f["anchor"],
    )
    return f, b, c, s3


def test_sdk_shapes_paginated_readback_atomic_attempt_and_immutable_objects(tmp_path):
    f, b, c, s3 = aws_fixture(tmp_path)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    client, _ = prepare(f)
    assert client.respond(f["request"]).final_error is None
    auth.finish("controlled-job", "diagnose")
    assert len(f["ledger"].read_events("controlled-job")) == 17
    assert len(f["anchor"].read_events("controlled-job")) == 17
    assert any("ExclusiveStartKey" in args for op, args in b.calls if op == "Query")
    assert any(
        "KeyMarker" in args for op, args in s3.calls if op == "ListObjectVersions"
    )
    assert all(body.closed for body in s3.bodies)
    # Every Attempt mutation is in the same transaction as its state and event.
    groups = [
        args["TransactItems"] for op, args in b.calls if op == "TransactWriteItems"
    ]
    attempt_groups = [
        group
        for group in groups
        if any(item["Put"]["Item"]["SK"]["S"].startswith("ATTEMPT#") for item in group)
    ]
    assert attempt_groups and all(len(group) in (3, 4) for group in attempt_groups)
    assert (
        len(attempt_groups[0]) == 4
    )  # First reserve atomically consumes its prepared ref.
    auth.recover("controlled-job", principal="simulation:controller")
    assert auth.status("controlled-job")["state"]["counts"]["total"] == 1
    old_key = next(
        key for key in s3.rows if key[1].startswith("anchors/") and "/000001-" in key[1]
    )
    s3.rows.pop(old_key)
    with pytest.raises(ClientError):
        auth.recover("controlled-job", principal="simulation:controller")
    assert f["ledger"].faults("controlled-job")


def test_partial_anchor_write_stays_stopped_even_when_service_returns(tmp_path):
    f, b, c, s3 = aws_fixture(tmp_path)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    s3.fail_anchor_write = True
    with pytest.raises(OSError, match="S3 anchor unavailable"):
        prepare(f)
    s3.fail_anchor_write = False
    report = auth.stop_report("controlled-job")
    assert report["ledger_and_anchor_verified"] is False
    assert report["faults"] and report["blockers"]
    assert f["credentials"].reads == 0
    with pytest.raises(ClientError):
        prepare(f)
