"""Real temporary Host/SQLite/trace, only model transport is scripted."""

import sqlite3
from contextlib import contextmanager

from agent_alfred import schema
from agent_alfred.clock import FakeClock, SystemClock
from agent_alfred.events import FanOutSink
from agent_alfred.managed_state import ManagedStateDirectory
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.config import MutableAssignmentProvider
from agent_alfred.runtime.host import RuntimeHost, SubmitRequest
from agent_alfred.settings import Settings
from agent_alfred.trace import RunBundleTraceSink

GATE = '{"retrieve":false,"query":null,"reason_code":"greeting"}'


@contextmanager
def ops_host(
    path,
    script,
    *,
    tools=(),
    policies=None,
    trace=True,
    secrets=(),
    adapter=False,
    overall_deadline_s=None,
):
    path.mkdir(exist_ok=True)
    conn = sqlite3.connect(path / "test.sqlite3", check_same_thread=False)
    schema.migrate(conn)
    clock = SystemClock() if adapter else FakeClock()
    state = ManagedStateDirectory.acquire(path / "state")
    sink = (
        RunBundleTraceSink(
            root=ManagedStateDirectory.acquire_trace_root(path / "traces"),
            clock=clock,
            process_instance_id="ops-test",
        )
        if trace
        else None
    )
    model = ScriptedModel(script)
    if adapter:
        import json
        from types import SimpleNamespace

        from agent_alfred.messages import ToolCallBlock
        from agent_alfred.model import ModelRef, ModelResult
        from agent_alfred.openai_compatible import OpenAICompatibleAdapter

        class Transport:
            def __init__(self):
                self.chat = SimpleNamespace(completions=self)
                self.items = iter(script)

            def create(self, **kwargs):
                item = next(self.items)
                message = {
                    "role": "assistant",
                    "content": item if isinstance(item, str) else "",
                }
                if isinstance(item, ModelResult):
                    message["tool_calls"] = [
                        {
                            "id": b.id,
                            "type": "function",
                            "function": {
                                "name": b.name,
                                "arguments": json.dumps(dict(b.input)),
                            },
                        }
                        for b in item.response.blocks
                        if isinstance(b, ToolCallBlock)
                    ]
                return {
                    "choices": [
                        {
                            "message": message,
                            "finish_reason": "tool_calls"
                            if message.get("tool_calls")
                            else "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }

        from agent_alfred.stream_fallback import StreamFallback

        model = StreamFallback(
            OpenAICompatibleAdapter(client=Transport(), model=ModelRef("test", "test")),
            clock=clock,
        )
    host = RuntimeHost(
        conn=conn,
        clock=clock,
        factory=ScriptedModelFactory(model),
        fanout=FanOutSink([sink] if sink else [], process_instance_id="ops-test"),
        process_instance_id="ops-test",
        snapshot_provider=MutableAssignmentProvider(
            endpoint_id="fixture",
            model_id="m",
            wire_style="openai",
            api_key="test-key",
            settings=Settings(overall_deadline_s=overall_deadline_s),
        ),
        settings=Settings(),
        file_state=state,
        extra_tools=tools,
        tool_policies=policies,
        secrets=secrets,
    )
    host.start()
    try:
        yield host, model, conn, clock
    finally:
        assert host.close()
        state.close()
        conn.close()


def run(host, message="Explicit test request"):
    accepted = host.submit(SubmitRequest(message=message))
    assert accepted.kind == "accepted"
    return accepted.run_id, host.wait(accepted.run_id)
