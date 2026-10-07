"""Closed per-case inputs; no material-store or controller object crosses IPC."""

from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal

from agent_alfred.messages import blocks_from_jsonable, blocks_to_jsonable
from agent_alfred.model import (
    AttemptRecord,
    ModelError,
    ModelRef,
    ModelResponse,
    ModelResult,
    Usage,
    parse_attempt_outcome,
)

from ..case_setup import validate_setup
from ..schema import identifier
from .contract import exact

CASE_CONTRACT = "V1-LOCAL-CASE-INPUT"
PARAMETERS = frozenset(
    {
        "max_steps",
        "input_character_limit",
        "gate_input_character_limit",
        "working_memory_rounds",
        "per_store_limit",
        "per_store_character_budget",
        "max_tokens",
        "per_attempt_timeout_s",
        "overall_deadline_s",
        "gate_model_budget_s",
        "stream",
        "stream_fallback",
    }
)
TOOLS = frozenset(
    {
        "create_event",
        "query_events",
        "read_persona",
        "update_persona",
        "draft_message",
    }
)


def case_input(batch, case):
    profile = batch["profiles"][0]
    dto = {
        "contract": CASE_CONTRACT,
        "version": 1,
        "batch_id": batch["batch_id"],
        "simulation": batch["simulation"],
        "profile": {
            key: deepcopy(profile[key])
            for key in (
                "id",
                "parameters",
                "inputs",
                "product_models",
                "local_tool_allowlist",
            )
        },
        "case": {
            key: deepcopy(case[key])
            for key in (
                "id",
                "operation",
                "input",
                "setup",
            )
        },
    }
    validate_case_input(dto)
    return dto


def validate_case_input(dto):
    exact(
        dto, "contract version batch_id simulation profile case", "local_case_invalid"
    )
    if (
        dto["contract"] != CASE_CONTRACT
        or type(dto["version"]) is not int
        or dto["version"] != 1
        or type(dto["simulation"]) is not bool
    ):
        raise ValueError("local_case_invalid")
    identifier(dto["batch_id"])
    profile, case = dto["profile"], dto["case"]
    exact(
        profile,
        "id parameters inputs product_models local_tool_allowlist",
        "local_case_profile_invalid",
    )
    exact(profile["inputs"], "persona", "local_case_inputs_invalid")
    if (
        type(profile["parameters"]) is not dict
        or set(profile["parameters"]) - PARAMETERS
        or profile["parameters"].get("stream") is not False
        or profile["parameters"].get("stream_fallback") is not False
        or not set(profile["local_tool_allowlist"]) <= TOOLS
    ):
        raise ValueError("local_case_parameters_invalid")
    models = profile["product_models"]
    if (
        not isinstance(models, list)
        or len(models) != 1
        or any(
            models[0].get(key) != value
            for key, value in {
                "endpoint_id": "deepseek",
                "model_id": "deepseek-flash",
                "wire_style": "openai",
                "thinking": "disabled",
            }.items()
        )
    ):
        raise ValueError("local_case_model_invalid")
    exact(
        models[0],
        "endpoint_id model_id wire_style thinking provider_version",
        "local_case_model_invalid",
    )
    exact(case, "id operation input setup", "local_case_fields_invalid")
    identifier(case["id"])
    if case["operation"] not in ("chat", "aggregate") or type(case["input"]) is not str:
        raise ValueError("local_case_operation_invalid")
    validate_setup(case, profile)


def runtime_inputs(dto):
    """Private runner context, deliberately not a valid schema4 material batch."""
    validate_case_input(dto)
    return {
        "batch_id": dto["batch_id"],
        "schema_version": 4,
        "simulation": dto["simulation"],
        "phase": "calibration",
        "profiles": [deepcopy(dto["profile"])],
    }, deepcopy(dto["case"])


def result_wire(result):
    attempts = []
    for attempt in result.attempts:
        value = asdict(attempt)
        cost = attempt.usage.endpoint_reported_cost_usd
        value["usage"]["endpoint_reported_cost_usd"] = (
            None if cost is None else str(cost)
        )
        attempts.append(value)
    response = None
    if result.response is not None:
        response = {
            "blocks": blocks_to_jsonable(result.response.blocks),
            "stop_reason": result.response.stop_reason,
            "model": asdict(result.response.model),
            "provider_model_id": result.response.provider_model_id,
        }
    return {
        "attempts": attempts,
        "response": response,
        "final_error": asdict(result.final_error) if result.final_error else None,
    }


def result_from_wire(value):
    exact(value, "attempts response final_error", "local_result_invalid")
    if not isinstance(value["attempts"], list) or len(value["attempts"]) != 1:
        raise ValueError("local_attempt_count_invalid")
    attempts = []
    for row in value["attempts"]:
        exact(row, "attempt_id streamed outcome usage error model")
        usage = dict(row["usage"])
        cost = usage["endpoint_reported_cost_usd"]
        if cost is not None:
            usage["endpoint_reported_cost_usd"] = Decimal(cost)
        attempts.append(
            AttemptRecord(
                row["attempt_id"],
                row["streamed"],
                parse_attempt_outcome(row["outcome"]),
                Usage(**usage),
                ModelError(**row["error"]) if row["error"] else None,
                ModelRef(**row["model"]) if row["model"] else None,
            )
        )
    response = value["response"]
    if response is not None:
        exact(response, "blocks stop_reason model provider_model_id")
        response = ModelResponse(
            blocks_from_jsonable(response["blocks"]),
            response["stop_reason"],
            ModelRef(**response["model"]),
            response["provider_model_id"],
        )
    return ModelResult(
        tuple(attempts),
        response,
        ModelError(**value["final_error"]) if value["final_error"] else None,
    )
