"""Bounded Run path observation; historical facts live only in the trace bundle.

The projection stores at most one Run and 2 MiB / 4096 selected facts. Overflow
invalidates the whole view, never drops its prefix to claim completeness.
"""

import copy
import hashlib
import json
import threading

from agent_alfred.events import BestEffortFlushResult, event_json_default
from agent_alfred.runtime.evidence import _read_trace

MAX_PATH_BYTES = 2 * 1024 * 1024
MAX_PATH_EVENTS = 4096
PATH_NAMES = {
    "path.stage",
    "path.captured",
    "path.wave",
    "graph.started",
    "graph.finished",
    "node.started",
    "node.finished",
    "node.skipped",
    "node.aborted",
    "step.started",
    "attempt.started",
    "run.finished",
}


class RunPathSink:
    name = "run_path"
    flush_at_run_end = False

    def __init__(self, max_bytes=MAX_PATH_BYTES, max_events=MAX_PATH_EVENTS):
        self.max_bytes, self.max_events = max_bytes, max_events
        self._lock = threading.Lock()
        self._run_id = None
        self._events = []
        self._bytes = 0
        self._reason = None

    def prepare(self, event):
        routing_decision = (
            event.payload.name == "notice" and event.payload.code == "routing_fallback"
        )
        if (
            event.payload.name not in PATH_NAMES
            and event.payload.name != "run.started"
            and not routing_decision
        ):
            return None
        try:
            # Project before serialization: even transient construction must
            # not copy model inputs, user messages or formal replies.
            if routing_decision:
                payload = dict(
                    name="notice",
                    code="routing_fallback",
                    detail=[
                        (key, value)
                        for key, value in event.payload.detail
                        if key in ("decision", "reason", "graph_id")
                    ],
                )
            elif event.payload.name in (
                "run.started",
                "step.started",
                "attempt.started",
                "run.finished",
            ):
                payload = {"name": event.payload.name}
                if event.payload.name == "run.started":
                    payload["purpose"] = event.payload.purpose
                if event.payload.name == "run.finished":
                    payload["outcome"] = event.payload.outcome
            else:
                payload = json.loads(
                    json.dumps(event.payload, default=event_json_default)
                )
            return payload, len(json.dumps(payload).encode())
        except Exception:
            return {"name": "path.lost"}, 0

    def commit(self, prepared, event):
        if prepared is None:
            return
        with self._lock:
            run_id = event.envelope.run_id
            payload, size = prepared
            if run_id != self._run_id:
                self._run_id, self._events = run_id, []
                self._bytes, self._reason = 0, None
            if self._reason:
                return
            if payload["name"] == "path.lost":
                self._reason = "observation_failed"
            elif (
                self._bytes + size > self.max_bytes
                or len(self._events) >= self.max_events
            ):
                self._reason = "current_limit"
            if self._reason:
                self._events = []
                return
            self._bytes += size
            env = event.envelope
            self._events.append(
                dict(
                    payload=payload,
                    payload_name=payload["name"],
                    seq=event.seq,
                    process_instance_id=event.process_instance_id,
                    run_id=run_id,
                    node_id=env.node_id,
                    step_index=env.step_index,
                    attempt_id=env.attempt_id,
                )
            )

    def mark_disabled(self, run_id):
        with self._lock:
            self._run_id, self._events = run_id, []
            self._reason = "observation_failed"

    def retire(self, run_id):
        with self._lock:
            if self._run_id == run_id:
                self._events, self._run_id = [], None

    def capture(self, run_id):
        """Pin a prefix in constant time; published records are never mutated.

        Writers only append or replace the list on retirement/loss. A reader
        holds the old list alive and its fixed length, independent of later
        appends, capacity loss, recording retirement or the next Run.
        """
        with self._lock:
            if self._run_id != run_id:
                return "missing", (), 0
            return self._reason or "live", self._events, len(self._events)

    @staticmethod
    def materialize(captured):
        state, events, size = captured
        return state, copy.deepcopy(events[:size])

    def snapshot(self, run_id):
        return self.materialize(self.capture(run_id))

    def flush(self, run_id):
        return BestEffortFlushResult(outcome="best_effort")

    def close(self):
        with self._lock:
            self._events = []
        return True


