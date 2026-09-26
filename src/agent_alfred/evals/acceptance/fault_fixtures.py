"""Closed, one-shot simulation actions at approved public product boundaries."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from agent_alfred.tools import ToolSuccess


def payload(result):
    return json.loads("".join(block.text for block in result.content))


def action_coverage(fixture, observations, requests):
    required = {
        "persona_version_conflict_v1": ["read_persona", "update_persona"],
        "file_publication_unknown_v1": ["draft_message"],
    }.get(fixture)
    if required is None:
        return {}
    started = {
        row["tool_name"] for row in requests if row["start_confirmation"] == "confirmed"
    }
    missing = [name for name in required if name not in started]
    return {
        "coverage": "not_covered" if missing or not observations else "requires_review",
        "required_model_tools": required,
        "missing_model_tools": missing,
    }


def persona_conflict(observations):
    def after_read(persona, result, context):
        if observations:
            return
        observed = payload(result)
        command = replace(
            context,
            call_id="fixture-" + uuid4().hex,
            step_index=None,
            source="acceptance_fixture",
            metering=None,
            events=None,
            node_id=None,
        )
        observation = {
            "boundary": "persona_read_return",
            "observed_at": datetime.now(UTC).isoformat(),
            "source": "simulation",
            "admission_scope": "active_run_exclusive_command",
            "trigger": {
                "run_id": context.run_id,
                "step_index": context.step_index,
                "call_id": context.call_id,
            },
            "command": {
                "tool_name": "update_persona",
                "run_id": command.run_id,
                "step_index": command.step_index,
                "call_id": command.call_id,
                "source": command.source,
            },
            "read": observed,
        }
        # Claim once before the public update and readback (which re-enters us).
        observations.append(observation)
        updated = persona.update(
            {
                "content": observed["content"]
                + "\n[acceptance persona conflict fixture v1]",
                "expected_version": observed["version"],
            },
            command,
        )
        observation["update_succeeded"] = isinstance(updated, ToolSuccess)
        observation["receipt"] = payload(updated)
        if not isinstance(updated, ToolSuccess):
            raise ValueError("persona_fixture_update_unverified")
        observation["readback"] = payload(persona.read({}, command))

    return after_read


def publication_unknown(observations):
    def before_create(operation_id):
        if observations:
            return
        observations.append(
            {
                "boundary": "durable_publication_intent_before_create",
                "observed_at": datetime.now(UTC).isoformat(),
                "source": "simulation",
                "operation_id": operation_id,
            }
        )
        raise OSError("acceptance_publication_fault")

    return before_create


def observe_recovery(host, state, operation_id, transport):
    from agent_alfred.messages import message_plain_text
    from agent_alfred.runtime.host import SubmitRequest

    # File commands run before any model preparation. The reopened Host also
    # has an empty, offline transport so a missed command cannot call a model.
    observations = []
    for prefix, kind in (
        ("查看操作 ", "explicit_public_operation_inspection"),
        ("恢复操作 ", "explicit_public_recovery_command"),
    ):
        session = host.create_session()
        command = prefix + operation_id
        accepted = host.submit(SubmitRequest(command, session_id=session))
        if accepted.kind != "accepted":
            raise ValueError("recovery_observation_admission_failed")
        settled = host.wait(accepted.run_id)
        evidence = host.read_run_evidence(accepted.run_id, trace_root=state / "traces")
        if transport.requests or evidence["attempts"]:
            raise ValueError("recovery_observation_model_dispatch_forbidden")
        output = message_plain_text(settled.reply) if settled.reply else None
        receipt = json.loads(output or "null")
        if not isinstance(receipt, dict) or receipt.get("operation_id") != operation_id:
            raise ValueError("recovery_observation_command_not_confirmed")
        observations.append(
            {
                "source": "simulation",
                "kind": kind,
                "observed_at": datetime.now(UTC).isoformat(),
                "run_id": accepted.run_id,
                "session_id": session,
                "command": command,
                "outcome": settled.outcome,
                "error": settled.error,
                "receipt": receipt,
                "evidence": evidence,
            }
        )
    before, recovered = observations
    return {**recovered, "before_recovery": before}
