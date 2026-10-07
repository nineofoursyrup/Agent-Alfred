"""Closed execution plan, actual wire payload and exact worst-case accounting."""

from copy import deepcopy

from agent_alfred.model import ModelRequest, NamedToolChoice, tool_schema_jsonable
from agent_alfred.openai_wire import to_wire_messages

from ..schema import digest, encode, hash_value
from ..supplement_decisions import instant
from .local_tokens import JUDGE_INPUT_MEASUREMENT

CONTRACT = "V1-CONTROLLED-CALIBRATION"
ADVISORY_CONTRACT = "V2-LOCAL-ADVISORY-CALIBRATION"
FLASH_CONTRACT = "V3-LOCAL-FLASH-CALIBRATION"
FLASH_POLICY = "flash_only"
FLASH_LIMITS = {"flash": 576, "pro": 0, "total": 576, "per_case": 16}
URL = "https://api.deepseek.com/chat/completions"
USD_SCALE = 10**12  # integer picodollars; no binary floating point in the ledger
MAX_CONTROL_BYTES = 64 * 1024
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
ADVISORY_BUDGET = "advisory_dispatch"
ADVISORY_RISKS = [
    "actual_input_tokens_may_exceed_preflight",
    "final_charge_may_exceed_planned_amount",
    "no_proven_final_bill_ceiling",
]
ADVISORY_NOTICE_ZH = (
    "USD25仅为本地计划派发阈值，供应商最终账单可能超过USD25，"
    "且没有已证明的最高超支额；Flash20000/Pro24000仅是完整请求的本地输入预检估计阈值，"
    "实际计费输入token可能超出，须以响应usage事后核验。"
)


def flash_input_policy():
    return {
        "contract": "V1-LOCAL-FLASH-INPUT-BY-PURPOSE",
        "product_max": 20000,
        "product_measurement": "original_complete_wire_utf8_planning_estimate",
        "judge_max": 32000,
        "judge_measurement": deepcopy(JUDGE_INPUT_MEASUREMENT),
    }


def input_limit(plan, operation):
    if operation["kind"] not in KINDS:
        raise ValueError("controlled_operation_invalid")
    if plan.get("model_policy") == FLASH_POLICY:
        policy = plan["input_policy"]
        return policy["product_max" if is_product(operation) else "judge_max"]
    return MODELS[model_group(plan, operation)][1]


def advisory_run_disclosure(target_units, *, judge_profile_change=None):
    integer(target_units, minimum=1, maximum=CAP_UNITS)
    value = {
        "contract": "V2-LOCAL-ADVISORY-RUN-DISCLOSURE",
        "version": 2,
        "budget_policy": ADVISORY_BUDGET,
        "target_units": target_units,
        "target_currency": "USD",
        "flash_input_preflight_max": MODELS["flash"][1],
        "pro_input_preflight_max": MODELS["pro"][1],
        "final_bill_ceiling_proven": False,
        "actual_input_ceiling_proven": False,
        "risks": list(ADVISORY_RISKS),
        "notice_zh": ADVISORY_NOTICE_ZH,
    }
    if judge_profile_change is not None:
        validate_profile_change(judge_profile_change)
        value.update(
            contract="V3-LOCAL-FLASH-RUN-DISCLOSURE",
            version=3,
            model_policy=FLASH_POLICY,
            judge_profile_change=deepcopy(judge_profile_change),
            limits=dict(FLASH_LIMITS),
            product_attempt_limit=480,
            judge_attempt_limit=96,
            output_max=MODELS["flash"][2],
            input_policy=flash_input_policy(),
            cross_model_independence=False,
            schema4_material_approval_inherited=False,
            notice_zh=(
                "本独立批次所有产品、裁判和盲复核仅使用deepseek-flash；"
                "产品/辅助沿用原字节估计和输入预检20000；裁判/盲复核输入预检32000，"
                "使用固定官方V4分词数据和tokenizers0.22.2计数完整wire再加256。"
                "所有输出8192，thinking disabled，无重试或fallback。"
                "总Attempt576（产品及辅助480、裁判及复核96），总时限10800秒；"
                "实例独立但不具跨模型独立性。原Pro证据、争议、未知费用保留。"
                "本次精确运行批准含模型差异，不继承新profile的schema4材料批准，"
                "不替代诊断后的质量检查点。USD25仅为本地计划派发阈值，"
                "最终账单可能超过USD25且无已证明的最高超支额；"
                "实际计费输入token可能超出预检，须以usage事后核验。"
            ),
        )
        value.pop("pro_input_preflight_max")
        value.pop("flash_input_preflight_max")
    return value


