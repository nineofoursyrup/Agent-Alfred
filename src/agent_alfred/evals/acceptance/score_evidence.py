"""Check recorded judge bytes and explicit references, without judging semantics."""

import json
import re

from .exact_format import unique_object
from .schema import scoring_rubric

SCORE_KEYS = ("dimensions", "prohibitions", "disputed", "suspected_safety")


def parse_judge_output(raw, case):
    value = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(value, dict) or set(value) != set(SCORE_KEYS):
        raise ValueError("invalid_judge_keys")
    for key, expected in (
        ("dimensions", case["applicability"]),
        ("prohibitions", case["forbidden"]),
    ):
        if not isinstance(value[key], dict) or set(value[key]) != set(expected):
            raise ValueError("incomplete_judge_output")
        for name, item in value[key].items():
            if (
                not isinstance(item, dict)
                or set(item) != {"status", "reason", "evidence"}
                or not isinstance(item["reason"], str)
                or item["status"] not in ("pass", "fail", "unknown", "na")
                or not item["reason"]
                or not item["evidence"]
            ):
                raise ValueError("invalid_judge_output")
            applicable = key == "prohibitions" or case["applicability"][name]
            if (item["status"] == "na") == applicable:
                raise ValueError("invalid_judge_applicability")
    if any(type(value[k]) is not bool for k in ("disputed", "suspected_safety")):
        raise ValueError("invalid_judge_flags")
    return value


def validate_grade_raw(grade, case):
    if grade["status"] != "scored" or grade.get("raw") is None:
        return
    try:
        parsed = parse_judge_output(grade["raw"], case)
        if any(parsed[key] != grade[key] for key in SCORE_KEYS):
            raise ValueError("grade_raw_mismatch")
    except ValueError, TypeError, KeyError:
        raise ValueError("grade_raw_mismatch") from None


def evidence_sources(batch, result):
    case = next(c for c in batch["cases"] if c["id"] == result["case_id"])
    sources = {
        result["id"]: result,
        "result:" + result["id"]: result,
        "case:" + case["id"]: case,
    }
    if scoring_rubric(batch):
        sources["rubric:" + scoring_rubric(batch)["id"]] = scoring_rubric(batch)
    return sources


def resolve_reference(ref, sources):
    try:
        identity, separator, pointer = ref.partition("#")
        value = sources[identity]
        if separator and pointer:
            if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
                raise ValueError("invalid_pointer")
            for token in pointer[1:].split("/"):
                token = token.replace("~1", "/").replace("~0", "~")
                if isinstance(value, list):
                    if not re.fullmatch(r"0|[1-9][0-9]*", token):
                        raise ValueError("invalid_index")
                    value = value[int(token)]
                else:
                    value = value[token]
        return value
    except AttributeError, KeyError, IndexError, TypeError, ValueError:
        raise ValueError("broken_score_evidence") from None


def validate_evidence(record, batch, result):
    sources = evidence_sources(batch, result)
    for kind in ("dimensions", "prohibitions"):
        for item in record[kind].values():
            refs = (
                item["evidence"]
                if isinstance(item["evidence"], list)
                else [item["evidence"]]
            )
            if not refs:
                raise ValueError("broken_score_evidence")
            for ref in refs:
                resolve_reference(ref, sources)
