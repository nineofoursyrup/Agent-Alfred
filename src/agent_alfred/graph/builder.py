"""Mutable authoring, immutable compiled execution."""

from dataclasses import dataclass

from .types import freeze


@dataclass(frozen=True)
class Node:
    node_id: str
    factory: object
    required_reads: tuple
    optional_reads: tuple
    writes: tuple
    skippable_by_config: bool
    terminal: object


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    kind: str = "normal"
    label: str | None = None


@dataclass(frozen=True)
class Router:
    source: str
    path: object
    path_map: object
    required_reads: tuple
    optional_reads: tuple


class GraphBuilder:
    def __init__(self, graph_id, *, tools=None, presentation=None):
        self.graph_id = graph_id
        self.nodes = []
        self.inputs = []
        self.edges = []
        self.routers = []
        self.tools = tools
        self.presentation = presentation or {}

    def declare_input(self, key, *, required=True):
        self.inputs.append((key, required))
        return self

    def add_node(
        self,
        node_id,
        factory,
        *,
        required_reads=(),
        optional_reads=(),
        writes=(),
        skippable_by_config=False,
        terminal=None,
    ):
        self.nodes.append(
            Node(
                node_id,
                factory,
                tuple(required_reads),
                tuple(optional_reads),
                tuple(writes),
                skippable_by_config,
                terminal,
            )
        )
        return self

    def add_edge(self, source, target):
        self.edges.append(Edge(source, target))
        return self

    def add_error_edge(self, source, target):
        self.edges.append(Edge(source, target, "error"))
        return self

    def add_conditional_edges(
        self, source, path, path_map, *, required_reads=(), optional_reads=()
    ):
        self.routers.append(
            Router(
                source,
                path,
                freeze(path_map),
                tuple(required_reads),
                tuple(optional_reads),
            )
        )
        for label, targets in path_map.items():
            for target in (targets,) if isinstance(targets, str) else targets:
                self.edges.append(Edge(source, target, "conditional", label))
        return self

    def compile(self):
        from .engine import CompiledGraph
        from .validation import compile_definition

        return CompiledGraph(*compile_definition(self))