def is_product(operation):
    return operation["kind"] in ("product", "auxiliary")


def model_group(plan, operation):
    return (
        "flash"
        if plan.get("model_policy") == FLASH_POLICY
        else KINDS[operation["kind"]][1]
    )


def group_for_model(model):
    for group, (identity, _, _) in MODELS.items():
        if identity == model:
            return group
    raise ValueError("controlled_model_invalid")


def flash_profile(profile):
    result = deepcopy(profile)
    result["model"]["model_id"] = MODELS["flash"][0]
    result["independence_policy"] = "distinct_instances_same_model"
    result.pop("id", None)
    return signed(result)


def validate_profile_change(change):
    exact(change, "before after", "runtime_judge_profile_mismatch")
    before = change["before"]
    if (
        type(before) is not dict
        or before.get("id") != digest({k: v for k, v in before.items() if k != "id"})
        or type(before.get("model")) is not dict
        or before["model"].get("endpoint_id") != "deepseek"
        or before["model"].get("model_id") != MODELS["pro"][0]
        or change["after"] != flash_profile(before)
    ):
        raise ValueError("runtime_judge_profile_mismatch")


def effective_judge_profile(plan, original):
    if plan.get("model_policy") != FLASH_POLICY:
        return deepcopy(original)
    change = plan["judge_profile_change"]
    validate_profile_change(change)
    if change["before"] != original:
        raise ValueError("runtime_judge_profile_mismatch")
    return deepcopy(change["after"])


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
        "messages": to_wire_messages(request),
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


