"""A real Dashboard for Memory page tests, with a stdin-controlled offline model.

Commands (one per line; each answers ``ok <command>``):

- ``plan create|update|invalid`` -- the next consolidation output;
- ``busy`` / ``idle`` -- hold or release the Host's own mutation gate;
- ``fail-commit`` / ``heal`` -- make consolidation's episode insert abort;
- ``mirror-fail on|off`` -- make managed mirror writes fail (not a conflict);
- ``overflow`` -- the broker's bounded ingress refuses its next frame;
- ``batch-read-race on|off`` -- overlap batch reads with a real memory write;
- ``stop`` -- close the Dashboard.
"""

import argparse
import json
import sys
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path

from server import BrowserModel

from agent_alfred.gateway.web.memory_api import MemoryApi
from agent_alfred.managed_state import ManagedDirectoryLease
from agent_alfred.memory.commands import CommandContext
from agent_alfred.memory.consolidation import CANDIDATE_PREFACE, CONSOLIDATION_SYSTEM
from agent_alfred.memory.types import ManualOrigin
from agent_alfred.messages import TextBlock, ToolCallBlock, ToolResultBlock
from agent_alfred.model import ModelResponse, ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.memory import GATE_SYSTEM
from agent_alfred.settings import Settings
from agent_alfred.wiring import build_dashboard

MIRROR_FAILURE = threading.Event()
_replace_bytes = ManagedDirectoryLease.replace_bytes


def _failing_replace_bytes(self, relative, payload, **kwargs):
    if MIRROR_FAILURE.is_set() and relative.name in ("facts.md", "episodes.md"):
        raise OSError("injected mirror write failure")
    return _replace_bytes(self, relative, payload, **kwargs)


ManagedDirectoryLease.replace_bytes = _failing_replace_bytes


class MemoryModel(BrowserModel):
    """Explicit saves become the real save_fact tool; plans follow stdin."""

    def __init__(self):
        super().__init__([])
        self.plan = "create"

    def respond(self, request, *, events=None, deadline=None):
        system = request.system[0].text if request.system else ""
        last = request.messages[-1].blocks if request.messages else ()
        text = next((b.text for b in last if isinstance(b, TextBlock)), "")

        def reply(block, stop_reason="end_turn"):
            result = ScriptedModel(["unused"]).respond(request)
            response = ModelResponse((block,), stop_reason, request.model)
            return self.committed(
                replace(result, response=response), request.model, events
            )

        if system == GATE_SYSTEM:
            # A valid model decision: retrieve with the user's own words.
            decision = {
                "retrieve": True,
                "query": text,
                "reason_code": "personal_information",
            }
            return reply(TextBlock(json.dumps(decision, ensure_ascii=False)))
        if system == CONSOLIDATION_SYSTEM:
            return reply(TextBlock(self._plan(request)))
        if request.tools and text.startswith("请记住 "):
            subject, _, fact = text.removeprefix("请记住 ").partition("：")
            call = ToolCallBlock(
                "memory-save", "save_fact", {"subject": subject, "fact": fact}
            )
            return reply(call, "tool_use")
        if last and isinstance(last[0], ToolResultBlock):
            if last[0].call_id == "memory-save":
                return reply(TextBlock("已记住。"))
        return super().respond(request, events=events, deadline=deadline)

    def _plan(self, request):
        if self.plan == "invalid":
            return "这不是 JSON"
        candidates = []
        for message in request.messages:
            for block in message.blocks:
                if isinstance(block, TextBlock) and block.text.startswith(
                    CANDIDATE_PREFACE
                ):
                    candidates = json.loads(block.text.removeprefix(CANDIDATE_PREFACE))
        protected = [item for item in candidates if item["human_protected"]]
        semantic = (
            [
                {
                    "action": "update",
                    "id": protected[0]["id"],
                    "subject": protected[0]["subject"],
                    "fact": protected[0]["fact"] + "（提炼改写）",
                }
            ]
            if self.plan == "update" and protected
            else [{"action": "create", "subject": "提炼", "fact": "浏览器提炼事实"}]
        )
        return json.dumps(
            {"semantic": semantic, "episode_summary": "浏览器提炼摘要"},
            ensure_ascii=False,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--threshold", type=int, default=10)
    args = parser.parse_args()
    builtin = args.state / "test-builtin"
    path = builtin / "browser-plan" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nname: browser-plan\ndescription: 内置计划\n---\n内置正文")
    model = MemoryModel()
    dashboard = build_dashboard(
        skill_builtin=builtin,
        state_dir=args.state,
        port=args.port,
        settings=Settings(consolidation_source_threshold=args.threshold),
        factory=ScriptedModelFactory(model),
    )
    try:
        dashboard.start()
        host = dashboard.host
        original_read = MemoryApi._read
        read_race_lock = threading.Lock()
        print("ready", flush=True)
        for line in sys.stdin:
            command = line.strip()
            if command == "stop":
                break
            if command.startswith("plan "):
                model.plan = command.removeprefix("plan ")
            elif command == "busy":
                assert host.try_begin_mutation() is None
            elif command == "idle":
                host.end_mutation()
            elif command == "batch-read-race on":

                def overlapping_write(api, params, **kwargs):
                    # The native revision bracket must produce the 409; do
                    # not replace the HTTP response with a fabricated error.
                    response = original_read(api, params, **kwargs)
                    if "batch_id" in params:
                        with read_race_lock:
                            deadline = time.monotonic() + 3
                            while (
                                host.snapshot().coordinator_state != "idle"
                                or host.mutation_in_flight()
                            ):
                                assert time.monotonic() < deadline, "Run did not settle"
                                time.sleep(0.005)
                            identity = uuid.uuid4().hex
                            receipt = api.memory.execute(
                                {
                                    "operation_id": identity,
                                    "kind": "semantic",
                                    "action": "save",
                                    "payload": {
                                        "subject": "read-race fixture",
                                        "fact": identity,
                                    },
                                },
                                CommandContext(ManualOrigin("cli"), "cli"),
                            )
                            assert "memory_id" in receipt, receipt
                    return response

                MemoryApi._read = overlapping_write
            elif command == "batch-read-race off":
                MemoryApi._read = original_read
            elif command in ("fail-commit", "heal"):
                with host._store.transaction() as conn:
                    conn.execute(
                        "CREATE TRIGGER browser_commit_failure AFTER INSERT ON "
                        "episodes BEGIN SELECT RAISE(ABORT, 'test-only'); END"
                        if command == "fail-commit"
                        else "DROP TRIGGER browser_commit_failure"
                    )
                    conn.commit()
            elif command.startswith("mirror-fail "):
                if command.endswith(" on"):
                    MIRROR_FAILURE.set()
                else:
                    MIRROR_FAILURE.clear()
            elif command == "overflow":
                ingress = dashboard.broker._ingress
                offer = ingress.offer

                def refuse(item, ingress=ingress, offer=offer):
                    ingress.offer = offer
                    return False

                ingress.offer = refuse
            print("ok " + command, flush=True)
    finally:
        if not dashboard.close():
            raise RuntimeError("Dashboard did not finish closing")


if __name__ == "__main__":
    main()
