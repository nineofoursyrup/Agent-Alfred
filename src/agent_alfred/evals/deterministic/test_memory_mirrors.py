"""Real managed Markdown output through the public standalone Host."""

from agent_alfred.evals.deterministic._managed_state_ownership_test_helpers import (
    build_standalone_host,
)
from agent_alfred.memory.commands import CommandContext
from agent_alfred.memory.types import ManualOrigin

CONTEXT = CommandContext(ManualOrigin("web"), "web")


def save(memory, operation="save", body="I prefer coriander"):
    return memory.execute(
        {
            "operation_id": operation,
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "food", "fact": body},
        },
        CONTEXT,
    )


def test_standalone_save_creates_complete_current_managed_mirror(tmp_path):
    host = build_standalone_host(tmp_path / "state")
    try:
        memory = host.memory_service
        saved = save(memory)
        assert saved["status"] == "saved"
        preview = memory.mirrors.preview("facts")
        assert preview["ready"] is True
        assert "I prefer coriander" in preview["text"]
        assert saved["memory_id"] in preview["text"]
        assert (tmp_path / "state/memory/facts.md").read_text() == preview["text"]
        assert memory.mirrors.preview("episodes")["ready"] is True
    finally:
        assert host.close()


def test_delete_file_failure_keeps_receipt_and_forbids_old_preview(
    tmp_path, monkeypatch
):
    from agent_alfred.evals.deterministic.test_forgetting import delete
    from agent_alfred.managed_state import ManagedDirectoryLease

    host = build_standalone_host(tmp_path / "state")
    try:
        memory = host.memory_service
        record = save(memory)
        save(memory, "independent", "Independent retained fact")
        original = ManagedDirectoryLease.replace_bytes

        def fail(self, relative, payload, **kwargs):
            if str(relative) == "facts.md":
                raise OSError("private IO failure")
            return original(self, relative, payload, **kwargs)

        monkeypatch.setattr(ManagedDirectoryLease, "replace_bytes", fail)
        receipt = delete(memory, record)
        assert receipt["status"] == "deleted"
        assert memory.get("semantic", record["memory_id"]) is None
        assert "I prefer coriander" in (tmp_path / "state/memory/facts.md").read_text()
        preview = memory.mirrors.preview("facts")
        assert not preview["ready"] and "text" not in preview
        assert memory.forgetting.get_forgetting("delete")["state"] != "complete"
        monkeypatch.setattr(ManagedDirectoryLease, "replace_bytes", original)
        result = memory.forgetting.retry_cleanup("delete", CONTEXT)
        assert result["state"] == "complete"
        preview = memory.mirrors.preview("facts")
        assert preview["ready"]
        assert "I prefer coriander" not in preview["text"]
        assert "Independent retained fact" in preview["text"]
        assert memory.get_operation("delete") == receipt
    finally:
        monkeypatch.undo()
        assert host.close()


def test_external_conflict_delete_and_stale_confirmation(tmp_path):
    from agent_alfred.evals.deterministic.test_forgetting import delete

    host = build_standalone_host(tmp_path / "state")
    try:
        memory = host.memory_service
        record = save(memory)
        path = tmp_path / "state/memory/facts.md"
        path.write_text("External private contents")
        old = memory.mirrors.status("facts")["confirmation"]
        receipt = delete(memory, record)
        status = memory.mirrors.preview("facts")
        assert status["conflict"] and status["dirty"]
        assert not status["ready"] and "text" not in status
        stale = memory.mirrors.confirm("facts", old, "old-confirm", CONTEXT)
        assert stale == {"error": {"code": "stale_confirmation"}}
        current = memory.mirrors.status("facts")["confirmation"]
        result = memory.mirrors.confirm("facts", current, "confirm", CONTEXT)
        assert result["status"] == "regenerated"
        assert memory.forgetting.get_forgetting("delete")["state"] == "complete"
        assert memory.get_operation("delete") == receipt
        assert memory.mirrors.preview("facts")["ready"]
        path.write_text("External successor")
        assert memory.mirrors.confirm("facts", current, "confirm", CONTEXT) == result
        assert path.read_text() == "External successor"
        assert not memory.mirrors.preview("facts")["ready"]
    finally:
        assert host.close()


