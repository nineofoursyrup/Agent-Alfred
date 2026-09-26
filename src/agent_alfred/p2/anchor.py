"""Account C monotonic anchor. C owns this code, table, bucket, and retention."""

import base64
import hashlib
from datetime import timedelta

from .common import (
    ZERO_DIGEST,
    BoundaryError,
    check_digest,
    check_id,
    decode,
    encode,
    exact,
    required_env,
    synthetic_only,
    utc_now,
)

# 16 requests with reserve/intent/preflight time fences and settlement, plus
# ISSUE, operation completion and stop/revoke events. Still a bounded trial.
MAX_REVISIONS = 256


class Anchor:
    def __init__(self, ddb, s3, table, bucket, retention_days):
        if not 1 <= retention_days <= 365:
            raise BoundaryError("invalid_anchor_retention")
        self.ddb, self.s3 = ddb, s3
        self.table, self.bucket = table, bucket
        self.retention_days = retention_days

    def _versions(self, job_id):
        prefix = f"anchors/{job_id}/"
        versions = {}
        kwargs = {"Bucket": self.bucket, "Prefix": prefix}
        while True:
            page = self.s3.list_object_versions(**kwargs)
            if page.get("DeleteMarkers"):
                raise BoundaryError("anchor_versions_unverifiable")
            for item in page.get("Versions", []):
                key = item["Key"]
                if key in versions or not item.get("VersionId"):
                    raise BoundaryError("anchor_versions_unverifiable")
                versions[key] = item["VersionId"]
            if not page.get("IsTruncated"):
                break
            if not page.get("NextKeyMarker") or not page.get("NextVersionIdMarker"):
                raise BoundaryError("anchor_versions_unverifiable")
            kwargs["KeyMarker"] = page["NextKeyMarker"]
            kwargs["VersionIdMarker"] = page["NextVersionIdMarker"]
        return versions

    def read(self, job_id):
        check_id(job_id)
        row = self.ddb.get_item(
            TableName=self.table,
            Key={"PK": {"S": job_id}},
            ConsistentRead=True,
        ).get("Item")
        revision = int(row["Revision"]["N"]) if row else 0
        current = check_digest(row["Digest"]["S"]) if row else ZERO_DIGEST
        if not 0 <= revision <= MAX_REVISIONS:
            raise BoundaryError("anchor_high_water_unverifiable")
        versions = self._versions(job_id)
        if len(versions) != revision:
            raise BoundaryError("anchor_versions_unverifiable")
        previous = ZERO_DIGEST
        for number in range(1, revision + 1):
            prefix = f"anchors/{job_id}/{number:04d}-"
            matching = [key for key in versions if key.startswith(prefix)]
            if len(matching) != 1:
                raise BoundaryError("anchor_chain_unverifiable")
            key = matching[0]
            retention = self.s3.get_object_retention(
                Bucket=self.bucket, Key=key, VersionId=versions[key]
            ).get("Retention", {})
            retain_until = retention.get("RetainUntilDate")
            if (
                retention.get("Mode") != "COMPLIANCE"
                or retain_until is None
                or retain_until <= utc_now()
            ):
                raise BoundaryError("anchor_retention_unverifiable")
            document = decode(
                self.s3.get_object(
                    Bucket=self.bucket, Key=key, VersionId=versions[key]
                )["Body"].read(256 * 1024 + 1)
            )
            exact(document, {"job_id", "revision", "previous_digest", "event_digest"})
            if (
                document["job_id"] != job_id
                or document["revision"] != number
                or document["previous_digest"] != previous
                or key != prefix + document["event_digest"] + ".json"
            ):
                raise BoundaryError("anchor_chain_unverifiable")
            previous = check_digest(document["event_digest"])
        if previous != current:
            raise BoundaryError("anchor_high_water_unverifiable")
        return {"job_id": job_id, "revision": revision, "digest": current}

    def commit(self, event):
        exact(event, {"job_id", "revision", "previous_digest", "event_digest"})
        job_id = check_id(event["job_id"])
        revision = event["revision"]
        if type(revision) is not int or not 1 <= revision <= MAX_REVISIONS:
            raise BoundaryError("anchor_revision_invalid")
        previous = check_digest(event["previous_digest"])
        event_digest = check_digest(event["event_digest"])
        observed = self.read(job_id)
        if observed != {
            "job_id": job_id,
            "revision": revision - 1,
            "digest": previous,
        }:
            raise BoundaryError("anchor_revision_conflict")
        if revision == 1:
            condition = "attribute_not_exists(PK)"
            values = None
        else:
            condition = "Revision = :old AND Digest = :previous"
            values = {
                ":old": {"N": str(revision - 1)},
                ":previous": {"S": previous},
            }
        update = {
            "TableName": self.table,
            "Key": {"PK": {"S": job_id}},
            "UpdateExpression": "SET Revision = :new, Digest = :digest",
            "ConditionExpression": condition,
            "ExpressionAttributeValues": {
                **(values or {}),
                ":new": {"N": str(revision)},
                ":digest": {"S": event_digest},
            },
        }
        # A failed S3 write leaves the high-water table ahead. It is deliberately
        # not repaired or retried by B: all future reads fail until C adjudicates.
        self.ddb.update_item(**update)
        key = f"anchors/{job_id}/{revision:04d}-{event_digest}.json"
        raw = encode(event)
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=raw,
            ContentType="application/json",
            ContentMD5=base64.b64encode(
                hashlib.md5(raw, usedforsecurity=False).digest()
            ).decode(),
            IfNoneMatch="*",
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=utc_now() + timedelta(days=self.retention_days),
        )
        confirmed = self.read(job_id)
        if confirmed["revision"] != revision or confirmed["digest"] != event_digest:
            raise BoundaryError("anchor_commit_unverifiable")
        print(
            encode(
                {
                    "p2_audit": "anchor_commit",
                    "job_id": job_id,
                    "revision": revision,
                    "event_digest": event_digest,
                }
            ).decode()
        )
        return confirmed


def handler(event, _context):
    synthetic_only()
    import boto3
    from botocore.config import Config

    service = Anchor(
        boto3.client("dynamodb"),
        # Match C's exact regional DNS allowlist, including us-east-1.
        boto3.client(
            "s3",
            config=Config(
                s3={
                    "addressing_style": "path",
                    "us_east_1_regional_endpoint": "regional",
                }
            ),
        ),
        required_env("P2_ANCHOR_TABLE"),
        required_env("P2_ANCHOR_BUCKET"),
        int(required_env("P2_RETENTION_DAYS")),
    )
    action = event.get("action") if type(event) is dict else None
    if action == "read" and set(event) == {"action", "job_id"}:
        return service.read(event["job_id"])
    if action == "commit" and set(event) == {"action", "event"}:
        return service.commit(event["event"])
    raise BoundaryError("anchor_action_invalid")
