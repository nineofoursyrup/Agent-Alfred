"""#81 real Graph/Host/trace path contract at the public HTTP seam."""

import json
import threading
from contextlib import contextmanager

import pytest

from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
    free_loopback_port,
)
from agent_alfred.evals.deterministic.test_behaviour_topology import get
from agent_alfred.evals.deterministic.test_message_routing import enable
from agent_alfred.evals.deterministic.test_runtime_skills import SKIP, submit
from agent_alfred.events import CapturingSink
from agent_alfred.graph import GraphBuilder, NodeOutcome, TerminalSpec, fn_node
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.host import SubmitRequest
from agent_alfred.wiring import build_dashboard


def test_ce10_real_routing_saved_path_contains_bound_topology_and_decisions(tmp_path):
    port = free_loopback_port()
    dashboard = build_dashboard(
        state_dir=tmp_path,
        port=port,
        factory=ScriptedModelFactory(ScriptedModel([SKIP, "full", "answer"])),
    )
    dashboard.start()
    try:
        enable(dashboard.host)
        accepted, result = submit(dashboard.host, "explain a graph")
        assert result.outcome == "completed"
        status, body = get(
            f"http://127.0.0.1:{port}", "/api/run-path?run_id=" + accepted.run_id
        )
        assert status == 200, body
        assert body["status"] == "available", body
        assert body["source"] == "trace"
        assert body["identity"]["graph_id"] == "message_routing"
        assert (
            body["identity"]["process_instance_id"]
            == dashboard.host.process_instance_id
        )
        assert len(body["description"]["topology"]["nodes"]) == 9
        assert len(body["edges"]) == 10
        assert all(e["state"] in ("taken", "not_taken") for e in body["edges"])
        assert all(w["state"] == "committed" for w in body["waves"])
        assert body["graph_outcome"] == "Completed"
        assert body["run"]["outcome"] == "completed"
        assert body["run"]["recording_state"] == "recorded"
    finally:
        assert dashboard.close()


def test_ce10_disabled_routing_proves_bypass_without_a_graph(tmp_path):
    port = free_loopback_port()
    dashboard = build_dashboard(
        state_dir=tmp_path,
        port=port,
        factory=ScriptedModelFactory(ScriptedModel([SKIP, "answer"])),
    )
    dashboard.start()
    try:
        accepted, _ = submit(dashboard.host, "ordinary loop")
        _, path = get(
            f"http://127.0.0.1:{port}", "/api/run-path?run_id=" + accepted.run_id
        )
        assert path["reason"] == "graph_bypassed"
        assert path["stages"] == [
            dict(stage="bypass", reason="disabled_by_config", entered=True)
        ]
        assert path["description"] is None
    finally:
        assert dashboard.close()


@contextmanager
def dashboard_at(path, *, script=None, factory=None, **options):
    port = free_loopback_port()
    dashboard = build_dashboard(
        state_dir=path,
        port=port,
        factory=factory
        or ScriptedModelFactory(ScriptedModel(script or [SKIP, "full", "answer"])),
        **options,
    )
    dashboard.start()
    try:
        yield dashboard, f"http://127.0.0.1:{port}"
    finally:
        assert dashboard.close()


def path_get(origin, run):
    status, body = get(origin, "/api/run-path?run_id=" + run)
    assert status == 200, body
    return body


class PublicationBarrier(CapturingSink):
    def __init__(self, name="path.wave", wave=0):
        super().__init__(name="path-test-barrier")
        self.target, self.wave = name, wave
        self.entered, self.release = threading.Event(), threading.Event()

    def prepare(self, event):
        if (
            event.payload.name == self.target
            and getattr(event.payload, "wave", 0) == self.wave
        ):
            self.entered.set()
            assert self.release.wait(10), "publication barrier was not released"
        return None


def test_ce04_atomic_wave_fact_is_required_before_commit_claim(tmp_path):
    barrier = PublicationBarrier()
    with dashboard_at(tmp_path, extra_sinks=(barrier,)) as (dashboard, origin):
        enable(dashboard.host)
        accepted = dashboard.host.submit(SubmitRequest("explain", wait_for_result=True))
        assert barrier.entered.wait(10)
        try:
            before = path_get(origin, accepted.run_id)
            assert before["source"] == "process"
            assert before["waves"][0]["state"] == "unknown"
            first = {n["node_id"]: n for n in before["nodes"]}
            assert first["classify"]["state"] == "succeeded"
            assert first["classify"]["commit"] == "pending"
            assert first["project_context"]["commit"] == "pending"
            assert all(e["state"] == "undecided" for e in before["edges"])
            # The original active evidence interface retains its existing contract.
            _, old = get(origin, "/api/run-evidence?run_id=" + accepted.run_id)
            assert old["trace_status"] == "live" and old["events"] == []
        finally:
            barrier.release.set()
        assert dashboard.host.wait(accepted.run_id).outcome == "completed"
        after = path_get(origin, accepted.run_id)
        assert after["waves"][0]["state"] == "committed"
        trace = next(tmp_path.rglob("trace.jsonl"))
        original = trace.read_bytes()
        events = original.splitlines(keepends=True)
        cut = next(
            i
            for i, e in enumerate(events)
            if json.loads(e)["payload_name"] == "path.wave"
        )
        trace.write_bytes(b"".join(events[:cut]))
        prefix = path_get(origin, accepted.run_id)
        assert prefix["status"] == "partial"
        assert prefix["waves"][0]["state"] == "unknown"
        assert all(e["state"] == "unknown" for e in prefix["edges"])
        trace.write_bytes(original)


def test_ce10_real_aggregation_success_and_source_skips(tmp_path):
    from agent_alfred.evals.deterministic.test_aggregation import save_fact

    with dashboard_at(tmp_path, script=["draft [[S1]]"]) as (dashboard, origin):
        host = dashboard.host
        save_fact(host)
        accepted = host.aggregate(
            session_id=host.create_session(),
            goal="coffee",
            keywords="coffee",
            sources=("semantic",),
        )
        assert host.wait(accepted.run_id).outcome == "completed"
        path = path_get(origin, accepted.run_id)
        assert path["status"] == "available", path
        assert path["identity"]["graph_id"] == "manual_aggregation"
        assert len(path["nodes"]) == 15 and len(path["edges"]) == 20
        nodes = {n["node_id"]: n for n in path["nodes"]}
        assert nodes["semantic_source"]["state"] == "succeeded"
        assert nodes["episodic_source"]["reason"] == "disabled_by_config"
        assert nodes["history_source"]["reason"] == "disabled_by_config"
        assert all(e["state"] in ("taken", "not_taken") for e in path["edges"])


