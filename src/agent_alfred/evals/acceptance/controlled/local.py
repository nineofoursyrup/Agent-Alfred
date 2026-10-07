"""Explicit local inspection and owner decisions; never an implicit model run."""

import argparse
import json
import re
from pathlib import Path

from agent_alfred.resource_rollback import ResumableRollback, raise_if_rollback_pending


def inspect(config_path=None):
    """Report missing installation evidence without opening an execution owner."""
    if config_path is None:
        return {
            "contract": "V1-LOCAL-PREFLIGHT",
            "version": 1,
            "trust_profile": "owner_trusted_local",
            "status": "BLOCKED",
            "blockers": ["local_installation_required"],
            "run_grant": False,
            "real_requests": 0,
        }
    from .local_installation import inspect_local_installation

    return inspect_local_installation(config_path)


def owner_decision(args):
    """Use the pinned native owner interface, never CLI-supplied approval facts."""
    from ..materials import strict_json
    from .local_runtime import install_local_runtime

    if args.config is None or not args.reason:
        raise ValueError("local_owner_decision_arguments_required")
    if args.command == "owner-decision":
        if args.request is None or args.decision is None or not args.evidence:
            raise ValueError("local_owner_decision_arguments_required")
        with args.request.open("rb") as stream:
            request = strict_json(stream.read(65537), limit=65536)
    elif not args.source_ref:
        raise ValueError("local_owner_decision_arguments_required")
    rollback = ResumableRollback()
    try:
        runtime = install_local_runtime(args.config, _rollback=rollback)
        if args.command == "revoke":
            record = runtime.decision_control.revoke(
                args.source_ref, reason=args.reason
            )
        else:
            carry = (
                {
                    "original_record": args.carry_forward_record,
                    "original_event_verified": False,
                }
                if args.carry_forward_record
                else None
            )
            record = runtime.decision_control.record(
                request,
                decision=args.decision,
                reason=args.reason,
                evidence=args.evidence,
                carry_forward=carry,
            )
        report = {
            "status": "RECORDED",
            "source_ref": record["source_ref"],
            "execution_started": False,
            "real_requests": 0,
        }
    except BaseException as failure:
        rollback.raise_failure(failure)
    rollback.close()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["inspect", "owner-decision", "revoke"])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument(
        "--decision", choices=["approved", "rejected", "confirmed", "dismissed"]
    )
    parser.add_argument("--reason")
    parser.add_argument("--evidence", action="append", default=[])
    parser.add_argument("--source-ref")
    parser.add_argument("--carry-forward-record")
    args = parser.parse_args(argv)
    try:
        report = (
            inspect(args.config) if args.command == "inspect" else owner_decision(args)
        )
    except Exception as error:
        raise_if_rollback_pending(error)
        reason = str(error) if type(error) is ValueError else "local_preflight_failed"
        if re.fullmatch(r"[a-z_]{1,100}", reason) is None:
            reason = "local_preflight_failed"
        report = {
            "contract": "V1-LOCAL-PREFLIGHT",
            "version": 1,
            "trust_profile": "owner_trusted_local",
            "status": "BLOCKED",
            "blockers": [reason],
            "run_grant": False,
            "real_requests": 0,
        }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report.get("status") == "RECORDED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
