"""Real Dashboard with a FIFO at worker extraction for lifecycle acceptance.

stdin arms the existing worker seam and reads actual process/owner state. It
never substitutes an HTTP response, SQL result, cancellation or deadline.
"""

import argparse
import json
import os
import select
import sqlite3
import sys
from pathlib import Path

from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.wiring import build_dashboard


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--threshold", type=int, default=10)
    args = parser.parse_args()
    dashboard = build_dashboard(
        state_dir=args.state,
        port=args.port,
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
    )
    dashboard.start()
    console = dashboard.host.database_console
    issue = console.issue
    real_killpg = os.killpg
    armed = False
    record = None
    pid = None
    hold = args.state / "worker-hold"
    ready = args.state / "worker-hold.ready"
    os.mkfifo(hold)
    os.mkfifo(ready)
    # The worker opens then closes its ready FIFO; select observes that real
    # handshake without a timer or synthetic HTTP completion.
    ready_fd = os.open(ready, os.O_RDONLY | os.O_NONBLOCK)

    def issuing(body):
        nonlocal armed, record
        response = issue(body)
        if armed:
            armed = False
            record = console._records[response[1]["query_id"]]
            record.barriers = {"extract": str(hold)}
        return response

    console.issue = issuing
    try:
        print("ready", flush=True)
        for line in sys.stdin:
            command = line.strip()
            if command == "stop":
                break
            if command == "arm":
                armed = True
            elif command == "kill-fail":
                def refuse_worker_stop(pgid, sig):
                    if record is not None and record.process is not None:
                        if pgid == record.process.pid:
                            raise OSError("injected worker stop failure")
                    return real_killpg(pgid, sig)

                os.killpg = refuse_worker_stop
            elif command == "heal":
                os.killpg = real_killpg
                console.invalidate_and_wait()
            elif command == "running":
                assert select.select([ready_fd], [], [], 3)[0], (
                    "worker did not reach FIFO"
                )
                assert record is not None and record.process is not None
                pid = record.process.pid
                assert record.process.poll() is None
                (args.state / "running.json").write_text(
                    json.dumps(
                        {
                            "query_id": record.query_id,
                            "pid": pid,
                            "status": record.status,
                            "cleanup": record.cleanup,
                        }
                    )
                )
            elif command == "released":
                with console._cv:
                    assert console._cv.wait_for(console._released_locked, 2), (
                        "owners retained"
                    )
                assert record is not None and record.status == "cancelled"
                assert record.cleanup == "released"
                assert record.process is None and record.owner is None
                assert record.response_owner is None and record.result is None
                assert not console._sends and not console._send_buffers
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    raise AssertionError("worker PID still alive")
                with sqlite3.connect(args.state / "db.sqlite3", timeout=0) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.rollback()
                (args.state / "released.json").write_text(
                    json.dumps(
                        {
                            "query_id": record.query_id,
                            "pid": pid,
                            "pid_exists": False,
                            "status": record.status,
                            "cleanup": record.cleanup,
                            "worker": None,
                            "response_owner": None,
                            "buffers": 0,
                            "source_write_lock": "acquired-and-released",
                        }
                    )
                )
            print("ok " + command, flush=True)
    finally:
        os.killpg = real_killpg
        os.close(ready_fd)
        assert dashboard.close()


if __name__ == "__main__":
    main()
