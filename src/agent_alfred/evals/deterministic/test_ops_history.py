"""Manual saved history through real Host, managed bundles and real tools."""

import json

import pytest

from agent_alfred.evals.deterministic.ops_support import GATE, ops_host, run
from agent_alfred.evals.deterministic.test_runtime_tools import calls
from agent_alfred.messages import TextBlock, ToolCallBlock
from agent_alfred.runtime.tool_history import HistoryError
from agent_alfred.tools import Tool, ToolSuccess
from agent_alfred.tools.calendar import object_schema


def test_long_artifact_segments_exact_utf8_and_replacement_rejected(tmp_path):
    body = "中🙂<script>bad()</script>" * 18000
    tool = Tool(
        "long_local",
        "Long fixture",
        object_schema({}),
        lambda a, c: ToolSuccess((TextBlock(body),)),
        "local_read",
    )
    with ops_host(
        tmp_path,
        [GATE, calls(ToolCallBlock("long", "long_local", {})), "done"],
        tools=(tool,),
    ) as (host, _, _, _):
        identity, _ = run(host)
        query = dict(run_id=identity, step_index=1, call_id="long", projection="audit")
        first = host.read_tool_history(tmp_path / "traces", **query)
        chunks = [first["text"]]
        segment = first
        while segment["next_cursor"]:
            segment = host.read_tool_history(
                tmp_path / "traces", **query, cursor=segment["next_cursor"]
            )
            assert segment["end"] - segment["start"] <= 256 * 1024
            chunks.append(segment["text"])
        assert "".join(chunks) == body
        projection = host.read_tool_history(
            tmp_path / "traces", **{**query, "projection": "model"}
        )
        assert "truncated original_bytes=" in projection["text"]
        path = next((tmp_path / "traces").glob("*/*/artifacts/tool-*.txt"))
        original = path.read_bytes()
        path.unlink()
        path.write_bytes(original)
        with pytest.raises(HistoryError):
            host.read_tool_history(
                tmp_path / "traces", **query, cursor=first["next_cursor"]
            )


def test_parameters_are_committed_redacted_and_prune_retains_accounting(tmp_path):
    with ops_host(
        tmp_path,
        [
            GATE,
            calls(
                ToolCallBlock(
                    "write",
                    "create_event",
                    {
                        "title": "sensitive-key-value",
                        "starts_at": "2026-09-12T00:00:00Z",
                    },
                )
            ),
            "done",
        ],
        secrets=("sensitive-key-value",),
        adapter=True,
    ) as (host, _, conn, _):
        identity, _ = run(host)
        query = dict(
            run_id=identity, step_index=1, call_id="write", projection="parameters"
        )
        parameters = host.read_tool_history(tmp_path / "traces", **query)
        assert "sensitive-key-value" not in parameters["text"]
        assert "starts_at" in parameters["text"]
        before = host.accounting_snapshot({"range": "all", "timezone": "UTC"})[
            "summary"
        ]
        from agent_alfred.schema import record_trace_prune

        record_trace_prune(
            conn,
            run_id=identity,
            prune_requested_at="2026-09-12T00:00:00Z",
            absence_confirmed_at="2026-09-12T00:00:00Z",
            prune_reason="manual",
        )
        conn.commit()
        with pytest.raises(HistoryError, match="trace_pruned"):
            host.read_tool_history(tmp_path / "traces", **query)
        after = host.accounting_snapshot({"range": "all", "timezone": "UTC"})["summary"]
        assert before == after
        assert host.tool_requests(identity)[0]["result"] == "succeeded"


def test_forged_artifact_and_duplicate_committed_identity_fail_closed(tmp_path):
    with ops_host(
        tmp_path, [GATE, calls(ToolCallBlock("q", "query_events", {})), "done"]
    ) as (host, _, _, _):
        identity, _ = run(host)
        path = next((tmp_path / "traces").glob("*/*/trace.jsonl"))
        events = [json.loads(line) for line in path.read_text().splitlines()]
        for event in events:
            if event["payload"].get("name") == "tool.finished":
                event["payload"]["audit_content"] = {
                    "artifact": "../../outside",
                    "bytes": 10,
                }
        path.write_text("".join(json.dumps(e) + "\n" for e in events))
        with pytest.raises(HistoryError, match="artifact_identity_mismatch"):
            host.read_tool_history(
                tmp_path / "traces",
                run_id=identity,
                step_index=1,
                call_id="q",
                projection="audit",
            )


def test_history_failed_close_remains_owned_until_host_close(tmp_path, monkeypatch):
    from agent_alfred.managed_state import ManagedFileLease

    with ops_host(
        tmp_path, [GATE, calls(ToolCallBlock("q", "query_events", {})), "done"]
    ) as (host, _, _, _):
        identifier, _ = run(host)
        original = ManagedFileLease.close
        retained = []
        armed = True

        def fail(lease):
            if armed and lease.path.name == "trace.jsonl":
                retained.append(lease)
                raise OSError("close withheld")
            return original(lease)

        monkeypatch.setattr(ManagedFileLease, "close", fail)
        with pytest.raises((HistoryError, OSError)):
            host.read_tool_history(
                tmp_path / "traces", run_id=identifier, step_index=1, call_id="q"
            )
        armed = False
        assert host.close()
        assert retained
        for lease in retained:
            with pytest.raises(RuntimeError):
                _ = lease.fd
