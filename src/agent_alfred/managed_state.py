"""Capability-based ownership for Agent-Alfred managed filesystem paths."""

from __future__ import annotations

import ctypes
import errno as errno_module
import os
import shlex
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Callable, Literal, TypeVar

from agent_alfred.resource_rollback import OwnedDescriptor, ResumableRollback

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


def _managed_dup(fd: int, *, role: str, path: Path) -> int:
    try:
        return os.dup(fd)
    except OSError as exc:
        raise _translate_os_error(
            exc, role=role, path=path, operation="dup"
        ) from exc


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


def _close_owned_descriptors(*descriptors: int | None) -> None:
    """Attempt every owned close and retain the first failure."""
    first_error: BaseException | None = None
    for fd in descriptors:
        if fd is None or fd < 0:
            continue
        try:
            os.close(fd)
        except BaseException as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


class _LexicalAncestorFence:
    """Hold and revalidate every directory from filesystem root to a parent."""

    def __init__(
        self, descriptors: list[int], names: list[_LexicalName]
    ) -> None:
        if not descriptors or len(names) != len(descriptors) - 1:
            raise ValueError("lexical ancestor fence shape is invalid")
        self._descriptors = descriptors
        self._names = names
        self._closed = False

    def verify(self, *, role: str, path: Path) -> None:
        if self._closed:
            raise RuntimeError("lexical ancestor fence is closed")
        root = _managed_fstat(self._descriptors[0], role=role, path=path)
        if not stat.S_ISDIR(root.st_mode):
            raise ManagedPathSecurityError(
                reason="identity_changed", role=role, path=path
            )
        for parent_fd, name, child_fd in zip(
            self._descriptors[:-1],
            self._names,
            self._descriptors[1:],
            strict=True,
        ):
            try:
                named = _managed_stat(
                    name.value, dir_fd=parent_fd, role=role, path=path
                )
            except OSError as exc:
                raise ManagedPathSecurityError(
                    reason="identity_changed",
                    role=role,
                    path=path,
                    errno=exc.errno,
                    operation="stat",
                ) from exc
            opened = _managed_fstat(child_fd, role=role, path=path)
            if (
                stat.S_ISLNK(named.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or not stat.S_ISDIR(opened.st_mode)
                or (named.st_dev, named.st_ino)
                != (opened.st_dev, opened.st_ino)
            ):
                raise ManagedPathSecurityError(
                    reason="identity_changed", role=role, path=path
                )

    def extend(
        self, *, name: _LexicalName, fd: int, role: str, path: Path
    ):
        descriptors: list[int] = []
        try:
            for descriptor in (*self._descriptors, fd):
                descriptors.append(_managed_dup(descriptor, role=role, path=path))
        except BaseException:
            _close_owned_descriptors(*descriptors)
            raise
        return _LexicalAncestorFence(descriptors, [*self._names, name])

    def close(self) -> None:
        if self._closed:
            return
        descriptors, self._descriptors = self._descriptors, []
        self._closed = True
        _close_owned_descriptors(*descriptors)


@dataclass
class _LexicalName:
    """Shared directory-entry name updated by an authorized rename."""

    value: str


def _close_capability(
    fd: int,
    anchor_fd: int | None,
    ancestor_fence: _LexicalAncestorFence | None,
) -> None:
    first_error: BaseException | None = None
    try:
        _close_owned_descriptors(fd, anchor_fd)
    except BaseException as exc:
        first_error = exc
    if ancestor_fence is not None:
        try:
            ancestor_fence.close()
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
    parent_fd: int,
    name: str,
    path: Path,
    role: str,
    directory: bool,
    flags: int,
    expected_mode: int,
    create_mode: int = 0,
    expected_named_identity: tuple[int, int] | None = None,
    preflight: bool = True,
) -> tuple[int, int]:
    """Open, type, tighten, revalidate, and anchor one named capability."""
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
        fd_owner = OwnedDescriptor(
            os.open(name, flags | nofollow, create_mode, dir_fd=parent_fd)
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
    rollback = ResumableRollback()
    rollback.own(fd_owner)
    try:
        opened = _managed_fstat(fd_owner.fd, role=role, path=path)
        identity = (opened.st_dev, opened.st_ino)
        if not expected_type(opened.st_mode):
            raise ManagedPathSecurityError(
                reason="wrong_type", role=role, path=path
            )
        try:
            named = _managed_stat(name, dir_fd=parent_fd, role=role, path=path)
        except OSError as exc:
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
        anchor_owner = OwnedDescriptor(
            _managed_dup(parent_fd, role=role, path=path)
        )
        rollback.own(anchor_owner)
    except BaseException as exc:
        rollback.raise_failure(exc)
    rollback.transfer(fd_owner)
    rollback.transfer(anchor_owner)
    return fd_owner.fd, anchor_owner.fd


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


@dataclass
class ManagedFileLease:
    path: Path
    fd: int
    _anchor_fd: int | None = None
    _name: str | None = None
    _role: str = "managed file"
    _ancestor_fence: _LexicalAncestorFence | None = None
    _closed: bool = False

    def close(self) -> None:
        fd, self.fd = self.fd, -1
        anchor_fd, self._anchor_fd = self._anchor_fd, None
        ancestor_fence, self._ancestor_fence = self._ancestor_fence, None
        self._closed = True
        _close_capability(fd, anchor_fd, ancestor_fence)

    def verify_identity(self) -> None:
        if self._ancestor_fence is not None:
            self._ancestor_fence.verify(role=self._role, path=self.path)
        _verify_named_descriptor(
            fd=self.fd,
            anchor_fd=self._anchor_fd,
            name=self._name,
            role=self._role,
            path=self.path,
            expected_type=stat.S_ISREG,
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

    def duplicate_fd(self) -> int:
        return _managed_dup(self.fd, role=self._role, path=self.path)

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
        **kwargs: object,
    ) -> ConnectionT:
        try:
            return opener(str(self._lease.path), **kwargs)
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


class ManagedDirectoryLease:
    def __init__(
        self,
        path: Path,
        fd: int,
        *,
        role: str,
        anchor_fd: int | None = None,
        name: str | None = None,
    ) -> None:
        self.path = path
        self._fd = fd
        self.role = role
        self._anchor_fd = anchor_fd
        self._name = name
        self._lexical_name = _LexicalName(name) if name is not None else None
        self._ancestor_fence: _LexicalAncestorFence | None = None
        self._closed = False

    @property
    def fd(self) -> int:
        if self._closed:
            raise RuntimeError("managed directory lease is closed")
        return self._fd

    def close(self) -> None:
        fd, self._fd = self._fd, -1
        anchor_fd, self._anchor_fd = self._anchor_fd, None
        ancestor_fence, self._ancestor_fence = self._ancestor_fence, None
        self._closed = True
        _close_capability(fd, anchor_fd, ancestor_fence)

    def verify_identity(self) -> None:
        if self._ancestor_fence is not None:
            self._ancestor_fence.verify(role=self.role, path=self.path)
        _verify_named_descriptor(
            fd=self.fd,
            anchor_fd=self._anchor_fd,
            name=self._name,
            role=self.role,
            path=self.path,
            expected_type=stat.S_ISDIR,
        )

    def _fence_for_child(
        self, *, role: str, path: Path
    ) -> _LexicalAncestorFence | None:
        if self._ancestor_fence is None or self._lexical_name is None:
            return None
        return self._ancestor_fence.extend(
            name=self._lexical_name, fd=self.fd, role=role, path=path
        )

    def fsync(self) -> None:
        self.verify_identity()
        os.fsync(self.fd)

    def remove_managed_staging(self) -> bool:
        """Remove this exact, capability-held unpublished bundle staging."""
        name = self._name
        anchor = self._anchor_fd
        if name is None or anchor is None:
            raise ValueError("staging reclamation requires a named child capability")
        suffix = name.removeprefix(".staging-")
        if (
            len(suffix) != 32
            or any(character not in "0123456789abcdef" for character in suffix)
        ):
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
        for entry in entries:
            info = _managed_stat(
                entry,
                dir_fd=self.fd,
                role="bundle staging entry",
                path=self.path / entry,
            )
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"unexpected symlink in staging: {entry!r}")
            if entry in allowed_files and not stat.S_ISREG(info.st_mode):
                raise ValueError(f"staging entry {entry!r} is not a regular file")
            if entry in allowed_directories and not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"staging entry {entry!r} is not a directory")
        for entry in allowed_files & set(entries):
            self.unlink_regular(PurePath(entry), missing_ok=False)
        if "artifacts" in entries:
            artifacts = self._open_directory(
                "artifacts",
                path=self.path / "artifacts",
                role="bundle artifacts directory",
            )
            try:
                if os.listdir(artifacts.fd):
                    raise ValueError("staging artifacts directory is not empty")
                self.remove_directory_if_owned(artifacts)
            finally:
                artifacts.close()
        self.verify_identity()
        os.rmdir(name, dir_fd=anchor)
        return True

    def create_directory(
        self, relative: PurePath, *, role: str
    ) -> ManagedDirectoryLease:
        parts = _validate_relative(relative)
        if len(parts) != 1:
            raise ValueError("create_directory requires a direct child")
        name = parts[0]
        path = self.path / name
        self.verify_identity()
        try:
            os.mkdir(name, 0o700, dir_fd=self.fd)
        except OSError as exc:
            raise _translate_os_error(
                exc,
                role=role,
                path=path,
                operation="mkdir",
                mode=0o700,
                object_kind="directory",
            ) from exc
        rollback = ResumableRollback()
        created_entry = object()
        created_identity: tuple[int, int] | None = None

        def remove_created_entry() -> bool:
            if created_identity is None:
                # mkdir returned, but the first identity observation did not.
                # The name may already denote a replacement, so there is no
                # object this transaction can prove it owns and safely remove.
                return False
            try:
                named = _managed_stat(name, dir_fd=self.fd, role=role, path=path)
            except FileNotFoundError:
                return True
            if (
                not stat.S_ISDIR(named.st_mode)
                or (named.st_dev, named.st_ino) != created_identity
            ):
                return True
            os.rmdir(name, dir_fd=self.fd)
            return True

        rollback.own(created_entry, remove_created_entry)
        try:
            created = _managed_stat(name, dir_fd=self.fd, role=role, path=path)
            created_identity = (created.st_dev, created.st_ino)
            lease = self._open_directory(
                name,
                path=path,
                role=role,
                expected_identity=created_identity,
            )
        except BaseException as exc:
            rollback.raise_failure(exc)
        rollback.transfer(created_entry)
        return lease

    def _open_directory(
        self,
        name: str,
        *,
        path: Path,
        role: str,
        expected_identity: tuple[int, int] | None = None,
    ) -> ManagedDirectoryLease:
        fd, anchor_fd = _mint_named_capability(
            parent_fd=self.fd,
            name=name,
            path=path,
            role=role,
            directory=True,
            flags=os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            expected_mode=0o700,
            expected_named_identity=expected_identity,
            preflight=False,
        )
        rollback = ResumableRollback()
        fd_owner = OwnedDescriptor(fd)
        anchor_owner = OwnedDescriptor(anchor_fd)
        rollback.own(fd_owner)
        rollback.own(anchor_owner)
        try:
            self.verify_identity()
            ancestor_fence = self._fence_for_child(role=role, path=path)
            if ancestor_fence is not None:
                rollback.own(ancestor_fence)
            lease = ManagedDirectoryLease(
                path, fd, role=role, anchor_fd=anchor_fd, name=name
            )
            lease._ancestor_fence = ancestor_fence
        except BaseException as exc:
            rollback.raise_failure(exc)
        rollback.transfer(fd_owner)
        rollback.transfer(anchor_owner)
        if ancestor_fence is not None:
            rollback.transfer(ancestor_fence)
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
        libc = ctypes.CDLL(None, use_errno=True)
        source_bytes = os.fsencode(source_name)
        target_bytes = os.fsencode(parts[0])
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
            result = rename(self.fd, source_bytes, self.fd, target_bytes, 0x0004)
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
            result = rename(self.fd, source_bytes, self.fd, target_bytes, 1)
        else:
            raise ManagedPathSecurityError(
                reason="unsupported_nofollow",
                role="bundle publication",
                path=self.path / parts[0],
            )
        if result != 0:
            error = ctypes.get_errno()
            if error == errno_module.EEXIST:
                raise FileExistsError(error, os.strerror(error), parts[0])
            raise OSError(error, os.strerror(error), parts[0])
        source.path = self.path / parts[0]
        source._name = parts[0]
        assert source._lexical_name is not None
        source._lexical_name.value = parts[0]

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

    def ensure_directory(self, relative: PurePath) -> ManagedDirectoryLease:
        parts = _validate_relative(relative)
        rollback = ResumableRollback()
        parent_owner = OwnedDescriptor(
            _managed_dup(self.fd, role=self.role, path=self.path)
        )
        rollback.own(parent_owner)
        result_anchor_owner: OwnedDescriptor | None = None
        try:
            parent_fence = self._fence_for_child(
                role=self.role, path=self.path
            )
            if parent_fence is not None:
                rollback.own(parent_fence)
        except BaseException as exc:
            rollback.raise_failure(exc)
        result_fence: _LexicalAncestorFence | None = None
        path = self.path
        try:
            for index, part in enumerate(parts):
                self.verify_identity()
                if parent_fence is not None:
                    parent_fence.verify(role="managed directory", path=path)
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
                child_fd, child_anchor_fd = _mint_named_capability(
                    parent_fd=parent_owner.fd,
                    name=part,
                    path=path,
                    role="managed directory",
                    directory=True,
                    flags=os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                    expected_mode=0o700,
                    preflight=False,
                )
                child_owner = OwnedDescriptor(child_fd)
                child_anchor_owner = OwnedDescriptor(child_anchor_fd)
                rollback.own(child_owner)
                rollback.own(child_anchor_owner)
                parent_owner.close()
                if result_anchor_owner is not None:
                    result_anchor_owner.close()
                parent_owner = child_owner
                result_anchor_owner = child_anchor_owner
                if index == len(parts) - 1:
                    result_fence, parent_fence = parent_fence, None
                elif parent_fence is not None:
                    next_fence = parent_fence.extend(
                        name=_LexicalName(part),
                        fd=child_owner.fd,
                        role="managed directory",
                        path=path,
                    )
                    rollback.own(next_fence)
                    parent_fence.close()
                    parent_fence = next_fence
            anchor_fd = (
                None if result_anchor_owner is None else result_anchor_owner.fd
            )
            lease = ManagedDirectoryLease(
                path,
                parent_owner.fd,
                role="managed directory",
                anchor_fd=anchor_fd,
                name=parts[-1],
            )
            lease._ancestor_fence = result_fence
        except BaseException as exc:
            rollback.raise_failure(exc)
        rollback.transfer(parent_owner)
        if result_anchor_owner is not None:
            rollback.transfer(result_anchor_owner)
        if result_fence is not None:
            rollback.transfer(result_fence)
        return lease

    def open_regular(
        self,
        relative: PurePath,
        *,
        access: Literal["read", "read_write", "append", "exclusive_write"],
        create: bool,
        role: str = "managed file",
    ) -> ManagedFileLease:
        return self._open_regular(
            relative,
            access=access,
            create=create,
            role=role,
            verify_lexical_parent=True,
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
        fd, anchor_fd = _mint_named_capability(
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
        rollback = ResumableRollback()
        fd_owner = OwnedDescriptor(fd)
        anchor_owner = OwnedDescriptor(anchor_fd)
        rollback.own(fd_owner)
        rollback.own(anchor_owner)
        try:
            if verify_lexical_parent:
                self.verify_identity()
            ancestor_fence = None
            if verify_lexical_parent:
                ancestor_fence = self._fence_for_child(role=role, path=path)
            if ancestor_fence is not None:
                rollback.own(ancestor_fence)
            lease = ManagedFileLease(
                path,
                fd,
                _anchor_fd=anchor_fd,
                _name=name,
                _role=role,
                _ancestor_fence=ancestor_fence,
            )
        except BaseException as exc:
            rollback.raise_failure(exc)
        rollback.transfer(fd_owner)
        rollback.transfer(anchor_owner)
        if ancestor_fence is not None:
            rollback.transfer(ancestor_fence)
        return lease

    def replace_bytes(self, relative: PurePath, payload: bytes) -> None:
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
        lease = self.open_regular(
            PurePath(temporary),
            access="exclusive_write",
            create=True,
            role="descriptor temporary file",
        )
        rollback = ResumableRollback()
        temporary_entry = object()
        rollback.own(
            temporary_entry,
            lambda: self.unlink_regular(PurePath(temporary), missing_ok=True),
        )
        rollback.own(lease)
        try:
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
            rollback.transfer(lease)
            published_target = object()
            rollback.own(
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
        rollback.transfer(published_target)
        rollback.transfer(temporary_entry)

    def _unlink_regular_identity(
        self, relative: PurePath, expected_identity: tuple[int, int]
    ) -> bool:
        """Remove a published name only while it still names our inode."""
        lease: ManagedFileLease | None = None
        try:
            lease = self._open_regular(
                relative,
                access="read",
                create=False,
                role="managed published file",
                verify_lexical_parent=False,
                expected_identity=expected_identity,
            )
        except FileNotFoundError:
            return True
        except ManagedPathSecurityError as exc:
            if exc.reason in {
                "identity_changed",
                "symlink",
                "wrong_type",
                "wrong_owner",
            }:
                return True
            raise
        try:
            named = _managed_stat(
                str(relative),
                dir_fd=self.fd,
                role="managed published file",
                path=self.path / str(relative),
            )
            if (named.st_dev, named.st_ino) != expected_identity:
                return True
            os.unlink(str(relative), dir_fd=self.fd)
            return True
        finally:
            lease.close()

    def unlink_regular(self, relative: PurePath, *, missing_ok: bool) -> None:
        lease: ManagedFileLease | None = None
        try:
            lease = self._open_regular(
                relative,
                access="read",
                create=False,
                role="managed file",
                verify_lexical_parent=False,
            )
            try:
                os.unlink(str(relative), dir_fd=self.fd)
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
        except FileNotFoundError:
            if not missing_ok:
                raise
        finally:
            if lease is not None:
                lease.close()


class ManagedStateLease(ManagedDirectoryLease):
    def ensure_trace_directory(self, relative: PurePath) -> ManagedTraceRoot:
        """Mint the path/fd-opaque root capability consumed by TraceSink."""
        return ManagedTraceRoot(self.ensure_directory(relative))


class ManagedTraceRoot:
    """Narrow trace-root capability: no absolute path or descriptor escape."""

    def __init__(self, lease: ManagedDirectoryLease) -> None:
        self._lease = lease

    def ensure_directory(self, relative: PurePath) -> ManagedDirectoryLease:
        return self._lease.ensure_directory(relative)

    def close(self) -> None:
        self._lease.close()


class ManagedStateDirectory:
    @classmethod
    def acquire_trace_root(cls, path: Path) -> ManagedTraceRoot:
        return ManagedTraceRoot(cls.acquire(path))

    @classmethod
    def acquire(cls, path: Path) -> ManagedStateLease:
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
        rollback = ResumableRollback()
        try:
            parent_owner = OwnedDescriptor(os.open(os.sep, directory_flags))
            rollback.own(parent_owner)
        except OSError as exc:
            raise _translate_os_error(
                exc,
                role="state root ancestor",
                path=Path(os.sep),
                operation="open",
                object_kind="directory",
            ) from exc
        current = Path(os.sep)
        ancestor_owners = [parent_owner]
        ancestor_names: list[_LexicalName] = []
        try:
            for part in parts[:-1]:
                current /= part
                try:
                    child_owner = OwnedDescriptor(
                        os.open(part, directory_flags, dir_fd=parent_owner.fd)
                    )
                    rollback.own(child_owner)
                except OSError as exc:
                    try:
                        entry = os.stat(
                            part, dir_fd=parent_owner.fd, follow_symlinks=False
                        )
                        is_link = stat.S_ISLNK(entry.st_mode)
                        wrong_type = not stat.S_ISDIR(entry.st_mode) and not is_link
                    except OSError:
                        is_link = False
                        wrong_type = False
                    raise _translate_os_error(
                        exc,
                        role="state root ancestor",
                        path=current,
                        operation="open",
                        object_kind="directory",
                        symlink=is_link,
                        observed_wrong_type=wrong_type,
                    ) from exc
                parent_owner = child_owner
                ancestor_owners.append(child_owner)
                ancestor_names.append(_LexicalName(part))
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
            fd, anchor_fd = _mint_named_capability(
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
            fd_owner = OwnedDescriptor(fd)
            anchor_owner = OwnedDescriptor(anchor_fd)
            rollback.own(fd_owner)
            rollback.own(anchor_owner)
            ancestor_fence = _LexicalAncestorFence(
                [owner.fd for owner in ancestor_owners], ancestor_names
            )
            rollback.own(ancestor_fence)
            for owner in ancestor_owners:
                rollback.transfer(owner)
        except BaseException as exc:
            rollback.raise_failure(exc)
        lease = ManagedStateLease(
            path,
            fd_owner.fd,
            role="state root",
            anchor_fd=anchor_owner.fd,
            name=name,
        )
        lease._ancestor_fence = ancestor_fence
        rollback.transfer(fd_owner)
        rollback.transfer(anchor_owner)
        rollback.transfer(ancestor_fence)
        return lease
