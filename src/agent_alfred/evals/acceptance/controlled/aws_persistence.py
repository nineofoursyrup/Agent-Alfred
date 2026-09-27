"""Dynamo/S3 implementation of the controlled durable document contract.

No clients, sessions, credentials or cloud resources are constructed here. The
trusted installation supplies SDK clients pinned to its separately governed B/C
tables and Object Lock buckets. Passing stubs tests the real request shapes, not
account identity, policies, network isolation, latency or deployed capacity.
"""

from datetime import UTC, datetime, timedelta
from functools import partial

from agent_alfred.resource_rollback import RollbackSlot

from ..materials import strict_json
from ..schema import digest, encode
from .contract import integer, text
from .persistence import (
    MAX_EVENT_BYTES,
    MAX_OBJECT_BYTES,
    MAX_OBJECTS,
    MAX_STATE_BYTES,
    DurableExecutionAnchor,
    DurableExecutionStore,
    bounded,
)
from .response_resources import close_body, owned_response


def _error_code(error):
    return getattr(error, "response", {}).get("Error", {}).get("Code")


def _body(response):
    """Read only after the SDK call's response owner has been installed."""
    body = response["Body"]
    return strict_json(body.read(MAX_OBJECT_BYTES + 1), limit=MAX_OBJECT_BYTES)


