"""P4 process termination and the specification's a-p public-path sample."""

import json
import sqlite3
import subprocess
import sys

from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic.test_message_routing import enable
from agent_alfred.evals.deterministic.test_routing_statistics import read
from agent_alfred.evals.deterministic.test_runtime_skills import SKIP, runtime, submit
from agent_alfred.gateway.web.api import DashboardApi
from agent_alfred.messages import ToolCallBlock
from agent_alfred.model import (
    AttemptRecord,
    ModelRef,
    ModelResponse,
    ModelResult,
    Usage,
)
from agent_alfred.runtime.host import SubmitRequest
from agent_alfred.runtime.routing import build_routing_graph, project_context


def crash(path, phase):
    # A separate OS process performs genuine admission/execution. _exit models
    # a hard exit without allowing Python finally/Host.close to settle results.
    script = r"""
import os, sys, threading
from pathlib import Path
from agent_alfred.evals.deterministic.test_runtime_skills import runtime, SKIP
from agent_alfred.evals.deterministic.test_message_routing import enable
from agent_alfred.runtime.host import SubmitRequest
pending = threading.Event()
def observe(snapshot):
    if snapshot.coordinator_state == "recording_pending": pending.set()
options = dict(publish_work=lambda item: None) if sys.argv[2] == "admitted" else dict(
    before_recording_commit=threading.Event(), snapshot_listener=observe)
with runtime(Path(sys.argv[1]), [SKIP, "no_reply"], **options) as (host, model, sink):
    if not host.behaviour()["enabled"]: enable(host)
    accepted = host.submit(SubmitRequest("不用回复"))
    assert accepted.kind == "accepted"
    if sys.argv[2] != "admitted": assert pending.wait(5)
    print(accepted.run_id, flush=True)
    os._exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path), phase],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def prepare_fixed_sample(tmp_path):
    clock = FakeClock()
    tool = ModelResult(
        (AttemptRecord("save-once", False, "committed", Usage()),),
        ModelResponse(
            (
                ToolCallBlock(
                    "save-one", "save_fact", {"subject": "user", "fact": "likes tea"}
                ),
            ),
            "tool_use",
            ModelRef("test", "m"),
        ),
        None,
    )
    # a,b,c,d,e,g; real Registry write in c, external model failure in c/d/g.
    script = [
        SKIP,
        "greeting",
        SKIP,
        "full",
        "answer",
        SKIP,
        "full",
        tool,
        RuntimeError("after write"),
        SKIP,
        "full",
        RuntimeError("answer failure"),
        "fallback answer",
        SKIP,
        "unrecognized",
        "answer",
        SKIP,
        RuntimeError("classifier failure"),
        "fallback answer",
    ]
    with runtime(tmp_path, script, clock=clock) as (host, _, _):
        enable(host)
        ids = [
            submit(host, message)[0].run_id
            for message in ("你好", "b", "save tea", "d", "e", "g")
        ]
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            assert conn.execute("SELECT count(*) FROM facts").fetchone()[0] == 1
            assert (
                conn.execute(
                    "SELECT count(*) FROM tool_ledger WHERE run_id=?", (ids[2],)
                ).fetchone()[0]
                == 1
            )

    # f: real recovery commit and NoAction.
    def recovered(tools):
        return build_routing_graph(tools, projection=lambda s: project_context({}))

    with runtime(
        tmp_path, [SKIP, "no_reply"], clock=clock, routing_graph_builder=recovered
    ) as (host, _, _):
        submit(host, "不用回复")

    # h: configuration is enabled but actual startup graph build fails.
    def unavailable(tools):
        raise ValueError("controlled graph build fault")

    with runtime(
        tmp_path, [SKIP, "answer"], clock=clock, routing_graph_builder=unavailable
    ) as (host, _, _):
        submit(host, "h")
    # i/j: physical processes exit after admission / produced result.
    crash(tmp_path, "admitted")
    crash(tmp_path, "result")
    with runtime(tmp_path, [SKIP, "ordinary"], clock=clock) as (host, _, _):
        api = DashboardApi(facade=host)
        state = api.behaviour()[1]
        assert (
            api.mutate_behaviour(
                dict(action="save", enabled=False, expected_revision=state["revision"])
            )[0]
            == 200
        )
        submit(host, "l")
    # m: an actual malformed configuration, not a forged statistics sample.
    (tmp_path / "state/behaviour.json").write_text("malformed")
    with runtime(tmp_path, [SKIP, "ordinary"], clock=clock) as (host, _, _):
        submit(host, "m")
        state = host.behaviour()
        assert (
            DashboardApi(facade=host).mutate_behaviour(
                dict(
                    action="recover",
                    expected_revision=state["revision"],
                    fingerprint=state["fingerprint"],
                )
            )[0]
            == 200
        )
        enable(host)

    # n: a real rejected handoff; o/p: actual other-purpose admissions.
    def reject(item):
        raise OSError("handoff failure")

    with runtime(tmp_path, [], clock=clock, publish_work=reject) as (host, _, _):
        assert host.submit(SubmitRequest("n")).kind == "handoff_failed"
    with runtime(tmp_path, ["probe"], clock=clock) as (host, _, _):
        accepted = host.aggregate(
            session_id=host.create_session(), goal="o", keywords="", sources=()
        )
        assert host.wait(accepted.run_id).outcome == "completed"
        accepted = host.submit(
            SubmitRequest(
                "p", purpose="inference_probe", endpoint_id="test", model_id="m"
            )
        )
        assert accepted.kind == "accepted"
        host.wait(accepted.run_id)
    return clock


def test_ce16_fixed_a_to_p_counts_from_real_runs_and_process_recovery(tmp_path):
    clock = prepare_fixed_sample(tmp_path)
    # k: admitted, not executed; holds the normal lease until Host.close.
    with runtime(tmp_path, [], clock=clock, publish_work=lambda item: None) as (
        host,
        _,
        _,
    ):
        assert host.submit(SubmitRequest("k")).kind == "accepted"
        data = read(host, clock)
        assert data["sample"] == dict(
            admitted=13,
            enabled=11,
            disabled=1,
            unknown=1,
            finished=10,
            pending=1,
            unknown_bypass=1,
        )
        group = data["groups"][0]
        assert group["total"] == 10
        assert group["decisions"]["counts"] == dict(
            quick=1, full=3, fallback=1, no_action=1, context_failure=0
        )
        assert (
            group["decisions"]["known"],
            group["decisions"]["none"],
            group["decisions"]["unknown"],
        ) == (6, 2, 2)
        assert group["decisions"]["ratios"]["full"]["value"] == 0.5
        assert group["decisions"]["coverage"]["value"] == 0.6
        assert group["fallback"]["rate"] == dict(numerator=2, denominator=8, value=0.25)
        assert group["recovered"]["rate"] == dict(
            numerator=1, denominator=8, value=0.125
        )
        assert group["fallback"]["coverage"]["value"] == 0.8
        assert group["bypass"]["yes"] == 1 and group["bypass"]["unknown"] == 2
        assert group["blocked"] == dict(total=1, reasons={"side_effect_occurred": 1})
        assert read(host, clock)["sample"] == data["sample"]
        assert (
            DashboardApi(facade=host).mutate_behaviour(
                dict(
                    action="save",
                    enabled=False,
                    expected_revision=host.behaviour()["revision"],
                )
            )[0]
            == 409
        )


def test_ce03_real_trace_deletion_and_write_failure_do_not_change_statistics(tmp_path):
    import shutil

    from agent_alfred import schema
    from agent_alfred.managed_state import ManagedStateDirectory
    from agent_alfred.trace import RunBundleTraceSink

    clock = FakeClock()
    traces = tmp_path / "traces"
    root = ManagedStateDirectory.acquire_trace_root(traces)
    trace = RunBundleTraceSink(
        root=root, clock=clock, process_instance_id="stats-trace"
    )
    recovered_mode = [False]

    def build(tools):
        def projection(snapshot):
            return project_context({} if recovered_mode[0] else snapshot)

        return build_routing_graph(tools, projection=projection)

    script = [
        SKIP,
        "greeting",
        SKIP,
        "no_reply",
        SKIP,
        "full",
        RuntimeError("node failure"),
        "fallback",
        SKIP,
        RuntimeError("classifier failure"),
        "fallback",
    ]
    with runtime(
        tmp_path, script, clock=clock, extra_sinks=(trace,), routing_graph_builder=build
    ) as (host, _, _):
        enable(host)
        ids = [submit(host, "你好")[0].run_id]
        recovered_mode[0] = True
        ids.append(submit(host, "不用回复")[0].run_id)
        recovered_mode[0] = False
        ids.append(submit(host, "full")[0].run_id)
        data = read(host, clock)
        files = list(traces.rglob("trace.jsonl"))
        assert len(files) == 3
        # Real manual trace pruning: delete sealed bundles and record absence
        # through the existing public persistence contract (ADR-0020).
        for file in files:
            shutil.rmtree(file.parent)
        assert not list(traces.rglob("trace.jsonl"))
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            for run_id in ids:
                schema.record_trace_prune(
                    conn,
                    run_id=run_id,
                    prune_requested_at=clock.wall.isoformat(),
                    absence_confirmed_at=clock.wall.isoformat(),
                    prune_reason="manual",
                )
        assert read(host, clock)["groups"] == data["groups"]
        original = trace._write_item

        def fail_notice(item):
            if getattr(item.event.payload, "code", None) == "routing_fallback":
                raise OSError("controlled trace IO failure")
            original(item)

        trace._write_item = fail_notice
        _, result = submit(host, "trace failure")
        assert result.outcome == "completed"
        final = read(host, clock)
        assert final["groups"][0]["fallback"]["yes"] == 2
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            assert any(
                json.loads(r[0])["trace_incomplete"]
                for r in conn.execute("SELECT telemetry FROM runs")
            )
    with runtime(tmp_path, [], clock=clock) as (host, model, _):
        restored = read(host, clock)
        assert restored["groups"] == final["groups"]
        assert restored["sample"] == final["sample"]
        assert model.requests == []
