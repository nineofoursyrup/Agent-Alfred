"""Capture disposable evaluation inputs at the model boundary, with receipts.

This is local acceptance evidence, not additional product memory or telemetry.
Only registered aggregation attempts may expose their submitted source bodies.
"""

import json

from agent_alfred.messages import message_plain_text
from agent_alfred.model import ModelCallInterrupted


class InputCapture:
    def __init__(self, factory):
        self.factory = factory
        self.attempts = {}

    def create(self, snapshot):
        owner = self
        client = self.factory.create(snapshot)

        class Client:
            def respond(self, request, *, events=None, deadline=None):
                result = None
                try:
                    result = client.respond(request, events=events, deadline=deadline)
                    return result
                except ModelCallInterrupted as error:
                    result = error.result
                    raise
                finally:
                    if result is not None:
                        for attempt in result.attempts:
                            owner.attempts[attempt.attempt_id] = request

        return Client()

    def aggregation_evidence(self, evidence):
        confirmed = [
            a for a in evidence.get("memory", {}).get("input_attempts", [])
            if a.get("purpose") == "aggregation"
            and a.get("dispatch_state") == "sent"
        ]
        captured = []
        try:
            for attempt in confirmed:
                request = self.attempts[attempt["attempt_id"]]
                if len(request.messages) != 1 or request.tools:
                    raise ValueError("unexpected_aggregation_request")
                value = json.loads(message_plain_text(request.messages[0]))
                if not isinstance(value["provided_sources"], list):
                    raise ValueError("unexpected_aggregation_sources")
                captured.append({
                    "attempt_id": attempt["attempt_id"],
                    "goal": value["goal"],
                    "provided_sources": value["provided_sources"],
                })
        except (KeyError, ValueError, TypeError):
            return {"status": "unavailable", "attempts": []}
        return {"status": "available" if captured else "unavailable",
                "attempts": captured}