def test_restart_relocation_and_full_store_output(tmp_path):
    import shutil

    original_root, moved = tmp_path / "state", tmp_path / "moved"
    host = build_standalone_host(original_root)
    try:
        memory = host.memory_service
        for index in range(30):
            assert (
                save(memory, f"save-{index}", f"Full record {index:02}")["status"]
                == "saved"
            )
        episode = memory.execute(
            {
                "operation_id": "episode",
                "kind": "episodic",
                "action": "save",
                "payload": {
                    "summary": "Complete episode " + "x" * 12000,
                    "occurred_at": "2026-09-01T00:00:00+00:00",
                    "occurred_until": None,
                },
            },
            CONTEXT,
        )
        assert episode["status"] == "saved"
        text = memory.mirrors.preview("facts")["text"]
        for index in range(30):
            assert f"Full record {index:02}" in text
        assert "x" * 12000 in memory.mirrors.preview("episodes")["text"]
    finally:
        assert host.close()
    shutil.copytree(original_root, moved)
    host = build_standalone_host(moved)
    try:
        assert not host.memory_service.mirrors.preview("facts")["ready"]
        host.recover()
        assert host.memory_service.mirrors.preview("facts")["ready"]
        assert host.memory_service.mirrors.preview("facts")["text"] == text
        assert host.memory_service.mirrors.status("facts")["path"] == str(
            moved / "memory/facts.md"
        )
    finally:
        assert host.close()


def test_replace_holds_admission_without_sqlite_lock_and_listener_busy_is_safe(
    tmp_path, monkeypatch
):
    from agent_alfred.managed_state import ManagedDirectoryLease

    host = build_standalone_host(tmp_path / "state")
    memory = host.memory_service
    original = ManagedDirectoryLease.replace_bytes
    checked = []

    def replace(self, relative, payload, **kwargs):
        if str(relative) in ("facts.md", "episodes.md"):
            assert not memory._db.transaction_in_progress
            assert host._db_lock.acquire(blocking=False)
            host._db_lock.release()
            assert host.try_begin_mutation() == "mutation_in_flight"
            checked.append(str(relative))
        return original(self, relative, payload, **kwargs)

    monkeypatch.setattr(ManagedDirectoryLease, "replace_bytes", replace)
    memory._memory_notifier = lambda event: save(memory, "reentrant")
    try:
        result = save(memory)
        assert result["status"] == "saved"
        assert sorted(checked) == ["episodes.md", "facts.md"]
        assert memory.get_operation("reentrant") is None
        assert memory.mirrors.preview("facts")["ready"]
    finally:
        monkeypatch.undo()
        assert host.close()


