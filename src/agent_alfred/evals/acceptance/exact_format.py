"""Mechanical checks for explicitly declared formats, never semantic scoring."""

import json


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_json_key")
        value[key] = item
    return value


def contract(case):
    gold = case["gold"]
    value = gold.get("exact_format") if isinstance(gold, dict) else None
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "kind", "value"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or value["kind"] not in ("exact_lines", "json_object")
    ):
        raise ValueError("invalid_exact_format")
    expected = value["value"]
    if value["kind"] == "exact_lines":
        if (not isinstance(expected, list) or not expected
                or any(not isinstance(v, str) or "\n" in v or "\r" in v
                       for v in expected)):
            raise ValueError("invalid_exact_format")
    elif not isinstance(expected, dict):
        raise ValueError("invalid_exact_format")
    return value


def check(case, result):
    rule = contract(case)
    if rule is None:
        return {"status": "not_applicable"}
    output = result["output"]
    matched = False
    if isinstance(output, str):
        if rule["kind"] == "exact_lines":
            matched = output == "\n".join(rule["value"])
        else:
            try:
                value = json.loads(output, object_pairs_hook=unique_object)
                matched = json.dumps(value, sort_keys=True) == json.dumps(
                    rule["value"], sort_keys=True
                )
            except (ValueError, TypeError, RecursionError):
                pass
    return {
        "kind": rule["kind"], "version": rule["version"],
        "status": "pass" if matched else "fail",
        "evidence": ["case:" + case["id"] + "#/gold/exact_format",
                     "result:" + result["id"] + "#/output"],
    }


def conflicts(observation, grade):
    if observation["status"] != "fail":
        return []
    return [name for name in ("completion", "correctness")
            if grade["dimensions"].get(name, {}).get("status") == "pass"]
