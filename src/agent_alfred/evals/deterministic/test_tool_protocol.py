"""Tool request declarations and historical serialization compatibility."""

import hashlib
import json
from dataclasses import fields
from pathlib import Path

import pytest

from agent_alfred.events import AttemptAborted, StepStarted
from agent_alfred.model import ModelError, ModelRef, ModelRequest
from agent_alfred.trace import _prepare_payload

FIXTURES = Path(__file__).parent / "fixtures" / "issue14"


def test_tool_spec_has_only_the_model_facing_fields_and_choice_is_recorded():
    from agent_alfred.model import NamedToolChoice, ToolSpec

    tool = ToolSpec("weather", "weather report", {"type": "object"})
    request = ModelRequest(
        ModelRef("test", "m"),
        None,
        (),
        tools=(tool,),
        tool_choice=NamedToolChoice("weather"),
    )
    assert [f.name for f in fields(tool)] == ["name", "description", "input_schema"]
    event = StepStarted(tool_names=("weather",), tool_choice=request.tool_choice)
    assert "weather" in _prepare_payload(event)
    assert "tool_choice" in _prepare_payload(event)


def test_raw_fragments_have_optional_id_and_name_without_changing_empty_golden():
    from agent_alfred.events import RawToolArgumentFragment

    fragment = RawToolArgumentFragment(None, None, '{"incomplete":')
    assert fragment.raw == '{"incomplete":'
    empty = AttemptAborted(
        attempt_id="a1", partial=True, error=ModelError(False, 400, "x", "a1", "c")
    )
    assert (
        _prepare_payload(empty).encode()
        == (FIXTURES / "baseline-aborted.json").read_bytes()
    )
    full = AttemptAborted(unparsed_tool_arguments=(fragment,))
    assert (
        json.loads(_prepare_payload(full))["unparsed_tool_arguments"][0]["call_id"]
        is None
    )


@pytest.mark.parametrize(
    "payload",
    [
        json.loads((FIXTURES / "baseline-aborted.json").read_bytes()),
        json.loads((FIXTURES / "baseline-step.json").read_bytes()),
        json.loads(_prepare_payload(StepStarted(tool_choice="required"))),
        ["arbitrary", {"payload-shape": "does not affect envelope reading"}],
    ],
)
def test_historical_and_new_payloads_cross_the_real_trace_reader(tmp_path, payload):
    from agent_alfred.runtime.evidence import _read_trace

    run = "historical"
    digest = hashlib.sha256(run.encode()).hexdigest()[:32]
    name = "000000Z-" + digest
    bundle = tmp_path / "2026-01-01" / name
    bundle.mkdir(parents=True)
    (bundle / "meta.json").write_text(
        json.dumps(
            dict(
                run_id=run,
                run_storage_id=digest,
                run_dir_name=name,
                created_at="2026-01-01T00:00:00Z",
                process_instance_id="historical-process",
            )
        )
    )
    envelope = dict(run_id=run, process_instance_id="historical-process")
    rows = [
        envelope | dict(seq=1, payload_name="fixture", payload=payload),
        envelope | dict(seq=2, payload_name="run.finished", payload={}),
    ]
    (bundle / "trace.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    status, read = _read_trace(tmp_path, run)
    assert status == "available"
    assert read[0]["payload"] == payload


def test_existing_browser_projection_does_not_depend_on_new_fields():
    root = Path(__file__).parents[2]
    paths = [root / "gateway/web/frames.py", *(root / "ops/static").glob("*.js")]
    for path in paths:
        content = path.read_text()
        assert "unparsed_tool_arguments" not in content
        assert "tool_choice" not in content


def test_request_keeps_existing_positional_max_tokens_argument():
    request = ModelRequest(ModelRef("test", "m"), None, (), (), 123)
    assert request.max_tokens == 123
    assert request.tool_choice == "auto"


def test_fragment_names_and_raw_text_cross_the_real_event_redactor():
    from agent_alfred.events import CapturingSink, FanOutSink, RawToolArgumentFragment
    from agent_alfred.redact import Redactor

    sink = CapturingSink(name="capture")
    fanout = FanOutSink(
        [sink], process_instance_id="test", redactor=Redactor(("synthetic-secret",))
    )
    emitted = fanout.emit(
        AttemptAborted(
            unparsed_tool_arguments=(
                RawToolArgumentFragment(None, "synthetic-secret", "synthetic-secret"),
            )
        )
    )
    fragment = emitted.payload.unparsed_tool_arguments[0]
    assert fragment.name == "***"
    assert fragment.raw == "***"
