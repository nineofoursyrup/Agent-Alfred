"""User decisions bind a complete generated summary, never execution authority.

The source is an injected, independent read-only boundary. Local event fields and
hashes are not authentication. The default has no trusted source.
"""

from copy import deepcopy
from datetime import UTC, datetime
from typing import Protocol

from .schema import GROUPS, digest, unique
from .supplement_schema import closed, identity, signed, text

STAGES = ("materials", "results", "calibration", "thresholds")


class DecisionSource(Protocol):
    def read_decision(self, source_ref: str) -> dict: ...


def instant(value):
    from .report import instant as parse

    return parse(value)


def _prerequisite_decisions(batch):
    ids = {
        s["id"] for s in batch["summaries"] if s["stage"] in ("materials", "thresholds")
    }
    return [e for e in batch["user_decisions"] if e["object_id"] in ids]


def objects(batch, stage, *, _legacy_summary=False, _with_prerequisites=False):
    fields = [
        "contract",
        "phase",
        "candidate_id",
        "profiles",
        "judge_profile",
        "cases",
        "manifest",
        "semantic_rubric",
        "review_policy",
        "seen_families",
        "seen_materials",
        "judge_tests",
    ]
    value = {k: batch[k] for k in fields}
    from .supplement_reviews import active_reviews

    value["material_reviews"] = [
        r for r in active_reviews(batch) if r["target"]["type"] == "materials"
    ]
    if stage in ("results", "calibration"):
        from .supplement_reviews import disputes

        value.update(
            {
                k: batch.get(k, [])
                for k in (
                    "results",
                    "grades",
                    "judge_test_results",
                    "review_disputes",
                    "adjudications",
                    "review_adjudications",
                )
            }
        )
        value["disputes"] = disputes(batch, _include_blind=not _legacy_summary)
        if _with_prerequisites:
            value.update(
                prerequisite_decisions=_prerequisite_decisions(batch),
                aggregation_policy=batch["aggregation_policy"],
                calibration=batch["calibration"],
            )
    if stage == "thresholds":
        value.update(
            aggregation_policy=batch["aggregation_policy"],
            calibration=batch["calibration"],
        )
    return deepcopy(value)


def make_summary(
    batch, stage="materials", *, decision_source=None, now=None, store=None
):
    """Freeze a complete view; saved decision snapshots never grant authority."""
    if decision_source is None and store is not None:
        decision_source = store.decision_source
    calibration = None
    if batch["phase"] == "formal" and stage in ("results", "calibration"):
        if store is not None:
            calibration = store._validate_links(batch, ())
    return _current_summary(batch, stage, decision_source, now, calibration)


def _current_summary(batch, stage, source, now, calibration):
    if stage in ("results", "calibration"):
        now = datetime.now(UTC) if now is None else now
        events = batch["adjudications"] + batch.get("review_adjudications", [])
        events += _prerequisite_decisions(batch)
        decisions = [e for e in events if verified(e, batch, source, now)]
        version = 3 if batch["phase"] == "formal" else 2
        calibration_decisions = [
            e
            for e in _calibration_events(calibration)
            if verified(e, calibration, source, now)
        ]
        return _make_summary(
            batch,
            stage,
            version=version,
            decisions=decisions,
            calibration=calibration,
            calibration_decisions=calibration_decisions,
        )
    return _make_summary(batch, stage)


def _calibration_events(calibration):
    if calibration is None:
        return []
    return [
        e
        for key in ("adjudications", "review_adjudications", "user_decisions")
        for e in calibration.get(key, [])
    ]


def _calibration_times(calibration):
    if calibration is None:
        return []
    return [
        instant(row[key])
        for collection, key in (
            ("results", "finished_at"),
            ("grades", "completed_at"),
            ("judge_test_results", "completed_at"),
            ("review_disputes", "compared_at"),
        )
        for row in calibration.get(collection, [])
    ] + [instant(e["at"]) for e in _calibration_events(calibration)]


class _DecisionSnapshot:
    """Replay a saved display for integrity checks, never admission or approval."""

    def __init__(self, events):
        self.events = {e["source_ref"]: e for e in events}

    def read_decision(self, source_ref):
        return self.events[source_ref]