def read_path(store, redactor, run_id, trace_root, service_id, read_at, live=None):
    """Select exactly one source. A caller-captured memory view is never merged."""
    with store.reading() as conn:
        row = conn.execute(
            "SELECT phase,outcome,telemetry FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        pruned = conn.execute(
            "SELECT prune_reason FROM trace_prunes WHERE run_id=?", (run_id,)
        ).fetchone()
    telemetry = json.loads(row[2]) if row[2] else {}
    run = dict(
        phase=row[0], outcome=row[1], recording_state="recorded" if row[2] else None
    )
    if live is not None:
        state, events, run = live
        source = "process"
        telemetry = {}  # Never graft later durable summary onto a memory read.
    else:
        state, events = _read_trace(trace_root, run_id, detailed=True)
        source = "trace"
    if pruned:
        state, events = "pruned", []
    result = dict(
        run_id=run_id,
        evidence_version=1,
        source=source,
        service_instance_id=service_id,
        read_at=read_at,
        run=run,
        trace_status=state,
        trace_incomplete=telemetry.get("trace_incomplete"),
        status="unavailable",
        reason=state,
        identity=None,
        description=None,
        nodes=[],
        edges=[],
        waves=[],
        graph_outcome=None,
        graph_reason=None,
        boundary=None,
        stages=[],
        # Independently retained business summary is not used to infer path.
        summary=summary(telemetry),
        limits=dict(
            current_bytes=MAX_PATH_BYTES,
            current_events=MAX_PATH_EVENTS,
            historical_bytes=32 * 1024 * 1024,
        ),
    )
    if state not in ("live", "available", "partial"):
        return redactor.redact_jsonable(result)
    try:
        result.update(project(events, run_id, state))
        return redactor.redact_jsonable(result)
    except Unsupported:
        result["reason"] = "unsupported"
    except ValueError, KeyError, TypeError, IndexError, AttributeError:
        result["reason"] = "corrupt"
    return redactor.redact_jsonable(result)


def summary(telemetry):
    memory = telemetry.get("memory", {})
    routing = memory.get("routing", {})
    aggregation = memory.get("aggregation", {})
    return dict(
        graph=memory.get("graph", {}).get("result"),
        fallback=routing.get("fallback"),
        graph_error=routing.get("error") or aggregation.get("error"),
        no_action=routing.get("reason_code") or aggregation.get("reason_code"),
        recoveries=routing.get("recoveries") or aggregation.get("recoveries"),
    )


class Unsupported(ValueError):
    pass


def require(value):
    if not value:
        raise ValueError("invalid path evidence")


def edge_key(edge):
    return tuple(edge[k] for k in ("source", "target", "kind", "label"))


