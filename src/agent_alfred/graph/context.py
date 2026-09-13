"""Run-owned dependencies and accounting; never exposed to pure callables."""

from dataclasses import dataclass, field
from uuid import uuid4

from agent_alfred.clock import SystemClock
from agent_alfred.events import EventEnvelope, GraphFinished, GraphStarted
from agent_alfred.loop.budget import RunBudget


@dataclass
class GraphRunContext:
    budget: RunBudget
    run_id: str = field(default_factory=lambda: str(uuid4()))
    session_id: str | None = None
    clock: object = field(default_factory=SystemClock)
    events: object = None
    source: str = "graph"
    tools: object = None
    tool_permission: object = None
    working_memory: tuple = ()
    tool_state: dict = field(default_factory=dict)
    model_results: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    transcript: list = field(default_factory=list)
    forced_stop: object = None
    pending_transcripts: dict = field(default_factory=dict)
    graph_identity: dict = field(default_factory=dict)
    started_s: float = 0.0
    ended_s: float = 0.0

    @property
    def duration_ms(self):
        return max(0, int((self.ended_s - self.started_s) * 1000))

    def emit(self, payload, node_id=None):
        if isinstance(payload, GraphStarted):
            self.started_s = self.clock.monotonic()
            self.graph_identity = dict(
                graph_id=payload.graph_id,
                topology_hash=payload.topology_hash,
                schema_version=payload.schema_version,
            )
        elif isinstance(payload, GraphFinished):
            self.ended_s = self.clock.monotonic()
        if self.events is not None:
            self.events.emit(
                payload,
                EventEnvelope(
                    self.clock.monotonic(),
                    self.run_id,
                    self.session_id,
                    None,
                    None,
                    node_id,
                    self.source,
                ),
            )

    def abort(self, node_ids):
        for node_id in node_ids:
            self.pending_transcripts.pop(node_id, None)
        for step in self.steps:
            if step["node_id"] in node_ids:
                step["outcome"] = "aborted_by_wave"


@dataclass(frozen=True)
class NodeContext:
    node_id: str
    error: object
    run: GraphRunContext


@dataclass(frozen=True)
class ForcedStop:
    outcome: str
    reply: object
    error: str | None
    facts: object


class RunForcedStop(Exception):
    """Expected control boundary, never eligible for a graph error edge."""
