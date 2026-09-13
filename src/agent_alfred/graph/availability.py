"""Boolean control-flow proof; no invocation of author code during compilation.

Reduced ordered decision diagrams represent reachability, including exclusive
route choices and handled failures. A required writer must dominate every taken
execution of its reader, and must belong to an earlier wave.
"""

from .types import GraphCompileError


class Decisions:
    def __init__(self):
        self.nodes = [(float("inf"), 0, 0), (float("inf"), 1, 1)]
        self.unique = {}
        self.cache = {}
        self.variables = 0

    def node(self, variable, low, high):
        if low == high:
            return low
        key = (variable, low, high)
        if key not in self.unique:
            self.unique[key] = len(self.nodes)
            self.nodes.append(key)
        return self.unique[key]

    def variable(self):
        self.variables += 1
        return self.node(self.variables, 0, 1)

    def combine(self, operation, a, b):
        key = (operation, a, b)
        if key in self.cache:
            return self.cache[key]
        if a < 2 and b < 2:
            return int(
                (bool(a) and bool(b))
                if operation == "and"
                else (bool(a) or bool(b))
                if operation == "or"
                else bool(a) != bool(b)
            )
        variable = min(self.nodes[a][0], self.nodes[b][0])
        al, ah = self.nodes[a][1:] if self.nodes[a][0] == variable else (a, a)
        bl, bh = self.nodes[b][1:] if self.nodes[b][0] == variable else (b, b)
        result = self.node(
            variable, self.combine(operation, al, bl), self.combine(operation, ah, bh)
        )
        self.cache[key] = result
        return result

    def both(self, a, b):
        return self.combine("and", a, b)

    def either(self, a, b):
        return self.combine("or", a, b)

    def negate(self, a):
        return self.combine("xor", a, 1)


def validate_availability(waves, edges, routers, inputs):
    logic = Decisions()
    available = {key: 1 if required else logic.variable() for key, required in inputs}
    edge_taken = {}
    routes = {r.source: r for r in routers}
    for wave in waves:
        candidate = {}
        for node in wave:
            inbound = [e for e in edges if e.target == node.node_id]
            active = 0 if inbound else 1
            for edge in inbound:
                active = logic.either(active, edge_taken[edge])
            if node.skippable_by_config:
                active = logic.both(active, logic.variable())
            outgoing = [e for e in edges if e.source == node.node_id]
            success = active
            if any(e.kind == "error" for e in outgoing):
                success = logic.both(active, logic.variable())
            _reads(logic, active, available, node.required_reads, node.optional_reads)
            for key in node.writes:
                candidate[key] = success
            route_choices = {}
            if node.node_id in routes:
                router = routes[node.node_id]
                _reads(
                    logic,
                    success,
                    {**available, **{k: success for k in node.writes}},
                    router.required_reads,
                    router.optional_reads,
                )
                remaining = success
                labels = list(router.path_map)
                for label in labels[:-1]:
                    choice = logic.variable()
                    route_choices[label] = logic.both(remaining, choice)
                    remaining = logic.both(remaining, logic.negate(choice))
                route_choices[labels[-1]] = remaining
            for edge in outgoing:
                edge_taken[edge] = (
                    logic.both(active, logic.negate(success))
                    if edge.kind == "error"
                    else route_choices[edge.label]
                    if edge.kind == "conditional"
                    else success
                )
        available.update(candidate)


def _reads(logic, active, available, required, optional):
    for key in required:
        if key not in available or logic.both(active, logic.negate(available[key])):
            raise GraphCompileError("required source not guaranteed: " + key)
    for key in optional:
        if key not in available or not logic.both(active, available[key]):
            raise GraphCompileError("optional source unavailable: " + key)
