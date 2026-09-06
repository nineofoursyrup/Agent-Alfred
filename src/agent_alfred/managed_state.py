"""Capability-based ownership for Agent-Alfred managed filesystem paths."""

from __future__ import annotations

import ctypes
import errno as errno_module
import os
import shlex
import stat
import sys
from datetime import date as calendar_date
from functools import partial
from pathlib import Path, PurePath
from typing import Callable, Literal, TypeVar

from agent_alfred.resource_rollback import (
    ConstructionOwner,
    OwnedDescriptor,
    OwnedResource,
    ResumableRollback,
    RollbackSlot,
)

Reason = Literal[
    "symlink",
    "wrong_type",
    "wrong_owner",
    "mode_tighten_failed",
    "identity_changed",
    "unsupported_nofollow",
    "permission_denied",
]
ConnectionT = TypeVar("ConnectionT")


class ManagedPathSecurityError(RuntimeError):
    """A closed, user-actionable refusal to use a managed object."""

    def __init__(
        self,
        *,
        reason: Reason,
        role: str,
        path: Path,
        errno: int | None = None,
        operation: str | None = None,
        expected_mode: int | None = None,
    ) -> None:
        self.reason = reason
        self.role = role
        self.path = path.absolute()
        self.errno = errno
        self.operation = operation
        self.repair_hint = _repair_hint(reason, self.path, expected_mode)
        detail = f"managed path refused: reason={reason} role={role}"
        if errno is not None:
            detail += f" errno={errno}"
        super().__init__(f"{detail}; repair: {self.repair_hint}")


def _repair_hint(reason: Reason, path: Path, mode: int | None) -> str:
    quoted = shlex.quote(str(path.absolute()))
    inspect = f"/bin/ls -ld -- {quoted}"
    if reason in {"mode_tighten_failed", "permission_denied"} and mode is not None:
        return f"{inspect}; /bin/chmod {mode:04o} {quoted}"
    return (
        f"{inspect}; verify type and owner, then explicitly move or replace this path"
    )


def _translate_os_error(
    exc: OSError,
    *,
    role: str,
    path: Path,
    operation: str,
    mode: int | None = None,
    object_kind: Literal["directory", "file", "unknown"] = "unknown",
    symlink: bool = False,
    observed_wrong_type: bool = False,
) -> BaseException:
    """Translate only closed security facts; preserve ordinary I/O failures."""
    reason: Reason | None = None
    if exc.errno in (errno_module.EACCES, errno_module.EPERM):
        reason = "permission_denied"
    elif exc.errno == errno_module.ELOOP or symlink:
        reason = "symlink"
    elif observed_wrong_type:
        reason = "wrong_type"
    elif object_kind == "directory" and exc.errno == errno_module.ENOTDIR:
        reason = "wrong_type"
    elif object_kind == "file" and exc.errno == errno_module.EISDIR:
        reason = "wrong_type"
    if reason is None:
        return exc
    return ManagedPathSecurityError(
        reason=reason,
        role=role,
        path=path,
        errno=exc.errno,
        operation=operation,
        expected_mode=mode,
    )


def _validate_relative(relative: PurePath) -> tuple[str, ...]:
    if relative.is_absolute() or not relative.parts:
        raise ValueError("managed relative path must be non-empty and relative")
    if any(part in ("", ".", "..") for part in relative.parts):
        raise ValueError("managed relative path cannot contain dot components")
    return relative.parts


def _managed_fstat(fd: int, *, role: str, path: Path) -> os.stat_result:
    try:
        return os.fstat(fd)
    except OSError as exc:
        raise _translate_os_error(
            exc, role=role, path=path, operation="fstat"
        ) from exc


def _managed_dup(
    rollback: ResumableRollback, fd: int, *, role: str, path: Path
) -> OwnedDescriptor:
    try:
        return OwnedDescriptor.duplicate(rollback, fd)
    except OSError as exc:
        raise _translate_os_error(
            exc, role=role, path=path, operation="dup"
        ) from exc


def _rename_no_replace(
    source: str,
    target: str,
    *,
    source_parent_fd: int,
    target_parent_fd: int,
    role: str,
    path: Path,
) -> None:
    """Atomically rename one owned name without replacing an occupant."""
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        rename = libc.renameatx_np
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            source_parent_fd,
            source_bytes,
            target_parent_fd,
            target_bytes,
            0x0004,
        )
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            source_parent_fd,
            source_bytes,
            target_parent_fd,
            target_bytes,
            1,
        )
    else:
        raise ManagedPathSecurityError(
            reason="unsupported_nofollow", role=role, path=path
        )
    if result != 0:
        error = ctypes.get_errno()
        if error == errno_module.EEXIST:
            raise FileExistsError(error, os.strerror(error), target)
        raise OSError(error, os.strerror(error), target)


def _staging_token(name: str) -> str | None:
    if not name.startswith(".staging-"):
        return None
    token = name.removeprefix(".staging-")
    if len(token) != 32 or any(
        character not in "0123456789abcdef" for character in token
    ):
        return None
    return token


def _managed_stat(
    target: str,
    *,
    role: str,
    path: Path,
    dir_fd: int,
) -> os.stat_result:
    try:
        return os.stat(target, dir_fd=dir_fd, follow_symlinks=False)
    except OSError as exc:
        raise _translate_os_error(
            exc, role=role, path=path, operation="stat"
        ) from exc


def _close_descriptor_owners(owners: tuple[OwnedDescriptor, ...]) -> None:
    """Close every token, retaining the first failure.

    A token consumes its descriptor number on first close, so whichever owner
    reaches it first closes it and any other finds nothing left to close.
    """
    first_error: BaseException | None = None
    for owner in owners:
        try:
            owner.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


