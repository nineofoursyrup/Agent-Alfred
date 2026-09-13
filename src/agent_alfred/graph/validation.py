"""Compile-time graph validation and topology cache."""

import hashlib
import json
import re
from dataclasses import asdict

from .types import GraphCompileError, freeze


def compile_definition(builder):
    validate_declarations(builder)
    nodes = tuple(builder.nodes)
    edges = tuple(builder.edges)
    remaining = list(nodes)
    settled = set()
    waves = []
    while remaining:
        ready = tuple(
            n
            for n in remaining
            if all(e.source in settled for e in edges if e.target == n.node_id)
        )
        if not ready:
            raise GraphCompileError("cycle or missing source")
        waves.append(ready)
        settled.update(n.node_id for n in ready)
        remaining = [n for n in remaining if n not in ready]
    from .availability import validate_availability

    validate_availability(waves, edges, builder.routers, builder.inputs)
    topology = {
        "inputs": dict(builder.inputs),
        "nodes": [
            dict(
                node_id=n.node_id,
                kind=n.factory.kind,
                required_reads=sorted(n.required_reads),
                optional_reads=sorted(n.optional_reads),
                writes=sorted(n.writes),
                tools=list(n.factory.tools),
                skippable_by_config=n.skippable_by_config,
                terminal=asdict(n.terminal) if n.terminal else None,
            )
            for n in nodes
        ],
        "edges": [asdict(e) for e in edges],
        "routers": [
            dict(
                source=r.source,
                path_map=dict(r.path_map),
                required_reads=sorted(r.required_reads),
                optional_reads=sorted(r.optional_reads),
            )
            for r in builder.routers
        ],
    }
    encoded = json.dumps(
        topology, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    description = dict(
        schema_version=1,
        topology_hash=hashlib.sha256(encoded.encode()).hexdigest(),
        topology=topology,
        presentation=builder.presentation,
    )
    return (
        builder.graph_id,
        nodes,
        tuple(builder.inputs),
        edges,
        tuple(builder.routers),
        tuple(waves),
        freeze(description),
    )


def validate_declarations(b):
    def require(condition, message):
        if not condition:
            raise GraphCompileError(message)

    require(isinstance(b.graph_id, str) and bool(b.graph_id), "invalid graph id")
    require(b.nodes and any(n.terminal for n in b.nodes), "graph needs a terminal")
    ids = [n.node_id for n in b.nodes]
    require(len(set(ids)) == len(ids), "duplicate node id")
    owners = []
    for key, required in b.inputs:
        require(
            isinstance(key, str) and key and type(required) is bool, "invalid input"
        )
        owners.append(key)
    catalog = {t.name for t in b.tools.declarations()} if b.tools is not None else set()
    for n in b.nodes:
        require(
            isinstance(n.node_id, str)
            and re.fullmatch(r"[a-z0-9_]{1,64}", n.node_id)
            and not n.node_id.startswith("__")
            and n.node_id not in {"start", "end", "error", "input", "output", "loop"},
            "invalid node id",
        )
        require(
            n.factory.kind in ("fn", "llm", "tool", "agent")
            and callable(n.factory.execute),
            "invalid factory",
        )
        require(type(n.skippable_by_config) is bool, "invalid skip declaration")
        for names in (n.required_reads, n.optional_reads, n.writes):
            require(
                all(isinstance(k, str) and k for k in names)
                and len(set(names)) == len(names),
                "invalid key declaration",
            )
        require(not set(n.required_reads) & set(n.optional_reads), "overlapping reads")
        owners.extend(n.writes)
        require(
            len(set(n.factory.tools)) == len(n.factory.tools)
            and all(t in catalog for t in n.factory.tools),
            "invalid tool whitelist",
        )
        if n.terminal:
            t = n.terminal
            require(t.kind in ("result", "no_action"), "invalid terminal")
            if t.kind == "result":
                require(
                    t.output_key in n.writes and t.reason_code is None,
                    "invalid output owner",
                )
            else:
                require(
                    t.output_key is None
                    and isinstance(t.reason_code, str)
                    and re.fullmatch(r"[a-z0-9_]{1,64}", t.reason_code),
                    "invalid reason code",
                )
            require(
                not any(e.source == n.node_id and e.kind != "error" for e in b.edges),
                "terminal has ordinary outgoing edge",
            )
    require(len(set(owners)) == len(owners), "duplicate writer")
    require(len(set(b.edges)) == len(b.edges), "duplicate edge")
    for e in b.edges:
        require(e.source in ids and e.target in ids, "unknown endpoint")
    require(
        len({r.source for r in b.routers}) == len(b.routers),
        "multiple condition groups",
    )
    for r in b.routers:
        require(r.source in ids and callable(r.path), "invalid router")
        require(
            r.path_map and all(isinstance(k, str) and k for k in r.path_map),
            "invalid path map",
        )
        require(
            not set(r.required_reads) & set(r.optional_reads),
            "overlapping router reads",
        )
        for targets in r.path_map.values():
            require(
                isinstance(targets, (str, tuple)) and bool(targets),
                "empty route target",
            )
    for n in b.nodes:
        incoming = [e for e in b.edges if e.target == n.node_id]
        errors = [e for e in incoming if e.kind == "error"]
        outgoing = [e for e in b.edges if e.source == n.node_id and e.kind == "error"]
        require(len(outgoing) <= 1, "multiple error targets")
        if errors:
            require(
                len(incoming) == 1 and not outgoing and not n.skippable_by_config,
                "error target must be exclusive",
            )
