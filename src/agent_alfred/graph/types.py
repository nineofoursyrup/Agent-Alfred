"""Immutable graph declarations and closed business results."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal


def freeze(value):
    """Own and recursively freeze values crossing the graph state boundary."""
    if isinstance(value, Mapping):
        return MappingProxyType({freeze(k): freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(freeze(v) for v in value)
    if value is None or type(value) in (str, int, float, bool, bytes):
        return value
    raise ValueError(f"unsupported state value: {type(value).__name__}")


class GraphCompileError(ValueError):
    pass


class ContextInvalid(ValueError):
    """Trusted business projection failure; survives provisional wave rollback."""


class GraphInvariantError(RuntimeError):
    pass


@dataclass(frozen=True)
class NodeOutcome:
    writes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NodeError:
    node_id: str | None
    code: str
    message: str
    side_effect_state: Literal["none", "occurred", "unknown"] = "none"


@dataclass(frozen=True)
class PureNodeContext:
    node_id: str
    error: NodeError | None = None


@dataclass(frozen=True)
class TerminalSpec:
    kind: str
    output_key: str | None = None
    reason_code: str | None = None

    @classmethod
    def result(cls, output_key):
        return cls("result", output_key=output_key)

    @classmethod
    def no_action(cls, reason_code):
        return cls("no_action", reason_code=reason_code)


@dataclass(frozen=True)
class Completed:
    output: Any


@dataclass(frozen=True)
class CompletedWithRecovery:
    output: Any
    recoveries: tuple[NodeError, ...]


@dataclass(frozen=True)
class NoAction:
    reason_code: str
    recoveries: tuple[NodeError, ...] = ()


@dataclass(frozen=True)
class Failed:
    error: NodeError
    side_effect_state: str
    committed_state: Mapping[str, Any]
    not_executed: tuple[str, ...] = ()
    forced_stop: Any = None


@dataclass(frozen=True)
class BudgetExhausted:
    error: NodeError
    side_effect_state: str
    committed_state: Mapping[str, Any]
    not_executed: tuple[str, ...] = ()


GraphResult = Completed | CompletedWithRecovery | NoAction | Failed | BudgetExhausted


@dataclass(frozen=True)
class NodeFactory:
    kind: str
    execute: Callable
    tools: tuple[str, ...] = ()


def fn_node(fn):
    return NodeFactory(
        "fn",
        lambda state, context: fn(
            state, PureNodeContext(context.node_id, context.error)
        ),
    )


def thaw(value):
    """Own a JSON view; tag bytes and non-string map keys without losing data."""
    import json

    if isinstance(value, Mapping):
        if all(isinstance(key, str) for key in value):
            return {key: thaw(item) for key, item in value.items()}
        return {"$map": [[thaw(key), thaw(item)] for key, item in value.items()]}
    if isinstance(value, (tuple, list)):
        return [thaw(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(
            (thaw(item) for item in value),
            key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False),
        )
    if isinstance(value, bytes):
        return {"$bytes_hex": value.hex()}
    return value
