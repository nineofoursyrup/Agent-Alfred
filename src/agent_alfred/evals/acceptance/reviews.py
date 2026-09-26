"""External review opinions, pinned to immutable evidence and distinct from rulings."""

import hashlib
from datetime import datetime

from .schema import (
    digest,
    hash_value,
    identifier,
    judge_identity,
    scoring_rubric,
    unique,
)
from .score_evidence import evidence_sources, resolve_reference


def binding(batch, grade_id):
    """Describe an existing grade; never manufacture missing legacy identities."""
    try:
        grade = next(g for g in batch["grades"] if g["id"] == grade_id)
        result = next(r for r in batch["results"] if r["id"] == grade["result_id"])
        case = next(c for c in batch["cases"] if c["id"] == result["case_id"])
        profile = next(p for p in batch["profiles"] if p["id"] == result["profile_id"])
    except StopIteration:
        raise ValueError("broken_review_binding") from None
    return {
        "batch_id": batch["batch_id"],
        "batch_sha256": digest(batch),
        "candidate_id": batch["candidate_id"],
        "contract": batch["contract"],
        "case_id": case["id"],
        "case_sha256": digest(case),
        "result_id": result["id"],
        "result_sha256": digest(result),
        "grade_id": grade["id"],
        "grade_sha256": digest(grade),
        "rubric_id": scoring_rubric(batch)["id"] if scoring_rubric(batch) else None,
        "judge_id": judge_identity(batch, profile),
        "judge_profile_sha256": digest(batch.get("judge_profile")),
    }


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("review_text_missing")


def _time(value):
    _text(value)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("review_timezone_required")
    return parsed


def _identity(record, fields, *, version=1):
    if not isinstance(record, dict) or set(record) != set(fields.split()):
        raise ValueError("invalid_review_fields")
    if type(record["version"]) is not int or record["version"] != version:
        raise ValueError("unknown_review_version")
    if record["id"] != digest({k: v for k, v in record.items() if k != "id"}):
        raise ValueError("review_identity_mismatch")
    _time(record["at"])
    _text(record["reason"])


def _evidence(refs, batch, result_id):
    if not isinstance(refs, list) or not refs or len(set(refs)) != len(refs):
        raise ValueError("invalid_review_evidence")
    result = next(r for r in batch["results"] if r["id"] == result_id)
    sources = evidence_sources(batch, result)
    for ref in refs:
        _text(ref)
        if resolve_reference(ref, sources) in (None, "", [], {}):
            raise ValueError("empty_review_evidence")


