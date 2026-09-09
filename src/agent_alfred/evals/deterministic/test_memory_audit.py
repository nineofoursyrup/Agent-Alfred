"""Audit identities survive restarts and never guess across key rotation."""

import stat

import pytest

from agent_alfred.memory.audit import AuditKey, AuditKeyUnavailable
from agent_alfred.redact import Redactor


def test_audit_key_reopens_and_rejects_permissive_files(tmp_path):
    path = tmp_path / "state" / "audit.key"
    key = AuditKey.load_or_create(path, Redactor([]))
    assert AuditKey.load_or_create(path, Redactor([])).fingerprint(
        b"fact"
    ) == key.fingerprint(b"fact")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    path.chmod(0o644)
    with pytest.raises(AuditKeyUnavailable):
        AuditKey.load_or_create(path, Redactor([]))


def test_audit_disk_encoding_is_registered_and_symlink_is_refused(tmp_path):
    path = tmp_path / "state" / "audit.key"
    redactor = Redactor([])
    key = AuditKey.load_or_create(path, redactor)
    encoded = path.read_text()
    import json

    assert redactor.redact_text(encoded) == "***"
    assert redactor.redact_text(json.loads(encoded)["key"]) == "***"
    assert "secret" not in repr(key)
    link = path.parent / "alias.key"
    link.symlink_to(path)
    with pytest.raises(AuditKeyUnavailable):
        AuditKey.load_or_create(link, redactor)


@pytest.mark.parametrize("existing", [False, True])
def test_stream_wrapper_failure_releases_the_open_descriptor(
    tmp_path, monkeypatch, existing
):
    import os

    from agent_alfred.memory import audit

    path = tmp_path / "state" / "audit.key"
    if existing:
        AuditKey.load_or_create(path, Redactor([]))
    descriptors = []

    def fail_wrapper(fd, *args, **kwargs):
        descriptors.append(fd)
        raise OSError("wrapper construction failed")

    monkeypatch.setattr(audit.os, "fdopen", fail_wrapper)
    with pytest.raises(AuditKeyUnavailable, match="audit_key_unavailable"):
        AuditKey.load_or_create(path, Redactor([]))
    assert descriptors
    for fd in descriptors:
        try:
            with pytest.raises(OSError):
                os.fstat(fd)
        finally:
            # Keep the RED reproduction from leaking into subsequent tests.
            try:
                os.close(fd)
            except OSError:
                pass


def test_a_failed_wrapper_close_retains_a_retryable_owner(tmp_path, monkeypatch):
    import os

    from agent_alfred.memory import audit
    from agent_alfred.resource_rollback import IncompleteRollback

    path = tmp_path / "state" / "audit.key"
    AuditKey.load_or_create(path, Redactor([]))
    real_fdopen = os.fdopen
    wrappers = []

    class FailingClose:
        def __init__(self, stream):
            self.stream = stream
            self.fd = stream.fileno()
            self.calls = 0

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def close(self):
            self.calls += 1
            if self.calls == 1:
                raise OSError("injected wrapper close failure")
            self.stream.close()

    def wrap(fd, *args, **kwargs):
        stream = FailingClose(real_fdopen(fd, *args, **kwargs))
        wrappers.append(stream)
        return stream

    monkeypatch.setattr(audit.os, "fdopen", wrap)
    with pytest.raises(AuditKeyUnavailable) as caught:
        AuditKey.load_or_create(path, Redactor([]))
    assert wrappers[0].calls == 1
    pending = caught.value.__cause__
    assert isinstance(pending, IncompleteRollback)
    assert pending.owner.retry()
    with pytest.raises(OSError):
        os.fstat(wrappers[0].fd)
    assert wrappers[0].stream.closed
    assert wrappers[0].calls == 2


def test_process_control_during_wrapper_construction_still_closes_fd(
    tmp_path, monkeypatch
):
    import os

    from agent_alfred.memory import audit

    path = tmp_path / "state" / "audit.key"
    AuditKey.load_or_create(path, Redactor([]))
    descriptors = []
    control = SystemExit("wrapper interrupted")

    def interrupt(fd, *args, **kwargs):
        descriptors.append(fd)
        raise control

    monkeypatch.setattr(audit.os, "fdopen", interrupt)
    with pytest.raises(SystemExit) as caught:
        AuditKey.load_or_create(path, Redactor([]))
    assert caught.value is control
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


def test_close_error_does_not_replace_wrapper_failure_or_reclose_fd(
    tmp_path, monkeypatch
):
    import os

    from agent_alfred.memory import audit
    from agent_alfred.resource_rollback import IncompleteRollback

    path = tmp_path / "state" / "audit.key"
    AuditKey.load_or_create(path, Redactor([]))
    real_close = os.close
    close_calls = []
    primary = OSError("original wrapper failure")

    def fail_wrapper(fd, *args, **kwargs):
        raise primary

    def close_then_fail(fd):
        close_calls.append(fd)
        real_close(fd)
        raise OSError("cleanup failed after descriptor was consumed")

    monkeypatch.setattr(audit.os, "fdopen", fail_wrapper)
    monkeypatch.setattr(audit.os, "close", close_then_fail)
    with pytest.raises(AuditKeyUnavailable) as caught:
        AuditKey.load_or_create(path, Redactor([]))
    pending = caught.value.__cause__
    assert isinstance(pending, IncompleteRollback)
    assert pending.failure is caught.value
    assert caught.value.__context__ is primary
    assert len(close_calls) == 1
    assert pending.owner.retry()
    assert len(close_calls) == 1


def test_nonregular_audit_key_is_rejected_before_reading(tmp_path):
    import os

    path = tmp_path / "state" / "audit.key"
    path.parent.mkdir(mode=0o700)
    os.mkfifo(path, mode=0o600)
    with pytest.raises(AuditKeyUnavailable, match="audit_key_permissions"):
        AuditKey.load_or_create(path, Redactor([]))