@pytest.mark.parametrize(
    "case,expected",
    [
        ("missing", "missing"),
        ("bad_line", "corrupt"),
        ("wrong_run", "corrupt"),
        ("wrong_process", "corrupt"),
        ("seq", "corrupt"),
        ("version", "unsupported"),
        ("structure_version", "unsupported"),
        ("legacy", "no_historical_structure"),
        ("limit", "too_large"),
        ("order", "corrupt"),
    ],
)
def test_ce07_storage_protocol_damage_never_fabricates_path(tmp_path, case, expected):
    with dashboard_at(tmp_path) as (dashboard, origin):
        enable(dashboard.host)
        accepted, _ = submit(dashboard.host, "explain")
        trace = next(tmp_path.rglob("trace.jsonl"))
        original = trace.read_bytes()
        events = [json.loads(line) for line in original.splitlines()]
        if case == "missing":
            trace.unlink()
        elif case == "bad_line":
            trace.write_bytes(
                b"\n".join(original.splitlines()[:3]) + b"\nnot-json\n" + original
            )
        elif case == "limit":
            with trace.open("wb") as output:
                output.truncate(32 * 1024 * 1024 + 1)
        else:
            capture = next(e for e in events if e["payload_name"] == "path.captured")
            if case == "wrong_run":
                capture["run_id"] = "another-run"
            elif case == "wrong_process":
                capture["process_instance_id"] = "another-process"
            elif case == "seq":
                capture["seq"] = 0
            elif case == "version":
                capture["payload"]["evidence_version"] = 999
            elif case == "structure_version":
                capture["payload"]["description"]["schema_version"] = 999
            elif case == "legacy":
                events.remove(capture)
            elif case == "order":
                event = next(e for e in events if e["payload_name"] == "node.started")
                event["payload"]["name"] = "node.finished"
                event["payload"]["outcome"] = "succeeded"
            trace.write_text("".join(json.dumps(e) + "\n" for e in events))
        damaged = path_get(origin, accepted.run_id)
        assert damaged["status"] == "unavailable", damaged
        assert damaged["reason"] == expected
        assert damaged["description"] is None and damaged["nodes"] == []
        assert damaged["run"]["outcome"] == "completed"
        trace.write_bytes(original)
        assert path_get(origin, accepted.run_id)["status"] == "available"


def public_business_account(origin, run_id, result):
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen

    from agent_alfred.messages import message_plain_text

    entry = get(origin, "/api/entry")[1]
    request = Request(
        origin + "/api/ops/snapshots",
        data=json.dumps(
            {
                "range": "7d",
                "timezone": "UTC",
                "run_id": run_id,
            }
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "Origin": origin,
            "X-Agent-Alfred-CSRF": entry["csrf_token"],
        },
    )
    with urlopen(request, timeout=5) as response:
        snapshot = json.load(response)
    status, detail = get(
        origin,
        "/api/ops/detail?"
        + urlencode(
            {
                "snapshot_id": snapshot["snapshot_id"],
                "run_id": run_id,
            }
        ),
    )
    assert status == 200, detail
    return dict(
        outcome=result.outcome,
        reply=message_plain_text(result.reply) if result.reply is not None else None,
        steps=result.step_count,
        attempts=[
            dict(
                outcome=a["outcome"],
                usage=a["usage"],
                cost=a["cost"],
            )
            for a in detail["run"]["attempts"]
        ],
        tools=detail["run"]["tools"],
    )


def baseline_business_account(path):
    with dashboard_at(path) as (dashboard, origin):
        enable(dashboard.host)
        accepted, result = submit(dashboard.host, "explain")
        return public_business_account(origin, accepted.run_id, result)


@pytest.mark.parametrize("failure", ["prepare", "commit", "capacity"])
def test_ce09_observer_loss_is_not_a_complete_current_prefix(
    tmp_path, monkeypatch, failure
):
    from agent_alfred.runtime.run_path import RunPathSink

    baseline = baseline_business_account(tmp_path / "baseline")

    class RecordBarrier:
        def __init__(self):
            self.entered, self.release = threading.Event(), threading.Event()

        def wait(self):
            self.entered.set()
            assert self.release.wait(10)

    barrier = RecordBarrier()
    if failure == "capacity":
        original = RunPathSink.__init__
        monkeypatch.setattr(
            RunPathSink, "__init__", lambda self: original(self, max_events=4)
        )
    else:
        original = getattr(RunPathSink, failure)

        def fail(self, *args):
            event = args[-1]
            if event.payload.name == "node.started":
                raise ValueError("controlled observer fault")
            return original(self, *args)

        monkeypatch.setattr(RunPathSink, failure, fail)
    with dashboard_at(tmp_path, before_recording_commit=barrier) as (dashboard, origin):
        enable(dashboard.host)
        accepted = dashboard.host.submit(SubmitRequest("explain", wait_for_result=True))
        assert barrier.entered.wait(10)
        try:
            path = path_get(origin, accepted.run_id)
            assert path["status"] == "unavailable", path
            assert path["reason"] == (
                "current_limit" if failure == "capacity" else "observation_failed"
            )
            assert not path["nodes"]
            assert path["run"]["outcome"] == "completed"
            assert path["run"]["recording_state"] == "pending"
        finally:
            barrier.release.set()
        result = dashboard.host.wait(accepted.run_id)
        assert result.outcome == "completed"
        assert result.step_count == 3
        # Observer failure cannot corrupt a separately successful retained trace.
        saved = path_get(origin, accepted.run_id)
        assert saved["status"] == "available", saved
        assert saved["graph_outcome"] == "Completed"
        assert public_business_account(origin, accepted.run_id, result) == baseline


@pytest.mark.parametrize("mode", ["node", "writes", "router"])
def test_ce02_prior_wave_and_incurred_attempt_survive_atomic_abort(tmp_path, mode):
    from agent_alfred.graph import llm_node

    def build(tools):
        b = GraphBuilder("message_routing")
        for key in ("task", "prepared_context", "has_loaded_skills"):
            b.declare_input(key)
        b.add_node(
            "first",
            llm_node(lambda s: "first model", binding="answer", output_key="first"),
            writes=("first",),
        )
        b.add_node("a", fn_node(lambda s, c: NodeOutcome({"a": 1})), writes=("a",))

        def second(s, c):
            if mode == "node":
                raise ValueError("second node failure")
            return NodeOutcome({"undeclared" if mode == "writes" else "b": 1})

        b.add_node("b", fn_node(second), writes=("b",))
        b.add_node(
            "last",
            fn_node(lambda s, c: NodeOutcome()),
            terminal=TerminalSpec.no_action("done"),
        )
        b.add_edge("first", "a")
        b.add_edge("first", "b")
        b.add_edge("a", "last")
        if mode == "router":
            b.add_conditional_edges("b", lambda s: "invalid", {"valid": "last"})
        else:
            b.add_edge("b", "last")
        return b.compile()

    with dashboard_at(
        tmp_path,
        script=[SKIP, "first result", "fallback answer"],
        routing_graph_builder=build,
    ) as (dashboard, origin):
        enable(dashboard.host)
        accepted, result = submit(dashboard.host, "task")
        path = path_get(origin, accepted.run_id)
        assert path["status"] == "available", path
        assert path["graph_outcome"] == "Failed"
        assert path["run"]["outcome"] == "completed"
        nodes = {n["node_id"]: n for n in path["nodes"]}
        assert nodes["first"]["commit"] == "committed"
        assert nodes["a"]["state"] == "succeeded"
        assert nodes["a"]["commit"] == "aborted"
        assert nodes["b"]["state"] == ("failed" if mode == "node" else "succeeded")
        assert nodes["b"]["commit"] == "aborted"
        assert nodes["last"]["state"] == "not_started"
        assert [w["state"] for w in path["waves"]] == [
            "committed",
            "aborted",
            "unknown",
        ]
        assert path["stages"] == [
            dict(stage="fallback", reason="graph_failed", entered=True)
        ]
        assert result.step_count == 3  # gate, real W1 model, ordinary fallback
        assert len(result.model_results) == 3


