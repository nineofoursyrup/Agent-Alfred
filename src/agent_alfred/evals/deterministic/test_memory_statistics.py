"""Ratios exclude unrecorded and unknown evidence instead of inventing zeros."""

from datetime import datetime, timezone

from agent_alfred.memory.statistics import summarize_statistics


def gate_fixture(outcome, *, character_budget=4000):
    from agent_alfred.evals.deterministic.test_memory_gate import (
        SearchBackend,
        fact_hit,
    )
    from agent_alfred.memory.retrieval_gate import fallback_decision, retrieve

    semantic = SearchBackend([fact_hit("known-id", "mint")] if outcome == "hit" else [])
    episodic = SearchBackend(
        error=OSError("private-error") if outcome == "error" else None
    )
    return retrieve(
        fallback_decision("hello" if outcome == "skip" else "mint"),
        semantic,
        episodic,
        fallback_reason="model_unavailable",
        per_store_character_budget=character_budget,
    ).evidence


def test_known_four_states_have_distinct_denominators():
    rows = []
    for outcome in ["skip"] * 2 + ["hit"] * 3 + ["miss"] + ["error"]:
        rows.append(
            {
                "recording_state": "recorded",
                "telemetry": {
                    "memory": {
                        "gate_state": "evaluated",
                        "gate": gate_fixture(outcome),
                    }
                },
            }
        )
    rows.extend(
        [
            {"recording_state": "recorded", "telemetry": {}},
            {"recording_state": "failed", "telemetry": None},
            {
                "recording_state": "recorded",
                "telemetry": {"memory": {"gate_state": "incomplete"}},
            },
        ]
    )
    start = datetime(2026, 9, 2, tzinfo=timezone.utc)
    end = datetime(2026, 9, 9, tzinfo=timezone.utc)
    summary = summarize_statistics(rows, since=start, until=end)
    assert summary["skip"] == {"numerator": 2, "denominator": 6, "ratio": 2 / 6}
    assert summary["hit"] == {"numerator": 3, "denominator": 4, "ratio": 3 / 4}
    assert summary["error"] == {"numerator": 1, "denominator": 7, "ratio": 1 / 7}
    assert summary["excluded"] == {
        "unrecorded": 1,
        "not_evaluated": 0,
        "incomplete": 1,
        "legacy_unknown": 1,
    }
    assert summary["since"] == "2026-09-02T00:00:00+00:00"
    empty = summarize_statistics([], since=start, until=end)
    assert empty["hit"]["ratio"] is None


def test_public_statistics_reads_recorded_chat_and_excludes_probe():
    from datetime import timedelta

    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic.test_runtime_memory_gate import SKIP, runtime
    from agent_alfred.runtime.host import SubmitRequest

    clock = FakeClock()
    with runtime([SKIP, "answer", "probe"], clock=clock) as (host, _, __):
        chat = host.submit(SubmitRequest("hello"))
        host.wait(chat.run_id)
        probe = host.submit(
            SubmitRequest(
                "probe", purpose="inference_probe", endpoint_id="test", model_id="m"
            )
        )
        host.wait(probe.run_id)
        result = host.memory_service.statistics(
            since=clock.wall_utc() - timedelta(days=7),
            until=clock.wall_utc() + timedelta(seconds=1),
        )
        assert result["counts"] == {"skip": 1, "hit": 0, "miss": 0, "error": 0}
        assert result["skip"]["ratio"] == 1
        assert result["hit"]["ratio"] is None


def test_malformed_v1_is_not_counted_as_a_hit():
    start = datetime(2026, 9, 2, tzinfo=timezone.utc)
    end = datetime(2026, 9, 9, tzinfo=timezone.utc)
    rows = [
        {
            "recording_state": "recorded",
            "telemetry": {
                "memory": {
                    "gate_state": "evaluated",
                    "gate": {"schema_version": 1, "outcome": "hit"},
                }
            },
        }
    ]
    result = summarize_statistics(rows, since=start, until=end)
    assert result["counts"]["hit"] == 0
    assert result["excluded"]["legacy_unknown"] == 1
    assert result["hit"]["denominator"] == 0


def test_inconsistent_references_and_private_fields_are_unsupported():
    from copy import deepcopy

    baseline = gate_fixture("hit")
    invalid = []
    for path, replacement in [
        (("schema_version",), 2),
        (("schema_version",), True),
        (("decision",), False),
        (("hit_count",), 2),
        (("selected_count",), 0),
        (("references", 0, "rank"), 2),
        (("references", 0, "record_version"), 0),
        (
            ("references", 0, "origin"),
            {"type": "manual", "source": "web", "subject": "private"},
        ),
        (("stores", "episodic", "status"), "not_queried"),
        (("stores", "semantic", "hit_count"), 2),
        (("timing", "search_ms"), -1),
        (("latency_ms",), float("nan")),
        (("references", 0, "omission_reason"), "character_budget"),
        (("input_disposition",), "no_hits"),
        (("rule_version",), "unrecognized"),
        (("reason_code",), "greeting"),
    ]:
        gate = deepcopy(baseline)
        target = gate
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = replacement
        invalid.append(gate)
    invalid.append({**baseline, "query": "private query"})
    rows = [
        {
            "recording_state": "recorded",
            "telemetry": {"memory": {"gate_state": "evaluated", "gate": gate}},
        }
        for gate in invalid
    ]
    result = summarize_statistics(
        rows,
        since=datetime(2026, 9, 2, tzinfo=timezone.utc),
        until=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )
    assert result["counts"] == {"skip": 0, "hit": 0, "miss": 0, "error": 0}
    assert result["excluded"]["legacy_unknown"] == len(invalid)


def test_real_partial_all_excluded_and_failure_evidence_remain_usable():
    from agent_alfred.evals.deterministic.test_memory_gate import (
        SearchBackend,
        fact_hit,
    )
    from agent_alfred.memory.evidence import valid_gate_evidence
    from agent_alfred.memory.retrieval_gate import fallback_decision, retrieve

    rows = []
    for budget in (1, 200, 4000):
        result = retrieve(
            fallback_decision("mint"),
            SearchBackend(
                [
                    fact_hit("short", "mint"),
                    fact_hit("long", "x" * 1000),
                    fact_hit("last", "mint"),
                ]
            ),
            SearchBackend(),
            fallback_reason="model_unavailable",
            per_store_character_budget=budget,
        )
        assert valid_gate_evidence(result.evidence)
        rows.append(
            {
                "recording_state": "recorded",
                "telemetry": {
                    "memory": {"gate_state": "evaluated", "gate": result.evidence}
                },
            }
        )
    failed = retrieve(
        fallback_decision("mint"),
        SearchBackend([fact_hit("short", "mint")]),
        SearchBackend(error=OSError("private")),
        fallback_reason="model_unavailable",
    ).evidence
    assert valid_gate_evidence(failed)
    first_failed = retrieve(
        fallback_decision("mint"),
        SearchBackend(error=OSError("private")),
        SearchBackend(),
        fallback_reason="model_unavailable",
    ).evidence
    assert valid_gate_evidence(first_failed)
    summary = summarize_statistics(
        rows,
        since=datetime(2026, 9, 2, tzinfo=timezone.utc),
        until=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )
    assert summary["counts"]["hit"] == 3
