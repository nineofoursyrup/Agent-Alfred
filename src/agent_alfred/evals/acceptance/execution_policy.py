"""Closed schema3 execution constraints; legacy phase semantics stay unchanged."""

import math
from copy import deepcopy
from dataclasses import replace

from agent_alfred.connections import CredentialOverlay


def bind_client(batch, snapshot, role):
    """Derive actual dispatch limits from one coherent signed profile."""
    from .schema import judge_model

    matches = []
    for profile in batch["profiles"]:
        models = (
            profile["product_models"]
            if role == "product"
            else [judge_model(batch, profile)]
        )
        for model in models:
            if (model["endpoint_id"], model["model_id"]) == (
                snapshot.endpoint_id,
                snapshot.model_id,
            ):
                matches.append((profile, model))
                break
    named = [pair for pair in matches if pair[0]["id"] == snapshot.config_version]
    matches = named or matches
    if len(matches) != 1:
        raise ValueError("approval_profile_ambiguous")
    profile, model = matches[0]
    parameters = profile["parameters"]
    if snapshot.wire_style != model["wire_style"] or any(
        name in parameters and getattr(snapshot, name) is not parameters[name]
        for name in ("stream", "stream_fallback")
    ):
        raise ValueError("approval_profile_mismatch")
    total = batch["authorization"]["total_seconds"]
    durations = {}
    for name in ("overall_deadline_s", "per_attempt_timeout_s"):
        actual = getattr(snapshot, name)
        if actual is None and name != "overall_deadline_s":
            raise ValueError("approval_profile_mismatch")
        values = [total]
        for value in (actual, parameters.get(name)):
            if value is not None:
                if (
                    type(value) not in (int, float)
                    or not math.isfinite(value)
                    or value <= 0
                ):
                    raise ValueError("approval_profile_mismatch")
                values.append(value)
        durations[name] = min(values)
    tokens = batch["authorization"]["max_output_tokens"]
    if role == "product" and "max_tokens" in parameters:
        ceiling = parameters["max_tokens"]
        if type(ceiling) is not int or ceiling <= 0:
            raise ValueError("approval_profile_mismatch")
        tokens = min(tokens, ceiling)
    return replace(snapshot, **durations), model, tokens, profile["id"]


def validate_profile(profile):
    expected = {
        "version": 1,
        "max_retries": 0,
        "sdk_max_retries": 0,
        "stream": False,
        "stream_fallback": False,
        "credential_scope": ["deepseek"],
        "stop_on_infrastructure_error": True,
        "require_case_tool_mask": True,
        "allow_external_business_effects": False,
    }
    value = profile.get("execution_policy")
    if (
        type(value) is not dict
        or set(value) != set(expected)
        or any(
            type(value[k]) is not type(v) or value[k] != v for k, v in expected.items()
        )
    ):
        raise ValueError("invalid_execution_policy")
    tools = profile.get("local_tool_allowlist")
    allowed = {
        "create_event",
        "query_events",
        "read_persona",
        "update_persona",
        "draft_message",
    }
    if (
        not isinstance(tools, list)
        or not all(isinstance(t, str) for t in tools)
        or len(set(tools)) != len(tools)
        or set(tools) - allowed
    ):
        raise ValueError("execution_policy_tool_scope")
    parameters = profile["parameters"]
    integers = {
        "max_steps",
        "max_tokens",
        "input_character_limit",
        "gate_input_character_limit",
        "working_memory_rounds",
        "per_store_limit",
        "per_store_character_budget",
    }
    seconds = {"overall_deadline_s", "per_attempt_timeout_s", "gate_model_budget_s"}
    if set(parameters) - (integers | seconds | {"stream", "stream_fallback"}):
        raise ValueError("execution_policy_settings_scope")
    for key, value in parameters.items():
        if key in integers:
            if key == "gate_input_character_limit" and value is None:
                continue
            minimum = 0 if key in ("max_steps", "working_memory_rounds") else 1
            if type(value) is not int or value < minimum:
                raise ValueError("execution_policy_invalid_limit")
        if key in seconds and (
            type(value) not in (int, float) or not math.isfinite(value) or value <= 0
        ):
            raise ValueError("execution_policy_invalid_limit")
    if any(parameters.get(key) is not False for key in ("stream", "stream_fallback")):
        raise ValueError("execution_policy_parameter_mismatch")
    for model in [*profile["product_models"], profile["judge_model"]]:
        if model["endpoint_id"] != "deepseek" or model["wire_style"] != "openai":
            raise ValueError("execution_policy_model_scope")


def policy(batch):
    """Return effective execution behavior without adding fields to old profiles."""
    if batch["schema_version"] == 3:
        for profile in batch["profiles"]:
            validate_profile(profile)
        value = batch["profiles"][0]["execution_policy"]
        override = batch.get("judge_profile", {}).get("model")
        if override and (
            override["endpoint_id"] not in value["credential_scope"]
            or override["wire_style"] != "openai"
        ):
            raise ValueError("execution_policy_model_scope")
        return deepcopy(value)
    trial = batch["phase"] == "trial"
    return {
        "max_retries": 0 if trial else 1,
        "sdk_max_retries": 0,
        "credential_scope": ["deepseek"] if trial else None,
        "stop_on_infrastructure_error": trial,
    }


def scoped_credentials(batch, credentials):
    """Freeze only authorized endpoint credentials into the disposable runtime."""
    values = (
        credentials.values()
        if isinstance(credentials, CredentialOverlay)
        else credentials or {}
    )
    scope = policy(batch)["credential_scope"]
    if scope is None:
        return CredentialOverlay(values, None)
    from agent_alfred.endpoints import list_endpoints

    names = {e.api_key_env for e in list_endpoints() if e.endpoint_id in scope}
    return CredentialOverlay(
        {name: values[name] for name in names if values.get(name)}, None
    )
