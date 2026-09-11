"""Dependency changes through trusted ports of an actual consolidation Host.

The injected port executes with the current Run's real permission; this is a
controlled in-process boundary, not support for concurrent external SQL writes.
Ordinary user commands are checked separately and remain busy under admission.
"""

import json
from dataclasses import replace

import httpx2
import pytest

from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic.test_memory_consolidation_runtime import (
    _adapter_host,
    _completion,
)
from agent_alfred.evals.deterministic.test_memory_consolidation_service import (
    CONTEXT,
    _complete_chat,
)
from agent_alfred.memory.commands import CommandContext
from agent_alfred.memory.types import ConsolidationOrigin, ManualOrigin


@pytest.mark.parametrize("stage", ["prepared", "dispatch", "finish", "approval"])
@pytest.mark.parametrize(
    "change",
    [
        "target-edit",
        "target-delete",
        "background-edit",
        "background-delete",
        "source",
        "use-chain",
        "protection",
    ],
)
def test_host_rechecks_dependency_changes(tmp_path, monkeypatch, stage, change):
    sent = []
    context = []
    state = {}

    def change_dependency():
        memory = host.memory_service
        trusted = (
            CONTEXT
            if stage == "approval"
            else replace(context[0], origin=ManualOrigin("web"))
        )
        name = (
            "upstream"
            if change in ("source", "use-chain")
            else "background"
            if change.startswith("background")
            else "target"
        )
        identifier = state[name]
        if change == "protection":
            command = {
                "action": "save",
                "payload": {"subject": "target", "fact": "OLD-target"},
            }
        elif change.endswith("edit"):
            command = {
                "action": "update",
                "expected_version": 1,
                "payload": {"id": identifier, "fact": "NEW-external"},
            }
        else:
            command = {
                "action": "delete",
                "expected_version": 1,
                "payload": {"id": identifier},
            }
        command.update(operation_id="change-dependency", kind="semantic")
        result = memory.execute(command, trusted)
        assert "error" not in result, result
        if change == "protection":
            current = memory.get("semantic", identifier)
            assert current.human_protected and current.record_version == 1

    def handle(request):
        sent.append(json.loads(request.content))
        if stage == "dispatch":
            change_dependency()
        return httpx2.Response(200, json=_completion(state["plan"]))

    host, conn, http = _adapter_host(tmp_path, handle, FakeClock(), traced=True)
    try:
        memory = host.memory_service
        for name in ("target", "background", "anchor", "upstream"):
            seed_context = (
                CommandContext(ConsolidationOrigin("seed"), "web")
                if name == "target"
                else CONTEXT
            )
            seed = memory.execute(
                {
                    "operation_id": "seed-" + name,
                    "kind": "semantic",
                    "action": "save",
                    "payload": {"subject": name, "fact": "OLD-" + name},
                },
                seed_context,
            )
            state[name] = seed["memory_id"]
        _complete_chat(memory, conn, "s", "r1", "I like coriander", "Noted.")
        _complete_chat(memory, conn, "s", "r2", "Still coriander", "Noted.")
        if change in ("source", "use-chain"):
            source = "r1"
            if change == "use-chain":
                _complete_chat(
                    memory, conn, "other", "root", "Earlier source", "Noted."
                )
                source = "root"
                assert "error" not in memory.forgetting.register_read(
                    "r1",
                    sources=("root",),
                    memories=(),
                    attempt_id="past-read",
                    purpose="answer",
                    context=CONTEXT,
                )
            assert "error" not in memory.forgetting.register_sources(
                "semantic",
                state["upstream"],
                1,
                groups=(source,),
                evidence_id="seed-upstream-source",
                context=CONTEXT,
            )
        state["plan"] = json.dumps(
            {
                "semantic": [
                    {
                        "action": "update",
                        "id": state[name],
                        "subject": name,
                        "fact": "CANDIDATE-" + name,
                    }
                    for name in ("target", "anchor")
                ],
                "episode_summary": "CANDIDATE-episode",
            }
        )
        original_begin = memory.consolidation.begin_generation
        original_finish = memory.consolidation.finish_generation

        def begin(*args, **kwargs):
            context.append(kwargs["context"])
            result = original_begin(*args, **kwargs)
            state["batch"] = result["batch_id"]
            assert result["status"] == "running"
            if stage == "prepared":
                change_dependency()
            return result

        def finish(*args, **kwargs):
            if stage == "finish":
                change_dependency()
            return original_finish(*args, **kwargs)

        monkeypatch.setattr(memory.consolidation, "begin_generation", begin)
        monkeypatch.setattr(memory.consolidation, "finish_generation", finish)
        admitted = host.generate_consolidation("s")
        host.wait(admitted.run_id, timeout=5)
        if stage == "approval":
            assert (
                memory.consolidation.get_batch(state["batch"])["status"]
                == "awaiting_approval"
            )
            change_dependency()
        view = memory.consolidation.get_batch(state["batch"])
        if change == "protection":
            assert view["status"] == "awaiting_approval", view
            assert memory.get("semantic", state["target"]).fact == "OLD-target"
            approved = memory.consolidation.approve(
                state["batch"], 1, context=CONTEXT, operation_id="approve-current"
            )
            assert approved["status"] == "succeeded", approved
            assert memory.get("semantic", state["target"]).human_protected
        else:
            assert view["status"] in ("invalidated", "failed"), view
            approved = memory.consolidation.approve(
                state["batch"], 1, context=CONTEXT, operation_id="approve-stale"
            )
            assert approved.get("status") != "succeeded", approved
            assert "plan" not in memory.consolidation.get_batch(state["batch"])
            assert memory.get("semantic", state["anchor"]).fact == "OLD-anchor"
            assert conn.execute("SELECT count(*) FROM episodes").fetchone() == (0,)
            assert conn.execute(
                "SELECT count(*) FROM memory_consolidation_source_results"
            ).fetchone() == (0,)
        if stage != "prepared" or change == "protection":
            assert len(sent) == 1
            wire = json.dumps(sent)
            assert "OLD-background" in wire and "OLD-target" in wire
            uses = conn.execute(
                "SELECT memory_id FROM memory_uses WHERE consumer=?",
                (admitted.run_id,),
            ).fetchall()
            assert (state["background"],) in uses
        else:
            assert sent == []
        assert host.snapshot().coordinator_state == "idle"
    finally:
        host.close()
        conn.close()
        http.close()