def _make_summary(
    batch,
    stage,
    *,
    version=1,
    decisions=(),
    legacy=False,
    calibration=None,
    calibration_decisions=(),
):
    if stage not in STAGES:
        raise ValueError("unknown_summary_stage")
    from .supplement_reviews import disputes

    snapshot = objects(
        batch, stage, _legacy_summary=legacy, _with_prerequisites=version >= 2
    )
    results = {r["case_id"]: r for r in batch["results"]}
    grades = {g["result_id"]: g for g in batch["grades"]}
    groups = {}
    for group in GROUPS:
        cases = [c for c in batch["cases"] if c["group"] == group]
        rows = []
        for case in cases:
            result = (
                results.get(case["id"]) if stage in ("results", "calibration") else None
            )
            grade = grades.get(result["id"]) if result else None
            rows.append(
                {
                    "case_id": case["id"],
                    "case_sha256": digest(case),
                    "material_id": case["material_id"],
                    "scene": case["scene"],
                    "source": case["source"],
                    "lineage": case["lineage"],
                    "obligations": case["obligations"],
                    "absent_categories": case["absent_categories"],
                    "applicability": case["applicability"],
                    "result": result,
                    "grade": grade,
                    "state": "material_draft"
                    if stage in ("materials", "thresholds")
                    else "not_run"
                    if result is None
                    else "judge_missing"
                    if grade is None
                    else grade["status"],
                    "originals": {
                        "case": "case:" + case["id"],
                        "result": "result:" + result["id"] if result else None,
                        "grade": grade["id"] if grade else None,
                    },
                }
            )
        groups[group] = {
            "cases": rows,
            "denominators": {
                d: sum(c["applicability"][d] for c in cases)
                for d in ("completion", "correctness", "selection")
            },
            "scenarios": {
                s: sum(c["scene"] == s for c in cases)
                for s in ("normal", "boundary", "failure_or_misleading")
            },
        }
    assessment = None
    if stage in ("results", "calibration"):
        # Freeze the observation time, including rulings made after review.
        # Current source eligibility is independently recomputed by approval().
        from .supplement_quality import quality

        times = [instant(r["finished_at"]) for r in batch["results"]]
        times += [instant(r["completed_at"]) for r in batch["judge_test_results"]]
        times += [instant(r["compared_at"]) for r in batch.get("review_disputes", [])]
        if version >= 2:
            times += [instant(g["completed_at"]) for g in batch["grades"]]
            times += [
                instant(e["at"])
                for e in batch["adjudications"] + batch.get("review_adjudications", [])
            ]
            times += [instant(e["at"]) for e in snapshot["prerequisite_decisions"]]
        times += _calibration_times(calibration)
        assessment = quality(
            batch,
            max(times, default=datetime(2000, 1, 1, tzinfo=UTC)),
            _DecisionSnapshot([*decisions, *calibration_decisions])
            if version >= 2
            else None,
            summary_mode=True,
            _legacy_summary=legacy,
            calibration=calibration,
        )
        if version == 3:
            assessment["calibration_assessment"] = (
                quality(
                    calibration,
                    max(times, default=datetime(2000, 1, 1, tzinfo=UTC)),
                    _DecisionSnapshot(calibration_decisions),
                )
                if calibration is not None
                else None
            )
    return signed(
        {
            "assessment": assessment,
            "version": version,
            **({"decision_snapshot": deepcopy(decisions)} if version >= 2 else {}),
            **(
                {
                    "calibration_snapshot": deepcopy(calibration),
                    "calibration_decision_snapshot": deepcopy(calibration_decisions),
                }
                if version == 3
                else {}
            ),
            "stage": stage,
            "contract": batch["contract"],
            "candidate_id": batch["candidate_id"],
            "object_sha256": digest(snapshot),
            "manifest": batch["manifest"],
            "groups": groups,
            "disputes": assessment["review_disputes"]
            if version >= 2
            else disputes(
                batch,
                materials_only=stage in ("materials", "thresholds"),
                _include_blind=not legacy,
            ),
            "objects": snapshot,
            "simulation": batch["simulation"],
            "conclusion": "summary_only_not_execution_or_release_permission",
        }
    )


