"""CE-10/16: actual SDK dispatch and source identity changes, deterministic IO."""

import json
from contextlib import contextmanager

import httpx2
import pytest
from openai import OpenAI

from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic.test_aggregation import save_fact
from agent_alfred.evals.deterministic.test_runtime_skills import runtime
from agent_alfred.evals.deterministic.test_skill_input_attempts import response
from agent_alfred.memory.types import ManualOrigin
from agent_alfred.model import ModelRef, ScriptedModelFactory
from agent_alfred.openai_compatible import OpenAICompatibleAdapter
from agent_alfred.retry import RetryPolicy, SystemSleeper
from agent_alfred.settings import Settings
from agent_alfred.stream_fallback import StreamFallback


@contextmanager
def wire_runtime(
    tmp_path, *, before_send=None, fail_first=False, fail_always=False, **options
):
    clock, sent = FakeClock(), []

    def dispatch(request):
        sent.append(json.loads(request.content))
        if fail_always or (fail_first and len(sent) == 1):
            return httpx2.Response(503, json={"error": {"message": "temporary"}})
        return response("草稿 [[S1]]")

    with httpx2.Client(transport=httpx2.MockTransport(dispatch)) as http:
        client = StreamFallback(
            OpenAICompatibleAdapter(
                client=OpenAI(
                    api_key="fixture",
                    base_url="http://fixture.invalid",
                    http_client=http,
                    max_retries=0,
                ),
                model=ModelRef("test", "m"),
            ),
            clock=clock,
        )
        client = RetryPolicy(
            client, clock=clock, sleeper=SystemSleeper(), retry_delay_s=0
        )
        with runtime(
            tmp_path,
            [],
            factory=ScriptedModelFactory(client),
            clock=clock,
            aggregation_before_send=before_send,
            **options,
        ) as (host, _, sink):
            yield host, sent, clock, sink


@pytest.mark.parametrize("changed_at, expected", [(1, 0), (2, 1), (99, 2)])
def test_ce10_16_revalidate_each_real_attempt_and_preserve_retry_cost(
    tmp_path, changed_at, expected
):
    holder, count = {}, []

    def barrier(run, attempt):
        count.append(attempt)
        if len(count) == changed_at:
            # Explicit fault seam: a public Store write, independent of the UI
            # MutationGate. The paired HTTP test proves users still receive 409.
            with holder["host"].memory_service.reading_stores() as stores:
                conn = stores[0]._conn
                conn.execute("BEGIN IMMEDIATE")
                stores[0].update(
                    holder["id"],
                    expected_version=1,
                    origin=ManualOrigin("web"),
                    fact="changed coffee",
                )
                conn.commit()

    with wire_runtime(
        tmp_path, before_send=barrier, fail_first=True, settings=Settings(max_steps=1)
    ) as (host, sent, _, _):
        holder.update(host=host, id=save_fact(host))
        accepted = host.aggregate(
            session_id=host.create_session(),
            goal="goal",
            keywords="coffee",
            sources=("semantic",),
        )
        result = host.wait(accepted.run_id)
        assert len(sent) == expected
        assert sum(len(r.attempts) for r in result.model_results) == expected
        assert result.step_count == 1
        assert result.outcome == ("completed" if changed_at == 99 else "failed")
        assert len(result.memory_telemetry["input_attempts"]) == expected
        assert result.memory_telemetry["aggregation"]["sources"]["semantic"][
            "actual_input_count"
        ] == (1 if expected else 0)
        if changed_at != 99:
            assert result.error == "input_evidence_unavailable"


@pytest.mark.parametrize("mode", ["deadline", "cancel", "input_registration"])
def test_ce10_16_safety_stops_before_dispatch(tmp_path, mode):
    holder = {}

    def barrier(run, attempt):
        if mode == "deadline":
            holder["clock"].monotonic_value = 20
        elif mode == "cancel":
            raise KeyboardInterrupt()

    with wire_runtime(
        tmp_path, before_send=barrier, settings=Settings(overall_deadline_s=5)
    ) as (host, sent, clock, _):
        holder["clock"] = clock
        save_fact(host)
        if mode == "input_registration":
            import sqlite3

            with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
                conn.execute(
                    "CREATE TRIGGER fail_input BEFORE INSERT ON run_input_explanations "
                    "BEGIN SELECT RAISE(ABORT,'IO failure'); END"
                )
        accepted = host.aggregate(
            session_id=host.create_session(),
            goal="goal",
            keywords="coffee",
            sources=("semantic",),
        )
        result = host.wait(accepted.run_id)
        assert result.outcome == ("interrupted" if mode == "cancel" else "failed")
        assert sent == []
        assert result.model_results == ()


