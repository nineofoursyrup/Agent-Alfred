"""CLI send path against an injected host."""

from __future__ import annotations

import io
import sqlite3

from agent_alfred import cli, schema
from agent_alfred.clock import FakeClock
from agent_alfred.events import CapturingSink, FanOutSink
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.host import RuntimeHost
from agent_alfred.settings import Settings


def test_cli_one_shot_prints_the_scripted_reply() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    sink = CapturingSink(flush_at_run_end=True)
    host = RuntimeHost(
        conn=conn,
        factory=ScriptedModelFactory(ScriptedModel(["hello there"])),
        settings=Settings(),
        clock=FakeClock(),
        fanout=FanOutSink([sink], process_instance_id="cli-test"),
        process_instance_id="cli-test",
    )
    out = io.StringIO()
    code = cli.run_injected(host, "hi", out=out)
    assert code == 0
    assert out.getvalue() == "hello there\n"
    stored = conn.execute(
        "SELECT json_extract(content, '$[0].text') FROM agent_log ORDER BY id"
    ).fetchall()
    assert stored == [("hi",), ("hello there",)]
