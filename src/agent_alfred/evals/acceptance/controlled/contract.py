"""Closed execution plan, actual wire payload and exact worst-case accounting."""

from copy import deepcopy

from agent_alfred.model import ModelRequest, NamedToolChoice, tool_schema_jsonable
from agent_alfred.openai_compatible import _to_wire_messages

from ..schema import digest, encode, hash_value
from ..supplement_decisions import instant

CONTRACT = "V1-CONTROLLED-CALIBRATION"
URL = "https://api.deepseek.com/chat/completions"
USD_SCALE = 10**12  # integer picodollars; no binary floating point in the ledger
CAP_UNITS = 25 * USD_SCALE
LIMITS = {"flash": 480, "pro": 96, "total": 576, "per_case": 16}
MODELS = {
    "flash": ("deepseek-flash", 20000, 8192),
    "pro": ("deepseek-v4-pro", 24000, 16384),
}
KINDS = {
    "product": ("product", "flash"),
    "auxiliary": ("product", "flash"),
    "judge_test": ("diagnostic", "pro"),
    "judge_review": ("diagnostic", "pro"),
    "grade": ("grading", "pro"),
    "grade_review": ("grading", "pro"),
}
PHASES = ("diagnostic", "checkpoint", "product", "grading", "closed")
COVERAGE = ["input", "output", "cache", "reasoning", "request", "other", "tax", "fx"]


def exact(value, fields, error="controlled_contract_invalid"):
    if type(value) is not dict or set(value) != set(fields.split()):
        raise ValueError(error)
    return value


def integer(value, *, minimum=0, maximum=None):
    if (
        type(value) is not int
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise ValueError("controlled_integer_invalid")
    return value


def text(value):
    if type(value) is not str or not value or len(value) > 256:
        raise ValueError("controlled_identity_invalid")
    return value


def signed(value):
    result = deepcopy(value)
    result["id"] = digest(result)
    return result


def wire_payload(request):
    """Serialize once; these exact bytes, including system/tools, are sent."""
    if type(request) is not ModelRequest:
        raise ValueError("model_request_required")
    payload = {
        "model": request.model.model_id,
        "messages": _to_wire_messages(request),
        "max_tokens": request.max_tokens,
        "thinking": {"type": request.thinking},
        "stream": False,
    }
    if request.response_format is not None:
        payload["response_format"] = {"type": request.response_format}
    if request.tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": tool_schema_jsonable(t.input_schema),
                },
            }
            for t in request.tools
        ]
        payload["tool_choice"] = (
            {"type": "function", "function": {"name": request.tool_choice.name}}
            if isinstance(request.tool_choice, NamedToolChoice)
            else request.tool_choice
        )
    # Conversion to canonical bytes fails before any credential access.
    encode(payload)
    return payload


def validate_payload(payload, operation):
    group = KINDS[operation["kind"]][1]
    model, _, output = MODELS[group]
    required = {"model", "messages", "max_tokens", "thinking", "stream"}
    optional = {"tools", "tool_choice"} | (
        {"response_format"} if group == "pro" else set()
    )
    if (
        type(payload) is not dict
        or set(payload) - required - optional
        or required - set(payload)
        or payload["model"] != model
        or payload["max_tokens"] != output
        or type(payload["max_tokens"]) is not int
        or payload["thinking"] != {"type": "disabled"}
        or payload["stream"] is not False
        or (
            group == "pro" and payload.get("response_format") != {"type": "json_object"}
        )
        or type(payload["messages"]) is not list
        or not payload["messages"]
        or ("tools" in payload) != ("tool_choice" in payload)
    ):
        raise ValueError("controlled_payload_policy_mismatch")
    if len(encode(payload)) > 512 * 1024:
        raise ValueError("controlled_payload_size_exceeded")


def execution_plan(
    *,
    binding,
    material_ref,
    worker,
    controller,
    operations,
    pricing,
    initial_phase="diagnostic",
    cap_units=CAP_UNITS,
    execution_mode="authorized",
    runtime_candidate=None,
):
    candidate_id = (
        digest(runtime_candidate)
        if runtime_candidate is not None
        else binding["candidate_id"]
    )
    value = signed(
        {
            "contract": CONTRACT,
            "version": 1,
            "binding_sha256": digest(binding),
            "candidate_id": candidate_id,
            "material_candidate_id": binding["candidate_id"],
            "runtime_candidate": deepcopy(runtime_candidate),
            "candidate_change": {
                "before": binding["candidate_id"],
                "after": candidate_id,
            },
            "material_ref": deepcopy(material_ref),
            "worker": worker,
            "controller": controller,
            "operations": deepcopy(operations),
            "pricing": deepcopy(pricing),
            "initial_phase": initial_phase,
            "limits": dict(LIMITS),
            "cap_units": cap_units,
            "total_seconds": 10800,
            "attempt_seconds": 120,
            "run_seconds": 900,
            "endpoint": URL,
            "retries": 0,
            "fallback": False,
            "execution_mode": execution_mode,
        }
    )
    validate_plan(value)
    return value


def initial_budget(plan):
    """The exact initial budget object covered by the #100 run decision."""
    validate_plan(plan)
    return {
        "cap_units": plan["cap_units"],
        "limits": deepcopy(plan["limits"]),
        "total_seconds": plan["total_seconds"],
        "currency": "USD",
        "scale": USD_SCALE,
    }


