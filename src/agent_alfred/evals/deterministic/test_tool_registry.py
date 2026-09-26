"""Issue #19: the public registry contract, independent of runtime wiring."""

import pytest

from agent_alfred.clock import FakeClock
from agent_alfred.tools import Tool, ToolRegistry, ToolSuccess


@pytest.fixture
def external_ledger():
    import sqlite3
    import threading

    from agent_alfred import schema
    from agent_alfred.runtime.recording import RecordingStore
    from agent_alfred.tools.ledger import ExternalToolLedger

    conn = sqlite3.connect(":memory:")
    schema.migrate(conn)
    try:
        yield ExternalToolLedger(RecordingStore(conn, threading.Lock()), FakeClock())
    finally:
        conn.close()


@pytest.mark.parametrize("name", ["bad.name", "", "x" * 65])
def test_registry_rejects_invalid_names_before_model_use(name):
    with pytest.raises(ValueError, match="name"):
        ToolRegistry(
            (
                Tool(
                    name,
                    "read",
                    {"type": "object"},
                    lambda args, ctx: ToolSuccess(()),
                    "local_read",
                ),
            ),
            clock=FakeClock(),
        )


def declaration(schema=None):
    return Tool(
        "read",
        "Read",
        schema or {"type": "object"},
        lambda args, ctx: ToolSuccess(()),
        "local_read",
    )


def test_registry_rejects_duplicate_names_and_unknown_schema_keywords():
    with pytest.raises(ValueError, match="duplicate"):
        ToolRegistry((declaration(), declaration()), clock=FakeClock())
    with pytest.raises(ValueError, match="keyword"):
        ToolRegistry((declaration({"type": "object", "oneOf": []}),), clock=FakeClock())


def test_schema_snapshot_cannot_change_after_registration():
    schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
        "additionalProperties": False,
    }
    registry = ToolRegistry((declaration(schema),), clock=FakeClock())
    schema["properties"].clear()
    exposed = registry.schemas()
    assert set(exposed[0].input_schema["properties"]) == {"title"}
    with pytest.raises(TypeError):
        exposed[0].input_schema["properties"]["evil"] = {}


