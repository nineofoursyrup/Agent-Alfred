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


if __name__ == "__main__":
    main()
