"""Independent B auditor readback of one actual Dispatch probe Lambda log."""

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .ctl import client_session, parse_config, required


def snapshot(session, config, state, invocation):
    function_arn = invocation["function_arn"]
    if function_arn not in {
        required(state, "service-b", "DispatchProbeFunctionArn"),
        required(state, "service-b", "DispatchIamProbeFunctionArn"),
        required(state, "service-b", "AuthorityProbeFunctionArn"),
        required(state, "service-b", "AuthorityIamProbeFunctionArn"),
    }:
        raise ValueError("Dispatch probe function outside controlled stack")
    request_id = invocation["request_id"]
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("Dispatch probe request ID missing")
    observed = datetime.fromisoformat(invocation["observed_at"])
    if observed.tzinfo is None:
        raise ValueError("Dispatch probe observation time untrusted")
    log_group = "/aws/lambda/" + function_arn.rsplit(":", 1)[1]
    logs = session.client("logs")
    matches = []
    inspected = 0
    for page in logs.get_paginator("filter_log_events").paginate(
        logGroupName=log_group,
        startTime=int((observed - timedelta(minutes=5)).timestamp() * 1000),
        endTime=int(
            (datetime.now(timezone.utc) + timedelta(minutes=1)).timestamp() * 1000
        ),
    ):
        for event in page.get("events", []):
            inspected += 1
            if inspected > 500:
                raise ValueError("Dispatch probe log search exceeds P2 bound")
            try:
                row = json.loads(event["message"])
            except ValueError:
                continue
            if (
                isinstance(row, dict)
                and row.get("p2_audit") == "lambda_negative_probe"
                and row.get("request_id") == request_id
            ):
                matches.append((event, row))
    if len(matches) != 1 or matches[0][1]["result"] != invocation["result"]:
        raise ValueError("Dispatch probe result not independently found in Lambda log")
    event, row = matches[0]
    return {
        "source": "B_AUDITOR_LAMBDA_LOG_READBACK",
        "account": config["accounts"]["b"],
        "region": config["region"],
        "auditor_role": config["auditor_role_arns"]["b"],
        "function_arn": function_arn,
        "request_id": request_id,
        "log_event_id": event["eventId"],
        "log_stream_name": event["logStreamName"],
        "log_timestamp": event["timestamp"],
        "result": row["result"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--invocation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("auditor evidence output already exists")
    config = parse_config(args.config, require_active=False)
    state = json.loads(args.state.read_text())
    invocation_raw = args.invocation.read_bytes()
    invocation = json.loads(invocation_raw)
    session = client_session(config, "b", role="auditor")
    record = snapshot(session, config, state, invocation)
    record["invocation_sha256"] = hashlib.sha256(invocation_raw).hexdigest()
    raw = json.dumps(record, indent=2, sort_keys=True).encode() + b"\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(raw)
    args.output.with_suffix(args.output.suffix + ".sha256").write_text(
        hashlib.sha256(raw).hexdigest() + "\n"
    )
    print(args.output)


if __name__ == "__main__":
    main()
