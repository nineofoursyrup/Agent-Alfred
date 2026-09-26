"""Explicit agent authority and provenance, never an invented human signature."""

from datetime import datetime

from .schema import digest, hash_value


def descriptor():
    value = {
        "version": 1,
        "authority": "agent-review-policy-r1",
        "model": "gpt-6-astra",
        "reasoning_effort": "xhigh",
        "fork_turns": "none",
        "fresh_instances": True,
        "blind_annotators": 2,
        "roles": [
            "material_approver",
            "blind_a",
            "blind_b",
            "adjudicator",
            "standards_reviewer",
            "spec_reviewer",
        ],
        "insufficient_evidence": "unknown_pending",
        "same_model_agreement": "not_proof",
        "authority_reference": "issue-84-user-agent-policy-2026-09-23",
    }
    return {**value, "id": digest(value)}


def validate_policy(value):
    if value != descriptor():
        raise ValueError("unsupported_review_policy")


def validate_actor(actor, *, role, policy):
    validate_policy(policy)
    fields = {
        "kind",
        "role",
        "agent_id",
        "model",
        "reasoning_effort",
        "fork_turns",
        "input_sha256",
        "output_sha256",
        "started_at",
        "completed_at",
        "reference",
    }
    if not isinstance(actor, dict) or set(actor) != fields:
        raise ValueError("invalid_agent_provenance")
    if (
        actor["kind"] != "agent"
        or actor["role"] != role
        or role not in policy["roles"]
        or any(
            actor[k] != policy[k] for k in ("model", "reasoning_effort", "fork_turns")
        )
        or not all(
            isinstance(actor[k], str) and actor[k].strip()
            for k in ("agent_id", "reference")
        )
    ):
        raise ValueError("invalid_agent_role")
    for key in ("input_sha256", "output_sha256"):
        hash_value(actor[key])
    start, end = (
        datetime.fromisoformat(actor[k]) for k in ("started_at", "completed_at")
    )
    if start.tzinfo is None or end.tzinfo is None or start > end:
        raise ValueError("invalid_agent_time")


def validate_approval(value, policy, *, role="material_approver"):
    validate_actor(value.get("actor"), role=role, policy=policy)
    if value.get("kind") != "agent_approved" or value.get("policy_id") != policy["id"]:
        raise ValueError("agent_approval_policy_mismatch")
    if value.get("by") != value["actor"]["agent_id"]:
        raise ValueError("agent_approval_identity_mismatch")
    if value.get("at") != value["actor"]["completed_at"]:
        raise ValueError("agent_approval_time_mismatch")


def validate_material_approval(value, batch):
    """Agent-reviewed schema3 materials are distinct from user execution consent."""
    from .schema import approval

    approval(value, allow_agent=batch["schema_version"] == 3)
    if value["kind"] == "agent_approved":
        validate_approval(value, batch.get("review_policy"))
    elif (
        value["kind"] == "approved"
        and batch["schema_version"] == 3
        and not batch["simulation"]
    ):
        raise ValueError("agent_approval_required")


def approval_valid(value, batch):
    if not value:
        return False
    if batch.get("schema_version", 1) != 3:
        return value.get("kind") == ("test" if batch["simulation"] else "approved")
    if value.get("kind") == "approved":
        return batch["simulation"]
    if value.get("kind") == "agent_approved":
        validate_approval(value, batch.get("review_policy"))
        return True
    return batch["simulation"] and value.get("kind") == "test"


def validate_panel(record, *, result_finished_at):
    """Bind two independently sealed blind outputs before the fresh adjudicator."""
    import hashlib

    policy = record["policy"]
    validate_actor(record["actor"], role="adjudicator", policy=policy)
    annotations = record["blind_annotations"]
    if not isinstance(annotations, list) or len(annotations) != 2:
        raise ValueError("two_blind_annotations_required")
    finished = datetime.fromisoformat(result_finished_at)
    if finished.tzinfo is None:
        raise ValueError("invalid_result_time")
    actors = [record["actor"]]
    for row, role in zip(annotations, ("blind_a", "blind_b")):
        if not isinstance(row, dict) or set(row) != {"actor", "output"}:
            raise ValueError("invalid_blind_annotation")
        validate_actor(row["actor"], role=role, policy=policy)
        if datetime.fromisoformat(row["actor"]["started_at"]) < finished:
            raise ValueError("blind_review_before_output")
        if (
            not isinstance(row["output"], str)
            or not row["output"].strip()
            or hashlib.sha256(row["output"].encode()).hexdigest()
            != row["actor"]["output_sha256"]
        ):
            raise ValueError("blind_output_hash_mismatch")
        if datetime.fromisoformat(
            row["actor"]["completed_at"]
        ) > datetime.fromisoformat(record["actor"]["started_at"]):
            raise ValueError("adjudication_before_blind_seal")
        actors.append(row["actor"])
    if len({a["agent_id"] for a in actors}) != 3:
        raise ValueError("agent_roles_not_independent")
    if (
        annotations[0]["actor"]["input_sha256"]
        != annotations[1]["actor"]["input_sha256"]
    ):
        raise ValueError("blind_input_mismatch")
    if record["at"] != record["actor"]["completed_at"]:
        raise ValueError("agent_adjudication_time_mismatch")


def validate_role_independence(*batches):
    """One known agent identity cannot acquire different roles in linked evidence.

    Repeated certificates and panels may retain the same identity in the same
    role. Human names, reviewer prose, and gate labels are not agent identities.
    """
    roles = {}
    for batch in batches:
        proofs = [
            batch.get(key) for key in ("case_set_approval", "calibration_approval")
        ]
        if batch.get("semantic_rubric"):
            proofs.append(batch["semantic_rubric"]["approval"])
        actors = [
            proof["actor"]
            for proof in proofs
            if proof and proof.get("kind") == "agent_approved"
        ]
        for key, kind in (
            ("adjudications", "agent_adjudication"),
            ("review_adjudications", "agent_review_adjudication"),
        ):
            for ruling in batch.get(key, []):
                if ruling.get("kind") == kind:
                    actors.append(ruling["actor"])
                    actors.extend(row["actor"] for row in ruling["blind_annotations"])
        for actor in actors:
            identity, role = actor["agent_id"], actor["role"]
            if identity in roles and roles[identity] != role:
                raise ValueError("agent_roles_not_independent")
            roles[identity] = role
