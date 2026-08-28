"""Command-line Gateway: REPL and one-shot chat against RuntimeHost.

Configuration precedence per knob (highest first): CLI flag >
``AGENT_ALFRED_*`` environment variable (``.env`` is loaded) > built-in
default. Parsing is fail-fast: an illegal value exits with a named error
instead of silently falling back. The persona comes from ``--persona-file``
or ``AGENT_ALFRED_PERSONA_FILE`` (UTF-8, non-empty), falling back to the
built-in default persona; the system prompt is composed from the persona and
the current local time at each request and never enters the session record or
the run transcript.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import TextIO

from agent_alfred.loop.assistant import LoopResult
from agent_alfred.messages import message_plain_text
from agent_alfred.render import ReplyRenderer, render_markdown_reply
from agent_alfred.runtime.host import RuntimeHost, SubmitRequest
from agent_alfred.settings import (
    ENV_MAX_STEPS,
    ENV_MAX_TOKENS,
    ENV_OVERALL_DEADLINE_S,
    ENV_PER_ATTEMPT_TIMEOUT_S,
    ENV_PERSONA_FILE,
    ENV_STREAM,
    ENV_STREAM_FALLBACK,
    ENV_WORKING_MEMORY_ROUNDS,
    MAX_STEPS_REACHED_TEXT,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-alfred",
        description=(
            "Local-first single-user AI assistant. Each knob resolves as: "
            "CLI flag > AGENT_ALFRED_* environment variable > built-in "
            "default. Illegal values fail fast; nothing falls back silently."
        ),
    )
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
    parser.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help=(
            "Max Steps per Run (one Step = one model response + its tool "
            f"batch). Overrides {ENV_MAX_STEPS}. 0 is legal and means the "
            "run stops before any model request."
        ),
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help=(
            "Max output tokens per model request. Overrides "
            f"{ENV_MAX_TOKENS}. Omit for no limit."
        ),
    )
    parser.add_argument(
        "--overall-deadline",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Wall-clock budget covering all retries and fallbacks of one "
            f"Run. Overrides {ENV_OVERALL_DEADLINE_S}."
        ),
    )
    parser.add_argument(
        "--per-attempt-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Timeout for one real network round-trip; the adapter receives "
            f"min(remaining overall, this). Overrides "
            f"{ENV_PER_ATTEMPT_TIMEOUT_S}."
        ),
    )
    stream_group = parser.add_mutually_exclusive_group()
    stream_group.add_argument(
        "--stream",
        dest="stream",
        action="store_true",
        default=None,
        help=(
            "Attempt streaming first for model responses. Overrides "
            f"{ENV_STREAM}."
        ),
    )
    stream_group.add_argument(
        "--no-stream",
        dest="stream",
        action="store_false",
        default=None,
        help="Never stream; non-streaming requests only.",
    )
    fallback_group = parser.add_mutually_exclusive_group()
    fallback_group.add_argument(
        "--stream-fallback",
        dest="stream_fallback",
        action="store_true",
        default=None,
        help=(
            "Allow a failed streaming attempt to fall back to a "
            f"non-streaming attempt. Overrides {ENV_STREAM_FALLBACK}."
        ),
    )
    fallback_group.add_argument(
        "--no-stream-fallback",
        dest="stream_fallback",
        action="store_false",
        default=None,
        help="A failed streaming attempt fails the Step; no fallback.",
    )
    parser.add_argument(
        "--working-memory-rounds",
        type=int,
        default=None,
        help=(
            "Recent user/assistant rounds reused from the session record as "
            f"working memory. Overrides {ENV_WORKING_MEMORY_ROUNDS}."
        ),
    )
    parser.add_argument(
        "--persona-file",
        default=None,
        help=(
            "Path to a UTF-8 persona file; its content replaces the built-in "
            f"default persona. Overrides {ENV_PERSONA_FILE}. Priority: this "
            "flag > the environment variable > the built-in default persona. "
            "The system prompt (persona + current local time) is composed per "
            "request and is never written to the session record or the run "
            "transcript."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    import os

    from dotenv import load_dotenv

    from agent_alfred.settings import SettingsError, load_settings

    # .env feeds the environment before anything reads it, flags still win.
    load_dotenv()
    try:
        settings = load_settings(
            os.environ,
            max_steps=args.max_steps,
            max_tokens=args.max_tokens,
            overall_deadline_s=args.overall_deadline,
            per_attempt_timeout_s=args.per_attempt_timeout,
            stream=args.stream,
            stream_fallback=args.stream_fallback,
            working_memory_rounds=args.working_memory_rounds,
            persona_file=args.persona_file,
        )
    except SettingsError as exc:
        parser.error(str(exc))  # exits with status 2
        raise AssertionError("unreachable") from None

    from pathlib import Path

    from agent_alfred.wiring import build_default_host

    state_dir = Path(args.state_dir) if args.state_dir else None
    host = build_default_host(state_dir=state_dir, settings=settings)
    host.start()
    try:
        session_id = host.create_session()
        if args.message is not None:
            return _one_shot(host, args.message, session_id, stream=settings.stream)
        return _repl(host, session_id, stream=settings.stream)
    finally:
        host.close()


def _one_shot(
    host: RuntimeHost, message: str, session_id: str, *, stream: bool
) -> int:
    return _send(host, message, session_id, sys.stdout, stream=stream)


def _repl(host: RuntimeHost, session_id: str, *, stream: bool) -> int:
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
        _send(host, line, session_id, sys.stdout, stream=stream)
    return 0


def _send(
    host: RuntimeHost,
    message: str,
    session_id: str,
    out: TextIO,
    *,
    renderer: ReplyRenderer | None = None,
    stream: bool = False,
) -> int:
    submitted = host.submit(
        SubmitRequest(
            message=message,
            session_id=session_id,
            gateway="cli",
            stream=stream,
        )
    )
    if submitted.kind != "accepted" or submitted.run_id is None:
        _print_submit_failure(submitted.kind, out)
        return 1
    result = host.wait(submitted.run_id)
    _print_result(result, out, renderer=renderer or render_markdown_reply)
    return 0 if result.outcome == "completed" else 1


def _print_submit_failure(kind: str, out: TextIO) -> None:
    if kind == "run_in_progress":
        out.write("Busy: a Run is already in progress.\n")
    elif kind == "recording_unavailable":
        out.write("Recording unavailable; refusing new Runs.\n")
    else:
        out.write(f"Admission failed ({kind}).\n")


def _print_result(
    result: LoopResult, out: TextIO, *, renderer: ReplyRenderer
) -> None:
    if result.reply is not None:
        renderer(message_plain_text(result.reply), out)
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
    renderer: ReplyRenderer | None = None,
) -> int:
    """Test seam: drive the CLI send path against an injected host."""
    stream = out if out is not None else sys.stdout
    if session_id is None:
        session_id = host.create_session()
    host.start()
    try:
        return _send(host, message, session_id, stream, renderer=renderer)
    finally:
        host.close()
