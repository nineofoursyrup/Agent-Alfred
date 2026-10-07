"""Offline local storage behavior; these tests do not prove macOS isolation."""

import os

import pytest

from agent_alfred.evals.acceptance.controlled.local_files import OwnerDirectory
from agent_alfred.evals.acceptance.controlled.local_persistence import (
    _LOCAL_STORAGE,
    AppendWitnessDocuments,
    LocalExecutionStore,
    LocalExecutionWitness,
)
from agent_alfred.evals.acceptance.controlled.persistence import (
    SQLiteDocuments,
    SQLiteExecutionAnchor,
    SQLiteExecutionStore,
)
from agent_alfred.evals.acceptance.controlled.store import ZERO
from agent_alfred.evals.acceptance.schema import digest


def protected(tmp_path):
    path = tmp_path.resolve() / "owner"
    path.mkdir(mode=0o700)
    return OwnerDirectory(path, os.geteuid())


def test_local_witness_reopens_and_cas_rejects_second_writer(tmp_path):
    directory = protected(tmp_path)
    first = AppendWitnessDocuments(directory, "witness", token=_LOCAL_STORAGE)
    second = AppendWitnessDocuments(directory, "witness", token=_LOCAL_STORAGE)
    try:
        first.transaction([("job", "head", {"revision": 1}, None)])
        assert second.read("job", "head") == {"revision": 1}
        with pytest.raises(ValueError, match="ledger_conflict"):
            second.transaction([("job", "head", {"revision": 2}, None)])
        second.transaction(
            [
                ("job", "head", {"revision": 2}, digest({"revision": 1})),
                ("job", "event2", {"first_failure": "kept"}, None),
            ]
        )
        assert first.read("job", "head") == {"revision": 2}
        assert first.read("job", "event2") == {"first_failure": "kept"}
    finally:
        first.close()
        second.close()
    reopened = AppendWitnessDocuments(directory, "witness", token=_LOCAL_STORAGE)
    try:
        assert reopened.generation() == 2
        assert reopened.read("job", "event2") == {"first_failure": "kept"}
    finally:
        reopened.close()


def test_partial_witness_append_is_not_repaired_or_ignored(tmp_path):
    directory = protected(tmp_path)
    journal = AppendWitnessDocuments(directory, "witness", token=_LOCAL_STORAGE)
    journal.transaction([("job", "head", {"revision": 1}, None)])
    with journal.path.open("ab") as stream:
        stream.write(b'{"incomplete":')
    with pytest.raises(ValueError, match="local_witness_incomplete"):
        journal.read("job", "head")
    journal.close()
    with pytest.raises(ValueError, match="local_witness_incomplete"):
        AppendWitnessDocuments(directory, "witness", token=_LOCAL_STORAGE)
    assert journal.path.read_bytes().endswith(b'{"incomplete":')


def test_local_witness_detects_observed_truncation_and_replacement(tmp_path):
    directory = protected(tmp_path)
    journal = AppendWitnessDocuments(directory, "witness", token=_LOCAL_STORAGE)
    journal.transaction([("job", "head", {"revision": 1}, None)])
    journal.path.write_bytes(b"")
    with pytest.raises(ValueError, match="local_witness_rollback"):
        journal.read("job", "head")
    journal.path.rename(journal.path.with_name("original"))
    journal.path.touch(mode=0o600)
    with pytest.raises(ValueError, match="local_witness_replaced"):
        journal.read("job", "head")
    journal.close()


def test_local_store_and_append_anchor_preserve_closed_fixture_guards(tmp_path):
    directory = protected(tmp_path)
    with pytest.raises(ValueError, match="trusted_local_installation_required"):
        LocalExecutionStore(directory, "ledger")
    store = LocalExecutionStore(directory, "ledger", token=_LOCAL_STORAGE)
    anchor = LocalExecutionWitness(directory, "witness", token=_LOCAL_STORAGE)
    try:
        reference = store.put_object({"first_result": "FAIL"})
        assert store.get_object(reference) == {"first_result": "FAIL"}
        event = {
            "job_id": "job",
            "revision": 1,
            "previous_digest": ZERO,
            "event_digest": "1" * 64,
        }
        anchor.commit(event)
        assert anchor.read("job")["digest"] == "1" * 64
        assert anchor.read_events("job") == [event]
        assert anchor.independent_administrator is False
        assert all(
            cls.synthetic_only
            for cls in (SQLiteDocuments, SQLiteExecutionStore, SQLiteExecutionAnchor)
        )
    finally:
        store.close()
        anchor.close()


def test_owner_paths_reject_symlinks_and_world_readable_files(tmp_path):
    directory = protected(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("no secret")
    (directory.path / "link").symlink_to(outside)
    with pytest.raises(ValueError, match="local_protected_file_required"):
        directory.read("link")
    shared = directory.path / "shared"
    shared.write_text("no secret")
    shared.chmod(0o644)
    with pytest.raises(ValueError, match="local_protected_file_required"):
        directory.read("shared")


@pytest.mark.parametrize("operation", ["read", "append"])
def test_same_size_cached_witness_rewrite_is_rejected_before_read_or_append(
    tmp_path, operation
):
    directory = protected(tmp_path)
    journal = AppendWitnessDocuments(directory, "witness", token=_LOCAL_STORAGE)
    try:
        journal.transaction([("part", "key", {"value": "first"}, None)])
        before = journal.path.stat()
        raw = journal.path.read_bytes()
        changed = raw.replace(b'"first"', b'"f1rst"')
        assert changed != raw and len(changed) == len(raw)
        with journal.path.open("r+b") as stream:
            stream.write(changed)
            stream.flush()
            os.fsync(stream.fileno())
        # Restoring mtime still changes ctime, so it cannot validate cached bytes.
        os.utime(journal.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert journal.path.stat().st_ino == before.st_ino
        with pytest.raises(ValueError, match="local_witness_prefix_changed"):
            if operation == "read":
                journal.read("part", "key")
            else:
                journal.transaction([("part", "next", {"value": "later"}, None)])
        assert journal.path.read_bytes() == changed
    finally:
        journal.close()


def test_own_appends_do_not_rehash_prefix_but_external_append_does(
    tmp_path, monkeypatch
):
    directory = protected(tmp_path)
    journal = AppendWitnessDocuments(directory, "witness", token=_LOCAL_STORAGE)
    reads = []
    original_pread = os.pread

    def observed(fd, count, offset):
        reads.append((count, offset))
        return original_pread(fd, count, offset)

    monkeypatch.setattr(os, "pread", observed)
    try:
        for index in range(64):
            journal.transaction([("part", str(index), {"value": index}, None)])
            assert journal.read("part", str(index)) == {"value": index}
        assert reads == []
        prefix_size = journal.offset
        other = AppendWitnessDocuments(directory, "witness", token=_LOCAL_STORAGE)
        try:
            other.transaction([("part", "other", {"value": "external"}, None)])
        finally:
            other.close()
        assert journal.read("part", "other") == {"value": "external"}
        assert sum(count for count, _ in reads) == prefix_size
        reads.clear()
        assert journal.read("part", "other") == {"value": "external"}
        assert reads == []
    finally:
        journal.close()
