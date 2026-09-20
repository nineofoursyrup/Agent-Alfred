"""Deterministic wave snapshots, provisional control flow and atomic publication."""

from collections.abc import Mapping
from dataclasses import dataclass

from agent_alfred.events import (
    GraphFinished,
    GraphStarted,
    NodeAborted,
    NodeFinished,
    NodeSkipped,
    NodeStarted,
    PathCaptured,
    WaveSettled,
)
from agent_alfred.loop.budget import RunBudget, StepBudgetExceeded
from agent_alfred.runtime.memory import (
    InputDeadlineExceeded,
    InputEvidenceError,
    InputLimitExceeded,
    InputResolutionError,
)
from agent_alfred.runtime.telemetry import AttemptObservationFailed
from agent_alfred.stream_fallback import OverallDeadlineExceeded

from .context import ForcedStop, GraphRunContext, NodeContext, RunForcedStop
from .nodes import NodeExecutionFailed
from .types import (
    BudgetExhausted,
    Completed,
    CompletedWithRecovery,
    ContextInvalid,
    Failed,
    GraphInvariantError,
    NoAction,
    NodeError,
    NodeOutcome,
    freeze,
)


def read_view(state, required, optional):
    missing = set(required) - state.keys()
    if missing:
        raise ValueError("missing required: " + ", ".join(sorted(missing)))
    return freeze({k: state[k] for k in (*required, *optional) if k in state})


