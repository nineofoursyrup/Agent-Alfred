"""Control recording at external sink/database seams over a test-only stdin."""

import argparse
import sqlite3
import sys
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

from server import BrowserModel

from agent_alfred.events import BarrierFlushResult
from agent_alfred.model import ScriptedModelFactory
from agent_alfred.wiring import build_dashboard, open_database


class BarrierSink:
    name = "browser-recording-barrier"
    flush_at_run_end = True

    def __init__(self):
        self.release = threading.Event()

    def prepare(self, event):
        return None

    def commit(self, prepared, event):
        return None

    def flush(self, run_id):
        print("pending", flush=True)
        self.release.wait()
        return BarrierFlushResult(outcome="flushed", dropped_events=0)

    def close(self):
        self.release.set()


class FailingDatabase:
    def __init__(self, inner, failure):
        self.inner = inner
        self.failure = failure

    def execute(self, sql, parameters=()):
        if self.failure.is_set() and "INSERT INTO agent_log" in sql:
            raise sqlite3.OperationalError("test recording failure")
        return self.inner.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    failure = threading.Event()
    barrier = BarrierSink()

    def database(state, *, _rollback):
        conn = open_database(state, _rollback=_rollback)
        wrapper = FailingDatabase(conn, failure)
        _rollback.own(wrapper)
        _rollback.transfer(conn)
        return wrapper

    with TemporaryDirectory(prefix="alfred-recording-browser-") as state:
        dashboard = build_dashboard(
            state_dir=Path(state), port=args.port,
            factory=ScriptedModelFactory(BrowserModel([])),
            extra_sinks=[barrier], open_database=database,
        )
        try:
            dashboard.start()
            print("ready", flush=True)
            for command in sys.stdin:
                if command.strip() == "stop":
                    break
                if command.strip() == "fail":
                    failure.set()
                barrier.release.set()
        finally:
            barrier.release.set()
            if not dashboard.close():
                raise RuntimeError("Dashboard did not finish closing")


def cli_flow(port):
    """Exercise the real CLI entry with its existing offline factory seam."""
    from agent_alfred.gateway.cli import main as cli_main

    class PausedModel(BrowserModel):
        def respond(self, request, *, events=None, deadline=None):
            print("model-entered", flush=True)
            # --message leaves stdin to this test's model pause. Read only
            # after startup succeeds, so failure has no reader to cancel/join.
            sys.stdin.readline()
            return super().respond(request, events=events, deadline=deadline)

    with TemporaryDirectory(prefix="alfred-cli-browser-") as state:
        result = cli_main(
            ["--state-dir", state, "--port", str(port),
             "--message", "CLI 闩锁运行"],
            factory=ScriptedModelFactory(PausedModel([])),
        )
        if result != 0:
            raise RuntimeError(f"CLI returned {result}")


def stream_flow(port):
    """A real streaming ModelClient paused by the existing stdin control."""
    from agent_alfred.events import (
        AttemptCommitted,
        AttemptStarted,
        BlockDelta,
        BlockStarted,
        BlockStopped,
    )
    from agent_alfred.model import AttemptRecord, ModelResult, ScriptedModel

    release = threading.Event()

    class StreamingModel(BrowserModel):
        def respond(self, request, *, events=None, deadline=None):
            result = ScriptedModel(["流式流程的唯一正式回复"]).respond(
                request, deadline=deadline,
            )
            attempt = result.attempts[0]
            if events is not None:
                events.emit(AttemptStarted(
                    attempt_id=attempt.attempt_id, model=request.model,
                    streamed=True,
                ))
                events.emit(BlockStarted(attempt_id=attempt.attempt_id))
                events.emit(BlockDelta(
                    attempt_id=attempt.attempt_id, text="正在流入的临时片段",
                ))
            print("delta-emitted", flush=True)
            release.wait()
            if events is not None:
                events.emit(BlockStopped(attempt_id=attempt.attempt_id))
                events.emit(AttemptCommitted(
                    attempt_id=attempt.attempt_id, blocks=result.response.blocks,
                    usage=attempt.usage,
                ))
            return ModelResult(
                attempts=(AttemptRecord(attempt.attempt_id, True, "committed",
                                        attempt.usage),),
                response=result.response, final_error=None,
            )

    with TemporaryDirectory(prefix="alfred-stream-browser-") as state:
        dashboard = build_dashboard(
            state_dir=Path(state), port=port,
            factory=ScriptedModelFactory(StreamingModel([])),
        )
        try:
            dashboard.start()
            print("ready", flush=True)
            for command in sys.stdin:
                if command.strip() == "stop":
                    break
                release.set()
        finally:
            release.set()
            if not dashboard.close():
                raise RuntimeError("Dashboard did not finish closing")


if __name__ == "__main__":
    main()