@pytest.mark.parametrize("route", ["full", "unknown", "no_reply"])
def test_ce10_routing_labels_and_no_action_are_real_facts(tmp_path, route):
    with dashboard_at(tmp_path, script=[SKIP, route, "answer"]) as (dashboard, origin):
        enable(dashboard.host)
        accepted, result = submit(
            dashboard.host, "不用回复" if route == "no_reply" else "explain"
        )
        path = path_get(origin, accepted.run_id)
        expected = (
            "fallback"
            if route == "unknown"
            else "no_action"
            if route == "no_reply"
            else route
        )
        selected = [
            e["label"]
            for e in path["edges"]
            if e["source"] == "join_route" and e["state"] == "taken"
        ]
        assert selected == [expected]
        assert path["stages"] == []
        if route == "no_reply":
            assert path["graph_outcome"] == "NoAction" and path["graph_reason"]
            assert result.reply is None


def test_ce10_context_error_recovery_preserves_failure(tmp_path):
    from agent_alfred.runtime.routing import build_routing_graph

    def build(tools):
        def broken(snapshot):
            raise ValueError("controlled projection failure")

        return build_routing_graph(tools, projection=broken)

    with dashboard_at(
        tmp_path, script=[SKIP, "greeting"], routing_graph_builder=build
    ) as (dashboard, origin):
        enable(dashboard.host)
        accepted, _ = submit(dashboard.host, "你好")
        path = path_get(origin, accepted.run_id)
        assert path["status"] == "available", path
        nodes = {n["node_id"]: n for n in path["nodes"]}
        assert nodes["project_context"]["state"] == "failed"
        assert nodes["recover_context"]["state"] == "succeeded"
        assert (
            next(e for e in path["edges"] if e["kind"] == "error")["state"] == "taken"
        )
        assert path["graph_outcome"] == "CompletedWithRecovery"


def test_ce05_real_side_effect_blocks_fallback_without_erasing_graph_failure(tmp_path):
    from agent_alfred.messages import ToolCallBlock
    from agent_alfred.model import (
        AttemptRecord,
        ModelRef,
        ModelResponse,
        ModelResult,
        Usage,
    )

    response = ModelResult(
        (AttemptRecord("tool-attempt", False, "committed", Usage()),),
        ModelResponse(
            (
                ToolCallBlock(
                    "save-one", "save_fact", {"subject": "user", "fact": "tea"}
                ),
            ),
            "tool_use",
            ModelRef("test", "m"),
        ),
        None,
    )
    with dashboard_at(
        tmp_path, script=[SKIP, "full", response, RuntimeError("after save")]
    ) as (dashboard, origin):
        enable(dashboard.host)
        accepted, result = submit(dashboard.host, "save tea")
        path = path_get(origin, accepted.run_id)
        assert result.outcome == "failed"
        assert path["graph_outcome"] == "Failed"
        assert path["stages"] == [
            dict(stage="fallback", reason="side_effect_occurred", entered=False)
        ]
        assert path["run"]["outcome"] == "failed"
        assert result.step_count == 4
        # A retained prefix may prove the blocked stage before Run finalization.
        trace = next(tmp_path.rglob("trace.jsonl"))
        original = trace.read_bytes()
        events = [json.loads(line) for line in original.splitlines()]
        index = next(
            i for i, event in enumerate(events) if event["payload_name"] == "path.stage"
        )
        trace.write_text("".join(json.dumps(e) + "\n" for e in events[: index + 1]))
        prefix = path_get(origin, accepted.run_id)
        assert prefix["status"] == "partial" and prefix["stages"] == path["stages"]
        # Blocked means no later Step/Attempt/tool may start either.
        import copy

        later = copy.deepcopy(
            next(e for e in events if e["payload_name"] == "step.started")
        )
        later["node_id"] = None
        forged = events[: index + 1] + [later] + events[index + 1 :]
        for seq, event in enumerate(forged, 1):
            event["seq"] = seq
        trace.write_text("".join(json.dumps(e) + "\n" for e in forged))
        assert path_get(origin, accepted.run_id)["reason"] == "corrupt"
        # An explicit completed Run fact contradicts this real side-effect block.
        trace.write_bytes(forbidden_fallback_trace(original))
        assert path_get(origin, accepted.run_id)["reason"] == "corrupt"
        trace.write_bytes(contradictory_stage_trace(original, "blocked_run_completed"))
        bad = path_get(origin, accepted.run_id)
        assert bad["reason"] == "corrupt" and not bad["stages"]
        assert bad["summary"] == path["summary"] and bad["run"] == path["run"]
        trace.write_bytes(original)
        assert path_get(origin, accepted.run_id)["stages"] == path["stages"]


def test_ce07_conflicting_duplicate_edge_is_corrupt(tmp_path):
    with dashboard_at(tmp_path) as (dashboard, origin):
        enable(dashboard.host)
        accepted, _ = submit(dashboard.host, "explain")
        trace = next(tmp_path.rglob("trace.jsonl"))
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        wave = next(e["payload"] for e in events if e["payload_name"] == "path.wave")
        wave["edges"].append(
            dict(
                wave["edges"][0],
                state="not_taken" if wave["edges"][0]["state"] == "taken" else "taken",
            )
        )
        trace.write_text("".join(json.dumps(e) + "\n" for e in events))
        path = path_get(origin, accepted.run_id)
        assert path["status"] == "unavailable" and path["reason"] == "corrupt"
        assert path["nodes"] == []


