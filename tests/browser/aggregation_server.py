"""Real Dashboard; deterministic model stream and recording IO barriers only."""

import argparse
import sqlite3
import sys
import threading
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from agent_alfred.events import (
    AttemptAborted,
    AttemptCommitted,
    AttemptStarted,
    BlockDelta,
    BlockStarted,
)
from agent_alfred.messages import TextBlock
from agent_alfred.model import (
    ModelError,
    ModelResult,
    ScriptedModel,
    ScriptedModelFactory,
)
from agent_alfred.wiring import build_dashboard


class DraftModel:
    def __init__(self):
        self.release = threading.Event()
        self.release.set()
        self.invalid = False
        self.fail = False
        self.started = threading.Event()

    def respond(self, request, *, events=None, deadline=None):
        draft = "无效候选 [[S9]]" if self.invalid else "已验证草稿 [[S1]]"
        result = ScriptedModel([draft]).respond(request, deadline=deadline)
        attempt = result.attempts[0]
        if events:
            events.emit(
                AttemptStarted(attempt_id=attempt.attempt_id, model=request.model)
            )
            events.emit(BlockStarted(attempt_id=attempt.attempt_id))
            events.emit(
                BlockDelta(attempt_id=attempt.attempt_id, text="不得展示的候选流")
            )
        self.started.set()
        print("stream-started", flush=True)
        assert self.release.wait(10)
        if self.fail:
            error = ModelError(
                retryable=False,
                status_code=None,
                body_excerpt="stream disconnected",
                attempt_id=attempt.attempt_id,
                code="stream_disconnected",
            )
            if events:
                events.emit(
                    AttemptAborted(
                        attempt_id=attempt.attempt_id,
                        partial=True,
                        blocks=(TextBlock("不得展示的候选流"),),
                        usage=attempt.usage,
                        error=error,
                    )
                )
            return ModelResult(
                attempts=(
                    replace(attempt, streamed=True, outcome="aborted", error=error),
                ),
                response=None,
                final_error=error,
            )
        if events:
            events.emit(
                AttemptCommitted(
                    attempt_id=attempt.attempt_id,
                    blocks=result.response.blocks,
                    usage=attempt.usage,
                )
            )
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--threshold", type=int)
    args = parser.parse_args()
    model = DraftModel()
    record = threading.Event()
    record.set()
    dashboard = build_dashboard(
        state_dir=args.state,
        port=args.port,
        skill_builtin=args.state / "builtin",
        factory=ScriptedModelFactory(model),
        before_recording_commit=record,
    )
    try:
        dashboard.start()
        print("ready", flush=True)
        for line in sys.stdin:
            command = line.strip()
            if command == "stop":
                break
            if command == "hold-model":
                model.release.clear()
            if command == "wait-stream":
                assert model.started.wait(5)
            if command == "release-model":
                model.release.set()
            if command == "invalid-model":
                model.invalid = True
            if command == "fail-model":
                model.fail = True
            if command == "hold-recording":
                record.clear()
            if command == "release-recording":
                record.set()
            if command == "repair-recording":
                with sqlite3.connect(args.state / "db.sqlite3") as conn:
                    conn.execute("DROP TRIGGER fail_recording")
            if command == "fail-recording":
                with sqlite3.connect(args.state / "db.sqlite3") as conn:
                    conn.execute(
                        "CREATE TRIGGER fail_recording BEFORE UPDATE OF phase ON runs "
                        "WHEN NEW.phase='finished' BEGIN "
                        "SELECT RAISE(ABORT,'IO fault'); END"
                    )
            print("ok " + command, flush=True)
    finally:
        model.release.set()
        record.set()
        assert dashboard.close()


if __name__ == "__main__":
    main()