def test_process_termination_recovers_exact_managed_temporary_and_replacement(tmp_path):
    import json
    import subprocess
    import sys

    for boundary in ("temporary", "replaced"):
        root = tmp_path / boundary
        host = build_standalone_host(root)
        try:
            save(host.memory_service)
        finally:
            assert host.close()
        code = """
import os, sys
from pathlib import Path
from agent_alfred.evals.deterministic.test_memory_mirrors import (
    build_standalone_host, save,
)
from agent_alfred.managed_state import ManagedFileLease
host = build_standalone_host(Path(sys.argv[1]))
if sys.argv[2] == "temporary":
    original = ManagedFileLease.write_all
    def write(self, payload, **kwargs):
        original(self, payload, **kwargs)
        if self.path.name.startswith(".facts.md."):
            os._exit(71)
    ManagedFileLease.write_all = write
else:
    original = os.replace
    def replace(src, dst, **kwargs):
        original(src, dst, **kwargs)
        if dst == "facts.md":
            os._exit(72)
    os.replace = replace
save(host.memory_service, "second", "Durable after process interruption")
os._exit(99)
"""
        child = subprocess.run(
            [sys.executable, "-c", code, str(root), boundary],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert child.returncode == (71 if boundary == "temporary" else 72), child.stderr
        unrelated = root / "memory/.facts.md.unrelated.tmp"
        unrelated.write_text("User independent copy")
        host = build_standalone_host(root)
        try:
            memory = host.memory_service
            assert memory.get_operation("second")["status"] == "saved"
            assert not memory.mirrors.preview("facts")["ready"]
            with memory._db.reading() as conn:
                intent = json.loads(
                    conn.execute(
                        "SELECT intent FROM memory_mirrors "
                        "WHERE target_id='memory/facts.md'"
                    ).fetchone()[0]
                )
            host.recover()
            assert memory.mirrors.preview("facts")["ready"]
            assert (
                "Durable after process interruption"
                in memory.mirrors.preview("facts")["text"]
            )
            assert not (root / "memory" / intent["temporary"]).exists()
            assert unrelated.read_text() == "User independent copy"
        finally:
            assert host.close()


def test_actual_after_replace_rollback_and_retry_preserve_database(
    tmp_path, monkeypatch
):
    import os

    import pytest

    host = build_standalone_host(tmp_path / "state")
    original = os.replace

    def interrupted(src, dst, **kwargs):
        original(src, dst, **kwargs)
        if dst == "facts.md":
            raise KeyboardInterrupt("after actual replace")

    try:
        save(host.memory_service)
        monkeypatch.setattr(os, "replace", interrupted)
        with pytest.raises(KeyboardInterrupt):
            save(host.memory_service, "second", "Durable receipt after replace")
        receipt = host.memory_service.get_operation("second")
        assert receipt["status"] == "saved"
        assert not (tmp_path / "state/memory/facts.md").exists()
        assert not host.memory_service.mirrors.preview("facts")["ready"]
        monkeypatch.setattr(os, "replace", original)
        assert (
            host.memory_service.mirrors.retry("facts", CONTEXT)["status"]
            == "regenerated"
        )
        assert (
            "Durable receipt after replace"
            in host.memory_service.mirrors.preview("facts")["text"]
        )
        assert host.memory_service.get_operation("second") == receipt
    finally:
        monkeypatch.undo()
        assert host.close()


def test_real_consolidation_protection_edit_and_late_source_refresh(
    tmp_path, monkeypatch
):
    from agent_alfred.evals.deterministic.test_forgetting import delete
    from agent_alfred.evals.deterministic.test_memory_consolidation_service import (
        PLAN,
        _complete_chat,
    )
    from agent_alfred.model import ScriptedModel, ScriptedModelFactory
    from agent_alfred.settings import OPENCODE_API_KEY_ENV, Settings
    from agent_alfred.wiring import build_default_host

    monkeypatch.setenv(OPENCODE_API_KEY_ENV, "offline-fixture")
    model = ScriptedModel([PLAN])
    host = build_default_host(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(model),
        settings=Settings(consolidation_source_threshold=2),
    )
    try:
        host.start()
        memory = host.memory_service
        conn = memory._db._conn
        _complete_chat(memory, conn, "s", "r1", "I like coriander", "Noted.")
        _complete_chat(memory, conn, "s", "r2", "Still coriander", "Noted.")
        run = host.generate_consolidation("s")
        assert run.kind == "accepted"
        assert host.wait(run.run_id, timeout=5).outcome == "completed"
        before = memory.mirrors.preview("facts")
        assert before["ready"] and "likes coriander" in before["text"]
        assert "protected: false" in before["text"]
        protected = save(memory, "protect", "likes coriander")
        assert protected["status"] == "already_exists"
        assert protected["record_version"] == 1
        after = memory.mirrors.preview("facts")
        assert after["required_generation"] > before["required_generation"]
        assert "protected: true" in after["text"]
        edited = memory.execute(
            {
                "operation_id": "edit",
                "kind": "semantic",
                "action": "update",
                "expected_version": 1,
                "payload": {"id": protected["memory_id"], "fact": "loves coriander"},
            },
            CONTEXT,
        )
        assert edited["record_version"] == 2
        assert "loves coriander" in memory.mirrors.preview("facts")["text"]
        source = memory.forgetting.register_sources(
            "semantic",
            edited["memory_id"],
            2,
            groups=("r1",),
            evidence_id="late-association",
            context=CONTEXT,
        )
        assert source["status"] == "registered"
        linked = memory.mirrors.preview("facts")
        assert linked["ready"] and "r1" in linked["text"]
        independent = save(memory, "independent", "Independent record")
        assert delete(memory, edited)["status"] == "deleted"
        assert memory.mirrors.preview("facts")["ready"]
        assert "loves coriander" not in memory.mirrors.preview("facts")["text"]
        assert independent["memory_id"] in memory.mirrors.preview("facts")["text"]
        assert len(model.requests) == 1
    finally:
        assert host.close()


def test_dashboard_uses_the_same_configured_managed_mirrors(tmp_path):
    from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
        free_loopback_port,
    )
    from agent_alfred.model import ScriptedModel, ScriptedModelFactory
    from agent_alfred.wiring import build_dashboard

    model = ScriptedModel(["unused"])
    dashboard = build_dashboard(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(model),
        port=free_loopback_port(),
    )
    try:
        dashboard.start()
        assert save(dashboard.host.memory_service)["status"] == "saved"
        preview = dashboard.host.memory_service.mirrors.preview("facts")
        assert preview["ready"]
        assert (tmp_path / "state/memory/facts.md").read_text() == preview["text"]
        assert model.requests == []
    finally:
        assert dashboard.close()