@pytest.mark.parametrize("mode", ["complete", "prefix", "missing", "recording_failed"])
def test_ce06_real_process_death_never_recovers_memory_as_durable(tmp_path, mode):
    import queue
    import subprocess
    import sys
    from urllib.request import Request, urlopen

    script = (
        __import__("pathlib").Path(__file__).resolve().parents[4]
        / "tests/browser/run_path_server.py"
    )
    port = free_loopback_port()
    origin = f"http://127.0.0.1:{port}"

    def start():
        process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                str(script),
                "--state",
                str(tmp_path),
                "--port",
                str(port),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        lines = queue.Queue()

        def read():
            for line in process.stdout:
                lines.put(line.strip())

        threading.Thread(target=read, daemon=True).start()
        assert lines.get(timeout=15) == "ready"
        return process, lines

    def command(process, lines, value):
        process.stdin.write(value + "\n")
        process.stdin.flush()
        while lines.get(timeout=10) != "ok " + value:
            pass

    def post(path, body):
        token = get(origin, "/api/entry")[1]["csrf_token"]
        with urlopen(
            Request(
                origin + path,
                data=json.dumps(body).encode(),
                headers={
                    "Content-Type": "application/json",
                    "x-agent-alfred-csrf": token,
                },
            ),
            timeout=10,
        ) as response:
            return json.load(response)

    process, lines = start()
    run = None
    try:
        settings = get(origin, "/api/behaviour")[1]
        post(
            "/api/behaviour",
            dict(action="save", expected_revision=settings["revision"], enabled=True),
        )
        command(process, lines, "hold-recording")
        if mode in ("prefix", "missing"):
            command(process, lines, "hold-trace " + mode)
        if mode == "recording_failed":
            command(process, lines, "fail-recording")
        session = post("/api/sessions", {})["session_id"]
        run = post("/api/runs", dict(message="explain", session_id=session))["run_id"]
        wanted = (
            "entered trace" if mode in ("prefix", "missing") else "entered recording"
        )
        while lines.get(timeout=15) != wanted:
            pass
        before = path_get(origin, run)
        assert before["source"] == "process"
        original = before["service_instance_id"]
        if mode == "recording_failed":
            command(process, lines, "release-recording")
            # An explicit HTTP predicate is the persistence barrier; no timing sleep.
            from concurrent.futures import ThreadPoolExecutor

            def failed():
                for _ in range(200):
                    current = path_get(origin, run)
                    if current["run"]["recording_state"] == "failed":
                        return current
                raise AssertionError("record failure never became observable")

            with ThreadPoolExecutor(1) as pool:
                assert (
                    pool.submit(failed).result(timeout=10)["run"]["outcome"]
                    == "completed"
                )
            command(process, lines, "repair-recording")
        process.kill()
        process.wait(timeout=10)
        process, lines = start()
        after = path_get(origin, run)
        assert after["source"] == "trace"
        assert after["service_instance_id"] != original
        assert after["run"]["outcome"] == "interrupted"
        if mode in ("complete", "recording_failed"):
            assert after["status"] == "available", after
            assert after["identity"]["process_instance_id"] == original
            assert after["graph_outcome"] == "Completed"
            assert after["run"]["recording_state"] is None
        elif mode == "prefix":
            assert after["status"] == "partial", after
            assert after["waves"][0]["state"] == "unknown"
        else:
            assert after["status"] == "unavailable"
            assert after["reason"] == "no_historical_structure"
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


@pytest.mark.parametrize("failure", ["write", "fsync"])
def test_ce09_trace_io_failure_preserves_business_and_marks_durable_gap(
    tmp_path, monkeypatch, failure
):
    from agent_alfred.managed_state import ManagedFileLease

    baseline = baseline_business_account(tmp_path / "baseline")
    method = "write_all" if failure == "write" else "fsync"
    original = getattr(ManagedFileLease, method)
    fired = []

    def fail(lease, *args, **kwargs):
        if lease.path.name == "trace.jsonl":
            if (
                failure == "fsync"
                or json.loads(args[0]).get("payload_name") == "path.wave"
            ):
                fired.append(True)
                raise OSError(5, "controlled trace IO")
        return original(lease, *args, **kwargs)

    monkeypatch.setattr(ManagedFileLease, method, fail)
    with dashboard_at(tmp_path) as (dashboard, origin):
        enable(dashboard.host)
        accepted, result = submit(dashboard.host, "explain")
        assert fired
        assert result.outcome == "completed" and result.step_count == 3
        path = path_get(origin, accepted.run_id)
        assert path["trace_incomplete"] is True
        assert public_business_account(origin, accepted.run_id, result) == baseline
        assert path["run"]["outcome"] == "completed"
        if failure == "write":
            assert path["status"] == "partial"
            assert path["waves"][0]["state"] == "unknown"
        else:
            # A failed fsync makes no durability promise; bytes already readable
            # can still prove facts in the retained bundle.
            assert path["status"] in ("available", "unavailable", "partial")


@pytest.mark.parametrize("same_hash", [True, False])
def test_ce01_captured_generation_and_presentation_never_follow_republication(
    tmp_path, same_hash
):
    from dataclasses import replace

    from agent_alfred.graph.types import freeze
    from agent_alfred.runtime.routing import build_routing_graph

    generation = [0]

    def build(tools):
        graph = build_routing_graph(tools)
        if not generation[0]:
            return graph
        if same_hash:
            d = graph.describe()
            d["presentation"]["policy"] = "G2 changed explanation"
            return replace(graph, description=freeze(d))
        b = GraphBuilder("message_routing")
        for key in ("task", "prepared_context", "has_loaded_skills"):
            b.declare_input(key)
        b.add_node(
            "different",
            fn_node(lambda s, c: NodeOutcome()),
            terminal=TerminalSpec.no_action("different"),
        )
        return b.compile()

    barrier = PublicationBarrier()
    with dashboard_at(
        tmp_path, routing_graph_builder=build, extra_sinks=(barrier,)
    ) as (dashboard, origin):
        enable(dashboard.host)
        g1 = get(origin, "/api/behaviour/topology?workflow=message_routing")[1]
        accepted = dashboard.host.submit(SubmitRequest("explain"))
        assert barrier.entered.wait(10)
        try:
            generation[0] = 1
            # Declared Host publication seam, not a replacement describe/result.
            dashboard.host._publish_tools(dashboard.host._tools)
            g2 = get(origin, "/api/behaviour/topology?workflow=message_routing")[1]
            assert (
                g1["description"]["topology_hash"] == g2["description"]["topology_hash"]
            ) is same_hash
            current = path_get(origin, accepted.run_id)
            assert current["description"] == g1["description"]
            assert (
                current["identity"]["publication_generation"]
                == g1["publication_generation"]
            )
        finally:
            barrier.release.set()
        dashboard.host.wait(accepted.run_id)
        saved = path_get(origin, accepted.run_id)
        assert saved["description"] == g1["description"]
        assert (
            saved["identity"]["publication_generation"] < g2["publication_generation"]
        )


def test_ce07_managed_prune_fact_keeps_run_and_accounting(tmp_path):
    import shutil
    import sqlite3

    from agent_alfred import schema

    with dashboard_at(tmp_path) as (dashboard, origin):
        enable(dashboard.host)
        accepted, _ = submit(dashboard.host, "explain")
        before = path_get(origin, accepted.run_id)
        trace = next(tmp_path.rglob("trace.jsonl"))
        shutil.rmtree(trace.parent)
        with sqlite3.connect(tmp_path / "db.sqlite3") as conn:
            schema.record_trace_prune(
                conn,
                run_id=accepted.run_id,
                prune_reason="manual",
                prune_requested_at="2026-09-20T00:00:00Z",
                absence_confirmed_at="2026-09-20T00:00:01Z",
            )
        path = path_get(origin, accepted.run_id)
        assert path["reason"] == "pruned" and path["status"] == "unavailable"
        assert not path["nodes"] and path["description"] is None
        assert path["run"] == before["run"] and path["summary"] == before["summary"]