class AwsDocuments:
    """Strong consistent document reads and paginated, atomic CAS transactions."""

    synthetic_only = False

    def __init__(self, *, dynamodb, s3, table, bucket, retention_days, now=None):
        self.ddb, self.s3 = dynamodb, s3
        self.table, self.bucket = text(table), text(bucket)
        self.retention_days = integer(retention_days, minimum=1, maximum=365)
        self.now = now or (lambda: datetime.now(UTC))
        self._cleanup = RollbackSlot()

    def retry_cleanup(self):
        """Trusted resource recovery; never repeats a read, write or dispatch."""
        return self._cleanup.retry()

    def generation(self):
        # The installed policy must make committed EVENT/ANCHOR rows immutable.
        # New handlers and explicit recover/read_events still verify the entire
        # chain. The running handler validates every new CAS append itself.
        return None

    def _decode(self, item):
        if item is None:
            return None
        value = strict_json(item["Document"]["S"].encode(), limit=MAX_STATE_BYTES)
        if digest(value) != item["Digest"]["S"]:
            raise ValueError("execution_document_unverifiable")
        return value

    def read(self, part, key):
        response = self.ddb.get_item(
            TableName=self.table,
            Key={"PK": {"S": part}, "SK": {"S": key}},
            ConsistentRead=True,
        )
        return self._decode(response.get("Item"))

    def scan(self, part, prefix):
        # Query the exact partition. Do not Scan the table or depend on the
        # first 1 MiB page; 8064+ event rows require complete pagination.
        kwargs = {
            "TableName": self.table,
            "ConsistentRead": True,
            "KeyConditionExpression": "PK = :partition AND begins_with(SK, :prefix)",
            "ExpressionAttributeValues": {
                ":partition": {"S": part},
                ":prefix": {"S": prefix},
            },
        }
        if not prefix:
            kwargs["KeyConditionExpression"] = "PK = :partition"
            del kwargs["ExpressionAttributeValues"][":prefix"]
        rows, cursors = [], set()
        while True:
            result = self.ddb.query(**kwargs)
            for item in result.get("Items", []):
                if item["PK"]["S"] != part or not item["SK"]["S"].startswith(prefix):
                    raise ValueError("execution_query_scope_mismatch")
                rows.append((item["SK"]["S"], self._decode(item)))
            cursor = result.get("LastEvaluatedKey")
            if not cursor:
                break
            identity = digest(cursor)
            if identity in cursors:
                raise ValueError("execution_query_pagination_unverifiable")
            cursors.add(identity)
            kwargs["ExclusiveStartKey"] = cursor
        if len({key for key, _ in rows}) != len(rows):
            raise ValueError("execution_query_duplicate")
        return sorted(rows)

    def transaction(self, writes):
        puts = []
        for part, key, value, old in writes:
            raw = bounded(value, MAX_STATE_BYTES if key == "STATE" else MAX_EVENT_BYTES)
            item = {
                "PK": {"S": part},
                "SK": {"S": key},
                "Digest": {"S": digest(value)},
                "Document": {"S": raw.decode()},
            }
            put = {
                "TableName": self.table,
                "Item": item,
                "ConditionExpression": "attribute_not_exists(PK)"
                if old is None
                else "Digest = :expected",
            }
            if old is not None:
                put["ExpressionAttributeValues"] = {":expected": {"S": old}}
            puts.append({"Put": put})
        try:
            self.ddb.transact_write_items(
                TransactItems=puts, ClientRequestToken=digest(puts)[:36]
            )
        except Exception as error:
            code = _error_code(error)
            reasons = getattr(error, "response", {}).get("CancellationReasons", [])
            if code == "ConditionalCheckFailedException" or (
                code == "TransactionCanceledException"
                and any(r.get("Code") == "ConditionalCheckFailed" for r in reasons)
                and all(
                    r.get("Code") in ("None", "ConditionalCheckFailed") for r in reasons
                )
            ):
                raise ValueError("ledger_conflict") from error
            raise

    def _put_immutable(self, key, value):
        raw = bounded(value, MAX_OBJECT_BYTES)
        try:
            saved = self.s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=raw,
                ContentType="application/json",
                IfNoneMatch="*",
                ObjectLockMode="COMPLIANCE",
                ObjectLockRetainUntilDate=self.now()
                + timedelta(days=self.retention_days),
                Metadata={"sha256": digest(value)},
            )
            version = saved.get("VersionId")
        except Exception as error:
            if _error_code(error) not in (
                "PreconditionFailed",
                "ConditionalRequestConflict",
            ):
                raise
            with owned_response(
                self._cleanup,
                partial(self.s3.get_object, Bucket=self.bucket, Key=key),
                close=partial(close_body, field="Body"),
            ) as existing:
                version = existing.get("VersionId")
                if _body(existing) != value:
                    raise ValueError("immutable_object_replaced") from error
        if not version or version == "null":
            raise ValueError("object_version_unverifiable")
        return version

    def _get_immutable(self, key, *, version=None):
        with owned_response(
            self._cleanup,
            partial(
                self.s3.get_object,
                Bucket=self.bucket,
                Key=key,
                **({"VersionId": version} if version else {}),
            ),
            close=partial(close_body, field="Body"),
        ) as result:
            observed = result.get("VersionId")
            value = _body(result)
        if not observed or observed == "null" or (version and observed != version):
            raise ValueError("object_version_unverifiable")
        retention = self.s3.get_object_retention(
            Bucket=self.bucket, Key=key, VersionId=observed
        ).get("Retention", {})
        until = retention.get("RetainUntilDate")
        if (
            retention.get("Mode") != "COMPLIANCE"
            or until is None
            or until <= self.now()
        ):
            raise ValueError("object_retention_unverifiable")
        return value

    def put_object(self, value):
        identity, size = digest(value), len(bounded(value, MAX_OBJECT_BYTES))
        present = self.read("OBJECT", identity)
        if present is not None:
            if self.get_object(identity) != value:
                raise ValueError("execution_object_mismatch")
            return identity
        count = self.read("META", "OBJECT_COUNT")
        if (count or {"count": 0})["count"] >= MAX_OBJECTS:
            raise ValueError("execution_object_capacity_exceeded")
        version = self._put_immutable("objects/" + identity + ".json", value)
        index = {"sha256": identity, "bytes": size, "version": version}
        self.transaction(
            [
                ("OBJECT", identity, index, None),
                (
                    "META",
                    "OBJECT_COUNT",
                    {"count": (count or {"count": 0})["count"] + 1},
                    digest(count) if count else None,
                ),
            ]
        )
        if self.get_object(identity) != value:
            raise ValueError("execution_object_mismatch")
        return identity

    def get_object(self, identity):
        index = self.read("OBJECT", identity)
        if index is None or index["sha256"] != identity:
            raise ValueError("execution_object_missing")
        value = self._get_immutable(
            "objects/" + identity + ".json", version=index["version"]
        )
        if digest(value) != identity or len(encode(value)) != index["bytes"]:
            raise ValueError("execution_object_mismatch")
        return value

    def object_capacity(self):
        rows = [row for _, row in self.scan("OBJECT", "")]
        return {
            "count": len(rows),
            "max_bytes": max((r["bytes"] for r in rows), default=0),
            "total_bytes": sum(r["bytes"] for r in rows),
        }

    @staticmethod
    def _anchor_key(event):
        prefix = f"anchors/{event['job_id']}/{event['revision']:06d}-"
        return prefix + event["event_digest"] + ".json"

    def save_anchor(self, event):
        self._put_immutable(self._anchor_key(event), event)

    def verify_anchor(self, event):
        if self._get_immutable(self._anchor_key(event)) != event:
            raise ValueError("anchor_object_mismatch")

    def verify_anchor_inventory(self, job_id, events):
        keys = {self._anchor_key(event) for event in events}
        observed, cursors = set(), set()
        kwargs = {"Bucket": self.bucket, "Prefix": f"anchors/{job_id}/"}
        while True:
            page = self.s3.list_object_versions(**kwargs)
            if page.get("DeleteMarkers"):
                raise ValueError("anchor_versions_unverifiable")
            for version in page.get("Versions", []):
                key = version["Key"]
                if key not in keys or key in observed or not version.get("VersionId"):
                    raise ValueError("anchor_versions_unverifiable")
                observed.add(key)
            if not page.get("IsTruncated"):
                break
            cursor = (page.get("NextKeyMarker"), page.get("NextVersionIdMarker"))
            if not all(cursor) or cursor in cursors:
                raise ValueError("anchor_pagination_unverifiable")
            cursors.add(cursor)
            kwargs.update(KeyMarker=cursor[0], VersionIdMarker=cursor[1])
        if observed != keys:
            raise ValueError("anchor_versions_unverifiable")


class AwsExecutionStore(DurableExecutionStore):
    synthetic_only = False

    def __init__(self, **configuration):
        super().__init__(AwsDocuments(**configuration))


class AwsExecutionAnchor(DurableExecutionAnchor):
    synthetic_only = False

    def __init__(self, **configuration):
        super().__init__(AwsDocuments(**configuration))
