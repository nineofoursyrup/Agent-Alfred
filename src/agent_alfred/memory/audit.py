"""Private, persistent HMAC identities for memory operations."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from agent_alfred.redact import Redactor
from agent_alfred.resource_rollback import (
    ConstructionOwner,
    IncompleteRollback,
    OwnedDescriptor,
    OwnedResource,
    ResumableRollback,
)


class AuditKeyUnavailable(RuntimeError):
    """The audit identity cannot be established safely."""


@dataclass(frozen=True)
class AuditKey:
    key_id: str
    _secret: bytes = field(repr=False)

    def fingerprint(self, content: bytes) -> tuple[str, str]:
        return hmac.new(self._secret, content, hashlib.sha256).hexdigest(), self.key_id

    @classmethod
    def load_or_create(cls, path: Path, redactor: Redactor) -> AuditKey:
        owner = ConstructionOwner()
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            parent = path.parent.lstat()
            if (
                not stat.S_ISDIR(parent.st_mode)
                or stat.S_IMODE(parent.st_mode) != 0o700
            ):
                raise AuditKeyUnavailable("audit_key_permissions")
            if parent.st_uid != os.getuid():
                raise AuditKeyUnavailable("audit_key_owner")
            try:
                stream = _open_stream(
                    owner.rollback, path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, "w"
                )
            except FileExistsError:
                pass
            else:
                json.dump(
                    {"key_id": secrets.token_hex(16), "key": secrets.token_hex(32)},
                    stream,
                )
                stream.flush()
                os.fsync(stream.fileno())
                owner.rollback.close()
            stream = _open_stream(
                owner.rollback, path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, "r"
            )
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()
            ):
                raise AuditKeyUnavailable("audit_key_permissions")
            encoded = stream.read(4097)
            owner.rollback.close()
            if len(encoded) > 4096:
                raise AuditKeyUnavailable("audit_key_invalid")
            data = json.loads(encoded)
            secret = bytes.fromhex(data["key"])
            if (
                len(secret) != 32
                or not isinstance(data["key_id"], str)
                or not data["key_id"]
            ):
                raise AuditKeyUnavailable("audit_key_invalid")
            redactor.remember(encoded, credential=True)
            redactor.remember(data["key"], credential=True)
            return cls(data["key_id"], secret)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Normalize without retrying a close which has already failed.
            # The typed cause keeps its original cleanup progress reachable.
            failure = AuditKeyUnavailable("audit_key_unavailable")
            pending = exc.__cause__
            if isinstance(pending, IncompleteRollback):
                pending.rebind(failure)
                raise failure
            owner.fail(failure)
        except BaseException as exc:
            owner.fail(exc)


def _open_stream(
    rollback: ResumableRollback, path: Path, flags: int, mode: str
) -> TextIO:
    descriptor = OwnedDescriptor.open(rollback, path, flags, 0o600)

    def initialize(holder: OwnedResource[TextIO]) -> None:
        # The wrapper borrows the FD throughout its lifetime. Even interruption
        # in fdopen's Python return edge cannot orphan or double-close the FD;
        # no bytes have been buffered before the wrapper is published here.
        holder.publish(os.fdopen(descriptor.fd, mode, encoding="utf-8", closefd=False))

    return OwnedResource.acquire(rollback, initialize)