def test_ce09_fixed_capacity_exact_boundary_and_no_message_copy():
    from agent_alfred.events import EventEnvelope, FanOutSink, RunStarted
    from agent_alfred.messages import text_message
    from agent_alfred.runtime.run_path import MAX_PATH_EVENTS, RunPathSink

    sink = RunPathSink()
    fanout = FanOutSink([sink], process_instance_id="capacity-process")
    envelope = EventEnvelope(0, "bounded-run", None, None, None, None, "cli")
    payload = RunStarted(user_message=text_message("user", "private input " * 200000))
    # Only name and purpose are projected: a huge body cannot consume path capacity.
    fanout.emit(payload, envelope)
    state, events = sink.snapshot("bounded-run")
    assert state == "live" and events[0]["payload"] == {
        "name": "run.started",
        "purpose": "chat",
    }
    for _ in range(MAX_PATH_EVENTS - 1):
        fanout.emit(RunStarted(), envelope)
    assert sink.snapshot("bounded-run")[0] == "live"
    fanout.emit(RunStarted(), envelope)
    assert sink.snapshot("bounded-run") == ("current_limit", [])
    fanout.close()


def test_ce21_guarded_http_retains_existing_host_origin_boundary(tmp_path):
    with dashboard_at(tmp_path) as (dashboard, origin):
        accepted, _ = submit(dashboard.host, "task")
        path = "/api/run-path?run_id=" + accepted.run_id
        assert get(origin, path, Origin="https://untrusted.example")[0] == 403
        assert get(origin, path, Host="untrusted.example")[0] == 400
        assert get(origin, "/api/run-path")[0] == 400
        assert get(origin, "/api/run-path?run_id=unknown")[0] == 404


def test_ce09_byte_limit_at_boundary_and_overflow_are_explicit():
    from agent_alfred.events import EventEnvelope, FanOutSink, RunStarted
    from agent_alfred.runtime.run_path import RunPathSink

    size = len(json.dumps({"name": "run.started", "purpose": "chat"}).encode())
    sink = RunPathSink(max_bytes=size)
    fanout = FanOutSink([sink], process_instance_id="byte-limit")
    envelope = EventEnvelope(0, "bytes", None, None, None, None, "cli")
    fanout.emit(RunStarted(), envelope)
    assert sink.snapshot("bytes")[0] == "live"
    fanout.emit(RunStarted(), envelope)
    assert sink.snapshot("bytes") == ("current_limit", [])
    fanout.close()


def test_ac22_real_routing_trace_still_exports_with_new_path_facts(tmp_path):
    from agent_alfred.evals.deterministic.test_trace_export import contents

    with dashboard_at(tmp_path) as (dashboard, origin):
        enable(dashboard.host)
        accepted, _ = submit(dashboard.host, "private routing input")
        service = dashboard.host.trace_exports
        task = service.wait(service.start(accepted.run_id)["task_id"])
        assert task["state"] == "ready", task
        files = contents(service, task)
        exported = b"\n".join(files.values())
        assert b"path.captured" in exported and b"path.wave" in exported
        assert b"private routing input" not in exported
        assert accepted.run_id.encode() not in exported
        assert path_get(origin, accepted.run_id)["status"] == "available"


def test_ce05_real_routing_answer_failure_enters_ordinary_fallback(tmp_path):
    with dashboard_at(
        tmp_path, script=[SKIP, "full", RuntimeError("answer failed"), "recovered"]
    ) as (dashboard, origin):
        enable(dashboard.host)
        accepted, result = submit(dashboard.host, "task")
        path = path_get(origin, accepted.run_id)
        assert path["graph_outcome"] == "Failed"
        assert path["stages"] == [
            dict(stage="fallback", reason="graph_failed", entered=True)
        ]
        assert path["run"]["outcome"] == "completed"
        assert result.step_count == 4
        from agent_alfred.messages import message_plain_text

        assert message_plain_text(result.reply) == "recovered"


PROTOCOL_CONTRADICTIONS = (
    "future_started",
    "future_finished",
    "future_skipped",
    "future_aborted",
    "not_taken_target",
    "unknown_route_label",
    "run_finished_before_graph",
    "contradictory_not_started",
    "unknown_graph_outcome",
    "premature_wave_commit",
)


def contradictory_trace(original, case):
    """Only abnormal protocol tests use this; every source is a real saved Run."""
    import copy

    events = [json.loads(line) for line in original.splitlines()]
    start = next(
        i for i, e in enumerate(events) if e["payload_name"] == "graph.started"
    )
    finish = next(e for e in events if e["payload_name"] == "graph.finished")
    if case.startswith("future_"):
        pair = [
            e
            for e in events
            if e.get("node_id") == "join_route"
            and e["payload_name"] in ("node.started", "node.finished")
        ]
        if case == "future_started":
            events.remove(pair[0])
            events.insert(start + 1, pair[0])
        else:
            events = events[: start + 1]
            if case == "future_finished":
                events.extend(pair)
            elif case == "future_skipped":
                event = copy.deepcopy(pair[0])
                event["payload_name"] = event["payload"]["name"] = "node.skipped"
                event["payload"]["reason"] = "all_inbound_not_taken"
                events.append(event)
            else:
                event = copy.deepcopy(pair[1])
                event["payload_name"] = event["payload"]["name"] = "node.aborted"
                event["payload"].pop("outcome", None)
                event["payload"]["reason"] = "node_failed"
                events.extend([pair[0], event])
    elif case == "unknown_route_label":
        index = next(
            i
            for i, e in enumerate(events)
            if e["payload_name"] == "node.finished"
            and e["payload"].get("route_label") is not None
        )
        events[index]["payload"]["route_label"] = "invented_route"
        events = events[: index + 1]
    elif case == "not_taken_target":
        wave = next(
            e
            for e in events
            if e["payload_name"] == "path.wave"
            and any(x["source"] == "full_agent" for x in e["payload"]["edges"])
        )
        edge = next(e for e in wave["payload"]["edges"] if e["target"] == "full_result")
        edge["state"] = "not_taken"
    elif case == "run_finished_before_graph":
        events.insert(
            start,
            copy.deepcopy(
                next(e for e in events if e["payload_name"] == "run.finished")
            ),
        )
    elif case == "contradictory_not_started":
        finish["payload"]["not_started"] = ["classify"]
    elif case == "unknown_graph_outcome":
        finish["payload"]["outcome"] = "invented_success"
    elif case == "premature_wave_commit":
        wave = next(e for e in events if e["payload_name"] == "path.wave")
        events.remove(wave)
        index = next(
            i for i, e in enumerate(events) if e["payload_name"] == "node.finished"
        )
        for fact in wave["payload"]["nodes"]:
            fact["state"] = "started"
        events.insert(index, wave)
    else:
        raise AssertionError(case)
    for seq, event in enumerate(events, 1):
        event["seq"] = seq
    return "".join(json.dumps(e) + "\n" for e in events).encode()