def test_tool_save_refreshes_under_existing_run_permission(tmp_path, monkeypatch):
    from agent_alfred.evals.deterministic.test_runtime_tools import calls
    from agent_alfred.managed_state import ManagedDirectoryLease
    from agent_alfred.messages import ToolCallBlock
    from agent_alfred.model import ScriptedModel, ScriptedModelFactory
    from agent_alfred.runtime.host import SubmitRequest
    from agent_alfred.settings import OPENCODE_API_KEY_ENV, Settings
    from agent_alfred.wiring import build_default_host

    monkeypatch.setenv(OPENCODE_API_KEY_ENV, "offline-fixture")
    model = ScriptedModel(
        [
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(
                ToolCallBlock("save", "save_fact", {"subject": "color", "fact": "blue"})
            ),
            "Saved.",
        ]
    )
    host = build_default_host(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(model),
        settings=Settings(consolidation_source_threshold=2),
    )
    original = ManagedDirectoryLease.replace_bytes
    observed = []
    try:
        host.start()

        def replace(self, target, body, **kwargs):
            if target.name == "facts.md":
                reason = host.try_begin_mutation()
                assert not host.memory_service._db.transaction_in_progress
                observed.append((target, reason))
            return original(self, target, body, **kwargs)

        monkeypatch.setattr(ManagedDirectoryLease, "replace_bytes", replace)
        run = host.submit(SubmitRequest("Please save that my favorite color is blue"))
        assert host.wait(run.run_id, timeout=5).outcome == "completed"
        preview = host.memory_service.mirrors.preview("facts")
        assert preview["ready"] and "blue" in preview["text"], (preview, observed)
        assert '"type":"tool"' in preview["text"]
        assert "protected: true" in preview["text"]
        assert observed
        assert all(reason == "mutation_in_flight" for _, reason in observed), observed
        assert len(model.requests) == 3
    finally:
        monkeypatch.undo()
        assert host.close()


def test_late_source_after_delete_removes_unsafe_copy_from_real_mirrors(tmp_path):
    from agent_alfred.evals.deterministic.test_forgetting import delete

    host = build_standalone_host(tmp_path / "state")
    try:
        memory = host.memory_service
        memory.forgetting.register_group(
            "source",
            kind="run",
            container_id="s",
            evidence="complete",
            evidence_id="proof",
            context=CONTEXT,
        )
        victim = save(memory)
        retained = save(memory, "retained", "Later linked body")
        independent = save(memory, "independent", "Independent body")
        memory.forgetting.register_sources(
            "semantic",
            victim["memory_id"],
            1,
            groups=("source",),
            evidence_id="before",
            context=CONTEXT,
        )
        receipt = delete(memory, victim)
        assert memory.forgetting.get_forgetting("delete")["state"] == "complete"
        result = memory.forgetting.register_sources(
            "semantic",
            retained["memory_id"],
            1,
            groups=("source",),
            evidence_id="late",
            context=CONTEXT,
        )
        assert result["status"] == "registered"
        preview = memory.mirrors.preview("facts")
        assert preview["ready"]
        assert "Later linked body" not in preview["text"]
        assert independent["memory_id"] in preview["text"]
        assert memory.get("semantic", retained["memory_id"]) is not None
        assert memory.get_operation("delete") == receipt
    finally:
        assert host.close()


def test_verify_failure_is_separate_from_database_and_other_file(tmp_path, monkeypatch):
    from agent_alfred.managed_state import ManagedDirectoryLease

    host = build_standalone_host(tmp_path / "state")
    original_replace = ManagedDirectoryLease.replace_bytes
    original_open = ManagedDirectoryLease.open_regular
    replaced = []

    def replace(self, relative, payload, **kwargs):
        original_replace(self, relative, payload, **kwargs)
        if relative.name == "facts.md":
            replaced.append(True)

    def unavailable(self, relative, **kwargs):
        if relative.name == "facts.md" and replaced:
            raise OSError("private read verification failure")
        return original_open(self, relative, **kwargs)

    try:
        host.recover()
        monkeypatch.setattr(ManagedDirectoryLease, "replace_bytes", replace)
        monkeypatch.setattr(ManagedDirectoryLease, "open_regular", unavailable)
        receipt = save(host.memory_service)
        assert receipt["status"] == "saved"
        status = host.memory_service.mirrors.preview("facts")
        assert not status["ready"] and "text" not in status
        assert status["generated_generation"] == status["required_generation"]
        assert status["verified_generation"] < status["required_generation"]
        assert host.memory_service.mirrors.preview("episodes")["ready"]
        monkeypatch.undo()
        assert (
            host.memory_service.mirrors.retry("facts", CONTEXT)["status"]
            == "regenerated"
        )
        assert host.memory_service.mirrors.preview("facts")["ready"]
        assert host.memory_service.get_operation("save") == receipt
    finally:
        monkeypatch.undo()
        assert host.close()