def test_execution_pairs_original_call_and_rejects_arguments_without_running():
    from agent_alfred.messages import TextBlock, ToolCallBlock
    from agent_alfred.tools import ToolContext

    tool = Tool(
        "echo",
        "Echo",
        {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        lambda args, ctx: ToolSuccess((TextBlock(args["text"]),)),
        "local_read",
    )
    registry = ToolRegistry((tool,), clock=FakeClock())
    context = ToolContext("run", 0, "original", "cli", 30)
    result = registry.execute(
        ToolCallBlock("original", "echo", {"text": "hello"}), context
    )
    assert result.block.call_id == "original"
    assert result.block.content[0].text == "hello"
    bad = registry.execute(ToolCallBlock("original", "echo", {"text": 1}), context)
    assert bad.block.is_error
    assert '"code":"invalid_input"' in bad.block.content[0].text
    unknown = registry.execute(ToolCallBlock("original", "missing", {}), context)
    assert unknown.block.call_id == "original"
    assert '"code":"unknown_tool"' in unknown.block.content[0].text


@pytest.mark.parametrize(
    "configured,authorization,visible,code",
    [
        (False, "unset", True, "configuration_required"),
        (False, "allowed", True, "configuration_required"),
        (False, "denied", False, "not_authorized"),
        (True, "unset", False, "not_authorized"),
        (True, "denied", False, "not_authorized"),
        (True, "allowed", True, None),
    ],
)
def test_external_policy(configured, authorization, visible, code, external_ledger):
    from dataclasses import replace

    from agent_alfred.messages import ToolCallBlock
    from agent_alfred.tools import ToolContext, ToolPolicy

    tool = replace(declaration(), effect="external")
    registry = ToolRegistry(
        (tool,),
        clock=FakeClock(),
        policies={"read": ToolPolicy(configured, authorization)},
        external_ledger=external_ledger,
    )
    assert bool(registry.schemas()) is visible
    result = registry.execute(
        ToolCallBlock("c", "read", {}), ToolContext("r", 0, "c", "cli", 100)
    )
    assert result.block.is_error is (code is not None)
    if code:
        assert f'"code":"{code}"' in result.block.content[0].text


def test_expired_budget_never_starts_and_context_carries_absolute_deadline():
    from dataclasses import replace

    from agent_alfred.messages import TextBlock, ToolCallBlock
    from agent_alfred.tools import ToolContext

    clock = FakeClock(monotonic_value=10)
    deadlines = []

    def execute(args, context):
        deadlines.append(context.deadline)
        return ToolSuccess((TextBlock("done"),))

    registry = ToolRegistry(
        (replace(declaration(), fn=execute, budget_s=30),), clock=clock
    )
    call = ToolCallBlock("c", "read", {})
    registry.execute(call, ToolContext("r", 0, "c", "cli", 12))
    assert deadlines == [12]
    clock.monotonic_value = 12
    expired = registry.execute(call, ToolContext("r", 0, "c", "cli", 12))
    assert expired.block.is_error
    assert deadlines == [12]


def test_registry_owns_events_and_retains_full_unicode_audit_projection():
    from dataclasses import replace

    from agent_alfred.events import CapturingSink, FanOutSink
    from agent_alfred.messages import TextBlock, ToolCallBlock
    from agent_alfred.tools import ToolContext

    sink = CapturingSink()
    events = FanOutSink([sink], process_instance_id="p")
    registry = ToolRegistry(
        (
            replace(
                declaration(),
                fn=lambda args, ctx: ToolSuccess((TextBlock("你好" * 20),)),
            ),
        ),
        clock=FakeClock(),
        model_content_limit=10,
    )
    result = registry.execute(
        ToolCallBlock("c", "read", {}),
        ToolContext("r", 0, "c", "cli", 30),
        events=events,
    )
    assert result.block.content[0].text.startswith("你好" * 5)
    assert "truncated" in result.block.content[0].text
    assert [e.payload.name for e in sink.events] == ["tool.started", "tool.finished"]
    finished = sink.events[-1].payload
    assert finished.audit_content == "你好" * 20
    assert finished.original_bytes == 120
    assert finished.model_content == result.block.content[0].text


def test_progress_permission_and_process_control_are_preserved():
    from dataclasses import replace

    from agent_alfred.events import CapturingSink, FanOutSink
    from agent_alfred.messages import TextBlock, ToolCallBlock
    from agent_alfred.tools import ToolContext

    sink = CapturingSink()
    events = FanOutSink([sink], process_instance_id="p")

    def progress(args, ctx):
        ctx.events.progress("working")
        return ToolSuccess((TextBlock("done"),))

    registry = ToolRegistry(
        (replace(declaration(), fn=progress, emits_progress=True),), clock=FakeClock()
    )
    registry.execute(
        ToolCallBlock("c", "read", {}),
        ToolContext("r", 0, "c", "cli", 30),
        events=events,
    )
    assert [e.payload.name for e in sink.events] == [
        "tool.started",
        "tool.progress",
        "tool.finished",
    ]
    assert sink.events[1].trace_policy == "transient"

    def interrupted(args, ctx):
        assert ctx.events is None
        raise KeyboardInterrupt

    registry = ToolRegistry(
        (replace(declaration(), fn=interrupted),), clock=FakeClock()
    )
    with pytest.raises(KeyboardInterrupt):
        registry.execute(
            ToolCallBlock("c", "read", {}),
            ToolContext("r", 0, "c", "cli", 30),
            events=events,
        )


def test_model_projection_uses_central_redactor_before_truncation():
    from dataclasses import replace

    from agent_alfred.messages import TextBlock, ToolCallBlock
    from agent_alfred.redact import Redactor
    from agent_alfred.tools import ToolContext

    registry = ToolRegistry(
        (
            replace(
                declaration(),
                fn=lambda args, ctx: ToolSuccess(
                    (TextBlock("secret-credential-value rest"),)
                ),
            ),
        ),
        clock=FakeClock(),
        redactor=Redactor(("secret-credential-value",)),
        model_content_limit=5,
    )
    result = registry.execute(
        ToolCallBlock("c", "read", {}), ToolContext("r", 0, "c", "cli", 30)
    )
    assert "secre" not in result.block.content[0].text


def _project_array_through_public_registry(audit, *, limit, redactor=None):
    import hashlib
    from dataclasses import replace

    from agent_alfred.events import CapturingSink, FanOutSink
    from agent_alfred.messages import TextBlock, ToolCallBlock
    from agent_alfred.redact import Redactor
    from agent_alfred.tools import ToolContext

    redactor = redactor or Redactor(())
    sink = CapturingSink()
    events = FanOutSink([sink], process_instance_id="projection", redactor=redactor)
    registry = ToolRegistry(
        (
            replace(
                declaration(),
                fn=lambda args, ctx: ToolSuccess((TextBlock(audit),)),
            ),
        ),
        clock=FakeClock(),
        redactor=redactor,
        model_content_limit=limit,
    )
    execution = registry.execute(
        ToolCallBlock("c", "read", {}),
        ToolContext("r", 0, "c", "cli", 30),
        events=events,
    )
    finished = sink.events[-1].payload
    assert finished.name == "tool.finished"
    assert finished.model_content == execution.block.content[0].text
    assert finished.audit_content == redactor.redact_text(audit)
    assert finished.original_bytes == len(finished.audit_content.encode("utf-8"))
    assert (
        finished.content_digest
        == hashlib.sha256(finished.audit_content.encode("utf-8")).hexdigest()
    )
    return execution.block.content[0].text


@pytest.mark.parametrize(
    "row_template",
    [
        '{"note":"SECRET","note":"safe"}',
        '{"nested":[{"note":"SECRET","note":"safe"}]}',
        '{"note":"SECRET","\\u006eote":"safe"}',
        '{"note":"old","note":"safe"}',
    ],
    ids=["original-st01", "nested-object", "escaped-key", "nonsensitive"],
)
def test_array_projection_rejects_duplicate_members_before_publication(row_template):
    from agent_alfred.redact import Redactor

    secret = "review-hidden-key-1234"
    escaped = "".join(f"\\u{ord(char):04x}" for char in secret)
    row = row_template.replace("SECRET", escaped)
    audit = " " * 100 + "[" + row + ',{"tail":"' + "x" * 500 + '"}]'

    projected = _project_array_through_public_registry(
        audit, limit=180, redactor=Redactor((secret,))
    )

    assert projected.startswith("[]\n[truncated ")
    assert secret not in projected
    assert escaped not in projected
    assert "omitted rows are unknown" in projected


def test_array_projection_rejects_duplicates_even_in_omitted_rows():
    audit = '[{"note":"visible"},{"padding":"' + "x" * 500
    audit += '","nested":{"note":"old","note":"safe"}}]'

    projected = _project_array_through_public_registry(audit, limit=40)

    assert projected.startswith("[]\n[truncated ")
    assert "omitted rows are unknown" in projected


def test_array_projection_keeps_complete_source_spans_and_unknown_boundary():
    row = (
        '{"note":"visible","number":1.234567890123456789,'
        '"nested":{"note":"\\u006f\\u006b"}}'
    )
    audit = " " * 100 + "[" + row + ',{"tail":"' + "x" * 500 + '"}]'

    projected = _project_array_through_public_registry(audit, limit=180)

    assert projected.split("\n[truncated ", 1)[0] == "[" + row + "]"
    assert "visible JSON rows are complete; omitted rows are unknown" in projected


def test_array_projection_within_limit_retains_original_text():
    audit = '  [{"number":1.234567890123456789,"note":"\\u006f\\u006b"}]  '

    projected = _project_array_through_public_registry(audit, limit=180)

    assert projected == audit


def test_nonarray_projection_retains_existing_prefix_boundary():
    audit = "ordinary text " * 40

    projected = _project_array_through_public_registry(audit, limit=40)

    assert projected.split("\n[truncated ", 1)[0] == audit[:40]
    assert "visible prefix may end mid-field; omitted content is unknown" in projected


def test_array_projection_preserves_exact_numeric_token():
    audit = '[{"value":1.234567890123456789},{"value":2,"note":"tail"}]'

    projected = _project_array_through_public_registry(audit, limit=36)

    assert "1.234567890123456789" in projected
    assert '"note"' not in projected
    assert "omitted rows are unknown" in projected


def test_array_projection_does_not_decode_escaped_registered_secret():
    from agent_alfred.redact import Redactor

    secret = "skFAKE123"
    escaped = "".join(f"\\u{ord(char):04x}" for char in secret)
    audit = '[{"note":"' + escaped + '"},{"note":"tail"}]'

    projected = _project_array_through_public_registry(
        audit, limit=75, redactor=Redactor((secret,))
    )

    assert secret not in projected
    assert escaped not in projected
    assert "omitted rows are unknown" in projected


def test_oversized_array_projection_omits_partial_rows():
    audit = '[{"id":1},{"memo":"' + ("x" * 1048600) + '"}]'

    projected = _project_array_through_public_registry(audit, limit=35)

    assert projected.startswith("[]\n[truncated ")
    assert '"memo"' not in projected
    assert "omitted rows are unknown" in projected


def test_calendar_same_operation_replays_but_new_call_can_repeat_action():
    import sqlite3
    import threading

    from agent_alfred import schema
    from agent_alfred.messages import ToolCallBlock
    from agent_alfred.runtime.recording import RecordingStore
    from agent_alfred.tools import ToolContext
    from agent_alfred.tools.calendar import CalendarTools

    conn = sqlite3.connect(":memory:")
    schema.migrate(conn)
    registry = ToolRegistry(
        CalendarTools(
            RecordingStore(conn, threading.Lock()), FakeClock()
        ).declarations(),
        clock=FakeClock(),
    )
    args = {"title": "same", "starts_at": "2026-09-10T08:00:00+08:00"}
    first = registry.execute(
        ToolCallBlock("c", "create_event", args), ToolContext("r", 1, "c", "cli", 30)
    )
    replay = registry.execute(
        ToolCallBlock("c", "create_event", args), ToolContext("r", 1, "c", "cli", 30)
    )
    assert first.block == replay.block
    registry.execute(
        ToolCallBlock("new", "create_event", args),
        ToolContext("r", 2, "new", "cli", 30),
    )
    assert conn.execute("SELECT count(*) FROM calendar_entries").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM tool_ledger").fetchone()[0] == 2
    conn.close()


def test_external_effect_records_started_before_call_then_final_state():
    import sqlite3
    import threading
    from dataclasses import replace

    from agent_alfred import schema
    from agent_alfred.messages import ToolCallBlock
    from agent_alfred.runtime.recording import RecordingStore
    from agent_alfred.tools import ToolContext, ToolPolicy
    from agent_alfred.tools.ledger import ExternalToolLedger

    conn = sqlite3.connect(":memory:")
    schema.migrate(conn)
    store = RecordingStore(conn, threading.Lock())

    def action(args, context):
        assert conn.execute("SELECT status FROM tool_ledger").fetchone()[0] == "started"
        return ToolSuccess(())

    registry = ToolRegistry(
        (replace(declaration(), fn=action, effect="external"),),
        clock=FakeClock(),
        policies={"read": ToolPolicy(True, "allowed")},
        external_ledger=ExternalToolLedger(store, FakeClock()),
    )
    result = registry.execute(
        ToolCallBlock("call", "read", {}), ToolContext("r", 1, "call", "cli", 30)
    )
    assert not result.block.is_error
    assert conn.execute("SELECT status FROM tool_ledger").fetchall() == [("succeeded",)]
    conn.close()


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "minimum": 3},
        {"type": "object", "properties": {"n": {"type": "number", "minimum": "bad"}}},
        {"type": "object", "properties": {"n": {"type": "string", "enum": "abc"}}},
    ],
)
def test_malformed_supported_schema_keywords_are_rejected(schema):
    with pytest.raises(ValueError):
        ToolRegistry((declaration(schema),), clock=FakeClock())
