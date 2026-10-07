"""Input A is an advisory local estimate, never real usage or an owner receipt."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from agent_alfred.evals.acceptance.controlled.contract import (
    CAP_UNITS,
    advisory_run_disclosure,
    input_limit,
    validate_plan,
)
from agent_alfred.evals.acceptance.controlled.local_runtime import LocalInstalledRuntime
from agent_alfred.evals.acceptance.controlled.local_tokens import (
    JUDGE_INPUT_MEASUREMENT,
    estimate_judge_wire,
)
from agent_alfred.evals.acceptance.controlled.store import MemoryExecutionStore
from agent_alfred.evals.acceptance.schema import digest

from .test_local_advisory_contract import _plan, _resign
from .test_local_flash_profile import flash_material


def test_input_a_is_closed_by_purpose_and_bound_to_exact_plan_and_disclosure():
    plan = _plan(advisory=True, judge_profile=flash_material()["judge_profile"])
    for kind in ("judge_test", "judge_review", "grade", "grade_review"):
        assert input_limit(plan, {"kind": kind}) == 32000
    for kind in ("product", "auxiliary"):
        assert input_limit(plan, {"kind": kind}) == 20000
    with pytest.raises(ValueError, match="controlled_operation_invalid"):
        input_limit(plan, {"kind": "unapproved"})
    legacy = _plan(advisory=True)
    assert input_limit(legacy, {"kind": "judge_test"}) == 24000
    notice = advisory_run_disclosure(
        CAP_UNITS, judge_profile_change=plan["judge_profile_change"]
    )
    assert notice["input_policy"] == plan["input_policy"]
    assert notice["input_policy"]["judge_measurement"] == JUDGE_INPUT_MEASUREMENT
    invalid = deepcopy(plan)
    invalid["input_policy"]["product_max"] = 32000
    with pytest.raises(ValueError, match="controlled_input_policy_invalid"):
        validate_plan(_resign(invalid))


def test_pinned_tokenizer_counts_complete_wire_and_rejects_changed_data(
    tmp_path, monkeypatch
):
    from agent_alfred.evals.acceptance.controlled import local_tokens

    # Fixed independent token count, observed with the official local data.
    payload = {
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": "hello world"}],
    }
    count = estimate_judge_wire(payload)
    assert count == 277
    expanded = dict(payload, tools=[{"name": "tool", "description": "word " * 1000}])
    assert estimate_judge_wire(expanded) > count + 950
    assert digest(payload) != digest(expanded)
    local_tokens._tokenizer.cache_clear()
    changed = tmp_path / "changed-tokenizer.json"
    changed.write_bytes(b"{}")
    monkeypatch.setattr(local_tokens, "TOKENIZER_PATH", changed)
    with pytest.raises(ValueError, match="local_judge_tokenizer_unverifiable"):
        estimate_judge_wire(payload)
    changed.unlink()
    with pytest.raises(ValueError, match="local_judge_tokenizer_unverifiable"):
        estimate_judge_wire(payload)


def test_quote_keeps_product_byte_limit_and_measures_all_judge_fields(monkeypatch):
    from agent_alfred.evals.acceptance.controlled import local_runtime

    plan = _plan(advisory=True, judge_profile=flash_material()["judge_profile"])
    budget = {
        "pricing": plan["pricing"],
        "input_policy": plan["input_policy"],
        "input_measurement": {"numerator": 1, "denominator": 1, "overhead": 0},
        "provider_evidence": {"path": "offline", "sha256": "b" * 64},
    }
    runtime = object.__new__(LocalInstalledRuntime)
    runtime.store = MemoryExecutionStore()
    runtime._billing = lambda *args, **kwargs: budget
    runtime._directory, runtime._config = None, {}
    monkeypatch.setattr(
        local_runtime,
        "read_readiness",
        lambda *args, **kwargs: {
            "identities": {"evidence": "offline"},
            "evidence": {"advisory_budget": budget["provider_evidence"]},
        },
    )
    payload = {
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": "word " * 9000}],
    }
    now = datetime.now(UTC)
    quote = runtime.quote(plan, {"kind": "grade"}, payload, now)
    assert 9000 < quote["input_estimate"] < 32000
    with pytest.raises(ValueError, match="local_input_preflight_exceeded"):
        runtime.quote(plan, {"kind": "product"}, payload, now)
    with pytest.raises(ValueError, match="local_input_preflight_exceeded"):
        runtime.quote(
            plan,
            {"kind": "grade"},
            dict(
                payload,
                tools=[
                    {
                        "description": "word " * 24000,
                    }
                ],
            ),
            now,
        )
    budget["input_policy"] = dict(plan["input_policy"], judge_max=64000)
    with pytest.raises(ValueError, match="controlled_input_policy_invalid"):
        runtime.quote(plan, {"kind": "grade"}, payload, now)