@pytest.mark.parametrize("case", PROTOCOL_CONTRADICTIONS)
def test_c2_review_protocol_contradictions_reject_and_restore_without_execution(
    tmp_path, case
):
    with dashboard_at(tmp_path) as (dashboard, origin):
        enable(dashboard.host)
        accepted, result = submit(dashboard.host, "explain")
        baseline = path_get(origin, accepted.run_id)
        account = public_business_account(origin, accepted.run_id, result)
        trace = next(tmp_path.rglob("trace.jsonl"))
        original = trace.read_bytes()
        trace.write_bytes(contradictory_trace(original, case))
        bad = path_get(origin, accepted.run_id)
        assert bad["status"] == "unavailable", bad
        assert bad["reason"] == "corrupt"
        assert bad["description"] is None and not bad["nodes"]
        assert bad["summary"] == baseline["summary"] and bad["run"] == baseline["run"]
        trace.write_bytes(original)
        restored = path_get(origin, accepted.run_id)
        assert restored["status"] == "available"
        assert restored["nodes"] == baseline["nodes"]
        assert restored["edges"] == baseline["edges"]
        assert public_business_account(origin, accepted.run_id, result) == account


STAGE_CONTRADICTIONS = (
    "fallback_before_graph",
    "bypass_after_graph",
    "unknown_stage",
    "nonboolean_entered",
    "stage_after_run",
    "bypass_before_graph",
    "unknown_reason",
    "duplicate_stage",
    "fallback_without_graph",
    "nocapture_wrong_identity",
    "nocapture_after_run",
    "nocapture_unknown_stage",
    "nocapture_nonboolean",
    "nocapture_duplicate",
)


def contradictory_stage_trace(original, case):
    import copy

    events = [json.loads(line) for line in original.splitlines()]
    if case == "blocked_run_completed":
        next(e for e in events if e["payload_name"] == "run.finished")["payload"][
            "outcome"
        ] = "completed"
    elif case.startswith("nocapture_"):
        stage = next(e for e in events if e["payload_name"] == "path.stage")
        if case == "nocapture_wrong_identity":
            stage["process_instance_id"] = "another-service"
        elif case == "nocapture_unknown_stage":
            stage["payload"]["stage"] = "invented_stage"
        elif case == "nocapture_nonboolean":
            stage["payload"]["entered"] = "false"
        elif case == "nocapture_duplicate":
            events.insert(events.index(stage), copy.deepcopy(stage))
        else:
            events.remove(stage)
            events.append(stage)
    else:
        event = copy.deepcopy(
            next(e for e in events if e["payload_name"] == "run.started")
        )
        event["payload_name"] = "path.stage"
        stage, reason, entered = "fallback", "graph_failed", True
        anchor, after = "graph.finished", True
        if case == "fallback_before_graph":
            anchor, after = "graph.started", False
        elif case in ("bypass_after_graph", "bypass_before_graph"):
            stage, reason = "bypass", "disabled_by_config"
            if case == "bypass_before_graph":
                anchor, after = "graph.started", False
        elif case == "unknown_stage":
            stage = "invented_stage"
        elif case == "nonboolean_entered":
            entered = "false"
        elif case == "unknown_reason":
            reason = "invented_reason"
        elif case == "stage_after_run":
            anchor = "run.finished"
        elif case == "fallback_without_graph":
            events = [
                e
                for e in events
                if not e["payload_name"].startswith(("graph.", "node.", "path."))
            ]
            anchor, after = "run.finished", False
        elif case == "duplicate_stage":
            stage, reason = "bypass", "disabled_by_config"
            events = [
                e
                for e in events
                if not e["payload_name"].startswith(("graph.", "node.", "path."))
            ]
            anchor, after = "run.finished", False
        event["payload"] = dict(
            name="path.stage",
            trace_policy="persist",
            evidence_version=1,
            stage=stage,
            reason=reason,
            entered=entered,
        )
        index = next(
            i for i, e in enumerate(events) if e["payload_name"] == anchor
        ) + int(after)
        events.insert(index, event)
        if case == "duplicate_stage":
            events.insert(index, copy.deepcopy(event))
    for seq, event in enumerate(events, 1):
        event["seq"] = seq
    return "".join(json.dumps(e) + "\n" for e in events).encode()


@pytest.mark.parametrize("case", STAGE_CONTRADICTIONS)
def test_c3_stage_protocol_rejects_and_restores_public_evidence(tmp_path, case):
    script = (
        [SKIP, "answer"] if case.startswith("nocapture_") else [SKIP, "full", "answer"]
    )
    with dashboard_at(tmp_path, script=script) as (dashboard, origin):
        if not case.startswith("nocapture_"):
            enable(dashboard.host)
        accepted, result = submit(dashboard.host, "explain")
        baseline = path_get(origin, accepted.run_id)
        account = public_business_account(origin, accepted.run_id, result)
        trace = next(tmp_path.rglob("trace.jsonl"))
        original = trace.read_bytes()
        trace.write_bytes(contradictory_stage_trace(original, case))
        bad = path_get(origin, accepted.run_id)
        assert bad["status"] == "unavailable" and bad["reason"] == "corrupt", bad
        assert not bad["stages"] and not bad["nodes"]
        assert bad["summary"] == baseline["summary"] and bad["run"] == baseline["run"]
        trace.write_bytes(original)
        restored = path_get(origin, accepted.run_id)
        assert restored["status"] == baseline["status"]
        assert restored["stages"] == baseline["stages"]
        assert restored["nodes"] == baseline["nodes"]
        assert public_business_account(origin, accepted.run_id, result) == account


def test_ce10_unavailable_graph_has_real_public_bypass_evidence(tmp_path):
    def broken(tools):
        raise ValueError("controlled graph construction failure")

    with dashboard_at(
        tmp_path, script=[SKIP, "ordinary"], routing_graph_builder=broken
    ) as (dashboard, origin):
        enable(dashboard.host)
        accepted, result = submit(dashboard.host, "task")
        path = path_get(origin, accepted.run_id)
        assert path["status"] == "unavailable" and path["reason"] == "graph_bypassed"
        assert path["stages"] == [
            dict(stage="bypass", reason="routing_unavailable", entered=True)
        ]
        assert path["description"] is None and not path["nodes"]
        assert result.outcome == "completed" and result.step_count == 2
        assert path["summary"]["fallback"]["reason"] == "routing_unavailable"


@pytest.mark.parametrize("mode", ["bypass", "fallback"])
def test_c3_legal_stage_prefix_remains_readable(tmp_path, mode):
    script = (
        [SKIP, "answer"]
        if mode == "bypass"
        else [SKIP, "full", RuntimeError("controlled failure"), "answer"]
    )
    with dashboard_at(tmp_path, script=script) as (dashboard, origin):
        if mode == "fallback":
            enable(dashboard.host)
        accepted, _ = submit(dashboard.host, "task")
        original = path_get(origin, accepted.run_id)
        trace = next(tmp_path.rglob("trace.jsonl"))
        events = trace.read_bytes().splitlines()
        index = next(
            i
            for i, line in enumerate(events)
            if json.loads(line)["payload_name"] == "path.stage"
        )
        trace.write_bytes(b"\n".join(events[: index + 1]) + b"\n")
        prefix = path_get(origin, accepted.run_id)
        assert prefix["stages"] == original["stages"]
        assert prefix["reason"] == ("graph_bypassed" if mode == "bypass" else None)
        assert prefix["status"] == ("unavailable" if mode == "bypass" else "partial")