def test_confirmation_return_loss_reads_only_own_operation(tmp_path, monkeypatch):
    import pytest

    host = build_standalone_host(tmp_path / "state")
    try:
        mirrors = host.memory_service.mirrors
        save(host.memory_service)
        path = tmp_path / "state/memory/facts.md"
        path.write_text("External edit")
        observation = mirrors.status("facts")["confirmation"]
        original = mirrors.rebuild

        def lose_return(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt("confirmed publication return lost")

        monkeypatch.setattr(mirrors, "rebuild", lose_return)
        with pytest.raises(KeyboardInterrupt):
            mirrors.confirm("facts", observation, "confirm", CONTEXT)
        receipt = mirrors.get_operation("confirm")["receipt"]
        assert receipt["status"] == "regenerated"
        monkeypatch.setattr(mirrors, "rebuild", original)
        path.write_text("External successor")
        assert mirrors.confirm("facts", observation, "confirm", CONTEXT) == receipt
        assert path.read_text() == "External successor"
        assert not mirrors.preview("facts")["ready"]
        path.unlink()
        assert mirrors.confirm("facts", observation, "missing", CONTEXT) == {
            "error": {"code": "stale_confirmation"}
        }
        assert not path.exists()
    finally:
        monkeypatch.undo()
        assert host.close()


def test_retained_temporary_close_owner_keeps_parent_until_cleanup(
    tmp_path, monkeypatch
):
    import pytest

    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.managed_state import ManagedFileLease

    baseline = _open_fd_count()
    host = build_standalone_host(tmp_path / "state")
    original = ManagedFileLease.close
    refuse_close = [True]

    def pending(self):
        if refuse_close[0] and self.path.name.startswith(".facts.md."):
            return False
        return original(self)

    try:
        save(host.memory_service)
        monkeypatch.setattr(ManagedFileLease, "close", pending)
        with pytest.raises(RuntimeError):
            save(host.memory_service, "second", "Keep durable ownership")
        assert host.memory_service.get_operation("second")["status"] == "saved"
        assert not host.memory_service.mirrors.preview("facts")["ready"]
        refuse_close[0] = False
        monkeypatch.setattr(ManagedFileLease, "close", original)
        assert host.memory_service.mirrors.close()
        assert (
            host.memory_service.mirrors.retry("facts", CONTEXT)["status"]
            == "regenerated"
        )
        assert host.memory_service.mirrors.preview("facts")["ready"]
        assert list((tmp_path / "state/memory").glob(".facts.md.*.tmp")) == []
    finally:
        refuse_close[0] = False
        monkeypatch.undo()
        assert host.close()
    assert _open_fd_count() == baseline


def test_inventory_fences_actual_files_before_participant_failure(
    tmp_path, monkeypatch
):
    import sqlite3

    from agent_alfred.evals.deterministic.test_forgetting import delete

    host = build_standalone_host(tmp_path / "state")
    try:
        memory = host.memory_service
        first = save(memory)
        second = save(memory, "second", "Second deletion")
        keep = save(memory, "keep", "Independent body")
        first_receipt = delete(memory, first, "delete-first")
        first_generation = memory.mirrors.status("facts")["required_generation"]
        second_receipt = delete(memory, second, "delete-second")
        assert memory.mirrors.preview("facts")["ready"]
        assert memory.mirrors.status("facts")["required_generation"] > first_generation
        original = memory.mirrors.invalidate

        def failed(*args, **kwargs):
            raise sqlite3.OperationalError("private participant error")

        monkeypatch.setattr(memory.mirrors, "invalidate", failed)
        result = memory.forgetting.reconcile_projections(CONTEXT)
        assert "error" in result
        assert not memory.mirrors.preview("facts")["ready"]
        assert not memory.mirrors.preview("episodes")["ready"]
        assert memory.forgetting.get_forgetting("delete-first")["state"] != "complete"
        monkeypatch.setattr(memory.mirrors, "invalidate", original)
        assert (
            memory.forgetting.reconcile_projections(CONTEXT)["status"] == "reconciled"
        )
        assert memory.forgetting.get_forgetting("delete-first")["state"] == "complete"
        assert memory.forgetting.get_forgetting("delete-second")["state"] == "complete"
        preview = memory.mirrors.preview("facts")
        assert preview["ready"] and keep["memory_id"] in preview["text"]
        assert first["memory_id"] not in preview["text"]
        assert second["memory_id"] not in preview["text"]
        assert memory.get_operation("delete-first") == first_receipt
        assert memory.get_operation("delete-second") == second_receipt
    finally:
        monkeypatch.undo()
        assert host.close()