def validate_payload(payload, operation, *, plan=None):
    group = model_group(plan or {}, operation)
    judge = not is_product(operation)
    model, _, output = MODELS[group]
    required = {"model", "messages", "max_tokens", "thinking", "stream"}
    optional = {"tools", "tool_choice"} | ({"response_format"} if judge else set())
    if (
        type(payload) is not dict
        or set(payload) - required - optional
        or required - set(payload)
        or payload["model"] != model
        or payload["max_tokens"] != output
        or type(payload["max_tokens"]) is not int
        or payload["thinking"] != {"type": "disabled"}
        or payload["stream"] is not False
        or (judge and payload.get("response_format") != {"type": "json_object"})
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
    budget_policy=None,
    flash_judge_profile=None,
):
    candidate_id = (
        digest(runtime_candidate)
        if runtime_candidate is not None
        else binding["candidate_id"]
    )
    if budget_policy not in (None, ADVISORY_BUDGET):
        raise ValueError("controlled_budget_policy_invalid")
    body = {
        "contract": ADVISORY_CONTRACT if budget_policy == ADVISORY_BUDGET else CONTRACT,
        "version": 2 if budget_policy == ADVISORY_BUDGET else 1,
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
    if budget_policy == ADVISORY_BUDGET:
        body["budget_policy"] = ADVISORY_BUDGET
    if flash_judge_profile is not None:
        body.update(
            contract=FLASH_CONTRACT,
            version=3,
            model_policy=FLASH_POLICY,
            input_policy=flash_input_policy(),
            limits=dict(FLASH_LIMITS),
            judge_profile_change={
                "before": deepcopy(flash_judge_profile),
                "after": flash_profile(flash_judge_profile),
            },
        )
    value = signed(body)
    validate_plan(value)
    return value


def initial_budget(plan):
    """The exact initial budget object covered by the #100 run decision."""
    validate_plan(plan)
    budget = {
        "cap_units": plan["cap_units"],
        "limits": deepcopy(plan["limits"]),
        "total_seconds": plan["total_seconds"],
        "currency": "USD",
        "scale": USD_SCALE,
    }
    if plan["version"] >= 2:
        budget["budget_policy"] = ADVISORY_BUDGET
        budget["final_bill_ceiling_proven"] = False
    if plan["version"] == 3:
        budget["input_policy"] = deepcopy(plan["input_policy"])
    return budget


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
    if type(plan) is not dict:
        raise ValueError("controlled_contract_invalid")
    version = plan.get("version")
    if type(version) is not int or version not in (1, 2, 3):
        raise ValueError("controlled_plan_invalid")
    exact(
        plan,
        "id contract version binding_sha256 candidate_id material_candidate_id "
        "runtime_candidate candidate_change material_ref "
        "worker controller operations pricing initial_phase limits cap_units "
        "total_seconds attempt_seconds "
        "run_seconds endpoint retries fallback execution_mode"
        + (" budget_policy" if version >= 2 else "")
        + (" model_policy judge_profile_change input_policy" if version == 3 else ""),
    )
    if (
        plan["id"] != digest({k: v for k, v in plan.items() if k != "id"})
        or plan["contract"]
        != {1: CONTRACT, 2: ADVISORY_CONTRACT, 3: FLASH_CONTRACT}[version]
        or type(plan["version"]) is not int
        or plan["version"] != version
        or plan["limits"] != (FLASH_LIMITS if version == 3 else LIMITS)
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
    if version >= 2 and (
        plan["budget_policy"] != ADVISORY_BUDGET
        or plan["execution_mode"] != "authorized"
        or plan["runtime_candidate"] is None
        or plan["cap_units"] != CAP_UNITS
    ):
        raise ValueError("controlled_budget_policy_invalid")
    integer(plan["cap_units"], minimum=1, maximum=CAP_UNITS)
    for key, value in plan["limits"].items():
        integer(value, minimum=0 if version == 3 and key == "pro" else 1)
    if version == 3:
        if digest(plan["input_policy"]) != digest(flash_input_policy()):
            raise ValueError("controlled_input_policy_invalid")
        if plan["model_policy"] != FLASH_POLICY:
            raise ValueError("controlled_model_policy_invalid")
        validate_profile_change(plan["judge_profile_change"])
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
        integer(op["max_calls"], minimum=1, maximum=16 if is_product(op) else 1)
        if not is_product(op):
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
    if type(plan["pricing"]) is not dict or set(plan["pricing"]) != (
        {"flash"} if version == 3 else {"flash", "pro"}
    ):
        raise ValueError("billing_unverifiable")
    for group, terms in plan["pricing"].items():
        validate_terms(terms, group, advisory=version >= 2)


def validate_terms(terms, group, *, advisory=False):
    exact(
        terms,
        "version model currency input_per_million output_per_million request_fee "
        "other_fee tax_numerator tax_denominator fx_numerator fx_denominator quantum "
        "coverage valid_from valid_until evidence"
        + (" planning_only source_currency source_price_ref risks" if advisory else ""),
        "billing_unverifiable",
    )
    if (
        type(terms["version"]) is not int
        or terms["version"] != (2 if advisory else 1)
        or terms["currency"] != "USD"
        or terms["model"] != MODELS[group][0]
        or terms["coverage"] != COVERAGE
        or not terms["evidence"]
    ):
        raise ValueError("billing_unverifiable")
    if advisory and (
        terms["planning_only"] is not True
        or terms["source_currency"] != "CNY"
        or terms["risks"] != ADVISORY_RISKS
        or type(terms["input_per_million"]) is not int
        or terms["input_per_million"] <= 0
        or type(terms["output_per_million"]) is not int
        or terms["output_per_million"] <= 0
    ):
        raise ValueError("billing_unverifiable")
    if advisory:
        hash_value(terms["source_price_ref"])
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
