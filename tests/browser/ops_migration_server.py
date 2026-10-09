"""Real Ops HTTP/Host/SQLite with deterministic model and catalog boundaries."""

import argparse
import sys
from decimal import Decimal
from pathlib import Path

from server import BrowserModel

from agent_alfred.events import AttemptAborted, AttemptCommitted, AttemptStarted
from agent_alfred.messages import TextBlock, ToolCallBlock
from agent_alfred.model import (
    AttemptRecord,
    ModelRef,
    ModelResponse,
    ModelResult,
    ScriptedModel,
    ScriptedModelFactory,
    Usage,
)
from agent_alfred.pricing import PriceQuote
from agent_alfred.runtime.host import SubmitRequest
from agent_alfred.schema import record_trace_prune
from agent_alfred.tools import Tool, ToolCost, ToolPolicy, ToolSuccess
from agent_alfred.tools.calendar import object_schema
from agent_alfred.wiring import build_dashboard

BODY = "中🙂<script>window.historyInjected=true</script>" * 18000
GATE = '{"retrieve":false,"query":null,"reason_code":"greeting"}'


class Model(BrowserModel):
    pending = None

    def respond(self, request, *, events=None, deadline=None):
        if self.pending is None:
            return super().respond(request, events=events, deadline=deadline)
        result = self.pending.respond(request, deadline=deadline)
        for attempt in result.attempts:
            events.emit(
                AttemptStarted(attempt_id=attempt.attempt_id, model=request.model)
            )
            event = (
                AttemptCommitted if attempt.outcome == "committed" else AttemptAborted
            )
            events.emit(
                event(
                    attempt_id=attempt.attempt_id,
                    usage=attempt.usage,
                    blocks=result.response.blocks
                    if attempt.outcome == "committed"
                    else (),
                )
            )
        return result


class Prices:
    factor = Decimal("1")

    def quote(self, endpoint, model, dimension):
        dimensions = {
            "uncached_input": "1",
            "cache_read": "0.1",
            "cache_write": "2",
            "output": "3",
        }
        return PriceQuote(
            Decimal(dimensions[dimension]) * self.factor,
            "catalog",
            stale=True,
            tiered=True,
            catalog_fetched_at="2026-10-01T00:00:00Z",
        )


class Factory(ScriptedModelFactory):
    prices = Prices()

    def catalog_prices(self):
        return self.prices


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--threshold")
    args = parser.parse_args()
    model = Model([])
    factory = Factory(model)
    tool = Tool(
        "long_fixture",
        "Offline long tool",
        object_schema({}),
        lambda a, c: ToolSuccess(
            (TextBlock(BODY),), cost=ToolCost("0.125", "credits", "receipt")
        ),
        "external",
        source_id="fixture-service",
        capability_id="long-read",
        cost_units=("credits",),
        cost_sources=("receipt",),
    )
    dashboard = build_dashboard(
        state_dir=args.state,
        port=args.port,
        factory=factory,
        extra_tools=(tool,),
        tool_policies={tool.name: ToolPolicy(configured=True)},
    )
    dashboard.start()
    host = dashboard.host
    print("ready", flush=True)
    try:
        for line in sys.stdin:
            command = line.strip()
            if command == "stop":
                break
            if command in ("mixed", "long", "simple"):
                if command == "mixed":
                    ref = ModelRef("fixture", "four-dim")
                    result = ModelResult(
                        (
                            AttemptRecord(
                                "exact-aborted",
                                False,
                                "aborted",
                                Usage(endpoint_reported_cost_usd=Decimal("1.25")),
                                model=ref,
                            ),
                            AttemptRecord(
                                "four-dim-aborted",
                                False,
                                "aborted",
                                Usage(
                                    total_input_tokens=1700,
                                    uncached_input_tokens=1000,
                                    cache_read_tokens=500,
                                    cache_write_tokens=200,
                                    output_tokens=2000,
                                    reasoning_tokens=100,
                                ),
                                model=ref,
                            ),
                            AttemptRecord(
                                "unknown-committed",
                                False,
                                "committed",
                                Usage(output_tokens=0),
                                model=ref,
                            ),
                        ),
                        ModelResponse((TextBlock("mixed done"),), "end_turn", ref),
                        None,
                    )
                    model.pending = ScriptedModel([GATE, result])
                elif command == "long":
                    declaration = next(
                        row
                        for row in host.tools_catalog()["tools"]
                        if row["name"] == tool.name
                    )
                    assert (
                        host.save_tool_authorization(
                            declaration["identity"], "allowed", 0
                        )["application_state"]
                        == "applied"
                    )
                    response = ModelResult(
                        (
                            AttemptRecord(
                                "long-attempt",
                                False,
                                "committed",
                                Usage(output_tokens=10),
                            ),
                        ),
                        ModelResponse(
                            (ToolCallBlock("long-call", tool.name, {}),),
                            "tool_use",
                            ModelRef("fixture", "model"),
                        ),
                        None,
                    )
                    model.pending = ScriptedModel([GATE, response, "long done"])
                else:
                    model.pending = None
                accepted = host.submit(SubmitRequest(message=command))
                assert accepted.kind == "accepted"
                host.wait(accepted.run_id)
                model.pending = None
            elif command == "price":
                factory.prices.factor = Decimal("10")
            elif command == "expire":
                with host._accounting.lock:
                    host._accounting.snapshots.clear()
            elif command == "prune":
                with host._store.transaction() as conn:
                    for row in conn.execute("SELECT run_id FROM runs").fetchall():
                        record_trace_prune(
                            conn,
                            run_id=row[0],
                            prune_requested_at="2026-10-09T00:00:00Z",
                            absence_confirmed_at="2026-10-09T00:00:00Z",
                            prune_reason="manual",
                        )
                    conn.commit()
            print("ok " + command, flush=True)
    finally:
        if not dashboard.close():
            raise RuntimeError("Dashboard close did not drain")


if __name__ == "__main__":
    main()