def validated_stages(events, run_id):
    """Validate the shared Run/Graph timeline even without captured structure.

    Missing graph completion can be a genuine fatal publication gap. A later
    stage, however, must be supported by the earlier facts it depends on.
    """
    stages = []
    process, previous = None, 0
    graph, captured, run_finished = None, False, False
    workflow, purpose, fallback = None, None, None
    blocked_reasons = {
        "overall_deadline",
        "forced_stop",
        "context_invalid",
        "side_effect_occurred",
        "side_effect_unknown",
        "budget_exhausted",
    }
    for event in events:
        require(event["run_id"] == run_id)
        identity = event["process_instance_id"]
        require(isinstance(identity, str) and bool(identity))
        if process is None:
            process = identity
        require(identity == process)
        require(type(event["seq"]) is int and event["seq"] > previous)
        previous = event["seq"]
        p = event["payload"]
        name = p.get("name", "")
        if name.startswith("path.") and (
            name not in ("path.captured", "path.wave", "path.stage")
            or type(p.get("evidence_version")) is not int
            or p["evidence_version"] != 1
        ):
            raise Unsupported()
        if name.startswith(("graph.", "node.", "path.")) or name in (
            "run.started",
            "step.started",
            "attempt.started",
            "tool.started",
        ):
            require(not run_finished)
        if name in ("step.started", "attempt.started", "tool.started"):
            require(not stages or stages[0]["entered"])
        if name == "run.started":
            purpose = p.get("purpose")
        elif name == "path.captured":
            require(not captured and graph is None and not stages)
            captured = True
            workflow = p["graph_id"]
        elif name == "graph.started":
            require(graph is None and not stages)
            graph = "running"
            require(workflow is None or workflow == p["graph_id"])
            workflow = p["graph_id"]
        elif name == "graph.finished":
            require(graph == "running")
            require(
                p["outcome"]
                in (
                    "Completed",
                    "CompletedWithRecovery",
                    "NoAction",
                    "Failed",
                    "BudgetExhausted",
                )
            )
            graph = p["outcome"]
        elif name.startswith("node.") or name == "path.wave":
            require(graph == "running")
        elif name == "notice" and p.get("code") == "routing_fallback":
            # The adapter's existing decision fact proves eligibility, including
            # forced stop, invalid context, tool uncertainty and remaining budget.
            # A node error string alone does not establish any of these states.
            require(not run_finished and not stages and fallback is None)
            require(workflow == "message_routing" and purpose in (None, "chat"))
            require(graph in ("Failed", "BudgetExhausted"))
            detail = p["detail"]
            require(isinstance(detail, (list, tuple)) and len(detail) == 3)
            fallback = dict(detail)
            require(set(fallback) == {"decision", "reason", "graph_id"})
            require(fallback["graph_id"] == workflow)
            require(fallback["decision"] in ("allowed", "blocked"))
            allowed = fallback["decision"] == "allowed"
            require(
                fallback["reason"] == "graph_failed"
                if allowed
                else fallback["reason"] in blocked_reasons
            )
            require(graph != "BudgetExhausted" or not allowed)
        elif name == "path.stage":
            require(purpose in (None, "chat"))
            require(not stages)
            stage, reason, entered = p["stage"], p["reason"], p["entered"]
            require(type(entered) is bool and isinstance(reason, str))
            require(event.get("node_id") is None)
            if stage == "bypass":
                require(graph is None and not captured)
                require(
                    entered and reason in ("disabled_by_config", "routing_unavailable")
                )
            elif stage == "fallback":
                require(workflow == "message_routing")
                require(graph in ("Failed", "BudgetExhausted"))
                require(fallback is not None)
                require(reason == fallback["reason"])
                require(entered == (fallback["decision"] == "allowed"))
                require(
                    reason == "graph_failed" if entered else reason in blocked_reasons
                )
            else:
                require(False)
            stages.append(dict(stage=stage, reason=reason, entered=entered))
        elif name == "run.finished":
            require(not run_finished)
            run_finished = True
            if "outcome" in p:
                require(
                    p["outcome"] in ("completed", "failed", "max_steps", "interrupted")
                )
                if (
                    stages
                    and not stages[0]["entered"]
                    and stages[0]["reason"] != "forced_stop"
                ):
                    require(p["outcome"] != "completed")
    return stages


