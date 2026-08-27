"""Command-line Gateway: REPL and one-shot chat against RuntimeHost."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import TextIO

from agent_alfred.loop.assistant import LoopResult
from agent_alfred.messages import message_plain_text
from agent_alfred.runtime.host import RuntimeHost, SubmitRequest
from agent_alfred.settings import MAX_STEPS_REACHED_TEXT


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-alfred")
    parser.add_argument(
        "-m",
        "--message",
        help="Send one message and exit (no REPL)",
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help="Override AGENT_ALFRED_HOME (must be absolute if set via env)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    from dotenv import load_dotenv

    load_dotenv()
    from pathlib import Path

    from agent_alfred.wiring import build_default_host

    state_dir = Path(args.state_dir) if args.state_dir else None
    host = build_default_host(state_dir=state_dir)
    host.start()
    try:
        session_id = host.create_session()
        if args.message is not None:
            return _one_shot(host, args.message, session_id)
        return _repl(host, session_id)
    finally:
        host.close()


def _one_shot(host: RuntimeHost, message: str, session_id: str) -> int:
    return _send(host, message, session_id, sys.stdout)


def _repl(host: RuntimeHost, session_id: str) -> int:
    from rich.console import Console

    console = Console()
    console.print("[bold]Agent-Alfred[/bold]  (Ctrl-D to exit)")
    while True:
        try:
            line = input("> ")
        except EOFError:
            console.print()
            return 0
        except KeyboardInterrupt:
            console.print()
            continue
        if not line.strip():
            continue
        _send(host, line, session_id, sys.stdout)
    return 0


def _send(
    host: RuntimeHost, message: str, session_id: str, out: TextIO
) -> int:
    submitted = host.submit(
        SubmitRequest(message=message, session_id=session_id, gateway="cli")
    )
    if submitted.kind != "accepted" or submitted.run_id is None:
        _print_submit_failure(submitted.kind, out)
        return 1
    result = host.wait(submitted.run_id)
    _print_result(result, out)
    return 0 if result.outcome == "completed" else 1


def _print_submit_failure(kind: str, out: TextIO) -> None:
    if kind == "run_in_progress":
        out.write("Busy: a Run is already in progress.\n")
    elif kind == "recording_unavailable":
        out.write("Recording unavailable; refusing new Runs.\n")
    else:
        out.write(f"Admission failed ({kind}).\n")


def _print_result(result: LoopResult, out: TextIO) -> None:
    if result.reply is not None:
        out.write(message_plain_text(result.reply) + "\n")
        return
    if result.outcome == "max_steps":
        out.write(MAX_STEPS_REACHED_TEXT + "\n")
        return
    error = result.error or result.outcome
    out.write(f"{result.outcome}: {error}\n")


def run_injected(
    host: RuntimeHost,
    message: str,
    *,
    session_id: str | None = None,
    out: TextIO | None = None,
) -> int:
    """Test seam: drive the CLI send path against an injected host."""
    stream = out if out is not None else sys.stdout
    if session_id is None:
        session_id = host.create_session()
    host.start()
    try:
        return _send(host, message, session_id, stream)
    finally:
        host.close()
