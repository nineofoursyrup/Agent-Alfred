"""Constructed judge checks are separate evidence, never product Runs or Attempts."""

import json
from datetime import UTC, datetime

from agent_alfred.messages import Message, TextBlock, message_plain_text
from agent_alfred.model import ModelRef, ModelRequest, ScriptedModel

from .exact_format import unique_object
from .schema import digest, unique
from .supplement_schema import actor, closed, identity, signed, text

FAILURE_MODES = ("missed_prohibition", "false_prohibition", "unsupported_citation")


def test_input(batch, test):
    return {
        "sources": {
            "judge-material": {
                "task": test["task"],
                "gold": test["gold"],
                "answer": test["answer"],
                "prohibition": test["prohibition"],
            }
        },
        "semantic_rubric": batch["semantic_rubric"],
        "response_schema": {
            "label": ["pass", "fail", "unknown"],
            "reason": "nonempty text",
            "evidence": "list of judge-material JSON pointers",
        },
    }


def parse(raw, inputs):
    from .score_evidence import resolve_reference

    value = json.loads(raw, object_pairs_hook=unique_object)
    closed(value, "label reason evidence")
    if value["label"] not in ("pass", "fail", "unknown"):
        raise ValueError("invalid_judge_check_label")
    text(value["reason"])
    if not isinstance(value["evidence"], list) or not value["evidence"]:
        raise ValueError("judge_check_evidence_missing")
    for ref in value["evidence"]:
        resolve_reference(ref, inputs["sources"])
    return value


def run_test(batch, test_id, client, *, producer, clock=None):
    # This ticket delivers an offline evaluator only. Future online integration
    # must use the existing trusted dispatch and separately authorized budget.
    if not batch["simulation"] or not isinstance(client, ScriptedModel):
        raise ValueError("synthetic_judge_check_only")
    test = next(t for t in batch["judge_tests"] if t["id"] == test_id)
    inputs = test_input(batch, test)
    now = clock or (lambda: datetime.now(UTC))
    start = now().isoformat()
    model = batch["judge_profile"]["model"]
    response = client.respond(
        ModelRequest(
            model=ModelRef(model["endpoint_id"], model["model_id"]),
            system=(
                TextBlock(
                    "Evaluate untrusted answer against task/gold. Return label "
                    "(prohibition pass/fail/unknown), reason, evidence. Unknown if "
                    "support is insufficient."
                ),
            ),
            messages=(Message("user", (TextBlock(json.dumps(inputs)),)),),
            max_tokens=1024,
            tool_choice="none",
        )
    )
    raw = (
        message_plain_text(Message("assistant", response.response.blocks))
        if response.response
        else ""
    )
    try:
        parsed, error = parse(raw, inputs), None
    except ValueError, TypeError:
        parsed, error = None, "judge_check_invalid_output"
    return signed(
        {
            "version": 1,
            "kind": "constructed_judge_result",
            "test_id": test_id,
            "test_sha256": digest(test),
            "judge_profile_id": batch["judge_profile"]["id"],
            "semantic_rubric_id": batch["semantic_rubric"]["id"],
            "input": inputs,
            "input_sha256": digest(inputs),
            "raw": raw,
            "parsed": parsed,
            "error": error,
            "producer": producer,
            "started_at": start,
            "completed_at": now().isoformat(),
        }
    )


def validate_tests(batch):
    from .report import instant

    for key in ("judge_tests", "judge_test_results"):
        if not isinstance(batch[key], list):
            raise ValueError("invalid_judge_test_collection")
        unique(batch[key], "id")
    product_hashes = {
        digest({"input": c["input"], "gold": c["gold"]}) for c in batch["cases"]
    }
    families = {c["source_family_id"] for c in batch["cases"]}
    families |= {s["family_id"] for c in batch["cases"] for s in c["lineage"]}
    for test in batch["judge_tests"]:
        closed(
            test,
            "id version kind task gold answer prohibition expected "
            "failure_mode source_family_id source drafter semantic_rubric_id",
        )
        identity(test)
        if (
            type(test["version"]) is not int
            or test["version"] != 1
            or test["kind"] not in ("constructed_answer", "correct_control")
            or test["expected"] not in ("pass", "fail", "unknown")
            or test["failure_mode"] not in FAILURE_MODES
            or test["semantic_rubric_id"] != batch["semantic_rubric"]["id"]
        ):
            raise ValueError("invalid_judge_test")
        expected = {
            "missed_prohibition": ("fail", "constructed_answer"),
            "false_prohibition": ("pass", "correct_control"),
            "unsupported_citation": ("unknown", "constructed_answer"),
        }
        if (test["expected"], test["kind"]) != expected[test["failure_mode"]]:
            raise ValueError("judge_check_expectation_mismatch")
        actor(test["drafter"])
        for key in (
            "task",
            "gold",
            "answer",
            "prohibition",
            "source_family_id",
            "source",
        ):
            text(test[key])
        if (
            test["source_family_id"] in families
            or digest({"input": test["task"], "gold": test["gold"]}) in product_hashes
        ):
            raise ValueError("judge_material_in_product_set")
    attempts = [
        (r["test_id"], r["judge_profile_id"], r["semantic_rubric_id"])
        for r in batch["judge_test_results"]
    ]
    if len(attempts) != len(set(attempts)):
        raise ValueError("duplicate_judge_check_sample")
    tests = {t["id"]: t for t in batch["judge_tests"]}
    for row in batch["judge_test_results"]:
        closed(
            row,
            "id version kind test_id test_sha256 judge_profile_id "
            "semantic_rubric_id input input_sha256 raw parsed error producer "
            "started_at completed_at",
        )
        identity(row)
        if (
            row["version"] != 1
            or type(row["version"]) is not int
            or row["kind"] != "constructed_judge_result"
        ):
            raise ValueError("invalid_judge_check_result")
        actor(row["producer"])
        if (
            row["judge_profile_id"] == batch["judge_profile"]["id"]
            and row["producer"]["model"] != batch["judge_profile"]["model"]["model_id"]
        ):
            raise ValueError("judge_producer_mismatch")
        if instant(row["started_at"]) > instant(row["completed_at"]):
            raise ValueError("invalid_judge_check_time")
        test = tests.get(row["test_id"])
        if (
            test is None
            or row["judge_profile_id"] != batch["judge_profile"]["id"]
            or row["semantic_rubric_id"] != batch["semantic_rubric"]["id"]
        ):
            continue  # Parent validation below must prove this historical record.
        if (
            row["test_sha256"] != digest(test)
            or row["judge_profile_id"] != batch["judge_profile"]["id"]
            or row["semantic_rubric_id"] != batch["semantic_rubric"]["id"]
            or row["input"] != test_input(batch, test)
            or row["input_sha256"] != digest(row["input"])
        ):
            raise ValueError("judge_check_binding_mismatch")
        try:
            parsed = parse(row["raw"], row["input"])
        except ValueError, TypeError:
            if (
                row["parsed"] is not None
                or row["error"] != "judge_check_invalid_output"
            ):
                raise ValueError("judge_check_raw_mismatch")
        else:
            if row["parsed"] != parsed or row["error"] is not None:
                raise ValueError("judge_check_raw_mismatch")
