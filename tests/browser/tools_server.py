"""Offline model plans over real Persona/Host/SQLite/HTTP boundaries."""

import argparse
import json
import sys
import threading
from dataclasses import replace
from pathlib import Path

from server import BrowserModel

from agent_alfred.messages import TextBlock, ToolCallBlock, ToolResultBlock
from agent_alfred.model import ModelResponse, ScriptedModel, ScriptedModelFactory
from agent_alfred.settings import Settings
from agent_alfred.tools import Tool, ToolSuccess
from agent_alfred.tools.calendar import object_schema
from agent_alfred.wiring import build_dashboard


class PersonaModel(BrowserModel):
    def respond(self, request, *, events=None, deadline=None):
        last = request.messages[-1].blocks
        prompt = next((b.text for b in last if isinstance(b, TextBlock)), "")
        if not request.tools:
            return super().respond(request, events=events, deadline=deadline)
        result = ScriptedModel(["离线回复"]).respond(request, deadline=deadline)
        current = "迁移验证人格" in "\n".join(b.text for b in request.system or ())
        blocks = (TextBlock(f"本 Run 使用更新人格：{str(current).lower()}"),)
        if prompt == "人格：读取":
            blocks = (ToolCallBlock("persona-read", "read_persona", {}),)
        elif prompt == "人格：更新":
            blocks = (ToolCallBlock("persona-read-update", "read_persona", {}),)
        elif prompt == "人格：冲突":
            blocks = (
                ToolCallBlock(
                    "persona-conflict",
                    "update_persona",
                    {
                        "content": "不得保存的冲突值",
                        "expected_version": "obsolete-version",
                    },
                ),
            )
        elif last and isinstance(last[0], ToolResultBlock):
            text = "\n".join(
                b.text for b in last[0].content if isinstance(b, TextBlock)
            )
            if last[0].call_id == "persona-read-update" and not last[0].is_error:
                version = json.loads(text)["version"]
                blocks = (
                    ToolCallBlock(
                        "persona-update",
                        "update_persona",
                        {
                            "content": "迁移验证人格；保留原有约束。",
                            "expected_version": version,
                        },
                    ),
                )
            else:
                blocks = (
                    TextBlock(text + f"\n本 Run 使用更新人格：{str(current).lower()}"),
                )
        response = ModelResponse(
            blocks,
            "tool_use" if isinstance(blocks[0], ToolCallBlock) else "end_turn",
            request.model,
        )
        return self.committed(replace(result, response=response), request.model, events)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--threshold")
    args = parser.parse_args()
    override = args.state / "external-persona.md"
    settings = Settings(
        persona=override.read_text()
        if override.exists()
        else "初始人格；保留原有约束。",
        persona_file=str(override) if override.exists() else None,
    )
    long_tool = Tool(
        "read_persona_looks_builtin",
        "长描述前段。"
        + "这是必须可以完整阅读的中文说明，不能只剩三行摘要。" * 80
        + "完整描述结束标识。",
        object_schema({}),
        lambda args, context: ToolSuccess((TextBlock("fixture only"),)),
        "local_read",
        source_id="unregistered/source/" + "长来源" * 40,
        capability_id="capability/" + "同名前缀" * 40,
    )
    dashboard = build_dashboard(
        state_dir=args.state,
        port=args.port,
        settings=settings,
        factory=ScriptedModelFactory(PersonaModel([])),
        extra_tools=(long_tool,),
    )
    dashboard.start()
    settled, completed = set(), threading.Condition()
    notify = dashboard.host.notify_run_done

    def notified(run_id):
        notify(run_id)
        with completed:
            settled.add(run_id)
            completed.notify_all()

    dashboard.host.notify_run_done = notified
    print("ready", flush=True)
    try:
        for line in sys.stdin:
            command = line.strip()
            if command == "stop":
                break
            if command.startswith("wait-settled "):
                with completed:
                    assert completed.wait_for(
                        lambda: command.removeprefix("wait-settled ") in settled, 5
                    ), "Run did not settle"
            else:
                raise ValueError("Unknown fixture command")
            print("ok " + command, flush=True)
    finally:
        assert dashboard.close(), "Dashboard did not drain"


if __name__ == "__main__":
    main()