def test_c4_http_snapshot_copy_does_not_hold_host_or_fanout_publication(
    tmp_path, monkeypatch
):
    import copy
    from concurrent.futures import ThreadPoolExecutor

    from agent_alfred.runtime import run_path

    graph = PublicationBarrier(name="graph.started")
    copy_entered, copy_release = threading.Event(), threading.Event()

    class RecordingBarrier:
        def __init__(self):
            self.entered, self.release = threading.Event(), threading.Event()

        def wait(self):
            self.entered.set()
            assert self.release.wait(10)

    recording = RecordingBarrier()
    original = copy.deepcopy

    def paused(value, *args, **kwargs):
        import sys

        caller = sys._getframe(1)
        if (
            caller.f_code.co_filename == run_path.__file__
            and caller.f_code.co_name in ("snapshot", "materialize")
            and not copy_entered.is_set()
        ):
            copy_entered.set()
            assert copy_release.wait(10)
        return original(value, *args, **kwargs)

    with dashboard_at(
        tmp_path, extra_sinks=(graph,), before_recording_commit=recording
    ) as (dashboard, origin):
        enable(dashboard.host)
        accepted = dashboard.host.submit(SubmitRequest("explain", wait_for_result=True))
        assert graph.entered.wait(10)
        with ThreadPoolExecutor() as pool:
            monkeypatch.setattr(copy, "deepcopy", paused)
            future = pool.submit(path_get, origin, accepted.run_id)
            try:
                assert copy_entered.wait(10)
                graph.release.set()
                assert recording.entered.wait(3), (
                    "observation copy blocked actual Graph/FanOut/Host progress"
                )
                recording.release.set()
                result = dashboard.host.wait(accepted.run_id)
                assert result.outcome == "completed"
            finally:
                copy_release.set()
                graph.release.set()
                recording.release.set()
            before = future.result(timeout=10)
        assert result.outcome == "completed" and result.step_count == 3
        assert before["source"] == "process"
        assert not before["boundary"]["graph_finished"]
        assert before["run"]["phase"] == "running"
        assert not any(n["commit"] == "committed" for n in before["nodes"])
        after = path_get(origin, accepted.run_id)
        assert after["source"] == "trace" and after["graph_outcome"] == "Completed"
        assert before["boundary"]["last_seq"] < after["boundary"]["last_seq"]


@pytest.mark.parametrize(
    "outcome,reason",
    [
        ("Failed", "terminal_count"),
        ("Completed", "terminal_count"),
        ("Failed", "node_failed"),
        ("BudgetExhausted", "budget_exhausted"),
    ],
)
def test_c4_complete_terminal_cannot_be_failed_by_protocol_mutation(
    tmp_path, outcome, reason
):
    with dashboard_at(tmp_path) as (dashboard, origin):
        enable(dashboard.host)
        accepted, result = submit(dashboard.host, "explain")
        baseline = path_get(origin, accepted.run_id)
        account = public_business_account(origin, accepted.run_id, result)
        trace = next(tmp_path.rglob("trace.jsonl"))
        original = trace.read_bytes()
        events = [json.loads(line) for line in original.splitlines()]
        terminal = next(
            e["payload"] for e in events if e["payload_name"] == "graph.finished"
        )
        terminal.update(outcome=outcome, reason=reason)
        trace.write_text("".join(json.dumps(e) + "\n" for e in events))
        bad = path_get(origin, accepted.run_id)
        assert bad["reason"] == "corrupt" and not bad["nodes"], bad
        assert bad["summary"] == baseline["summary"] and bad["run"] == baseline["run"]
        trace.write_bytes(original)
        assert path_get(origin, accepted.run_id)["nodes"] == baseline["nodes"]
        assert public_business_account(origin, accepted.run_id, result) == account


@pytest.mark.parametrize("stage", ["fallback", "bypass_without_capture"])
def test_c4_failed_aggregation_cannot_claim_ordinary_fallback(tmp_path, stage):
    import copy

    from agent_alfred.evals.deterministic.test_aggregation import save_fact

    with dashboard_at(tmp_path, script=[RuntimeError("synthesis unavailable")]) as (
        dashboard,
        origin,
    ):
        save_fact(dashboard.host)
        accepted = dashboard.host.aggregate(
            session_id=dashboard.host.create_session(),
            goal="coffee",
            keywords="coffee",
            sources=("semantic",),
        )
        result = dashboard.host.wait(accepted.run_id)
        baseline = path_get(origin, accepted.run_id)
        assert (
            baseline["status"] == "available" and baseline["graph_outcome"] == "Failed"
        )
        assert (
            baseline["identity"]["graph_id"] == "manual_aggregation"
            and not baseline["stages"]
        )
        trace = next(tmp_path.rglob("trace.jsonl"))
        original = trace.read_bytes()
        events = [json.loads(line) for line in original.splitlines()]
        index = next(
            i for i, e in enumerate(events) if e["payload_name"] == "graph.finished"
        )
        event = copy.deepcopy(events[index])
        event["payload_name"] = "path.stage"
        event["payload"] = dict(
            name="path.stage",
            trace_policy="persist",
            evidence_version=1,
            stage="fallback",
            reason="graph_failed",
            entered=True,
        )
        events.insert(index + 1, event)
        if stage == "bypass_without_capture":
            event["payload"].update(stage="bypass", reason="disabled_by_config")
            events = [
                e
                for e in events
                if not e["payload_name"].startswith(("graph.", "node."))
                and e["payload_name"] not in ("path.captured", "path.wave")
            ]
        for seq, event in enumerate(events, 1):
            event["seq"] = seq
        trace.write_text("".join(json.dumps(e) + "\n" for e in events))
        bad = path_get(origin, accepted.run_id)
        assert bad["reason"] == "corrupt" and not bad["stages"], bad
        assert bad["summary"] == baseline["summary"] and bad["run"] == baseline["run"]
        trace.write_bytes(original)
        assert path_get(origin, accepted.run_id)["nodes"] == baseline["nodes"]
        assert result.outcome == "failed" and result.step_count == 1


def terminal_count_graph(tools, count):
    """Invoke the real engine with a fixed, legal disabled-node configuration."""
    from agent_alfred.graph import GraphBuilder, NodeOutcome, TerminalSpec, fn_node

    builder = GraphBuilder("message_routing")
    for key in ("task", "prepared_context", "has_loaded_skills"):
        builder.declare_input(key)
    for name in ("a", "b"):
        builder.add_node(
            name,
            fn_node(lambda s, c: NodeOutcome()),
            skippable_by_config=True,
            terminal=TerminalSpec.no_action(name),
        )
    compiled = builder.compile()
    disabled = ("a", "b") if count == 0 else ("b",) if count == 1 else ()

    class ConfiguredGraph:
        def describe(self):
            return compiled.describe()

        def invoke(self, inputs, *, context):
            return compiled.invoke(inputs, disabled=disabled, context=context)

    return ConfiguredGraph()


