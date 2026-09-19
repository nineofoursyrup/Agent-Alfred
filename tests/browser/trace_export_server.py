"""Real Host/tools/bundles; only the model and explicit fault controls are scripted."""

import argparse
import os
import select
import socket
import sys
import threading
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from server import BrowserModel

from agent_alfred.messages import TextBlock, ToolCallBlock, ToolResultBlock
from agent_alfred.model import ModelResponse, ScriptedModel, ScriptedModelFactory
from agent_alfred.tools import Tool, ToolSuccess
from agent_alfred.tools.calendar import object_schema
from agent_alfred.wiring import build_dashboard


class ExportModel(BrowserModel):
    def respond(self, request, *, events=None, deadline=None):
        last = request.messages[-1].blocks
        if request.tools:
            if any(isinstance(b, TextBlock) and b.text == "导出工具验收" for b in last):
                blocks = (ToolCallBlock("export-large-call", "export_large", {}),)
            elif any(
                isinstance(b, ToolResultBlock) and b.call_id == "export-large-call"
                for b in last
            ):
                blocks = (ToolCallBlock("export-inline-call", "query_events", {}),)
            else:
                return super().respond(request, events=events, deadline=deadline)
            result = ScriptedModel(["done"]).respond(request, deadline=deadline)
            result = replace(
                result, response=ModelResponse(blocks, "tool_use", request.model)
            )
            return self.committed(result, request.model, events)
        return super().respond(request, events=events, deadline=deadline)


class EnteredEvent(threading.Event):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()

    def wait(self, timeout=None):
        self.entered.set()
        return super().wait(timeout)


class IOControls:
    def __init__(self, dashboard, state):
        self.dashboard, self.state = dashboard, state
        self.read_gate, self.send_gate = EnteredEvent(), EnteredEvent()
        self.recording_gate = EnteredEvent()
        self.read_identity = None
        self.hold_send = False
        self.sent = 0
        self.original_read, self.original_send, self.original_select = (
            os.pread,
            socket.socket.send,
            select.select,
        )
        os.pread = self.read
        controls = self

        def send(sock, raw, *args, **kwargs):
            return controls.send(sock, raw, *args, **kwargs)

        socket.socket.send = send
        select.select = self.select

    def trace(self):
        return next((self.state / "traces").glob("*/*/trace.jsonl"))

    def read(self, fd, size, offset):
        raw = self.original_read(fd, size, offset)
        info = os.fstat(fd)
        if threading.current_thread().name == "trace-export" and self.read_identity == (
            info.st_dev,
            info.st_ino,
        ):
            self.read_identity = None
            assert self.read_gate.wait(20)
        return raw

    def send(self, sock, raw, *args, **kwargs):
        if self.hold_send and sock.getsockname()[1] == self.dashboard.port:
            if self.sent:
                if not self.send_gate.is_set():
                    raise BlockingIOError()
                self.hold_send = False
            else:
                self.sent = self.original_send(sock, raw[:64], *args, **kwargs)
                return self.sent
        return self.original_send(sock, raw, *args, **kwargs)

    def select(self, read, write, errors, timeout=None):
        if (
            self.hold_send
            and write
            and write[0].getsockname()[1] == self.dashboard.port
        ):
            assert self.send_gate.wait(20)
        return self.original_select(read, write, errors, timeout)

    def command(self, command):
        if command == "hold-read":
            self.read_gate.clear()
            self.read_gate.entered.clear()
            info = self.trace().stat()
            self.read_identity = (info.st_dev, info.st_ino)
        elif command == "await-read":
            assert self.read_gate.entered.wait(5)
        elif command == "release-read":
            self.read_gate.set()
        elif command == "hold-send":
            self.sent = 0
            self.hold_send = True
            self.send_gate.clear()
            self.send_gate.entered.clear()
        elif command == "await-send":
            assert self.send_gate.entered.wait(5)
        elif command == "release-send":
            self.send_gate.set()
        elif command == "missing-artifact":
            next((self.trace().parent / "artifacts").glob("tool-*.txt")).unlink()
        elif command == "replace-source":
            trace = self.trace()
            replacement = trace.parent / "replacement"
            replacement.write_bytes(trace.read_bytes())
            replacement.chmod(0o600)
            replacement.replace(trace)
        elif command == "recording-hold-fail":
            host = self.dashboard.host
            host._recorder._before_recording_commit = self.recording_gate
            with host._store.reading() as conn:
                conn.execute("""CREATE TRIGGER export_recording_failure
                    BEFORE UPDATE OF telemetry ON runs WHEN NEW.telemetry IS NOT NULL
                    BEGIN SELECT RAISE(FAIL, 'fixture recording failure'); END""")
                conn.commit()
        elif command == "await-recording":
            assert self.recording_gate.entered.wait(5)
        elif command == "release-recording":
            self.recording_gate.set()
        elif command == "unknown-recording":
            with self.dashboard.host._store.reading() as conn:
                conn.execute("UPDATE runs SET telemetry=NULL")
                conn.commit()

    def close(self):
        self.read_gate.set()
        self.send_gate.set()
        self.recording_gate.set()
        os.pread, socket.socket.send, select.select = (
            self.original_read,
            self.original_send,
            self.original_select,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--threshold")
    args = parser.parse_args()
    body = (
        "诊断正文🙂<script>not executed</script> newly-secret " + str(args.state) + "\n"
    ) * 6000
    tool = Tool(
        "export_large",
        "fixture",
        object_schema({}),
        lambda a, c: ToolSuccess((TextBlock(body),)),
        "local_read",
    )
    dashboard = build_dashboard(
        state_dir=args.state,
        port=args.port,
        factory=ScriptedModelFactory(ExportModel([])),
        extra_tools=(tool,),
    )
    controls = None
    try:
        dashboard.start()
        controls = IOControls(dashboard, args.state)
        print("ready", flush=True)
        for line in sys.stdin:
            command = line.strip()
            if command == "stop":
                break
            if command == "protect":
                dashboard.host._redactor.remember("newly-secret")
            controls.command(command)
            print("ok " + command, flush=True)
    finally:
        if controls is not None:
            controls.close()
        assert dashboard.close()


if __name__ == "__main__":
    main()
