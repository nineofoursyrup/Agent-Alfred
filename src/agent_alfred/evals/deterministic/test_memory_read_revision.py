"""Memory read results and their invalidation revision describe one snapshot."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_alfred.evals.deterministic.test_memory_http import (
    generate,
    prepared_dashboard,
    read,
    seed_sources,
)
from agent_alfred.evals.deterministic.test_memory_mirrors import CONTEXT, save
from agent_alfred.gateway.web.memory_api import MemoryApi


@pytest.mark.parametrize("mode", ["batch", "mirror", "mirrors", "queue", "operation"])
def test_body_is_not_labeled_with_post_delete_revision(tmp_path, monkeypatch, mode):
    dashboard, model = prepared_dashboard(tmp_path, monkeypatch, [])
    entered = threading.Event()
    release = threading.Event()
    original = MemoryApi._envelope
    observed = {}
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="s6-read-proof")
    try:
        memory = dashboard.host.memory_service
        marker = "READ_PROOF_PRIVATE_FIXTURE"
        saved = save(memory, body=marker)
        if mode in {"batch", "queue", "operation"}:
            model._script = [
                json.dumps(
                    {
                        "semantic": [
                            {
                                "action": "update",
                                "id": saved["memory_id"],
                                "subject": "food",
                                "fact": marker + " successor",
                            }
                        ],
                        "episode_summary": marker + " summary",
                    }
                )
            ]
            seed_sources(dashboard)
            batch = generate(dashboard)
            assert batch["status"] == "awaiting_approval"
            route = {
                "batch": "/api/memory/consolidation?batch_id=" + batch["batch_id"],
                "queue": "/api/memory/consolidation",
                "operation": "/api/memory/consolidation?operation_id=begin:"
                + batch["generation_run_id"],
            }[mode]
        else:
            route = (
                "/api/memory/mirrors?name=facts&preview=1"
                if mode == "mirror"
                else "/api/memory/mirrors?preview=1"
            )
        key = {"batch": "batch", "queue": "batches", "operation": "result"}.get(
            mode, "mirrors"
        )
        before = memory.memory_revision

        def paused(api, **values):
            if key in values and not entered.is_set():
                if mode in {"batch", "mirror", "mirrors"}:
                    assert marker in json.dumps(values)
                if mode == "batch":
                    assert values["batch"]["candidate_available"] is True
                elif mode in {"mirror", "mirrors"}:
                    assert values["mirrors"][0]["ready"] is True
                elif mode == "queue":
                    assert values["batches"][0]["status"] == "awaiting_approval"
                else:
                    assert values["result"]["status"] == "awaiting_approval"
                observed["captured_revision"] = api.memory.memory_revision
                entered.set()
                assert release.wait(10), "test did not release HTTP envelope"
            return original(api, **values)

        monkeypatch.setattr(MemoryApi, "_envelope", paused)
        future = pool.submit(read, dashboard, route)
        assert entered.wait(10), "GET did not reach the envelope barrier"
        deleted = memory.execute(
            {
                "operation_id": "read-proof-delete",
                "kind": "semantic",
                "action": "delete",
                "expected_version": 1,
                "payload": {"id": saved["memory_id"]},
            },
            CONTEXT,
        )
        assert deleted["status"] == "deleted"
        after = memory.memory_revision
        assert after > before
        release.set()
        status, body = future.result(timeout=10)
        monkeypatch.setattr(MemoryApi, "_envelope", original)
        fresh_status, fresh = read(dashboard, route)
        old_body = marker in json.dumps(body)
        observed.update(
            mode=mode,
            before_revision=before,
            delete_revision=after,
            http_status=status,
            response_revision=body.get("memory_revision"),
            old_body_present=old_body,
            candidate_available=body.get("batch", {}).get("candidate_available"),
            mirror_ready=(body.get("mirrors") or [{}])[0].get("ready"),
            fresh_status=fresh_status,
            fresh_old_body_present=marker in json.dumps(fresh),
            model_requests=len(model.requests),
        )
        assert marker not in json.dumps(fresh), (
            "serial read control must deny removed body"
        )
    finally:
        release.set()
        pool.shutdown(wait=True, cancel_futures=True)
        monkeypatch.setattr(MemoryApi, "_envelope", original)
        observed["dashboard_closed"] = dashboard.close()
        observed["owned_client_threads_alive"] = [
            thread.name
            for thread in threading.enumerate()
            if thread.name.startswith("s6-read-proof")
        ]
        print(json.dumps(observed, sort_keys=True), flush=True)
    assert observed["dashboard_closed"]
    assert observed["owned_client_threads_alive"] == []
    assert not (status == 200 and old_body and body["memory_revision"] >= after), (
        "old body was paired with the committed deletion's new revision"
    )
    assert (
        status != 200 or body["memory_revision"] < after or body[key] == fresh[key]
    ), "a stale current projection was labeled with the post-delete revision"