def project(events, run_id, coverage):
    stages = validated_stages(events, run_id)
    captured = [e for e in events if e["payload"].get("name") == "path.captured"]
    if not captured:
        return dict(
            status="unavailable",
            stages=stages,
            reason="graph_bypassed"
            if any(s["stage"] == "bypass" and s["entered"] for s in stages)
            else "no_historical_structure",
        )
    require(len(captured) == 1)
    capture = captured[0]
    payload = capture["payload"]
    if payload.get("evidence_version") != 1:
        raise Unsupported()
    d = payload["description"]
    if d.get("schema_version") != 1:
        raise Unsupported()
    t = d["topology"]
    # Same canonicalization as describe; presentation is deliberately excluded.
    digest = hashlib.sha256(
        json.dumps(
            t, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    require(digest == d["topology_hash"])
    process = capture["process_instance_id"]
    require(isinstance(process, str) and bool(process))
    generation = payload["publication_generation"]
    require(type(generation) is int and generation > 0)
    graph_id = payload["graph_id"]
    nodes = {
        n["node_id"]: dict(
            node_id=n["node_id"],
            state="unknown",
            reason=None,
            commit="unknown",
            references=[],
        )
        for n in t["nodes"]
    }
    require(len(nodes) == len(t["nodes"]))
    waves = [
        dict(wave=i, nodes=ids, state="unknown", reason=None)
        for i, ids in enumerate(payload["waves"])
    ]
    flat = [n for w in waves for n in w["nodes"]]
    require(len(flat) == len(nodes) and set(flat) == set(nodes))
    edges = {edge_key(e): dict(e, state="unknown") for e in t["edges"]}
    require(len(edges) == len(t["edges"]))
    node_waves = {name: w["wave"] for w in waves for name in w["nodes"]}
    declarations = {n["node_id"]: n for n in t["nodes"]}
    incoming = {name: [] for name in nodes}
    for e in edges.values():
        require(e["source"] in nodes and e["target"] in nodes)
        require(e["kind"] in ("normal", "conditional", "error"))
        require(node_waves[e["source"]] < node_waves[e["target"]])
        incoming[e["target"]].append(e)
    started, finished, run_finished, previous = False, False, False, 0
    route_labels = {}
    routers = {r["source"]: r["path_map"] for r in t["routers"]}
    outcome, reason = None, None
    settled = set()
    for event in events:
        require(event["run_id"] == run_id and event["process_instance_id"] == process)
        require(type(event["seq"]) is int and event["seq"] > previous)
        previous = event["seq"]
        p = event["payload"]
        name, node_id = p.get("name"), event.get("node_id")
        graph_fact = name in (
            "path.captured",
            "graph.started",
            "path.wave",
            "graph.finished",
            "node.started",
            "node.finished",
            "node.skipped",
            "node.aborted",
        )
        if graph_fact:
            require(not run_finished)
        if name == "run.finished":
            require(not run_finished)
            run_finished = True
        elif name == "graph.started":
            require(not started and event["seq"] > capture["seq"])
            require(p["topology_hash"] == digest and p["schema_version"] == 1)
            require(
                p["graph_id"] == payload["graph_id"]
                and isinstance(p["graph_id"], str)
                and bool(p["graph_id"])
            )
            graph_id, started = p["graph_id"], True
        elif name in ("node.started", "node.finished", "node.skipped", "node.aborted"):
            require(started and not finished and node_id in nodes)
            n = nodes[node_id]
            current_wave = node_waves[node_id]
            require(current_wave not in settled)
            require(all(waves[i]["state"] == "committed" for i in range(current_wave)))
            inbound = incoming[node_id]
            require(all(e["state"] in ("taken", "not_taken") for e in inbound))
            active = not inbound or any(e["state"] == "taken" for e in inbound)
            if name == "node.started":
                require(active)
                require(n["state"] == "unknown")
                n.update(state="started", commit="pending")
            elif name == "node.skipped":
                require(n["state"] == "unknown")
                require(
                    p["reason"] == "disabled_by_config"
                    and declarations[node_id]["skippable_by_config"]
                    or p["reason"] == "all_inbound_not_taken"
                    and not active
                )
                n.update(state="skipped", reason=p["reason"], commit="pending")
            else:
                require(n["state"] == "started")
                require(
                    name == "node.aborted"
                    or p.get("outcome") in ("succeeded", "failed")
                )
                if name == "node.finished":
                    label = p.get("route_label")
                    if p["outcome"] == "succeeded" and node_id in routers:
                        require(isinstance(label, str) and label in routers[node_id])
                    else:
                        require(label is None)
                    route_labels[node_id] = label
                n.update(
                    state="aborted" if name == "node.aborted" else p["outcome"],
                    reason=p.get("reason"),
                    commit="pending",
                )
        elif name == "path.wave":
            if p.get("evidence_version") != 1:
                raise Unsupported()
            require(started and not finished)
            i = p["wave"]
            require(type(i) is int and 0 <= i < len(waves) and i not in settled)
            require(p["state"] in ("committed", "aborted"))
            require(all(waves[j]["state"] == "committed" for j in range(i)))
            w = waves[i]
            fact_ids = [n["node_id"] for n in p["nodes"]]
            require(
                len(set(fact_ids)) == len(fact_ids) and set(fact_ids) <= set(w["nodes"])
            )
            if p["state"] == "committed":
                require(set(fact_ids) == set(w["nodes"]))
                expected = {k for k in edges if k[0] in fact_ids}
                require(
                    len(p["edges"]) == len(expected)
                    and {edge_key(e) for e in p["edges"]} == expected
                )
            else:
                require(not p["edges"] and bool(fact_ids))
                require(
                    set(fact_ids)
                    == {
                        name for name in w["nodes"] if nodes[name]["state"] == "aborted"
                    }
                )
                require(
                    all(
                        nodes[name]["state"] in ("unknown", "aborted")
                        for name in w["nodes"]
                    )
                )
            for fact in p["nodes"]:
                n = nodes[fact["node_id"]]
                if p["state"] == "aborted":
                    require(
                        n["state"] == "aborted"
                        and fact["state"] in ("succeeded", "failed", "aborted")
                    )
                else:
                    require(
                        n["state"] == fact["state"]
                        and n["state"] in ("succeeded", "failed", "skipped")
                    )
                    if n["state"] == "skipped":
                        require(n["reason"] == fact["reason"])
                require(n["commit"] == "pending")
                n.update(state=fact["state"], reason=fact["reason"], commit=p["state"])
            for e in p["edges"]:
                require(edge_key(e) in edges and e["state"] in ("taken", "not_taken"))
                source_state = nodes[e["source"]]["state"]
                taken = (
                    source_state == "failed"
                    if e["kind"] == "error"
                    else source_state == "succeeded"
                    and (
                        e["kind"] == "normal"
                        or route_labels.get(e["source"]) == e["label"]
                    )
                )
                require((e["state"] == "taken") == taken)
                edges[edge_key(e)]["state"] = e["state"]
            w.update(state=p["state"], reason=p.get("reason"))
            settled.add(i)
        elif name == "graph.finished":
            require(started and not finished)
            outcome, reason = p["outcome"], p.get("reason")
            require(
                outcome
                in (
                    "Completed",
                    "CompletedWithRecovery",
                    "NoAction",
                    "Failed",
                    "BudgetExhausted",
                )
            )
            require(not any(n["commit"] == "pending" for n in nodes.values()))
            unstarted = p.get("not_started") or ()
            require(len(set(unstarted)) == len(unstarted))
            for node_id in unstarted:
                require(
                    node_id in nodes
                    and nodes[node_id]["state"] in ("unknown", "skipped")
                )
                if nodes[node_id]["state"] == "unknown":
                    nodes[node_id]["state"] = "not_started"
            complete = all(w["state"] == "committed" for w in waves)
            terminals = [
                declarations[name]["terminal"]
                for name, n in nodes.items()
                if n["state"] == "succeeded" and declarations[name]["terminal"]
            ]
            if outcome in ("Failed", "BudgetExhausted"):
                aborted = [w for w in waves if w["state"] == "aborted"]
                if complete or reason == "terminal_count":
                    require(
                        complete
                        and outcome == "Failed"
                        and reason == "terminal_count"
                        and len(terminals) != 1
                    )
                elif aborted:
                    require(len(aborted) == 1 and aborted[0]["reason"] == reason)
                else:
                    # The engine can fail before starting any node in a wave.
                    require(
                        outcome == "Failed"
                        and reason
                        in (
                            None,
                            "invalid_input",
                            "invalid_configuration",
                            "overall_deadline",
                        )
                    )
                    if reason != "overall_deadline":
                        require(not settled)
                if outcome == "BudgetExhausted":
                    require(reason == "budget_exhausted")
            if outcome in ("Completed", "CompletedWithRecovery", "NoAction"):
                require(not unstarted and complete)
                require(len(terminals) == 1)
                require(
                    (outcome == "NoAction") == (terminals[0]["kind"] == "no_action")
                )
                if outcome == "NoAction":
                    require(reason == terminals[0]["reason_code"])
                else:
                    require(reason is None)
                    recovered = any(
                        e["kind"] == "error"
                        and e["state"] == "taken"
                        and nodes[e["target"]]["state"] == "succeeded"
                        for e in edges.values()
                    )
                    require((outcome == "CompletedWithRecovery") == recovered)
            finished = True
        elif name in ("step.started", "attempt.started") and node_id in nodes:
            ref = {k: event.get(k) for k in ("step_index", "attempt_id")}
            if ref not in nodes[node_id]["references"]:
                nodes[node_id]["references"].append(ref)
    if coverage == "live":
        for n in nodes.values():
            if n["state"] == "unknown":
                n["state"] = "not_started"
        for e in edges.values():
            if e["state"] == "unknown":
                e["state"] = "undecided"
    return dict(
        status="partial" if coverage == "partial" or not started else "available",
        reason=None,
        identity=dict(
            run_id=run_id,
            workflow=graph_id,
            graph_id=graph_id,
            process_instance_id=process,
            publication_generation=generation,
            topology_hash=digest,
            schema_version=1,
            evidence_version=1,
        ),
        description=d,
        nodes=list(nodes.values()),
        edges=list(edges.values()),
        waves=waves,
        graph_outcome=outcome,
        graph_reason=reason,
        stages=stages,
        boundary=dict(
            first_seq=events[0]["seq"],
            last_seq=previous,
            graph_finished=finished,
            coverage=coverage,
        ),
    )
