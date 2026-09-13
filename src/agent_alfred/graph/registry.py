"""Startup graph compilation and explicit, frozen name lookup."""

from types import MappingProxyType

from .engine import CompiledGraph


class GraphRegistry:
    def __init__(self, graphs):
        self._graphs = MappingProxyType(
            {
                name: graph if isinstance(graph, CompiledGraph) else graph.compile()
                for name, graph in graphs.items()
            }
        )

    def get(self, graph_id):
        try:
            return self._graphs[graph_id]
        except KeyError:
            raise KeyError(f"unknown graph: {graph_id}") from None
