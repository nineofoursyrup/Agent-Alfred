"""Real Overview sources; only external model output, clocks and IO are controlled."""

import argparse
import json
import sys
import threading
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from server import BrowserModel

from agent_alfred.clock import SystemClock
from agent_alfred.memory.commands import CommandContext
from agent_alfred.memory.types import ManualOrigin
from agent_alfred.messages import TextBlock
from agent_alfred.model import (
    AttemptRecord,
    ModelResponse,
    ModelResult,
    ScriptedModel,
    ScriptedModelFactory,
    Usage,
)
from agent_alfred.pricing import PriceQuote
from agent_alfred.runtime.host import SubmitRequest
from agent_alfred.runtime.memory import GATE_SYSTEM
from agent_alfred.wiring import build_dashboard


class Clock(SystemClock):
    wall = None

    def wall_utc(self):
        return self.wall or super().wall_utc()


class Prices:
    value = Decimal("1")

    def quote(self, endpoint, model, dimension):
        return PriceQuote(self.value, "catalog", stale=True)


class Model(BrowserModel):
    mode = "exact"

    def respond(self, request, *, events=None, deadline=None):
        gate = request.system and request.system[0].text == GATE_SYSTEM
        result = ScriptedModel(
            [
                '{"retrieve":false,"query":null,"reason_code":"greeting"}'
                if gate
                else "离线测试正式回复，不属于总览"
            ]
        ).respond(request, events=events, deadline=deadline)
        if gate:
            usages = [("committed", Usage(endpoint_reported_cost_usd=Decimal(0)))]
        elif self.mode == "mixed":
            usages = [
                ("aborted", Usage(endpoint_reported_cost_usd=Decimal("1.25"))),
                ("aborted", Usage(uncached_input_tokens=1000, output_tokens=2000)),
                ("committed", Usage()),
            ]
        else:
            amount = {"exact": "0.125", "zero": "0", "tiny": "0.000000001"}.get(
                self.mode
            )
            usages = [
                (
                    "committed",
                    Usage(endpoint_reported_cost_usd=Decimal(amount))
                    if amount is not None
                    else Usage(),
                )
            ]
        return ModelResult(
            tuple(
                AttemptRecord(
                    uuid.uuid4().hex, False, outcome, usage, model=request.model
                )
                for outcome, usage in usages
            ),
            ModelResponse(
                (TextBlock(result.response.blocks[0].text),), "end_turn", request.model
            ),
            None,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--threshold")
    args = parser.parse_args()
    model, clock, prices = Model([]), Clock(), Prices()

    class Factory(ScriptedModelFactory):
        def catalog_prices(self):
            return prices

    saving, release = threading.Event(), threading.Event()
    release.set()

    class BeforeCommit:
        def wait(self):
            saving.set()
            assert release.wait(20), "recording IO barrier was not released"

    dashboard = build_dashboard(
        state_dir=args.state,
        port=args.port,
        factory=Factory(model),
        clock=clock,
        before_recording_commit=BeforeCommit(),
    )
    dashboard.start()
    host = dashboard.host
    identities = []
    retained = None
    print("ready", flush=True)
    try:
        for line in sys.stdin:
            command = line.strip()
            if command == "stop":
                break
            if command.startswith("runs "):
                for _ in range(int(command.split()[1])):
                    accepted = host.submit(
                        SubmitRequest(message="synthetic overview fixture")
                    )
                    assert accepted.kind == "accepted"
                    assert host.wait(accepted.run_id) is not None
                    identities.append(accepted.run_id)
            elif command.startswith("mode "):
                model.mode = command.split()[1]
            elif command.startswith("time "):
                clock.wall = datetime.fromisoformat(command.split()[1])
            elif command == "advance-day":
                clock.wall = clock.wall_utc() + timedelta(days=1)
            elif command == "price-change":
                prices.value = Decimal("10")
            elif command.startswith("memory "):
                for index in range(int(command.split()[1])):
                    saved = host.memory_service.execute(
                        {
                            "operation_id": uuid.uuid4().hex,
                            "kind": "semantic",
                            "action": "save",
                            "payload": {
                                "subject": "Overview fixture",
                                "fact": uuid.uuid4().hex,
                            },
                        },
                        CommandContext(ManualOrigin("cli"), "cli"),
                    )
                    assert saved["status"] == "saved", saved
            elif command.startswith("telemetry "):
                kind = command.split()[1]
                with host._store.transaction() as conn:
                    if kind == "bad-time":
                        conn.execute(
                            "UPDATE runs SET started_at='broken',accepted_at='broken'"
                        )
                    elif kind == "no-model":
                        conn.execute(
                            "UPDATE runs SET telemetry=?",
                            (
                                json.dumps(
                                    {
                                        "accounting_version": 1,
                                        "trace_incomplete": False,
                                        "attempts": [],
                                    }
                                ),
                            ),
                        )
                    elif kind == "legacy-tool-gap":
                        for run_id, raw in conn.execute(
                            "SELECT run_id,telemetry FROM runs"
                        ).fetchall():
                            value = json.loads(raw)
                            if value.get("attempts"):
                                value.pop("accounting_version", None)
                            conn.execute(
                                "UPDATE runs SET telemetry=? WHERE run_id=?",
                                (json.dumps(value), run_id),
                            )
                    elif kind == "interrupted":
                        conn.execute(
                            "UPDATE runs SET phase='finished',outcome='interrupted',"
                            "telemetry=NULL"
                        )
                    elif kind == "broken":
                        conn.execute("UPDATE runs SET telemetry='{}'")
                    conn.commit()
            elif command == "opaque-runs":
                with host._store.transaction() as conn:
                    conn.execute("PRAGMA ignore_check_constraints=ON")
                    for index, old_id in enumerate(identities):
                        new_id = "collision-id-" + "长标识/ ?%#" * 12 + str(index)
                        conn.execute(
                            "UPDATE runs SET run_id=?,purpose=? WHERE run_id=?",
                            (
                                new_id,
                                "future-purpose" if index == 0 else "chat",
                                old_id,
                            ),
                        )
                    conn.commit()
                    conn.execute("PRAGMA ignore_check_constraints=OFF")
            elif command == "short-ttl":
                host._accounting.ttl = 2
            elif command == "hold-recording":
                saving.clear()
                release.clear()
            elif command == "wait-recording":
                assert saving.wait(10)
            elif command == "release-recording":
                release.set()
            elif command == "retain-ops":
                host.accounting_snapshot({"range": "all", "timezone": "UTC"})
                retained = dict(host._accounting.snapshots)
            elif command == "check-ops":
                assert retained is not None
                assert host._accounting.snapshots == retained
                with host._store.transaction() as conn:
                    assert not conn.in_transaction
            elif command == "prune":
                from agent_alfred.schema import record_trace_prune

                with host._store.transaction() as conn:
                    for run_id in identities:
                        record_trace_prune(
                            conn,
                            run_id=run_id,
                            prune_requested_at=clock.wall_utc().isoformat(),
                            absence_confirmed_at=clock.wall_utc().isoformat(),
                            prune_reason="manual",
                        )
                    conn.commit()
            else:
                raise ValueError(command)
            print("ok " + command, flush=True)
    finally:
        release.set()
        assert dashboard.close(), "Dashboard did not drain"


if __name__ == "__main__":
    main()