@pytest.mark.parametrize("count", [0, 1, 2])
def test_c4_real_terminal_count_controls_over_http(tmp_path, count):
    with dashboard_at(
        tmp_path,
        script=[SKIP, "ordinary"],
        routing_graph_builder=lambda tools: terminal_count_graph(tools, count),
    ) as (dashboard, origin):
        enable(dashboard.host)
        accepted, result = submit(dashboard.host, "task")
        path = path_get(origin, accepted.run_id)
        assert path["status"] == "available", path
        assert path["graph_outcome"] == ("NoAction" if count == 1 else "Failed")
        assert path["graph_reason"] == ("a" if count == 1 else "terminal_count")
        assert sum(n["state"] == "succeeded" for n in path["nodes"]) == count
        assert all(w["state"] == "committed" for w in path["waves"])
        assert path["stages"] == (
            []
            if count == 1
            else [dict(stage="fallback", reason="graph_failed", entered=True)]
        )
        assert result.outcome == "completed"


def fallback_eligibility_graph(tools, case):
    """Real engine/Host policy under explicit dependency/control failures."""
    from agent_alfred.graph.context import ForcedStop, RunForcedStop
    from agent_alfred.graph.nodes import NodeExecutionFailed
    from agent_alfred.graph.types import ContextInvalid, NodeFactory, freeze

    def execute(state, context):
        run = context.run
        if case == "budget_exhausted":
            while True:
                run.budget.reserve_step(context.node_id)
        if case == "overall_deadline":
            run.clock.monotonic_value = run.deadline + 1
            return NodeOutcome()
        if case == "forced_stop":
            run.forced_stop = ForcedStop("failed", None, "controlled_stop", freeze({}))
            raise RunForcedStop()
        if case == "context_invalid":
            raise ContextInvalid("invalidated actual node context")
        if case == "side_effect_unknown":
            # Explicit metering-health fault: production registry determines
            # unknown; no policy result or GraphResult is substituted.
            tools._metering.failed.set()
            raise ValueError("metering unavailable")
        if case == "allowed_error_code":

            class CodedFailure(NodeExecutionFailed):
                @property
                def public_code(self):
                    return "overall_deadline"

            raise CodedFailure("node-local code is not a Run forced stop")
        raise ValueError("ordinary recoverable graph failure")

    b = GraphBuilder("message_routing")
    for key in ("task", "prepared_context", "has_loaded_skills"):
        b.declare_input(key)
    b.add_node(
        "controlled",
        NodeFactory("fn", execute),
        terminal=TerminalSpec.no_action("unused"),
    )
    return b.compile()


def forbidden_fallback_trace(original):
    events = [json.loads(line) for line in original.splitlines()]
    next(e["payload"] for e in events if e["payload_name"] == "path.stage").update(
        stage="fallback", reason="graph_failed", entered=True
    )
    return "".join(json.dumps(e) + "\n" for e in events).encode()


@pytest.mark.parametrize(
    "case",
    [
        "budget_exhausted",
        "overall_deadline",
        "forced_stop",
        "context_invalid",
        "side_effect_unknown",
        "allowed_error_code",
        "ordinary",
    ],
)
def test_c5_real_host_fallback_eligibility_rejects_forgery_and_restores(tmp_path, case):
    from agent_alfred.clock import FakeClock
    from agent_alfred.settings import Settings

    with dashboard_at(
        tmp_path,
        script=[SKIP, "ordinary"],
        clock=FakeClock(),
        settings=Settings(max_steps=3, overall_deadline_s=30),
        routing_graph_builder=lambda tools: fallback_eligibility_graph(tools, case),
    ) as (dashboard, origin):
        enable(dashboard.host)
        accepted, result = submit(dashboard.host, "task")
        original_path = path_get(origin, accepted.run_id)
        allowed = case in ("ordinary", "allowed_error_code")
        assert original_path["status"] == "available", original_path
        assert original_path["stages"] == [
            dict(
                stage="fallback",
                reason="graph_failed" if allowed else case,
                entered=allowed,
            )
        ]
        trace = next(tmp_path.rglob("trace.jsonl"))
        original = trace.read_bytes()
        events = [json.loads(line) for line in original.splitlines()]
        stage_index = next(
            i for i, e in enumerate(events) if e["payload_name"] == "path.stage"
        )
        trace.write_text(
            "".join(json.dumps(e) + "\n" for e in events[: stage_index + 1])
        )
        prefix = path_get(origin, accepted.run_id)
        assert (
            prefix["status"] == "partial"
            and prefix["stages"] == original_path["stages"]
        )
        trace.write_bytes(original)
        if not allowed:
            trace.write_bytes(forbidden_fallback_trace(original))
            bad = path_get(origin, accepted.run_id)
            assert (
                bad["reason"] == "corrupt" and not bad["nodes"] and not bad["stages"]
            ), bad
            assert (
                bad["summary"] == original_path["summary"]
                and bad["run"] == original_path["run"]
            )
            trace.write_bytes(original)
            assert (
                path_get(origin, accepted.run_id)["stages"] == original_path["stages"]
            )
        assert result.outcome == (
            "completed"
            if allowed
            else "max_steps"
            if case == "budget_exhausted"
            else "failed"
        )


@pytest.mark.parametrize("case", ["overall_deadline", "budget_exhausted"])
def test_c5_production_routing_policy_blocks_and_restores(tmp_path, case):
    from agent_alfred.clock import FakeClock
    from agent_alfred.settings import Settings

    clock = FakeClock()

    class TimedModel(ScriptedModel):
        def respond(self, request, *, events=None, deadline=None):
            result = super().respond(request, events=events, deadline=deadline)
            if case == "overall_deadline" and "Classify only" in str(request.system):
                clock.monotonic_value += 6
            return result

    model = TimedModel([SKIP, "full", "must not execute"])
    with dashboard_at(
        tmp_path,
        factory=ScriptedModelFactory(model),
        clock=clock,
        settings=Settings(
            max_steps=2 if case == "budget_exhausted" else 8,
            overall_deadline_s=4 if case == "overall_deadline" else 30,
        ),
    ) as (dashboard, origin):
        enable(dashboard.host)
        accepted, result = submit(dashboard.host, "task")
        baseline = path_get(origin, accepted.run_id)
        assert baseline["status"] == "available", baseline
        assert len(baseline["nodes"]) == 9
        assert baseline["stages"] == [
            dict(stage="fallback", reason=case, entered=False)
        ]
        assert baseline["graph_reason"] == case
        assert baseline["graph_outcome"] == (
            "Failed" if case == "overall_deadline" else "BudgetExhausted"
        )
        assert len(model.requests) == 2
        account = public_business_account(origin, accepted.run_id, result)
        trace = next(tmp_path.rglob("trace.jsonl"))
        original = trace.read_bytes()
        trace.write_bytes(forbidden_fallback_trace(original))
        bad = path_get(origin, accepted.run_id)
        assert bad["reason"] == "corrupt" and not bad["stages"] and not bad["nodes"]
        assert bad["summary"] == baseline["summary"] and bad["run"] == baseline["run"]
        trace.write_bytes(original)
        restored = path_get(origin, accepted.run_id)
        assert (
            restored["nodes"] == baseline["nodes"]
            and restored["stages"] == baseline["stages"]
        )
        assert public_business_account(origin, accepted.run_id, result) == account
        assert len(model.requests) == 2
