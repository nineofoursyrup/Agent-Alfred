"""Send only the frozen request; reconcile provisional reads with real receipts."""

from dataclasses import replace

from agent_alfred.aggregation.input import model_request
from agent_alfred.graph.types import thaw
from agent_alfred.runtime.input_budget import INPUT_VERSION, input_characters
from agent_alfred.runtime.memory import (
    InputEvidenceError,
    InputResolutionError,
    memory_context,
)


class AggregationModel:
    def __init__(
        self, *, item, graph_context, ledger, service, store, before_send=None
    ):
        self.item, self.context, self.ledger = item, graph_context, ledger
        self.service, self.store, self.before_send = service, store, before_send

    def respond(self, request, *, events=None, deadline=None):
        from agent_alfred.graph.nodes import NodeExecutionFailed
        from agent_alfred.messages import ToolCallBlock
        from agent_alfred.model import EndpointUnconfigured, ModelUnsupported

        prepared = thaw(self.context.committed_state["prepared"])
        pending = {}
        actual = model_request(prepared, request.model, request.max_tokens)
        items = prepared["items"]
        refs = [
            dict(
                kind=i["kind"],
                memory_id=i["memory_id"],
                record_version=i["record_version"],
            )
            for i in items
            if i["kind"] != "history"
        ]
        groups = [i["run_id"] for i in items if i["kind"] == "history"]
        identities = tuple(
            (r["kind"], r["memory_id"], r["record_version"]) for r in refs
        )

        def preflight(attempt_id, effective_deadline):
            self.context.checkpoint()
            if self.before_send is not None:
                self.before_send(self.item.run_id, attempt_id)
            self.context.checkpoint()
            if (
                effective_deadline is not None
                and self.context.clock.monotonic() >= effective_deadline
            ):
                from agent_alfred.runtime.memory import InputDeadlineExceeded

                raise InputDeadlineExceeded()
            explanation = dict(
                step_index=self.context.budget.used - 1,
                attempt_id=attempt_id,
                purpose="aggregation",
                references=refs,
                working_history_groups=groups,
                input_characters=input_characters(actual),
                measurement_version=INPUT_VERSION,
                input_limit=self.item.aggregation["settings"].input_character_limit,
            )
            try:
                with self.store.transaction() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    for ref in refs:
                        table = "facts" if ref["kind"] == "semantic" else "episodes"
                        row = conn.execute(
                            f"SELECT record_version FROM {table} WHERE id=?",
                            (ref["memory_id"],),
                        ).fetchone()
                        if row != (ref["record_version"],):
                            raise InputEvidenceError("input_sources_changed")
                    permissions = self.service.forgetting.evaluate_automatic_records(
                        identities, transaction=conn
                    )
                    history = self.service.forgetting.evaluate_history(
                        groups, purpose="working_window", transaction=conn
                    )
                    if any("error" in v or v["denied"] for v in (permissions, history)):
                        raise InputEvidenceError("input_sources_changed")
                    registered = self.service.forgetting.register_read(
                        self.item.run_id,
                        sources=groups,
                        memories=identities,
                        attempt_id=attempt_id,
                        purpose="aggregation",
                        context=memory_context(self.item),
                        input_explanation=explanation,
                        provisional=True,
                        transaction=conn,
                    )
                    if "error" in registered:
                        raise InputEvidenceError("input_evidence_unavailable")
                    self.service.forgetting.prepare_commit(conn)
                    pending[attempt_id] = explanation
                    conn.commit()
            except InputEvidenceError:
                raise
            except Exception as error:
                raise InputEvidenceError("input_evidence_unavailable") from error
            self.context.checkpoint()
            if (
                effective_deadline is not None
                and self.context.clock.monotonic() >= effective_deadline
            ):
                from agent_alfred.runtime.memory import InputDeadlineExceeded

                raise InputDeadlineExceeded()

        try:
            result = self.ledger.respond(
                replace(actual, on_attempt_preflight=preflight),
                events=events,
                deadline=deadline,
            )
            if result.response and any(
                isinstance(b, ToolCallBlock) for b in result.response.blocks
            ):
                raise NodeExecutionFailed("invalid_draft_tool_request")
            return result
        except (EndpointUnconfigured, ModelUnsupported) as error:
            raise NodeExecutionFailed("model_unavailable") from error
        finally:
            actual_ids = {
                a.attempt_id for r in self.ledger.model_results for a in r.attempts
            }
            for identity in pending:
                sent = identity in actual_ids
                resolved = self.service.forgetting.resolve_input_registration(
                    self.item.run_id,
                    identity,
                    sent=sent,
                    context=memory_context(self.item),
                )
                if "error" in resolved:
                    raise InputResolutionError("input_resolution_unavailable")
                if sent:
                    self.item.memory_telemetry["input_attempts"].append(
                        resolved["explanation"]
                    )
                else:
                    self.item.memory_telemetry.setdefault(
                        "input_not_sent_attempts", []
                    ).append(identity)
