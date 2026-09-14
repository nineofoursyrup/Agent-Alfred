"""Freeze unavailable configuration without inventing an endpoint or model."""

from dataclasses import dataclass


@dataclass(frozen=True)
class UnavailableModel:
    overall_deadline_s: float | None
    reason: str = "model_unavailable"
    endpoint_id: None = None
    model_id: None = None


class DeferredModel:
    def __init__(self, factory, snapshot):
        self.factory, self.snapshot = factory, snapshot
        self.client = None

    def respond(self, request, *, events=None, deadline=None):
        from agent_alfred.model import EndpointUnconfigured

        if isinstance(self.snapshot, UnavailableModel):
            raise EndpointUnconfigured("model_unavailable")
        if self.client is None:
            self.client = self.factory.create(self.snapshot)
        return self.client.respond(request, events=events, deadline=deadline)