def _rebuild_summary(batch, summary):
    """Check the claimed historical display without trusting its decisions."""
    decisions = summary.get("decision_snapshot", [])
    if not isinstance(decisions, list):
        raise ValueError("invalid_summary_decision_snapshot")
    events = batch["adjudications"] + batch.get("review_adjudications", [])
    events += _prerequisite_decisions(batch)
    if any(e not in events for e in decisions):
        raise ValueError("summary_decision_not_in_scope")
    unique(decisions, "source_ref")
    calibration = summary.get("calibration_snapshot")
    calibration_decisions = summary.get("calibration_decision_snapshot", [])
    if not isinstance(calibration_decisions, list):
        raise ValueError("invalid_summary_decision_snapshot")
    if calibration is not None:
        reference = summary["objects"].get("calibration")
        if (
            not isinstance(calibration, dict)
            or not reference
            or calibration.get("batch_id") != reference["batch_id"]
            or digest(calibration) != reference["sha256"]
            or calibration.get("schema_version") != 4
            or calibration.get("phase") != "calibration"
        ):
            raise ValueError("summary_calibration_mismatch")
        from .schema import validate

        validate(calibration)
    if any(e not in _calibration_events(calibration) for e in calibration_decisions):
        raise ValueError("summary_decision_not_in_scope")
    unique(calibration_decisions, "source_ref")
    # One authoritative reference cannot assert contradictory events across the
    # current batch and its dependency, even when replaying historical displays.
    by_ref = {}
    for event in [*decisions, *calibration_decisions]:
        previous = by_ref.setdefault(event["source_ref"], event)
        if previous != event:
            raise ValueError("summary_decision_source_conflict")
    current = _make_summary(
        batch,
        summary["stage"],
        version=summary["version"],
        decisions=decisions,
        calibration=calibration,
        calibration_decisions=calibration_decisions,
    )
    if summary["version"] == 1 and summary != current:
        return _make_summary(batch, summary["stage"], legacy=True)
    return current


def decision_request(summary):
    return {
        "object_type": "summary",
        "object_id": summary["id"],
        "object_sha256": digest(summary),
        "manifest": summary["manifest"],
    }


def dispute_request(batch, dispute):
    return {
        "object_type": "dispute",
        "object_id": dispute["id"],
        "object_sha256": digest(dispute),
        "manifest": batch["manifest"],
    }


def validate_event(event):
    closed(
        event,
        "id version kind subject source_ref at decision object_type "
        "object_id object_sha256 manifest reason evidence",
    )
    identity(event)
    if type(event["version"]) is not int or event["version"] != 1:
        raise ValueError("unknown_user_decision_version")
    if event["kind"] not in ("synthetic_user_decision", "user_decision"):
        raise ValueError("explicit_user_decision_required")
    for key in ("subject", "source_ref", "reason"):
        text(event[key])
    instant(event["at"])
    choices = {
        "summary": ("approved", "rejected"),
        "dispute": ("confirmed_violation", "dismissed", "insufficient_evidence"),
    }
    if (
        event["object_type"] not in choices
        or event["decision"] not in choices[event["object_type"]]
    ):
        raise ValueError("invalid_user_decision")
    from .schema import hash_value

    hash_value(event["object_id"])
    hash_value(event["object_sha256"])
    if not isinstance(event["manifest"], list) or not isinstance(
        event["evidence"], list
    ):
        raise ValueError("invalid_user_decision_scope")
    for ref in event["evidence"]:
        text(ref)
    if event["object_type"] == "dispute" and not event["evidence"]:
        raise ValueError("decision_evidence_required")


def verified(event, batch, source, now):
    if source is None or instant(event["at"]) > now:
        return False
    if event["kind"] == "synthetic_user_decision" and not batch["simulation"]:
        return False
    try:
        observed = source.read_decision(event["source_ref"])
        return observed == event
    except OSError, ValueError, KeyError:
        return False


def validate_records(batch):
    for key in ("summaries", "user_decisions", "adjudications", "review_adjudications"):
        if not isinstance(batch.get(key, []), list):
            raise ValueError("invalid_supplement_collection")
        unique(batch.get(key, []), "id")
    for summary in batch["summaries"]:
        closed(
            summary,
            "id version stage contract candidate_id object_sha256 manifest "
            "groups disputes objects simulation conclusion assessment"
            + (" decision_snapshot" if summary.get("version") in (2, 3) else "")
            + (
                " calibration_snapshot calibration_decision_snapshot"
                if summary.get("version") == 3
                else ""
            ),
        )
        identity(summary)
        if (
            type(summary["version"]) is not int
            or summary["version"] not in (1, 2, 3)
            or summary["stage"] not in STAGES
            or (
                summary["version"] in (2, 3)
                and summary["stage"] not in ("results", "calibration")
            )
            or (summary["version"] == 3 and summary["objects"].get("phase") != "formal")
        ):
            raise ValueError("unknown_summary_version")
        if digest(summary["objects"]) != summary["object_sha256"]:
            raise ValueError("summary_object_mismatch")
        # Old summaries keep their original identity and can be read without
        # their decision source. Only current source-bound views can be approved.
        current = _rebuild_summary(batch, summary)
        if summary["object_sha256"] == current["object_sha256"] and summary != current:
            raise ValueError("summary_coverage_mismatch")
    summaries = {s["id"]: s for s in batch["summaries"]}
    for event in batch["user_decisions"]:
        validate_event(event)
        target = summaries.get(event["object_id"])
        if target is None or any(
            event[k] != v for k, v in decision_request(target).items()
        ):
            raise ValueError("summary_decision_binding_mismatch")
    from .supplement_reviews import disputes

    targets = {d["id"]: d for d in disputes(batch)}
    for event in batch["adjudications"] + batch.get("review_adjudications", []):
        validate_event(event)
        target = targets.get(event["object_id"])
        if target is None:
            # Historical rulings can survive a new material identity. The parent
            # validator requires those exact bytes to come from an ancestor.
            continue
        if any(event[k] != v for k, v in dispute_request(batch, target).items()):
            raise ValueError("dispute_decision_binding_mismatch")
        from .score_evidence import resolve_reference
        from .supplement_reviews import blind_input

        data = blind_input(batch, target["target"])
        sources = data.get("sources", {"materials": data})
        for ref in event["evidence"]:
            if resolve_reference(ref, sources) in (None, "", [], {}):
                raise ValueError("decision_evidence_missing")
        if instant(event["at"]) < instant(target["at"]):
            raise ValueError("decision_before_dispute")