def validate(batch):
    records = batch.get("review_disputes", [])
    if not isinstance(records, list):
        raise ValueError("invalid_review_collection")
    unique(records, "id")
    observations = set()
    for record in records:
        _identity(record, "id version kind binding item reason evidence at source")
        if record["kind"] != "agent_review_dispute":
            raise ValueError("invalid_review_kind")
        target = record["binding"]
        expected = binding(batch, target["grade_id"])
        # The sample still belongs to its original batch and candidate. A new
        # review package does not refresh sampling time or rebind it to new code.
        expected.update(
            batch_id=target["batch_id"], batch_sha256=target["batch_sha256"]
        )
        identifier(target["batch_id"])
        hash_value(target["batch_sha256"])
        if target != expected:
            raise ValueError("review_binding_mismatch")
        result = next(r for r in batch["results"] if r["id"] == target["result_id"])
        if _time(record["at"]) < _time(result["finished_at"]):
            raise ValueError("review_before_sample")
        grade = next(g for g in batch["grades"] if g["id"] == target["grade_id"])
        item = record["item"]
        if (
            set(item) != {"kind", "name"}
            or item["kind"] not in ("dimensions", "prohibitions")
            or item["name"] not in grade[item["kind"]]
        ):
            raise ValueError("invalid_review_item")
        source = record["source"]
        if set(source) != {"kind", "name", "reference", "content", "sha256"}:
            raise ValueError("invalid_review_source")
        if source["kind"] != "agent":
            raise ValueError("review_is_not_adjudication")
        for field in ("name", "reference", "content"):
            _text(source[field])
        if hashlib.sha256(source["content"].encode()).hexdigest() != source["sha256"]:
            raise ValueError("review_source_mismatch")
        _evidence(record["evidence"], batch, target["result_id"])
        observation = (
            target["grade_sha256"],
            digest(item),
            source["name"],
            source["reference"],
            source["sha256"],
        )
        if observation in observations:
            raise ValueError("duplicate_review_observation")
        observations.add(observation)
    rulings = batch.get("review_adjudications", [])
    if not isinstance(rulings, list):
        raise ValueError("invalid_review_collection")
    unique(rulings, "id")
    unique(rulings, "review_id")
    by_id = {r["id"]: r for r in records}
    for ruling in rulings:
        agent = ruling.get("kind") == "agent_review_adjudication"
        if agent:
            _identity(
                ruling,
                "id version kind review_id policy actor blind_annotations "
                "at reason outcome evidence",
                version=2,
            )
            if (
                batch["schema_version"] == 3
                and ruling["policy"] != batch["review_policy"]
            ):
                raise ValueError("adjudication_policy_mismatch")
        else:
            if batch["schema_version"] == 3:
                raise ValueError("agent_adjudication_required")
            _identity(
                ruling, "id version kind review_id human at reason outcome evidence"
            )
            if ruling["kind"] != "human_review_adjudication":
                raise ValueError("explicit_human_adjudication_required")
            _text(ruling["human"])
        review = by_id.get(ruling["review_id"])
        if review is None:
            raise ValueError("broken_review_adjudication")
        if agent:
            from .review_policy import validate_panel

            result = next(
                r for r in batch["results"] if r["id"] == review["binding"]["result_id"]
            )
            validate_panel(ruling, result_finished_at=result["finished_at"])
        outcomes = (
            ("dismissed", "confirmed_violation", "insufficient_evidence")
            if agent
            else ("dismissed", "confirmed_violation")
        )
        if ruling["outcome"] not in outcomes:
            raise ValueError("invalid_review_adjudication_outcome")
        if _time(ruling["at"]) < _time(review["at"]):
            raise ValueError("review_adjudication_before_review")
        _evidence(ruling["evidence"], batch, review["binding"]["result_id"])


def validate_links(batch, parent, read_link):
    """A revision carries the entire history and cites a real ancestor package."""
    records = batch.get("review_disputes", [])
    previous = parent.get("review_disputes", []) if parent else []
    if records[: len(previous)] != previous:
        raise ValueError("review_history_replaced")
    if parent:
        for key in ("review_adjudications", "adjudications"):
            prior = parent.get(key, [])
            if records and batch.get(key, [])[: len(prior)] != prior:
                raise ValueError("review_adjudication_history_replaced")
    for record in records[len(previous) :]:
        target = record["binding"]
        ancestor = parent
        while ancestor and ancestor["batch_id"] != target["batch_id"]:
            link = ancestor["parent"]
            ancestor = read_link(link["batch_id"]) if link else None
        if ancestor is None or binding(ancestor, target["grade_id"]) != target:
            raise ValueError("review_source_binding_mismatch")


def assess(batch, now):
    """A dismissal clears this opinion only; original failures remain untouched."""
    failures, blockers, rows = [], [], []
    rulings = {a["review_id"]: a for a in batch.get("review_adjudications", [])}
    for review in batch.get("review_disputes", []):
        ruling = rulings.get(review["id"])
        status = "pending"
        if ruling is not None and _time(ruling["at"]) <= now:
            status = ruling["outcome"]
            if status == "insufficient_evidence":
                status = "pending"
                blockers.append("adjudication_required:" + review["binding"]["case_id"])
            if status == "confirmed_violation":
                failures.append("review_violation:" + review["id"])
        else:
            blockers.append("adjudication_required:" + review["binding"]["case_id"])
        if _time(review["at"]) > now:
            blockers.append("invalid_review_time:" + review["id"])
        rows.append(
            {
                "review_id": review["id"],
                "kind": review["kind"],
                "binding": review["binding"],
                "item": review["item"],
                "reason": review["reason"],
                "evidence": review["evidence"],
                "source": {k: v for k, v in review["source"].items() if k != "content"},
                "status": status,
                "adjudication": ruling,
            }
        )
    return {"failures": failures, "blockers": blockers, "records": rows}
