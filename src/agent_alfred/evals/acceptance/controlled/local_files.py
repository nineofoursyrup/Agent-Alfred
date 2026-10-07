"""Owner protected paths for the explicit local installation, never a sandbox.

These checks defend the trusted host against accidental path substitution. The
runner's actual inability to reach these paths is a separate installation proof.
The trusted owner/root can still replace or roll back the complete installation.
"""

import os
import stat
from pathlib import Path

from ..materials import strict_json


class OwnerDirectory:
    def __init__(self, path, owner_uid):
        self.path = Path(path).absolute()
        self.owner_uid = owner_uid
        if type(owner_uid) is not int or owner_uid != os.geteuid():
            raise ValueError("local_owner_identity_mismatch")
        self._parents(self.path)
        info = self.path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("local_protected_directory_required")
        self.identity = (info.st_dev, info.st_ino)

    def _parents(self, path):
        for node in (path, *path.parents):
            info = node.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or info.st_uid not in (0, self.owner_uid)
                or info.st_mode & 0o022
            ):
                # A private child of the system temporary directory is safe
                # against another uid only when sticky-bit semantics apply.
                if not (
                    node != path
                    and stat.S_ISDIR(info.st_mode)
                    and info.st_uid == 0
                    and info.st_mode & stat.S_ISVTX
                ):
                    raise ValueError("local_path_not_owner_protected")

    def check(self):
        self._parents(self.path)
        info = self.path.lstat()
        if (info.st_dev, info.st_ino) != self.identity or info.st_mode & 0o077:
            raise ValueError("local_protected_directory_changed")

    def file(self, name, *, create=False):
        self.check()
        path = Path(name)
        if not path.is_absolute():
            path = self.path / path
        if path.parent != self.path or path.name in ("", ".", ".."):
            raise ValueError("local_path_outside_protected_directory")
        if not path.exists() and not path.is_symlink():
            if not create:
                raise ValueError("local_protected_file_missing")
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
            directory = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != self.owner_uid
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise ValueError("local_protected_file_required")
        return path

    def read(self, name, *, limit=8 * 1024 * 1024):
        path = self.file(name)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            observed = path.lstat()
            if (info.st_dev, info.st_ino) != (observed.st_dev, observed.st_ino):
                raise ValueError("local_protected_file_changed")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                raw = stream.read(limit + 1)
            if len(raw) > limit:
                raise ValueError("local_protected_file_too_large")
            return raw
        finally:
            os.close(descriptor)

    def json(self, name, *, limit=8 * 1024 * 1024):
        return strict_json(self.read(name, limit=limit), limit=limit)