@pytest.mark.parametrize(
    "mode",
    [
        "invalid",
        "oversized",
        "extra_fields",
        "timeout",
        "permission",
        "cancel",
        "deadline",
    ],
)
def test_ce09_16_source_fault_categories(tmp_path, mode):
    from agent_alfred.runtime.memory import InputEvidenceError

    clock = FakeClock()

    class Source:
        def __init__(self, real):
            self.real = real

        def read(self, kind, request, context):
            if kind != "semantic":
                return self.real.read(kind, request, context)
            if mode == "invalid":
                return {"schema_version": 1}
            if mode in ("oversized", "extra_fields"):
                value = self.real.read(kind, request, context)
                if mode == "oversized":
                    value["items"] = [{}] * (request["limits"]["per_store_limit"] + 1)
                else:
                    value["copied_body"] = "must not enter persisted metadata"
                return value
            if mode == "timeout":
                raise TimeoutError()
            if mode == "permission":
                raise InputEvidenceError()
            if mode == "cancel":
                raise KeyboardInterrupt()
            clock.monotonic_value = 10
            return self.real.read(kind, request, context)

    with runtime(
        tmp_path,
        [],
        clock=clock,
        aggregation_sources=Source,
        settings=Settings(overall_deadline_s=5),
    ) as (host, model, _):
        accepted = host.aggregate(
            session_id=host.create_session(),
            goal="goal",
            keywords="coffee",
            sources=("semantic",),
        )
        result = host.wait(accepted.run_id)
        assert model.requests == []
        facts = result.memory_telemetry["aggregation"]
        if mode in ("invalid", "oversized", "extra_fields", "timeout"):
            assert facts["reason_code"] == "sources_unavailable"
            assert facts["sources"]["semantic"]["code"] == (
                "local_timeout" if mode == "timeout" else "invalid_source_result"
            )
        else:
            assert result.outcome == ("interrupted" if mode == "cancel" else "failed")
            assert facts["graph_result"] == "Failed"


def test_ce16_real_tool_metering_failure_forces_stop(tmp_path):
    import sqlite3

    with runtime(tmp_path, []) as (host, model, sink):
        save_fact(host)
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            conn.execute(
                "CREATE TRIGGER fail_meter BEFORE INSERT ON tool_metering "
                "BEGIN SELECT RAISE(ABORT,'IO failure'); END"
            )
        accepted = host.aggregate(
            session_id=host.create_session(),
            goal="goal",
            keywords="coffee",
            sources=("semantic", "history"),
        )
        result = host.wait(accepted.run_id)
        assert result.outcome == "failed"
        assert result.error == "metering_unconfirmed"
        assert result.reply is None and model.requests == []
        assert result.memory_telemetry["aggregation"]["graph_result"] == "Failed"
        assert not any(
            e.envelope.node_id == "synthesis" and e.payload.name == "node.started"
            for e in sink.events
        )


def test_ce10_prepared_but_unsent_registration_does_not_become_use(tmp_path):
    import sqlite3

    class CannotSend:
        def respond(self, request, *, events=None, deadline=None):
            # External adapter preflight ran; failure before transport produces
            # no Attempt receipt. The real ledger must reconcile it as not_sent.
            request.on_attempt_preflight("prepared-only", deadline)
            raise ValueError("encoding failed before dispatch")

    with runtime(tmp_path, [], factory=ScriptedModelFactory(CannotSend())) as (
        host,
        _,
        _,
    ):
        save_fact(host)
        accepted = host.aggregate(
            session_id=host.create_session(),
            goal="goal",
            keywords="coffee",
            sources=("semantic",),
        )
        result = host.wait(accepted.run_id)
        assert result.outcome == "failed" and result.model_results == ()
        assert result.memory_telemetry["input_attempts"] == []
        assert result.memory_telemetry["input_not_sent_attempts"] == ["prepared-only"]
        assert result.memory_telemetry["aggregation"]["provided"] == []
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            assert conn.execute("SELECT COUNT(*) FROM memory_uses").fetchone() == (0,)
            assert conn.execute("SELECT COUNT(*) FROM history_reads").fetchone() == (0,)
