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
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

from agent_alfred.gateway.web.api import DashboardApi
from agent_alfred.gateway.web.lifecycle import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    EntryDescriptor,
)
from agent_alfred.loop.assistant import LoopResult
from agent_alfred.messages import message_plain_text
from agent_alfred.model import ModelClientFactory
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
    Settings,
)

# Every Dashboard component already gives one close attempt a bounded wait:
# RuntimeHost uses its worker-join bound and SSEBroker uses its drain bound.
# The CLI must nevertheless ask again when that honest attempt says progress
# is incomplete. Three attempts let the resumable state machine advance
# without turning a permanently refusing component into an unbounded loop.
_CLOSE_PROGRESS_ATTEMPTS = 3


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
    parser.add_argument(
        "--serve",
        action="store_true",
        help=(
            "Run the local Dashboard instead of the REPL. It binds "
            "127.0.0.1 and never picks another port, so a busy port or a "
            "second instance fails loudly instead of moving."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"Dashboard port (default {DEFAULT_PORT}). Only this port is tried.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    factory: ModelClientFactory | None = None,
    build: Callable[..., Any] | None = None,
) -> int:
    """The one entry point. ``--serve`` serves; the default serves *and* chats.

    ``factory`` and ``build`` are seams: without them this function would
    build the production model client and the production Dashboard, neither
    of which a test can drive.
    """
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

    directory = Path(args.state_dir) if args.state_dir else None
    port = DEFAULT_PORT if args.port is None else args.port
    if args.serve:
        return serve_dashboard(
            state_dir=directory,
            settings=settings,
            port=port,
            out=sys.stdout,
            factory=factory,
            build=build,
        )
    runtime = _build_runtime(
        build=build,
        state_dir=directory,
        settings=settings,
        port=port,
        factory=factory,
    )
    return _chat_in_the_foreground(runtime, args, settings, out=sys.stdout)


def _build_runtime(
    *,
    build: Callable[..., Any] | None,
    state_dir: Path | None,
    settings: Settings,
    port: int,
    factory: ModelClientFactory | None,
) -> Any:
    from agent_alfred.settings import resolve_state_dir
    from agent_alfred.wiring import build_dashboard

    directory = (
        Path(state_dir) if state_dir is not None else resolve_state_dir()
    )
    assemble = build if build is not None else build_dashboard
    return assemble(
        state_dir=directory,
        settings=settings,
        factory=factory,
        port=port,
        trace_root=directory / "traces",
    )


def _start_or_report(runtime: Any, out: TextIO) -> int | None:
    """Start the Dashboard, or say why it did not come up.

    Both surfaces answer a refused start the same way: name the reason,
    advance its retryable rollback, and return a failure. A silent fallback
    to "chat without a Dashboard" would hide a held lock or a taken port --
    and those are precisely the two situations the user has to hear about.
    """
    try:
        runtime.start()
    except BaseException as exc:  # noqa: BLE001 - reported, then closed
        _close_runtime(runtime, out)
        out.write(f"dashboard unavailable: {exc}\n")
        out.flush()
        return 1
    return None


def _close_runtime(runtime: Any, out: TextIO) -> bool:
    """Advance resumable close progress without claiming endless success.

    One ``close()`` is already a bounded wait at the runtime boundary. False
    is not failure and not completion: it means the runtime still owns the
    resources needed by a worker or stream and the same owner must ask again.
    A fixed attempt budget avoids an unbounded shutdown loop; exhausting it
    leaves ownership untouched and gives the CLI an explicit failure status.
    """
    for _attempt in range(_CLOSE_PROGRESS_ATTEMPTS):
        if runtime.close():
            return True
    out.write(
        "dashboard shutdown incomplete after "
        f"{_CLOSE_PROGRESS_ATTEMPTS} attempts; runtime resources remain owned\n"
    )
    out.flush()
    return False


def _announce(descriptor: EntryDescriptor, out: TextIO) -> None:
    """Name the port, the instance and the pid -- the entry descriptor's
    whole content, spoken once, on whichever surface started it."""
    out.write(
        f"dashboard on {DEFAULT_HOST}:{descriptor.port} "
        f"(instance {descriptor.instance_id}, pid {descriptor.pid})\n"
    )
    out.flush()


def _chat_in_the_foreground(
    runtime: Any, args: Any, settings: Settings, *, out: TextIO
) -> int:
    """The default path: the Dashboard behind, the CLI in front.

    One process, one Host (#23 §1). The CLI's transient events never touch
    the database, so a separate process would have no way to show them to a
    browser; and two processes would split ``seq`` and
    ``process_instance_id``, which is what every SSE cursor is made of.

    The Dashboard is therefore started here, before the first prompt, and
    closed after the last one -- in reverse order, so the socket, the
    descriptor and the lock are all released before this function returns.
    """
    failure = _start_or_report(runtime, out)
    if failure is not None:
        return failure
    host = runtime.host
    _announce(runtime.descriptor, out)
    result = 1
    try:
        created = DashboardApi(facade=host).create_session()
        if created.session_id is None:
            _print_session_creation_failure(created.code, out)
        elif args.message is not None:
            result = _one_shot(
                host,
                args.message,
                created.session_id,
                out,
                stream=settings.stream,
            )
        else:
            result = _repl(host, created.session_id, stream=settings.stream)
    finally:
        close_complete = _close_runtime(runtime, out)
    return result if close_complete else 1


def serve_dashboard(
    *,
    state_dir: Path | None,
    settings: Settings,
    port: int | None = None,
    out: TextIO | None = None,
    stop: threading.Event | None = None,
    factory: ModelClientFactory | None = None,
    build: Callable[..., Any] | None = None,
) -> int:
    """Run the Dashboard and nothing else, until interrupted.

    The Host and the Dashboard share one process on purpose (ADR-0013): the
    replay ring is the only source for an active Run, and a second process
    would have to invent a second one. Refusing to start is therefore the
    right answer to a held lock or a busy port -- the alternative is a
    Dashboard that shows a different truth than the one being recorded.

    It goes through the same ordered start-up the CLI path uses, because
    there is only one correct order and it is not up to a caller to
    rediscover it.
    """
    stream = sys.stdout if out is None else out
    directory = Path(state_dir) if state_dir is not None else None
    runtime = _build_runtime(
        build=build,
        state_dir=directory,
        settings=settings,
        port=DEFAULT_PORT if port is None else port,
        factory=factory,
    )
    failure = _start_or_report(runtime, stream)
    if failure is not None:
        return failure
    _announce(runtime.descriptor, stream)
    try:
        waiter = stop if stop is not None else threading.Event()
        waiter.wait()
    except KeyboardInterrupt:
        pass
    finally:
        close_complete = _close_runtime(runtime, stream)
    return 0 if close_complete else 1


def _one_shot(
    host: RuntimeHost,
    message: str,
    session_id: str,
    out: TextIO,
    *,
    stream: bool,
) -> int:
    return _send(host, message, session_id, out, stream=stream)


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


def _print_session_creation_failure(code: str | None, out: TextIO) -> None:
    """Report an authoritative refusal without echoing exception detail."""
    if code in ("mutation_in_flight", "run_in_progress"):
        out.write(
            "Busy: another mutation or Run is in progress; "
            "Session was not created.\n"
        )
    elif code == "recording_unavailable":
        out.write("Recording unavailable; Session was not created.\n")
    elif code == "admission_failed":
        out.write("Admission failed; Session was not created.\n")
    else:
        out.write("Session unavailable; Session was not created.\n")
    out.flush()


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