@dataclass(frozen=True)
class CompiledGraph:
    graph_id: str
    nodes: tuple
    inputs: tuple
    edges: tuple
    routers: tuple
    waves: tuple
    description: object

    def describe(self):
        from .types import thaw

        return thaw(self.description)

    def invoke(self, inputs, *, disabled=(), context=None):
        context = context or GraphRunContext(budget=RunBudget(0))
        if not context.budget.claim_graph(context.run_id):
            return Failed(
                NodeError(None, "graph_already_invoked", "one graph per Run"),
                context.tools.side_effect_state(context.run_id)
                if context.tools
                else "none",
                freeze({}),
            )
        # Capture the actual execution instance, before any node executes. No
        # query or finalizer looks up today's graph to reconstruct history.
        try:
            captured = PathCaptured(
                self.describe(), context.publication_generation,
                tuple(tuple(n.node_id for n in wave) for wave in self.waves),
                self.graph_id,
            )
        except Exception:
            captured = None
        if captured is not None:
            context.emit(captured)
        context.emit(GraphStarted(self.graph_id, self.description["topology_hash"], 1))
        try:
            state = dict(freeze(inputs))
        except (TypeError, ValueError) as exc:
            result = Failed(
                NodeError(None, "invalid_input", type(exc).__name__), "none", freeze({})
            )
            context.emit(GraphFinished(type(result).__name__))
            return result
        statuses, decisions, errors = {}, {}, {}
        recoveries, terminals, started = [], [], []
        current_started = []
        wave_index = None

        def failed(error, exhausted=False):
            for name in current_started:
                context.emit(NodeAborted(error.code), name)
            context.abort(current_started)
            if current_started and wave_index is not None:
                context.emit(WaveSettled(
                    wave_index, "aborted",
                    tuple(dict(
                        node_id=name,
                        state=wave_status.get(name, "failed"
                            if name == error.node_id else "aborted"),
                        reason=error.code,
                    ) for name in current_started), (), error.code,
                ))
            context.emit(GraphFinished(
                "BudgetExhausted" if exhausted else "Failed", error.code,
                tuple(n.node_id for n in self.nodes if n.node_id not in started),
            ))
            side_effect = (
                context.tools.side_effect_state(context.run_id)
                if context.tools is not None
                else "none"
            )
            cls = BudgetExhausted if exhausted else Failed
            from dataclasses import replace

            error = replace(error, side_effect_state=side_effect)
            args = (
                error,
                side_effect,
                freeze(state),
                tuple(n.node_id for n in self.nodes if n.node_id not in started),
            )
            return cls(
                *args, **({"forced_stop": context.forced_stop} if cls is Failed else {})
            )

        expected = dict(self.inputs)
        if set(state) - expected.keys() or any(
            k not in state for k, req in self.inputs if req
        ):
            return failed(
                NodeError(None, "invalid_input", "undeclared or missing graph input")
            )
        if any(
            n not in {x.node_id for x in self.nodes if x.skippable_by_config}
            for n in disabled
        ):
            return failed(
                NodeError(None, "invalid_configuration", "node cannot be disabled")
            )
        for wave_index, wave in enumerate(self.waves):
            snapshot = freeze(state)
            pending, wave_status, wave_edges, wave_errors = {}, {}, {}, {}
            current_started = []
            for node in wave:
                name = node.node_id
                incoming = [e for e in self.edges if e.target == name]
                active = not incoming or any(decisions[e] for e in incoming)
                if not active or name in disabled:
                    wave_status[name] = "skipped"
                    continue
                try:
                    context.checkpoint()
                except OverallDeadlineExceeded:
                    context.forced_stop = ForcedStop(
                        "failed", None, "overall_deadline", freeze({})
                    )
                    return failed(
                        NodeError(name, "overall_deadline", "Run deadline reached")
                    )
                started.append(name)
                current_started.append(name)
                context.emit(NodeStarted(), name)
                source_error = next(
                    (errors[e.source] for e in incoming if e.kind == "error"), None
                )
                try:
                    view = read_view(snapshot, node.required_reads, node.optional_reads)
                    outcome = node.factory.execute(
                        view, NodeContext(name, source_error, context)
                    )
                    context.checkpoint()
                    if not isinstance(outcome, NodeOutcome):
                        raise ValueError("node must return NodeOutcome")
                    writes = freeze(outcome.writes)
                    if (
                        isinstance(writes, Mapping)
                        and node.terminal
                        and node.terminal.kind == "result"
                        and node.terminal.output_key not in writes
                    ):
                        raise ValueError("missing terminal output")
                    pending[name] = writes
                    wave_status[name] = "succeeded"
                except RunForcedStop:
                    return failed(
                        NodeError(name, "run_forced_stop", "Run finalization required")
                    )
                except OverallDeadlineExceeded:
                    context.forced_stop = ForcedStop(
                        "failed", None, "overall_deadline", freeze({})
                    )
                    return failed(
                        NodeError(name, "overall_deadline", "Run deadline reached")
                    )
                except (
                    GraphInvariantError,
                    AttemptObservationFailed,
                    InputDeadlineExceeded,
                    InputEvidenceError,
                    InputLimitExceeded,
                    InputResolutionError,
                ) as exc:
                    failed(NodeError(name, "run_input_failed", type(exc).__name__))
                    raise
                except Exception as exc:
                    if isinstance(exc, ContextInvalid):
                        context.context_invalid = True
                    error = NodeError(
                        name,
                        "budget_exhausted"
                        if isinstance(exc, StepBudgetExceeded)
                        else exc.public_code if isinstance(exc, NodeExecutionFailed)
                        else "node_failed",
                        type(exc).__name__,
                        context.tools.side_effect_state(context.run_id)
                        if context.tools
                        else "none",
                    )
                    if not any(
                        e.source == name and e.kind == "error" for e in self.edges
                    ):
                        return failed(error, isinstance(exc, StepBudgetExceeded))
                    wave_status[name] = "failed"
                    wave_errors[name] = error
                    context.abort((name,))
                except BaseException as exc:
                    failed(NodeError(name, "run_interrupted", type(exc).__name__))
                    raise
            # Validate the entire write batch after all callable executions.
            written = set()
            for node in wave:
                writes = pending.get(node.node_id, {})
                if (
                    not isinstance(writes, Mapping)
                    or set(writes) - set(node.writes)
                    or written.intersection(writes)
                    or state.keys() & writes.keys()
                ):
                    return failed(
                        NodeError(
                            node.node_id, "invalid_writes", "illegal wave write set"
                        )
                    )
                written.update(writes)
            labels = {}
            for router in self.routers:
                if wave_status.get(router.source) != "succeeded":
                    continue
                try:
                    view = read_view(
                        {**snapshot, **pending[router.source]},
                        router.required_reads,
                        router.optional_reads,
                    )
                    label = router.path(view)
                    if label not in router.path_map:
                        raise ValueError("unknown route label")
                    labels[router.source] = label
                except Exception as exc:
                    return failed(
                        NodeError(router.source, "routing_failed", type(exc).__name__)
                    )
            for edge in self.edges:
                if edge.source not in wave_status:
                    continue
                status = wave_status[edge.source]
                wave_edges[edge] = (
                    status == "failed"
                    if edge.kind == "error"
                    else status == "succeeded"
                    and (edge.kind == "normal" or labels[edge.source] == edge.label)
                )
            for node in wave:
                name = node.node_id
                if wave_status[name] == "succeeded":
                    state.update(pending[name])
                    context.committed_state = freeze(state)
                    context.transcript.extend(context.pending_transcripts.pop(name, ()))
                    for step in context.steps:
                        if step["node_id"] == name and step["outcome"] == "provisional":
                            step["outcome"] = "committed"
                    if node.terminal:
                        terminals.append(node)
                    for edge in self.edges:
                        if (
                            edge.target == name
                            and edge.kind == "error"
                            and decisions[edge]
                        ):
                            recoveries.append(errors[edge.source])
            for node in wave:
                status = wave_status[node.node_id]
                if status == "skipped":
                    reason = (
                        "disabled_by_config"
                        if node.node_id in disabled
                        else "all_inbound_not_taken"
                    )
                    context.emit(NodeSkipped(reason), node.node_id)
                else:
                    context.emit(
                        NodeFinished(status, route_label=labels.get(node.node_id)),
                        node.node_id,
                    )
            context.emit(WaveSettled(
                wave_index, "committed",
                tuple(dict(
                    node_id=node.node_id, state=wave_status[node.node_id],
                    reason=("disabled_by_config" if node.node_id in disabled
                            else "all_inbound_not_taken")
                    if wave_status[node.node_id] == "skipped"
                    else wave_errors[node.node_id].code
                    if node.node_id in wave_errors else None,
                ) for node in wave),
                tuple(dict(source=edge.source, target=edge.target,
                           kind=edge.kind, label=edge.label,
                           state="taken" if taken else "not_taken")
                      for edge, taken in wave_edges.items()),
            ))
            current_started = []
            statuses.update(wave_status)
            errors.update(wave_errors)
            decisions.update(wave_edges)
        if len(terminals) != 1:
            return failed(
                NodeError(None, "terminal_count", "expected one successful terminal")
            )
        terminal = terminals[0].terminal
        if terminal.kind == "no_action":
            context.emit(GraphFinished("NoAction", terminal.reason_code))
            return NoAction(terminal.reason_code, tuple(recoveries))
        context.emit(
            GraphFinished("CompletedWithRecovery" if recoveries else "Completed")
        )
        output = state[terminal.output_key]
        return (
            CompletedWithRecovery(output, tuple(recoveries))
            if recoveries
            else Completed(output)
        )
