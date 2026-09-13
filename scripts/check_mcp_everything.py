"""Explicitly authorized, isolated Everything echo acceptance (never default CI)."""

import argparse
import json
from pathlib import Path

from agent_alfred.connections import CredentialOverlay
from agent_alfred.evals.deterministic.test_runtime_tools import calls
from agent_alfred.gateway.web.api import DashboardApi
from agent_alfred.messages import ToolCallBlock
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.host import SubmitRequest
from agent_alfred.wiring import build_default_host


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", required=True, type=Path)
    parser.add_argument("--entry", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    assert args.node.is_absolute() and args.entry.is_absolute()
    args.state_dir.mkdir(parents=True, exist_ok=False)
    (args.state_dir / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "everything": {
                        "command": str(args.node),
                        "args": [str(args.entry)],
                        "enabled": True,
                    }
                }
            }
        )
    )
    gate = '{"retrieve":false,"query":null,"reason_code":"greeting"}'
    invocation = calls(
        ToolCallBlock("echo", "mcp_everything_echo", {"message": "alfred-mcp-smoke"})
    )
    model = ScriptedModel(
        [gate, invocation, "Not authorized", gate, invocation, "Done"]
    )
    host = build_default_host(
        state_dir=args.state_dir,
        factory=ScriptedModelFactory(model),
        credentials=CredentialOverlay({}, None),
    )
    evidence = {}
    try:
        evidence["discovery"] = host.connections()["mcp"]
        catalog = host.tools_catalog()
        tool = next(t for t in catalog["tools"] if t.get("original_name") == "echo")
        assert tool["availability"] == "configured"
        assert tool["authorization"] == "unset"
        assert all(
            t["authorization"] != "allowed"
            for t in catalog["tools"]
            if t.get("server_key")
        )
        host.start()
        first = host.submit(SubmitRequest(message="echo alfred-mcp-smoke"))
        assert host.wait(first.run_id, 15)
        evidence["unauthorized"] = host.tool_requests(first.run_id)
        assert evidence["unauthorized"][0]["start_confirmation"] == "not_started"
        assert evidence["unauthorized"][0]["reason"] == "not_authorized"
        assert "error" not in host.save_tool_authorization(
            tool["identity"], "allowed", catalog["revision"]
        )
        second = host.submit(SubmitRequest(message="echo alfred-mcp-smoke"))
        assert host.wait(second.run_id, 15)
        evidence["authorized"] = host.tool_requests(second.run_id)
        assert evidence["authorized"][0]["result"] == "succeeded"
        assert evidence["authorized"][0]["cost"]["kind"] == "unknown"
        assert "Echo: alfred-mcp-smoke" in str(model.requests[-1].messages)
        evidence["echo"] = "Echo: alfred-mcp-smoke"
        evidence["final_catalog"] = host.tools_catalog()
        assert all(
            t["authorization"]
            == ("allowed" if t["original_name"] == "echo" else "unset")
            for t in evidence["final_catalog"]["tools"]
            if t.get("server_key")
        )
        api = DashboardApi(facade=host)
        status, snapshot = api.accounting_write(
            "/api/ops/snapshots", {"range": "all", "timezone": "UTC"}
        )
        assert status == 200
        status, detail = api.accounting_read(
            "/api/ops/detail",
            {"snapshot_id": snapshot["snapshot_id"], "run_id": second.run_id},
        )
        assert status == 200
        evidence["ops"] = detail
        evidence["server_info"] = host._mcp.rows["everything"]["server_info"]
        evidence["pid"] = host._mcp.rows["everything"]["session"].process.pid
    finally:
        evidence["closed"] = host.close()
        args.evidence.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        assert evidence["closed"]


if __name__ == "__main__":
    main()
