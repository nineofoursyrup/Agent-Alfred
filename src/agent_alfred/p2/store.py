"""Account B conditional transaction store for synthetic P2 grant state."""

from .common import BoundaryError, check_digest, check_id, decode, digest, encode


class DynamoJobStore:
    def __init__(self, client, table):
        self.client, self.table = client, table

    def get(self, job_id):
        check_id(job_id)
        item = self.client.get_item(
            TableName=self.table,
            Key={"PK": {"S": job_id}, "SK": {"S": "STATE"}},
            ConsistentRead=True,
        ).get("Item")
        if item is None:
            return None
        state = decode(item["Document"]["S"])
        if (
            type(state) is not dict
            or state.get("job_id") != job_id
            or state.get("revision") != int(item["Revision"]["N"])
            or state.get("event_digest") != item["Digest"]["S"]
        ):
            raise BoundaryError("authority_state_unverifiable")
        check_digest(state["event_digest"])
        event_row = self.client.get_item(
            TableName=self.table,
            Key={"PK": {"S": job_id}, "SK": {"S": f"EVENT#{state['revision']:04d}"}},
            ConsistentRead=True,
        ).get("Item")
        if (
            not event_row
            or event_row.get("Digest", {}).get("S") != state["event_digest"]
        ):
            raise BoundaryError("authority_event_unverifiable")
        event = decode(event_row["Document"]["S"])
        if (
            type(event) is not dict
            or event.get("job_id") != job_id
            or event.get("revision") != state["revision"]
            or event.get("state_digest")
            != digest(
                {key: value for key, value in state.items() if key != "event_digest"}
            )
            or digest(event) != state["event_digest"]
        ):
            raise BoundaryError("authority_event_unverifiable")
        return state

    def put(self, state, event, *, old_revision, old_digest):
        job_id = check_id(state["job_id"])
        revision = state["revision"]
        if type(revision) is not int or revision != old_revision + 1:
            raise BoundaryError("authority_revision_invalid")
        check_digest(old_digest)
        check_digest(state["event_digest"])
        if event["revision"] != revision or event["job_id"] != job_id:
            raise BoundaryError("authority_event_invalid")
        condition = (
            "attribute_not_exists(PK)"
            if old_revision == 0
            else "Revision = :old AND Digest = :previous"
        )
        state_put = {
            "TableName": self.table,
            "Item": {
                "PK": {"S": job_id},
                "SK": {"S": "STATE"},
                "Revision": {"N": str(revision)},
                "Digest": {"S": state["event_digest"]},
                "Document": {"S": encode(state).decode()},
            },
            "ConditionExpression": condition,
        }
        if old_revision:
            state_put["ExpressionAttributeValues"] = {
                ":old": {"N": str(old_revision)},
                ":previous": {"S": old_digest},
            }
        event_put = {
            "TableName": self.table,
            "Item": {
                "PK": {"S": job_id},
                "SK": {"S": f"EVENT#{revision:04d}"},
                "Digest": {"S": digest(event)},
                "Document": {"S": encode(event).decode()},
            },
            "ConditionExpression": "attribute_not_exists(PK)",
        }
        # Both B entries commit or neither. C's independent commit follows;
        # there is no false claim of a cross-account transaction.
        self.client.transact_write_items(
            TransactItems=[{"Put": state_put}, {"Put": event_put}],
            ClientRequestToken=digest({"job_id": job_id, "revision": revision})[:36],
        )
