"""Explicit, session-bound draft requests; independent of ordinary chat."""

from dataclasses import dataclass

KINDS = ("semantic", "episodic", "history")


@dataclass(frozen=True)
class AggregationRequest:
    session_id: str
    goal: str
    keywords: str
    sources: tuple[str, ...]

    def __post_init__(self):
        if not all(type(x) is str for x in (self.session_id, self.goal, self.keywords)):
            raise ValueError("invalid_aggregation_request")
        if not self.session_id or not self.goal.strip():
            raise ValueError("invalid_aggregation_request")
        if (
            not isinstance(self.sources, (tuple, list))
            or any(x not in KINDS for x in self.sources)
            or len(set(self.sources)) != len(self.sources)
        ):
            raise ValueError("invalid_aggregation_sources")
        object.__setattr__(
            self, "sources", tuple(k for k in KINDS if k in self.sources)
        )
        if any(k in self.sources for k in ("semantic", "episodic")):
            if not self.keywords.strip():
                raise ValueError("aggregation_keywords_required")
