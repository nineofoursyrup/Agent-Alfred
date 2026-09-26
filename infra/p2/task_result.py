"""Read a stopped synthetic task and its bounded stdout as B's auditor."""

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .ctl import client_session, parse_config


def launch_readback(session, task_arn, created_at):
    trail = session.client("cloudtrail")
    for page in trail.get_paginator("lookup_events").paginate(
        LookupAttributes=[{"AttributeKey": "EventName", "AttributeValue": "RunTask"}],
        StartTime=created_at - timedelta(minutes=5),
        EndTime=datetime.now(timezone.utc),
    ):
        for event in page.get("Events", []):
            document = json.loads(event["CloudTrailEvent"])
            tasks = (document.get("responseElements") or {}).get("tasks") or []
            if not any(row.get("taskArn") == task_arn for row in tasks):
                continue
            network = (
                (document.get("requestParameters") or {})
                .get("networkConfiguration", {})
                .get("awsvpcConfiguration", {})
            )
            if not network.get("securityGroups") or not network.get("subnets"):
                raise ValueError("CloudTrail RunTask network is incomplete")
            return {
                "event_id": event["EventId"],
                "security_groups": network["securityGroups"],
                "subnets": network["subnets"],
                "assign_public_ip": network.get("assignPublicIp"),
            }
    raise ValueError("CloudTrail RunTask launch event not yet delivered")


def snapshot(session, state, task_arn):
    b = state["service-b"]
    ecs = session.client("ecs")
    result = ecs.describe_tasks(cluster=b["WorkerClusterName"], tasks=[task_arn])
    if len(result.get("tasks", [])) != 1:
        raise ValueError("task not found in controlled cluster")
    task = result["tasks"][0]
    if task["lastStatus"] != "STOPPED":
        raise ValueError("task not stopped; readback is not final")
    if task["taskDefinitionArn"] not in (b["WorkerTaskArn"], b["ExecutorTaskArn"]):
        raise ValueError("task definition mismatch")
    name = (
        "workertask"
        if task["taskDefinitionArn"] == b["WorkerTaskArn"]
        else "executortask"
    )
    stream = name + "/p2-controlled/" + task_arn.rsplit("/", 1)[1]
    log_client = session.client("logs")
    events = []
    token = None
    while True:
        kwargs = {
            "logGroupName": b["TaskLogGroupName"],
            "logStreamName": stream,
            "startFromHead": True,
        }
        if token:
            kwargs["nextToken"] = token
        page = log_client.get_log_events(**kwargs)
        events.extend(page["events"])
        if len(events) > 100:
            raise ValueError("task log exceeds P2 evidence bound")
        if page["nextForwardToken"] == token:
            break
        token = page["nextForwardToken"]
    probe_result = None
    probe_log_timestamp = None
    for event in events:
        try:
            parsed = json.loads(event["message"])
        except ValueError:
            continue
        if isinstance(parsed, dict) and "all_blocked" in parsed:
            probe_result = parsed
            probe_log_timestamp = event["timestamp"]
    return {
        "task_arn": task_arn,
        "task_definition_arn": task["taskDefinitionArn"],
        "task_role_arn": ecs.describe_task_definition(
            taskDefinition=task["taskDefinitionArn"]
        )["taskDefinition"]["taskRoleArn"],
        "launch": launch_readback(session, task_arn, task["createdAt"]),
        "last_status": task["lastStatus"],
        "stop_code": task.get("stopCode"),
        "stopped_reason": task.get("stoppedReason"),
        "containers": [
            {"name": item["name"], "exit_code": item.get("exitCode")}
            for item in task.get("containers", [])
        ],
        "log_stream": stream,
        "events": [
            {"timestamp": item["timestamp"], "message": item["message"]}
            for item in events
        ],
        "probe_result": probe_result,
        "probe_log_timestamp": probe_log_timestamp,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--task-arn", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("evidence output already exists")
    config = parse_config(args.config, require_active=False)
    session = client_session(config, "b", role="auditor")
    state = json.loads(args.state.read_text())
    record = snapshot(session, state, args.task_arn)
    raw = json.dumps(record, indent=2, sort_keys=True).encode() + b"\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(raw)
    args.output.with_suffix(args.output.suffix + ".sha256").write_text(
        hashlib.sha256(raw).hexdigest() + "\n"
    )
    print(args.output)


if __name__ == "__main__":
    main()