def approval(batch, stage, source, now, *, _calibration=None):
    by_id = {s["id"]: s for s in batch["summaries"]}
    candidates = [
        e for e in batch["user_decisions"] if by_id[e["object_id"]]["stage"] == stage
    ]
    if not candidates:
        return None
    if (
        batch["phase"] == "formal"
        and stage in ("results", "calibration")
        and _calibration is None
    ):
        return None
    summary = _current_summary(batch, stage, source, now, _calibration)
    events = [
        e
        for e in candidates
        if e["object_id"] == summary["id"] and verified(e, batch, source, now)
    ]
    if not events:
        return None
    latest_time = max(instant(e["at"]) for e in events)
    latest = [e for e in events if instant(e["at"]) == latest_time]
    if any(e["decision"] == "rejected" for e in latest):
        return None
    return latest[-1]


def cutoff(batch, inherited=None):
    times = [instant(r["sampled_at"]) for r in batch["results"]]
    times += [instant(r["started_at"]) for r in batch["judge_test_results"]]
    times += [instant(v) for v in (batch.get("budget_started_at"), inherited) if v]
    return min(times) if times else None


def validate_chronology(batch, inherited=None):
    bound = cutoff(batch, inherited)
    by_id = {s["id"]: s for s in batch["summaries"]}
    for event in batch["user_decisions"]:
        summary = by_id[event["object_id"]]
        if (
            summary["stage"] == "materials"
            and bound is not None
            and instant(event["at"]) > bound
        ):
            raise ValueError("material_decision_after_execution")
        review_ends = [
            instant(r["compared_at"]) for r in summary["objects"]["material_reviews"]
        ]
        if review_ends and instant(event["at"]) < max(review_ends):
            raise ValueError("decision_before_material_review")
        if summary["stage"] in ("results", "calibration"):
            grade_ends = [
                instant(g["completed_at"]) for g in summary["objects"]["grades"]
            ]
            ends = [instant(r["finished_at"]) for r in summary["objects"]["results"]]
            ends += [
                instant(r["completed_at"])
                for r in summary["objects"]["judge_test_results"]
            ]
            ends += _calibration_times(summary.get("calibration_snapshot"))
            ends += [
                instant(r["compared_at"]) for r in summary["objects"]["review_disputes"]
            ]
            ends += grade_ends
            ends += [
                instant(e["at"])
                for key in ("adjudications", "review_adjudications")
                for e in summary["objects"][key]
            ]
            ends += [
                instant(e["at"])
                for e in summary["objects"].get("prerequisite_decisions", [])
            ]
            if ends and instant(event["at"]) < max(ends):
                raise ValueError("decision_before_reviewed_results")


def validate_history(batch, parent):
    if parent and parent["schema_version"] != 4:
        raise ValueError("regrade_schema_changed")
    for key in (
        "summaries",
        "user_decisions",
        "review_disputes",
        "adjudications",
        "review_adjudications",
        "judge_test_results",
    ):
        previous = parent.get(key, []) if parent else []
        rows = batch.get(key, [])
        if rows[: len(previous)] != previous:
            raise ValueError("supplement_history_replaced")
        if key == "summaries":
            for summary in rows[len(previous) :]:
                if summary != _rebuild_summary(batch, summary):
                    raise ValueError("summary_coverage_mismatch")