def _verify_named_descriptor(
    *,
    fd: int,
    anchor_fd: int | None,
    name: str | None,
    role: str,
    path: Path,
    expected_type: Callable[[int], bool],
) -> None:
    """Verify that a held descriptor still owns its typed directory entry."""
    if anchor_fd is None or name is None:
        return
    try:
        named = _managed_stat(name, dir_fd=anchor_fd, role=role, path=path)
    except OSError as exc:
        raise ManagedPathSecurityError(
            reason="identity_changed",
            role=role,
            path=path,
            errno=exc.errno,
            operation="stat",
        ) from exc
    opened = _managed_fstat(fd, role=role, path=path)
    if (
        stat.S_ISLNK(named.st_mode)
        or not expected_type(named.st_mode)
        or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise ManagedPathSecurityError(
            reason="identity_changed", role=role, path=path
        )


def _verify_fd(
    fd: int,
    *,
    path: Path,
    role: str,
    directory: bool,
    expected_mode: int,
    expected_identity: tuple[int, int] | None = None,
) -> os.stat_result:
    info = _managed_fstat(fd, role=role, path=path)
    correct_type = (
        stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    )
    if not correct_type:
        raise ManagedPathSecurityError(reason="wrong_type", role=role, path=path)
    if info.st_uid != os.geteuid():
        raise ManagedPathSecurityError(reason="wrong_owner", role=role, path=path)
    if (
        expected_identity is not None
        and (info.st_dev, info.st_ino) != expected_identity
    ):
        raise ManagedPathSecurityError(
            reason="identity_changed", role=role, path=path
        )
    try:
        os.fchmod(fd, expected_mode)
    except OSError as exc:
        if exc.errno not in (errno_module.EACCES, errno_module.EPERM):
            raise
        raise ManagedPathSecurityError(
            reason="mode_tighten_failed",
            role=role,
            path=path,
            errno=exc.errno,
            operation="fchmod",
            expected_mode=expected_mode,
        ) from exc
    checked = _managed_fstat(fd, role=role, path=path)
    checked_type = (
        stat.S_ISDIR(checked.st_mode)
        if directory
        else stat.S_ISREG(checked.st_mode)
    )
    if not checked_type:
        raise ManagedPathSecurityError(reason="wrong_type", role=role, path=path)
    if checked.st_uid != os.geteuid():
        raise ManagedPathSecurityError(reason="wrong_owner", role=role, path=path)
    if (checked.st_dev, checked.st_ino) != (info.st_dev, info.st_ino):
        raise ManagedPathSecurityError(reason="identity_changed", role=role, path=path)
    if stat.S_IMODE(checked.st_mode) != expected_mode:
        raise ManagedPathSecurityError(
            reason="mode_tighten_failed",
            role=role,
            path=path,
            expected_mode=expected_mode,
        )
    return checked


def _mint_named_capability(
    *,
    rollback: ResumableRollback,
    parent_fd: int,
    name: str,
    path: Path,
    role: str,
    directory: bool,
    flags: int,
    expected_mode: int,
    create_mode: int = 0,
    expected_named_identity: tuple[int, int] | None = None,
    identity_observer: Callable[[tuple[int, int]], None] | None = None,
    preflight: bool = True,
) -> tuple[OwnedDescriptor, OwnedDescriptor]:
    """Open, type, tighten, revalidate, and anchor one named capability.

    ``rollback`` is the caller's owner, established before this call. Both
    descriptors are owned there from the instant they exist and are handed
    back as their own tokens, so neither this return edge nor the caller's
    store can leave a minted capability without a reachable owner.
    """
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ManagedPathSecurityError(
            reason="unsupported_nofollow", role=role, path=path
        )
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    object_kind: Literal["directory", "file"] = (
        "directory" if directory else "file"
    )
    if preflight:
        try:
            existing = _managed_stat(name, dir_fd=parent_fd, role=role, path=path)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode):
                raise ManagedPathSecurityError(
                    reason="symlink", role=role, path=path
                )
            if not expected_type(existing.st_mode):
                raise ManagedPathSecurityError(
                    reason="wrong_type", role=role, path=path
                )
            if existing.st_uid != os.geteuid():
                raise ManagedPathSecurityError(
                    reason="wrong_owner", role=role, path=path
                )
    try:
        fd_owner = OwnedDescriptor.open(
            rollback,
            name,
            flags | nofollow,
            create_mode,
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        raise
    except OSError as exc:
        try:
            entry = _managed_stat(name, dir_fd=parent_fd, role=role, path=path)
            is_link = stat.S_ISLNK(entry.st_mode)
            wrong_type = not expected_type(entry.st_mode) and not is_link
        except OSError:
            is_link = False
            wrong_type = False
        raise _translate_os_error(
            exc,
            role=role,
            path=path,
            operation="open",
            mode=expected_mode,
            object_kind=object_kind,
            symlink=is_link,
            observed_wrong_type=wrong_type,
        ) from exc
    try:
        opened = _managed_fstat(fd_owner.fd, role=role, path=path)
        identity = (opened.st_dev, opened.st_ino)
        if identity_observer is not None:
            identity_observer(identity)
        if not expected_type(opened.st_mode):
            raise ManagedPathSecurityError(
                reason="wrong_type", role=role, path=path
            )
        try:
            named = _managed_stat(name, dir_fd=parent_fd, role=role, path=path)
        except OSError as exc:
            translated = _translate_os_error(
                exc,
                role=role,
                path=path,
                operation="stat",
                mode=expected_mode,
                object_kind=object_kind,
            )
            if translated is not exc:
                raise translated from exc
            raise ManagedPathSecurityError(
                reason="identity_changed",
                role=role,
                path=path,
                errno=exc.errno,
                operation="stat",
            ) from exc
        if (
            stat.S_ISLNK(named.st_mode)
            or not expected_type(named.st_mode)
            or (named.st_dev, named.st_ino) != identity
            or (
            expected_named_identity is not None
            and expected_named_identity != identity
            )
        ):
            raise ManagedPathSecurityError(
                reason="identity_changed", role=role, path=path
            )
        _verify_fd(
            fd_owner.fd,
            path=path,
            role=role,
            directory=directory,
            expected_mode=expected_mode,
            expected_identity=identity,
        )
        _verify_named_descriptor(
            fd=fd_owner.fd,
            anchor_fd=parent_fd,
            name=name,
            role=role,
            path=path,
            expected_type=expected_type,
        )
        anchor_owner = _managed_dup(
            rollback, parent_fd, role=role, path=path
        )
    except BaseException as exc:
        rollback.raise_failure(exc)
    return fd_owner, anchor_owner


def _write_all(fd: int, payload: bytes, *, retries: int = 0) -> None:
    view = memoryview(payload)
    while view:
        try:
            written = os.write(fd, view)
        except (InterruptedError, BlockingIOError):
            if retries <= 0:
                raise
            retries -= 1
            continue
        if written == 0:
            raise OSError(errno_module.EIO, "zero-byte managed write")
        view = view[written:]


class _ManagedCapabilityLease:
    """Shared descriptor ownership and identity contract for managed leases."""

    def __init__(
        self,
        path: Path,
        fd: int,
        *,
        role: str,
        expected_type: Callable[[int], bool],
        anchor_fd: int | None = None,
        name: str | None = None,
    ) -> None:
        self.path = path
        self._fd = fd
        self._role = role
        self._expected_type = expected_type
        self._anchor_fd = anchor_fd
        self._name = name
        self._closed = False
        self._descriptor_owners: tuple[OwnedDescriptor, ...] = ()

    @property
    def fd(self) -> int:
        if self._closed:
            raise RuntimeError(f"{self._role} lease is closed")
        return self._fd

    @property
    def role(self) -> str:
        return self._role

    def adopt_descriptors(self, *owners: OwnedDescriptor) -> None:
        """Close through the same tokens the construction rollback holds."""
        self._descriptor_owners = owners

    def close(self) -> None:
        if self._closed:
            return
        owners = self._descriptor_owners
        if not owners:
            # Public leases can be constructed without construction tokens.
            # Publish equivalent tokens on the lease before either close can
            # run, so an interrupted close still has a reachable owner.
            owners = tuple(
                OwnedDescriptor(descriptor)
                for descriptor in (self._fd, self._anchor_fd)
                if descriptor is not None and descriptor >= 0
            )
            self._descriptor_owners = owners
        _close_descriptor_owners(owners)
        # Every token above has a durable close result. These fields are only
        # mirrors now, and may safely lag across an asynchronous interruption.
        self._fd = -1
        self._anchor_fd = None
        self._descriptor_owners = ()
        self._closed = True

    def verify_identity(self) -> None:
        _verify_named_descriptor(
            fd=self.fd,
            anchor_fd=self._anchor_fd,
            name=self._name,
            role=self._role,
            path=self.path,
            expected_type=self._expected_type,
        )


class ManagedFileLease(_ManagedCapabilityLease):
    def __init__(
        self,
        path: Path,
        fd: int,
        *,
        _anchor_fd: int | None = None,
        _name: str | None = None,
        _role: str = "managed file",
    ) -> None:
        super().__init__(
            path,
            fd,
            role=_role,
            expected_type=stat.S_ISREG,
            anchor_fd=_anchor_fd,
            name=_name,
        )

    def write_all(self, payload: bytes, *, retries: int = 0) -> None:
        self.verify_identity()
        _write_all(self.fd, payload, retries=retries)

    def write_lock_diagnostic(self, fd: int, payload: bytes) -> None:
        """Replace one held duplicate's diagnostic before publishing its lock."""
        self.verify_identity()
        os.ftruncate(fd, 0)
        _write_all(fd, payload)
        os.fsync(fd)
        self.verify_identity()

    def fsync(self) -> None:
        self.verify_identity()
        os.fsync(self.fd)

    def stat(self) -> os.stat_result:
        return _managed_fstat(self.fd, role=self._role, path=self.path)

    def duplicate_fd(self, *, _rollback: ResumableRollback) -> OwnedDescriptor:
        return _managed_dup(
            _rollback, self.fd, role=self._role, path=self.path
        )

    def connection_token(self) -> ManagedConnectionToken:
        """Capture the one validated file identity a path-only client may use."""
        self.verify_identity()
        info = self.stat()
        _verify_connection_target(self, info, (info.st_dev, info.st_ino))
        return ManagedConnectionToken(self, (info.st_dev, info.st_ino))


class ManagedConnectionToken:
    """Fence a path-only opener with capability and lexical identity checks."""

    def __init__(
        self, lease: ManagedFileLease, identity: tuple[int, int]
    ) -> None:
        self._lease = lease
        self._identity = identity

    def connect(
        self,
        opener: Callable[..., ConnectionT],
        *,
        _rollback: ResumableRollback,
        **kwargs: object,
    ) -> ConnectionT:
        try:
            connection = OwnedResource.acquire(
                _rollback,
                partial(opener, str(self._lease.path), **kwargs),
            )
            return connection
        except BaseException as exc:
            try:
                self.verify()
            except ManagedPathSecurityError as security_error:
                raise security_error from exc
            raise

    def verify(self) -> None:
        self._lease.verify_identity()
        _verify_connection_target(self._lease, self._lease.stat(), self._identity)


def _verify_connection_target(
    lease: ManagedFileLease,
    info: os.stat_result,
    identity: tuple[int, int],
) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise ManagedPathSecurityError(
            reason="wrong_type", role=lease._role, path=lease.path
        )
    if info.st_uid != os.geteuid():
        raise ManagedPathSecurityError(
            reason="wrong_owner", role=lease._role, path=lease.path
        )
    if (
        (info.st_dev, info.st_ino) != identity
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise ManagedPathSecurityError(
            reason="identity_changed", role=lease._role, path=lease.path
        )


class ManagedDirectoryLease(_ManagedCapabilityLease):
    def __init__(
        self,
        path: Path,
        fd: int,
        *,
        role: str,
        anchor_fd: int | None = None,
        name: str | None = None,
    ) -> None:
        super().__init__(
            path,
            fd,
            role=role,
            expected_type=stat.S_ISDIR,
            anchor_fd=anchor_fd,
            name=name,
        )

    def fsync(self) -> None:
        self.verify_identity()
        os.fsync(self.fd)

    def remove_managed_staging(self) -> bool:
        """Validate and remove one stale staging tree under process exclusion."""
        name = self._name
        anchor = self._anchor_fd
        if name is None or anchor is None:
            raise ValueError("staging reclamation requires a named child capability")
        if _staging_token(name) is None:
            raise ValueError(f"not a managed staging directory: {name!r}")
        self.verify_identity()
        allowed_files = {"meta.json", "trace.jsonl"}
        allowed_directories = {"artifacts"}
        entries = os.listdir(self.fd)
        unexpected = sorted(
            entry
            for entry in entries
            if entry not in allowed_files | allowed_directories
        )
        if unexpected:
            raise ValueError(f"unexpected entries in staging {name!r}: {unexpected}")
        file_leases: dict[str, ManagedFileLease] = {}
        artifacts: ManagedDirectoryLease | None = None
        rollback = RollbackSlot()
        try:
            for entry in entries:
                expected_type = (
                    stat.S_ISREG if entry in allowed_files else stat.S_ISDIR
                )
                expected_mode = 0o600 if entry in allowed_files else 0o700
                entry_path = self.path / entry
                info = _managed_stat(
                    entry,
                    dir_fd=self.fd,
                    role="bundle staging entry",
                    path=entry_path,
                )
                if stat.S_ISLNK(info.st_mode):
                    raise ValueError(f"unexpected symlink in staging: {entry!r}")
                if not expected_type(info.st_mode):
                    kind = "regular file" if entry in allowed_files else "directory"
                    raise ValueError(f"staging entry {entry!r} is not a {kind}")
                if (
                    info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != expected_mode
                ):
                    raise ValueError(
                        f"staging entry {entry!r} has unmanaged ownership or mode"
                    )
                identity = (info.st_dev, info.st_ino)
                entry_rollback = ResumableRollback()
                rollback.begin(entry_rollback)
                if entry in allowed_files:
                    file_leases[entry] = self._open_regular(
                        PurePath(entry),
                        access="read",
                        create=False,
                        role="bundle staging entry",
                        verify_lexical_parent=False,
                        expected_identity=identity,
                        _rollback=entry_rollback,
                    )
                    continue
                artifacts = self._open_directory(
                    entry,
                    path=entry_path,
                    role="bundle artifacts directory",
                    expected_identity=identity,
                    _rollback=entry_rollback,
                )
                if os.listdir(artifacts.fd):
                    raise ValueError("staging artifacts directory is not empty")

            # The process lock excludes another conforming publisher while the
            # retained capabilities are revalidated and removed by parent fd.
            for entry, lease in file_leases.items():
                opened = _managed_fstat(
                    lease.fd, role=lease._role, path=lease.path
                )
                named = _managed_stat(
                    entry, dir_fd=self.fd, role=lease._role, path=self.path / entry
                )
                if (opened.st_dev, opened.st_ino) != (
                    named.st_dev,
                    named.st_ino,
                ):
                    raise ManagedPathSecurityError(
                        reason="identity_changed",
                        role=lease._role,
                        path=self.path / entry,
                    )
            if artifacts is not None:
                identity = _managed_fstat(
                    artifacts.fd, role=artifacts.role, path=artifacts.path
                )
                named = _managed_stat(
                    "artifacts",
                    dir_fd=self.fd,
                    role=artifacts.role,
                    path=self.path / "artifacts",
                )
                if (identity.st_dev, identity.st_ino) != (
                    named.st_dev,
                    named.st_ino,
                ):
                    raise ManagedPathSecurityError(
                        reason="identity_changed",
                        role=artifacts.role,
                        path=self.path / "artifacts",
                    )
            self.verify_identity()
            for entry in sorted(file_leases):
                os.unlink(entry, dir_fd=self.fd)
            if artifacts is not None:
                os.rmdir("artifacts", dir_fd=self.fd)
            self.verify_identity()
            os.rmdir(name, dir_fd=anchor)
        except BaseException as exc:
            rollback.raise_failure(exc)
        rollback.close()
        return True

    def _remove_owned_regular_name(
        self,
        *,
        name: str,
        lease: ManagedFileLease,
        expected_identity: tuple[int, int],
    ) -> None:
        named = _managed_stat(
            name, dir_fd=self.fd, role=lease._role, path=self.path / name
        )
        if (named.st_dev, named.st_ino) != expected_identity:
            raise ManagedPathSecurityError(
                reason="identity_changed", role=lease._role, path=self.path / name
            )
        os.unlink(name, dir_fd=self.fd)

    def create_directory(
        self,
        relative: PurePath,
        *,
        role: str,
        _rollback: ResumableRollback | None = None,
    ) -> ManagedDirectoryLease:
        parts = _validate_relative(relative)
        if len(parts) != 1:
            raise ValueError("create_directory requires a direct child")
        name = parts[0]
        path = self.path / name
        self.verify_identity()
        owner = ConstructionOwner(_rollback)
        rollback = owner.rollback
        created_identity: tuple[int, int] | None = None

        def remove_created_entry() -> bool:
            try:
                parent_fd = self.fd
            except RuntimeError:
                try:
                    path.lstat()
                except FileNotFoundError:
                    return True
                return False
            try:
                named = _managed_stat(
                    name, dir_fd=parent_fd, role=role, path=path
                )
            except FileNotFoundError:
                return True
            if created_identity is None:
                # ``mkdir`` returned, but no descriptor or stat established
                # which inode the name denoted. It may already be a foreign
                # replacement, so this rollback has nothing it can safely
                # remove by name. A later retry may retire after an operator
                # or startup reclaimer removes that uncertain entry.
                return False
            if not stat.S_ISDIR(named.st_mode):
                return False
            if created_identity is not None and (
                named.st_dev,
                named.st_ino,
            ) != created_identity:
                return False
            os.rmdir(name, dir_fd=parent_fd)
            return True

        created_entry = OwnedResource[None](
            lambda _created: remove_created_entry()
        )
        rollback.own(created_entry)
        try:
            # The holder records mkdir's successful ``None`` result before
            # Python regains an instruction boundary. An OSError leaves it
            # unset, so rollback cannot remove a pre-existing name.
            created_entry.capture_c_result(
                partial(os.mkdir, name, 0o700, dir_fd=self.fd)
            )
        except OSError as exc:
            translated = _translate_os_error(
                exc,
                role=role,
                path=path,
                operation="mkdir",
                mode=0o700,
                object_kind="directory",
            )
            try:
                raise translated from exc
            except BaseException as failure:
                owner.fail(failure)
        except BaseException as exc:
            owner.fail(exc)
        try:
            observed = _managed_stat(
                name,
                dir_fd=self.fd,
                role=role,
                path=path,
            )
            observed_identity = (observed.st_dev, observed.st_ino)
            lease = self._open_directory(
                name,
                path=path,
                role=role,
                expected_identity=observed_identity,
                _rollback=rollback,
            )
            created = _managed_fstat(lease.fd, role=role, path=path)
            created_identity = (created.st_dev, created.st_ino)
            lease.verify_identity()
            owner.publish(lease, parts=(created_entry,))
        except BaseException as exc:
            owner.fail(exc)
        return lease

    def reclaim_stale_trace_staging(self) -> int:
        """Remove validated stale trace staging after the process lock is held."""
        try:
            traces_info = _managed_stat(
                "traces",
                dir_fd=self.fd,
                role="trace root",
                path=self.path / "traces",
            )
        except FileNotFoundError:
            return 0
        rollback = ResumableRollback()
        try:
            traces = self._open_directory(
                "traces",
                path=self.path / "traces",
                role="trace root",
                expected_identity=(traces_info.st_dev, traces_info.st_ino),
                _rollback=rollback,
            )
            removed = 0
            for date_name in os.listdir(traces.fd):
                try:
                    if calendar_date.fromisoformat(date_name).isoformat() != date_name:
                        continue
                except ValueError:
                    continue
                date_rollback = ResumableRollback()
                try:
                    date_info = _managed_stat(
                        date_name,
                        dir_fd=traces.fd,
                        role="trace date directory",
                        path=traces.path / date_name,
                    )
                    if (
                        not stat.S_ISDIR(date_info.st_mode)
                        or date_info.st_uid != os.geteuid()
                        or stat.S_IMODE(date_info.st_mode) != 0o700
                    ):
                        continue
                    date = traces._open_directory(
                        date_name,
                        path=traces.path / date_name,
                        role="trace date directory",
                        expected_identity=(date_info.st_dev, date_info.st_ino),
                        _rollback=date_rollback,
                    )
                    for name in os.listdir(date.fd):
                        if _staging_token(name) is None:
                            continue
                        staging_rollback = ResumableRollback()
                        try:
                            info = _managed_stat(
                                name,
                                dir_fd=date.fd,
                                role="bundle staging directory",
                                path=date.path / name,
                            )
                            if (
                                not stat.S_ISDIR(info.st_mode)
                                or info.st_uid != os.geteuid()
                                or stat.S_IMODE(info.st_mode) != 0o700
                            ):
                                continue
                            staging = date._open_directory(
                                name,
                                path=date.path / name,
                                role="bundle staging directory",
                                expected_identity=(info.st_dev, info.st_ino),
                                _rollback=staging_rollback,
                            )
                            if staging.remove_managed_staging():
                                removed += 1
                        except (ManagedPathSecurityError, OSError, ValueError) as exc:
                            if not staging_rollback.retry():
                                staging_rollback.raise_incomplete(exc)
                            continue
                        except BaseException as exc:
                            staging_rollback.raise_failure(exc)
                        staging_rollback.close()
                except (ManagedPathSecurityError, OSError, ValueError) as exc:
                    if not date_rollback.retry():
                        date_rollback.raise_incomplete(exc)
                    continue
                except BaseException as exc:
                    date_rollback.raise_failure(exc)
                date_rollback.close()
        except BaseException as exc:
            rollback.raise_failure(exc)
        rollback.close()
        return removed

    def _open_directory(
        self,
        name: str,
        *,
        path: Path,
        role: str,
        expected_identity: tuple[int, int] | None = None,
        identity_observer: Callable[[tuple[int, int]], None] | None = None,
        _rollback: ResumableRollback | None = None,
    ) -> ManagedDirectoryLease:
        owner = ConstructionOwner(_rollback)
        try:
            fd_owner, anchor_owner = _mint_named_capability(
                rollback=owner.rollback,
                parent_fd=self.fd,
                name=name,
                path=path,
                role=role,
                directory=True,
                flags=os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                expected_mode=0o700,
                expected_named_identity=expected_identity,
                identity_observer=identity_observer,
                preflight=False,
            )
            self.verify_identity()
            lease = ManagedDirectoryLease(
                path,
                fd_owner.fd,
                role=role,
                anchor_fd=anchor_owner.fd,
                name=name,
            )
            lease.adopt_descriptors(fd_owner, anchor_owner)
            owner.publish(lease, parts=(fd_owner, anchor_owner))
        except BaseException as exc:
            owner.fail(exc)
        return lease

    def rename_directory_no_replace(
        self,
        source: ManagedDirectoryLease,
        target: PurePath,
    ) -> None:
        parts = _validate_relative(target)
        if len(parts) != 1 or source.path.parent != self.path:
            raise ValueError("rename requires a direct child of this directory")
        self.verify_identity()
        source.verify_identity()
        source_name = source.path.name
        named = _managed_stat(
            source_name,
            dir_fd=self.fd,
            role="bundle staging directory",
            path=source.path,
        )
        opened = _managed_fstat(
            source.fd, role="bundle staging directory", path=source.path
        )
        if stat.S_ISLNK(named.st_mode) or not stat.S_ISDIR(named.st_mode):
            raise ManagedPathSecurityError(
                reason="wrong_type",
                role="bundle staging directory",
                path=source.path,
            )
        if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise ManagedPathSecurityError(
                reason="identity_changed",
                role="bundle staging directory",
                path=source.path,
            )
        _rename_no_replace(
            source_name,
            parts[0],
            source_parent_fd=self.fd,
            target_parent_fd=self.fd,
            role="bundle publication",
            path=self.path / parts[0],
        )
        # RENAME_EXCL / RENAME_NOREPLACE protects only the destination.  The
        # source is still resolved by name inside the syscall and could have
        # changed after the checks above.  Do not transfer the capability to
        # the public name until that name is proved to hold the descriptor we
        # prepared.  A mismatch is publication-uncertain: the caller must not
        # write through, repair, or delete either name.
        _verify_named_descriptor(
            fd=source.fd,
            anchor_fd=self.fd,
            name=parts[0],
            role="bundle publication",
            path=self.path / parts[0],
            expected_type=stat.S_ISDIR,
        )
        source.path = self.path / parts[0]
        source._name = parts[0]

    def remove_directory_if_owned(self, child: ManagedDirectoryLease) -> bool:
        """Remove an empty direct child only while its name still names its fd."""
        if child.path.parent != self.path:
            raise ValueError("remove requires a direct child")
        try:
            named = _managed_stat(
                child.path.name,
                dir_fd=self.fd,
                role=child.role,
                path=child.path,
            )
        except FileNotFoundError:
            return False
        opened = _managed_fstat(child.fd, role=child.role, path=child.path)
        if (
            stat.S_ISLNK(named.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            return False
        os.rmdir(child.path.name, dir_fd=self.fd)
        return True

    def ensure_directory(
        self,
        relative: PurePath,
        *,
        _rollback: ResumableRollback | None = None,
    ) -> ManagedDirectoryLease:
        parts = _validate_relative(relative)
        owner = ConstructionOwner(_rollback)
        rollback = owner.rollback
        result_anchor_owner: OwnedDescriptor | None = None
        path = self.path
        try:
            parent_owner = _managed_dup(
                rollback, self.fd, role=self.role, path=self.path
            )
            for part in parts:
                self.verify_identity()
                path = path / part
                try:
                    os.mkdir(part, 0o700, dir_fd=parent_owner.fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise _translate_os_error(
                        exc,
                        role="managed directory",
                        path=path,
                        operation="mkdir",
                        mode=0o700,
                        object_kind="directory",
                    ) from exc
                child_owner, child_anchor_owner = _mint_named_capability(
                    rollback=rollback,
                    parent_fd=parent_owner.fd,
                    name=part,
                    path=path,
                    role="managed directory",
                    directory=True,
                    flags=os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                    expected_mode=0o700,
                    preflight=False,
                )
                parent_owner.close()
                if result_anchor_owner is not None:
                    result_anchor_owner.close()
                parent_owner = child_owner
                result_anchor_owner = child_anchor_owner
            assert result_anchor_owner is not None
            lease = ManagedDirectoryLease(
                path,
                parent_owner.fd,
                role="managed directory",
                anchor_fd=result_anchor_owner.fd,
                name=parts[-1],
            )
            lease.adopt_descriptors(parent_owner, result_anchor_owner)
            owner.publish(lease, parts=(parent_owner, result_anchor_owner))
        except BaseException as exc:
            owner.fail(exc)
        return lease

    def open_regular(
        self,
        relative: PurePath,
        *,
        access: Literal["read", "read_write", "append", "exclusive_write"],
        create: bool,
        role: str = "managed file",
        _rollback: ResumableRollback | None = None,
    ) -> ManagedFileLease:
        return self._open_regular(
            relative,
            access=access,
            create=create,
            role=role,
            verify_lexical_parent=True,
            _rollback=_rollback,
        )

    def _open_regular(
        self,
        relative: PurePath,
        *,
        access: Literal["read", "read_write", "append", "exclusive_write"],
        create: bool,
        role: str,
        verify_lexical_parent: bool,
        expected_identity: tuple[int, int] | None = None,
        _rollback: ResumableRollback | None = None,
    ) -> ManagedFileLease:
        parts = _validate_relative(relative)
        if len(parts) != 1:
            raise ValueError("open_regular requires a direct child")
        name = parts[0]
        path = self.path / name
        if verify_lexical_parent:
            self.verify_identity()
        modes = {
            "read": os.O_RDONLY,
            "read_write": os.O_RDWR,
            "append": os.O_WRONLY | os.O_APPEND,
            "exclusive_write": os.O_WRONLY | os.O_EXCL,
        }
        flags = modes[access] | getattr(os, "O_CLOEXEC", 0)
        if create:
            flags |= os.O_CREAT
        # A name can still be exchanged after the lstat-style preflight.  A
        # non-blocking open makes that race fail closed even if the replacement
        # is a FIFO; regular-file semantics are unchanged and the fd is typed
        # and identity-checked immediately below.
        flags |= getattr(os, "O_NONBLOCK", 0)
        owner = ConstructionOwner(_rollback)
        rollback = owner.rollback
        try:
            fd_owner, anchor_owner = _mint_named_capability(
                rollback=rollback,
                parent_fd=self.fd,
                name=name,
                path=path,
                role=role,
                directory=False,
                flags=flags,
                expected_mode=0o600,
                create_mode=0o600,
                expected_named_identity=expected_identity,
            )
            if verify_lexical_parent:
                self.verify_identity()
            lease = ManagedFileLease(
                path,
                fd_owner.fd,
                _anchor_fd=anchor_owner.fd,
                _name=name,
                _role=role,
            )
            lease.adopt_descriptors(fd_owner, anchor_owner)
            owner.publish(lease, parts=(fd_owner, anchor_owner))
        except BaseException as exc:
            owner.fail(exc)
        return lease

    def replace_bytes(
        self,
        relative: PurePath,
        payload: bytes,
        *,
        published: Callable[[], None] | None = None,
    ) -> None:
        parts = _validate_relative(relative)
        if len(parts) != 1:
            raise ValueError("replace_bytes requires a direct child")
        target = parts[0]
        target_path = self.path / target
        try:
            existing = _managed_stat(
                target,
                dir_fd=self.fd,
                role="managed replacement target",
                path=target_path,
            )
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise _translate_os_error(
                exc,
                role="managed replacement target",
                path=target_path,
                operation="stat",
                mode=0o600,
                object_kind="file",
            ) from exc
        else:
            if stat.S_ISLNK(existing.st_mode):
                raise ManagedPathSecurityError(
                    reason="symlink",
                    role="managed replacement target",
                    path=target_path,
                )
            if not stat.S_ISREG(existing.st_mode):
                raise ManagedPathSecurityError(
                    reason="wrong_type",
                    role="managed replacement target",
                    path=target_path,
                )
            if existing.st_uid != os.geteuid():
                raise ManagedPathSecurityError(
                    reason="wrong_owner",
                    role="managed replacement target",
                    path=target_path,
                )
        temporary = f".{target}.{os.getpid()}.tmp"
        rollback = RollbackSlot()
        temporary_rollback = ResumableRollback()
        lease_rollback = ResumableRollback()
        rollback.begin(temporary_rollback)
        rollback.begin(lease_rollback)
        temporary_entry = object()
        temporary_rollback.own(
            temporary_entry,
            lambda: self.unlink_regular(PurePath(temporary), missing_ok=True),
        )
        try:
            lease = self.open_regular(
                PurePath(temporary),
                access="exclusive_write",
                create=True,
                role="descriptor temporary file",
                _rollback=lease_rollback,
            )
            lease.write_all(payload)
            lease.fsync()
            temporary_info = lease.stat()
            temporary_identity = (temporary_info.st_dev, temporary_info.st_ino)
            try:
                closed = lease.close()
            except BaseException as exc:
                rollback.raise_failure(exc)
            if closed is False:
                rollback.raise_failure(
                    RuntimeError("temporary file cleanup is incomplete")
                )
            lease_rollback.transfer(lease)
            published_target = object()
            published_rollback = ResumableRollback()
            rollback.begin(published_rollback)
            published_rollback.own(
                published_target,
                lambda: self._unlink_regular_identity(
                    PurePath(target), temporary_identity
                ),
            )
            try:
                os.replace(
                    temporary,
                    target,
                    src_dir_fd=self.fd,
                    dst_dir_fd=self.fd,
                )
                if published is not None:
                    published()
            except OSError as exc:
                raise _translate_os_error(
                    exc,
                    role="managed replacement target",
                    path=target_path,
                    operation="replace",
                    mode=0o600,
                    object_kind="file",
                ) from exc
        except BaseException as exc:
            rollback.raise_failure(exc)
        published_rollback.transfer(published_target)
        temporary_rollback.transfer(temporary_entry)
        rollback.close()

    def _unlink_regular_identity(
        self, relative: PurePath, expected_identity: tuple[int, int]
    ) -> bool:
        """Remove a published name only while it still names our inode."""
        rollback = ResumableRollback()
        try:
            lease = self._open_regular(
                relative,
                access="read",
                create=False,
                role="managed published file",
                verify_lexical_parent=False,
                expected_identity=expected_identity,
                _rollback=rollback,
            )
        except FileNotFoundError as exc:
            if not rollback.retry():
                rollback.raise_incomplete(exc)
            return True
        except ManagedPathSecurityError as exc:
            if exc.reason in {
                "identity_changed",
                "symlink",
                "wrong_type",
                "wrong_owner",
            }:
                if not rollback.retry():
                    rollback.raise_incomplete(exc)
                return True
            rollback.raise_failure(exc)
        except BaseException as exc:
            rollback.raise_failure(exc)
        try:
            self._remove_owned_regular_name(
                name=str(relative),
                lease=lease,
                expected_identity=expected_identity,
            )
        except BaseException as exc:
            rollback.raise_failure(exc)
        rollback.close()
        return True

    def unlink_regular(self, relative: PurePath, *, missing_ok: bool) -> None:
        rollback = ResumableRollback()
        try:
            lease = self._open_regular(
                relative,
                access="read",
                create=False,
                role="managed file",
                verify_lexical_parent=False,
                _rollback=rollback,
            )
            info = lease.stat()
            try:
                self._remove_owned_regular_name(
                    name=str(relative),
                    lease=lease,
                    expected_identity=(info.st_dev, info.st_ino),
                )
            except FileNotFoundError:
                if not missing_ok:
                    raise
            except OSError as exc:
                raise _translate_os_error(
                    exc,
                    role="managed file",
                    path=self.path / str(relative),
                    operation="unlink",
                    mode=0o600,
                    object_kind="file",
                ) from exc
        except FileNotFoundError as exc:
            if not missing_ok:
                rollback.raise_failure(exc)
        except BaseException as exc:
            rollback.raise_failure(exc)
        rollback.close()


class ManagedStateLease(ManagedDirectoryLease):
    def ensure_trace_directory(
        self,
        relative: PurePath,
        *,
        _rollback: ResumableRollback | None = None,
    ) -> ManagedTraceRoot:
        """Mint the path/fd-opaque root capability consumed by TraceSink."""
        owner = ConstructionOwner(_rollback)
        try:
            lease = self.ensure_directory(relative, _rollback=owner.rollback)
            root = ManagedTraceRoot(lease)
            owner.publish(root, parts=(lease,))
            return root
        except BaseException as exc:
            owner.fail(exc)


class ManagedTraceRoot:
    """Narrow trace-root capability: no absolute path or descriptor escape."""

    def __init__(self, lease: ManagedDirectoryLease) -> None:
        self._lease = lease

    def ensure_directory(
        self,
        relative: PurePath,
        *,
        _rollback: ResumableRollback | None = None,
    ) -> ManagedDirectoryLease:
        return self._lease.ensure_directory(relative, _rollback=_rollback)

    def close(self) -> None:
        self._lease.close()


class ManagedStateDirectory:
    @classmethod
    def acquire_trace_root(
        cls, path: Path, *, _rollback: ResumableRollback | None = None
    ) -> ManagedTraceRoot:
        owner = ConstructionOwner(_rollback)
        try:
            lease = cls.acquire(path, _rollback=owner.rollback)
            root = ManagedTraceRoot(lease)
            owner.publish(root, parts=(lease,))
            return root
        except BaseException as exc:
            owner.fail(exc)

    @classmethod
    def acquire(
        cls, path: Path, *, _rollback: ResumableRollback | None = None
    ) -> ManagedStateLease:
        path = Path(path).absolute()
        parts = path.parts[1:]
        if not parts:
            raise ValueError("filesystem root cannot be a managed state directory")
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise ManagedPathSecurityError(
                reason="unsupported_nofollow", role="state root", path=path
            )
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
        owner = ConstructionOwner(_rollback)
        rollback = owner.rollback
        parent_path = path.parent
        try:
            try:
                parent_owner = OwnedDescriptor.open(
                    rollback, parent_path, directory_flags
                )
            except OSError as exc:
                raise _translate_os_error(
                    exc,
                    role="state root parent",
                    path=parent_path,
                    operation="open",
                    object_kind="directory",
                ) from exc
            name = parts[-1]
            try:
                os.mkdir(name, 0o700, dir_fd=parent_owner.fd)
            except FileExistsError:
                pass
            except OSError as exc:
                raise _translate_os_error(
                    exc,
                    role="state root",
                    path=path,
                    operation="mkdir",
                    mode=0o700,
                    object_kind="directory",
                ) from exc
            named_before_open = _managed_stat(
                name, dir_fd=parent_owner.fd, role="state root", path=path
            )
            fd_owner, anchor_owner = _mint_named_capability(
                rollback=rollback,
                parent_fd=parent_owner.fd,
                name=name,
                path=path,
                role="state root",
                directory=True,
                flags=os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                expected_mode=0o700,
                expected_named_identity=(
                    named_before_open.st_dev,
                    named_before_open.st_ino,
                ),
            )
            parent_owner.close()
            rollback.transfer(parent_owner)
            lease = ManagedStateLease(
                path,
                fd_owner.fd,
                role="state root",
                anchor_fd=anchor_owner.fd,
                name=name,
            )
            lease.adopt_descriptors(fd_owner, anchor_owner)
            owner.publish(lease, parts=(fd_owner, anchor_owner))
        except BaseException as exc:
            owner.fail(exc)
        return lease
