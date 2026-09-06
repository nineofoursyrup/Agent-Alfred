"""Immutable FIFO transitions shared by ingress and connection queues.

Callers own synchronization and publish each replacement with one store.
The handed-off rotation remains reachable while reversal runs outside their
producer condition; an interrupted reversal can therefore be retried.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Self

from agent_alfred.gateway.web.frames import FrameCost


@dataclass(frozen=True, slots=True)
class FifoNode[T]:
    item: T
    cost: FrameCost
    next: FifoNode[T] | None


@dataclass(frozen=True, slots=True)
class FifoState[T]:
    front: FifoNode[T] | None
    back: FifoNode[T] | None
    rotation: FifoNode[T] | None
    usage: FrameCost
    size: int

    def pop_front(self) -> Self:
        front = self.front
        if front is None:
            raise RuntimeError("FIFO has no front to consume")
        return replace(
            self, front=front.next, usage=self.usage - front.cost,
            size=self.size - 1,
        )

    def begin_rotation(self) -> Self:
        if self.front is not None or self.rotation is not None:
            raise RuntimeError("FIFO already has a front or rotation")
        if self.back is None:
            raise RuntimeError("non-empty FIFO has no queue cell")
        return replace(self, back=None, rotation=self.back)

    def finish_rotation(
        self, source: FifoNode[T] | None, front: FifoNode[T] | None,
    ) -> Self:
        if self.front is None and self.rotation is source:
            return replace(self, front=front, rotation=None)
        return self


def reverse_nodes[T](node: FifoNode[T] | None) -> FifoNode[T] | None:
    """Build a local front without mutating any published queue state."""
    front = None
    while node is not None:
        front = FifoNode(item=node.item, cost=node.cost, next=front)
        node = node.next
    return front