def operation_for(batch, *, operation_id, kind, object_id, instance_id, max_calls=1):
    """Bind a purpose to an actual frozen object, without sending its gold fields."""
    if kind not in KINDS:
        raise ValueError("controlled_operation_invalid")
    diagnostic = kind in ("judge_test", "judge_review")
    target = next(
        (
            row
            for row in batch["judge_tests" if diagnostic else "cases"]
            if row["id"] == object_id
        ),
        None,
    )
    if target is None:
        raise ValueError("plan_material_object_mismatch")
    return {
        "id": operation_id,
        "kind": kind,
        "object_id": object_id,
        "case_id": None if diagnostic else object_id,
        "instance_id": instance_id,
        "data_scope_sha256": digest(target),
        "max_calls": max_calls,
    }


def validate_plan(plan):
    exact(
        plan,
        "id contract version binding_sha256 candidate_id material_candidate_id "
        "runtime_candidate candidate_change material_ref "
        "worker controller operations pricing initial_phase limits cap_units "
        "total_seconds attempt_seconds "
        "run_seconds endpoint retries fallback execution_mode",
    )
    if (
        plan["id"] != digest({k: v for k, v in plan.items() if k != "id"})
        or plan["contract"] != CONTRACT
        or type(plan["version"]) is not int
        or plan["version"] != 1
        or plan["limits"] != LIMITS
        or plan["endpoint"] != URL
        or plan["total_seconds"] != 10800
        or plan["attempt_seconds"] != 120
        or plan["run_seconds"] != 900
        or type(plan["retries"]) is not int
        or plan["retries"] != 0
        or plan["fallback"] is not False
        or plan["initial_phase"] not in PHASES[:-1]
        or plan["execution_mode"]
        not in ("authorized", "synthetic_replay", "synthetic_capacity")
        or (
            plan["initial_phase"] != "diagnostic"
            and plan["execution_mode"] != "synthetic_capacity"
        )
    ):
        raise ValueError("controlled_plan_invalid")
    integer(plan["cap_units"], minimum=1, maximum=CAP_UNITS)
    for value in plan["limits"].values():
        integer(value, minimum=1)
    for key in ("binding_sha256", "candidate_id", "material_candidate_id"):
        hash_value(plan[key])
    if plan["candidate_change"] != {
        "before": plan["material_candidate_id"],
        "after": plan["candidate_id"],
    }:
        raise ValueError("runtime_candidate_mismatch")
    candidate = plan["runtime_candidate"]
    if candidate is not None and (
        type(candidate) is not dict
        or type(candidate.get("files")) is not dict
        or digest(candidate) != plan["candidate_id"]
    ):
        raise ValueError("runtime_candidate_mismatch")
    for key in ("worker", "controller"):
        text(plan[key])
    if plan["worker"] == plan["controller"]:
        raise ValueError("controller_worker_identity_conflict")
    if type(plan["operations"]) is not list or not 1 <= len(plan["operations"]) <= 216:
        raise ValueError("controlled_plan_invalid")
    ids, instances, counts, cases = set(), set(), {}, set()
    for op in plan["operations"]:
        exact(op, "id kind object_id case_id instance_id data_scope_sha256 max_calls")
        for key in ("id", "object_id", "instance_id"):
            text(op[key])
        hash_value(op["data_scope_sha256"])
        if op["kind"] not in KINDS or op["id"] in ids:
            raise ValueError("controlled_operation_invalid")
        ids.add(op["id"])
        group = KINDS[op["kind"]][1]
        integer(op["max_calls"], minimum=1, maximum=16 if group == "flash" else 1)
        if group == "pro":
            if op["instance_id"] in instances:
                raise ValueError("judge_instance_reused")
            instances.add(op["instance_id"])
            counts[op["kind"]] = counts.get(op["kind"], 0) + 1
        else:
            text(op["case_id"])
            cases.add(op["case_id"])
    if len(cases) > 30 or any(
        counts.get(k, 0) > n
        for k, n in (
            ("judge_test", 18),
            ("judge_review", 18),
            ("grade", 30),
            ("grade_review", 30),
        )
    ):
        raise ValueError("controlled_plan_quota_exceeded")
    if type(plan["pricing"]) is not dict or set(plan["pricing"]) != {"flash", "pro"}:
        raise ValueError("billing_unverifiable")
    for group, terms in plan["pricing"].items():
        validate_terms(terms, group)


def validate_terms(terms, group):
    exact(
        terms,
        "version model currency input_per_million output_per_million request_fee "
        "other_fee tax_numerator tax_denominator fx_numerator fx_denominator quantum "
        "coverage valid_from valid_until evidence",
        "billing_unverifiable",
    )
    if (
        type(terms["version"]) is not int
        or terms["version"] != 1
        or terms["currency"] != "USD"
        or terms["model"] != MODELS[group][0]
        or terms["coverage"] != COVERAGE
        or not terms["evidence"]
    ):
        raise ValueError("billing_unverifiable")
    for key in (
        "input_per_million",
        "output_per_million",
        "request_fee",
        "other_fee",
        "tax_numerator",
    ):
        integer(terms[key])
    for key in ("tax_denominator", "fx_numerator", "fx_denominator", "quantum"):
        integer(terms[key], minimum=1)
    if instant(terms["valid_from"]) >= instant(terms["valid_until"]):
        raise ValueError("billing_unverifiable")


def cost_units(terms, input_tokens, output_tokens):
    """Round once upwards to the independently attested billing quantum."""
    integer(input_tokens)
    integer(output_tokens)
    numerator = (
        input_tokens * terms["input_per_million"]
        + output_tokens * terms["output_per_million"]
        + (terms["request_fee"] + terms["other_fee"]) * 1000000
    )
    numerator *= (terms["tax_denominator"] + terms["tax_numerator"]) * terms[
        "fx_numerator"
    ]
    denominator = (
        1000000 * terms["tax_denominator"] * terms["fx_denominator"] * terms["quantum"]
    )
    return ((numerator + denominator - 1) // denominator) * terms["quantum"]
