"""Offline-by-default public evidence workflow; no implicit external requests."""

import argparse
import json
import re
from pathlib import Path

from .report import axis
from .safety import ensure_safe
from .schema import validate
from .store import EvidenceStore


def read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    ensure_safe(value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "prepare",
            "proposal",
            "regrade",
            "recover",
            "validate",
            "dry-run",
            "execute",
            "judge",
            "import",
            "import-grades",
            "import-reviews",
            "adjudicate-reviews",
            "adjudicate",
            "collect",
            "report",
            "verify",
            "delete",
        ],
    )
    parser.add_argument("--store")
    parser.add_argument("--batch")
    parser.add_argument("--new-batch")
    parser.add_argument("--input")
    parser.add_argument("--output")
    parser.add_argument("--workspace")
    parser.add_argument("--candidate-root")
    parser.add_argument("--output-scope")
    parser.add_argument("--operations", nargs="+", choices=["product", "judge"])
    parser.add_argument("--operation", choices=["product", "judge"])
    parser.add_argument(
        "--gate",
        choices=[
            "sync",
            "ruff",
            "skills",
            "env",
            "pytest",
            "build",
            "installations",
            "typecheck",
            "browser",
        ],
    )
    args = parser.parse_args()
    try:
        result = dispatch(args)
        ensure_safe(result)
    except Exception:
        import sys

        error = sys.exception()
        reason = str(error) if type(error) is ValueError else "invalid_input"
        if not re.fullmatch("[a-z_]{1,80}", reason):
            reason = "invalid_input"
        try:
            ensure_safe(reason)
        except ValueError:
            reason = "operation_failed"
        result = {
            "offline_engineering": axis(blockers=[reason]),
            "v1_release": axis(blockers=[reason]),
        }
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output:
        with Path(args.output).open("x", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
    print(encoded)
    return (
        2 if result.get("v1_release", {}).get("verdict") in ("BLOCKED", "FAIL") else 0
    )


def dispatch(args):
    if args.command == "proposal":
        from .admission import proposal

        if not args.output_scope or not args.operations:
            raise ValueError("proposal_scope_required")
        return proposal(validate(read_json(args.input)), output_scope=args.output_scope,
                        operations=args.operations)
    if args.command == "prepare":
        from .candidate import capture
        from .examples import offline_batch

        batch = offline_batch(capture(args.candidate_root))
        from .judge_protocol import current_profile

        batch["judge_profile"] = current_profile(batch["profiles"][0]["judge_model"])
        if args.batch:
            batch["batch_id"] = args.batch
        return batch
    if args.command == "validate":
        batch = validate(read_json(args.input))
        from .authorization_history import assess

        return {"valid": True, "validation_scope": "schema_only",
                "batch_id": batch["batch_id"], "authorization": assess(batch),
                "online_executable": False}
    store = EvidenceStore(args.store)
    if args.command == "delete":
        store.delete(args.batch)
        return {"batch_id": args.batch, "deleted": True}
    if args.command == "recover":
        batch = store.recover(args.batch, args.operation, new_batch=args.new_batch)
    elif args.command == "regrade":
        batch = store.revise(
            args.batch, args.new_batch, configuration=read_json(args.input)
        )
    elif args.command in ("dry-run", "execute"):
        from .runner import run_offline

        batch = read_json(args.input)
        if args.command == "execute":
            # Explicit execute is the only path that reads model credentials.
            from .admission import require_source

            require_source(batch)
            # This CLI has no synthetic authority/MockTransport attachment.
            raise ValueError("simulation_session_and_mock_required")
        else:
            batch = run_offline(batch, args.workspace)
        store.import_batch(batch)
    elif args.command == "import":
        batch = read_json(args.input)
        store.import_batch(batch)
    elif args.command in (
        "import-grades", "adjudicate", "import-reviews", "adjudicate-reviews"
    ):
        imported = read_json(args.input)
        batch = store.revise(
            args.batch,
            args.new_batch,
            **{
                {
                    "import-grades": "grades",
                    "adjudicate": "adjudications",
                    "import-reviews": "reviews",
                    "adjudicate-reviews": "review_adjudications",
                }[args.command]: imported,
            },
        )
    elif args.command == "collect":
        from .collect import collect_gate

        batch = store.read(args.batch)
        gate = collect_gate(
            args.gate, batch["candidate"], args.candidate_root, args.workspace
        )
        batch = store.revise(args.batch, args.new_batch, gates=[gate])
    elif args.command == "judge":
        from .online_judge import grade_batch

        original = store.read(args.batch)
        authorization = read_json(args.input)
        batch = grade_batch(
            original,
            authorization,
            candidate_root=args.candidate_root,
            store=store,
            new_batch=args.new_batch,
        )
        batch = store.revise(
            args.batch, args.new_batch, grades=batch["grades"], execution=batch
        )
    else:
        batch = store.read(args.batch)
    return store.publish_report(batch["batch_id"], candidate_root=args.candidate_root)


if __name__ == "__main__":
    raise SystemExit(main())
