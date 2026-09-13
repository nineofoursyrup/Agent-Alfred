"""D5 / CE-08: dialect semantics through real discovery, Loop and workers."""

import json
import time

import pytest

from agent_alfred.evals.deterministic.test_mcp import fixture_host
from agent_alfred.evals.deterministic.test_mcp_review import allow
from agent_alfred.evals.deterministic.test_runtime_tools import calls
from agent_alfred.mcp.validation import Validator
from agent_alfred.messages import ToolCallBlock
from agent_alfred.runtime.host import SubmitRequest
from agent_alfred.tools.schema import freeze

DRAFT7 = "http://json-schema.org/draft-07/schema#"
DRAFT2020 = "https://json-schema.org/draft/2020-12/schema"


@pytest.mark.parametrize("dialect", [DRAFT7, DRAFT7[:-1], DRAFT2020, DRAFT2020 + "#"])
@pytest.mark.parametrize(
    "is_error,valid", [(False, True), (False, False), (True, False)]
)
def test_ce08_d5_real_host_dialect_output_and_passthrough(
    tmp_path, dialect, is_error, valid
):
    draft7 = "draft-07" in dialect
    input_schema = {"$schema": dialect, "type": "object", "required": ["message"]}
    output_schema = {
        "$schema": dialect,
        "$id": "https://schema.invalid/root",
        "type": "object",
        "definitions" if draft7 else "$defs": {"answer": {"type": "string"}},
        "properties": {
            "answer": {
                "$ref": "#/definitions/answer" if draft7 else "#/$defs/answer",
                "type": "number" if draft7 else "string",
            },
            "tuple": {
                "type": "array",
                "items" if draft7 else "prefixItems": [{"type": "integer"}],
                "additionalItems" if draft7 else "items": False,
            },
        },
        "required": ["answer", "tuple"],
    }
    arguments = {"unexpected": ["原文", 9]}  # Missing required input is server-owned.
    host, model, state = fixture_host(
        tmp_path,
        {
            "tools": [
                {
                    "name": "echo",
                    "inputSchema": input_schema,
                    "outputSchema": output_schema,
                }
            ],
            "result": {
                "content": [{"type": "text", "text": "safe response"}],
                "isError": is_error,
                "structuredContent": {
                    "answer": "text",
                    "tuple": [1] if valid else [1, 2],
                },
            },
        },
        script=[
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(ToolCallBlock("echo", "mcp_test_echo", arguments)),
            "done",
        ],
    )
    try:
        assert host.connections()["mcp"]["servers"][0]["available_tools"] == 1
        allow(host)
        host.start()
        run = host.submit(SubmitRequest(message="echo"))
        assert host.wait(run.run_id, 10)
        expected = "failed" if is_error else "succeeded" if valid else "unknown"
        row = host.tool_requests(run.run_id)[0]
        assert row["result"] == expected
        assert row["cost"]["kind"] == "unknown"
        advertised = next(
            t for t in model.requests[1].tools if t.name == "mcp_test_echo"
        )
        assert advertised.input_schema == freeze(input_schema)
        frames = [
            json.loads(line)
            for line in (state / "requests.jsonl").read_text().splitlines()
        ]
        assert [f["params"] for f in frames if f.get("method") == "tools/call"] == [
            {"name": "echo", "arguments": arguments}
        ]
        assert len(model.requests) == (2 if expected == "unknown" else 3)
    finally:
        assert host.close()


@pytest.mark.parametrize("dialect", [DRAFT7, DRAFT2020])
@pytest.mark.parametrize(
    "addition",
    [
        {"$ref": "https://127.0.0.1:1/private"},
        {"$ref": "file:///private"},
        {"$ref": "#"},
        {"$dynamicRef": "#x"},
        {
            "properties": {
                "value": {"$schema": "https://json-schema.org/draft/2019-09/schema"}
            }
        },
        {"$vocabulary": {"urn:custom": True}},
    ],
)
def test_ce08_d5_both_dialects_retain_isolated_rejection(dialect, addition):
    validator = Validator()
    try:
        schema = {"$schema": dialect, "type": "object", **addition}
        assert validator.validate(schema, time.monotonic() + 5) is not None
        assert validator.process is None
    finally:
        assert validator.close()


@pytest.mark.parametrize("dialect,nested", [(DRAFT7, DRAFT2020), (DRAFT2020, DRAFT7)])
def test_ce08_d5_mixed_nested_dialects_remain_unsupported(dialect, nested):
    validator = Validator()
    try:
        schema = {
            "$schema": dialect,
            "type": "object",
            "properties": {"value": {"$schema": nested, "type": "string"}},
        }
        assert (
            validator.validate(schema, time.monotonic() + 5)
            == "schema_dialect_unsupported"
        )
    finally:
        assert validator.close()
