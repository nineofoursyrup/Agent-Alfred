"""Explicit trusted-host Python code/dependency inventory.

Only declared standard-library/site-package/Alfred code roots are traversed. No
home, environment values, provider configuration, Keychain or credentials are
read. This pins bytes; actual runner non-write access remains a #105 obligation.
"""

import ctypes
import hashlib
import os
import stat
import sys
import sysconfig
from copy import deepcopy
from importlib.util import source_from_cache
from pathlib import Path

from ..schema import hash_value
from .contract import exact


def _code_roots():
    import agent_alfred

    roots = {
        Path(sysconfig.get_path(name)).resolve()
        for name in ("stdlib", "platstdlib", "purelib", "platlib")
    }
    roots.add(Path(agent_alfred.__file__).resolve().parent)
    # Nested site-packages are already inventoried by their enclosing root.
    return sorted(
        (
            path
            for path in roots
            if not any(path != other and path.is_relative_to(other) for other in roots)
        ),
        key=str,
    )


def _python_paths():
    paths = []
    roots = _code_roots()
    for entry in sys.path:
        if not entry:
            raise ValueError("local_host_clean_import_path_required")
        path = Path(entry).absolute()
        if path.exists():
            resolved = path.resolve()
            if not any(resolved.is_relative_to(root) for root in roots):
                raise ValueError("local_host_import_path_outside_code_roots")
        paths.append(str(path))
    return paths


def _native_images():
    if sys.platform != "darwin":
        raise ValueError("local_macos_installation_required")
    system = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    count = system._dyld_image_count
    count.argtypes, count.restype = [], ctypes.c_uint32
    name = system._dyld_get_image_name
    name.argtypes, name.restype = [ctypes.c_uint32], ctypes.c_char_p
    images = set()
    for index in range(count()):
        path = Path(os.fsdecode(name(index))).resolve()
        if not str(path).startswith(("/System/Library/", "/usr/lib/")):
            images.add(path)
    return images


def _identity(path):
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid not in (0, os.geteuid())
        or info.st_mode & 0o022
    ):
        raise ValueError("local_host_code_not_protected")
    return [
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    ]


def _inventory(roots):
    files = set()
    for root in roots:
        if root.is_symlink() or not root.is_dir():
            raise ValueError("local_host_code_root_invalid")
        pending = [root]
        while pending:
            directory = pending.pop()
            if directory.is_symlink():
                raise ValueError("local_host_code_symlink")
            if "__pycache__" in directory.parts:
                continue
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if entry.name == "__pycache__":
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                    if path.suffix in (".pyc", ".pyo"):
                        continue
                    if entry.is_symlink():
                        raise ValueError("local_host_code_symlink")
                    if entry.is_file(follow_symlinks=False):
                        files.add(path)
    return files


def _loaded_origins():
    paths = set()
    for module in tuple(sys.modules.values()):
        origin = getattr(module, "__file__", None)
        if origin is None:
            continue
        path = Path(origin).resolve()
        if path.suffix == ".pyc":
            try:
                path = Path(source_from_cache(str(path)))
            except ValueError:
                pass
        paths.add(path)
    return paths


def capture_host_environment():
    """Explicit code-only export input; caller saves it in the protected root.

    Run in the final isolated installed host interpreter (normally python -I),
    after importing the dispatch stack. An arbitrary checkout/pytest path is
    intentionally not accepted as the installed production import environment.
    """
    paths = _python_paths()
    roots = _code_roots()
    files = _inventory(roots) | _native_images() | {Path(sys.executable).resolve()}
    return {
        "contract": "V1-LOCAL-HOST-ENVIRONMENT",
        "version": 1,
        "python_executable": str(Path(sys.executable).resolve()),
        "python_version": sys.version,
        "sys_path": paths,
        "roots": [str(root) for root in roots],
        "files": [
            {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "identity": _identity(path),
            }
            for path in sorted(files, key=str)
        ],
    }


class HostEnvironmentVerifier:
    def __init__(self, manifest):
        exact(
            manifest,
            "contract version python_executable python_version sys_path roots files",
        )
        self.manifest = deepcopy(manifest)
        if (
            manifest["contract"] != "V1-LOCAL-HOST-ENVIRONMENT"
            or type(manifest["version"]) is not int
            or manifest["version"] != 1
            or manifest["python_executable"] != str(Path(sys.executable).resolve())
            or manifest["python_version"] != sys.version
            or manifest["roots"] != [str(path) for path in _code_roots()]
            or manifest["sys_path"] != _python_paths()
            or type(manifest["files"]) is not list
        ):
            raise ValueError("local_host_environment_mismatch")
        self._files = {}
        allowed = (
            _inventory(_code_roots())
            | _native_images()
            | {Path(sys.executable).resolve()}
        )
        for row in manifest["files"]:
            exact(row, "path sha256 identity")
            path = Path(row["path"])
            hash_value(row["sha256"])
            if path not in allowed or path in self._files:
                raise ValueError("local_host_inventory_invalid")
            before = _identity(path)
            if (
                before != row["identity"]
                or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]
                or _identity(path) != before
            ):
                raise ValueError("local_host_code_changed")
            self._files[path] = before
        if set(self._files) != allowed:
            raise ValueError("local_host_inventory_incomplete")
        # This is a partition of the fixed manifest, not a cached observation
        # of the filesystem. verify() still checks roots and scans them afresh.
        roots = _code_roots()
        self._root_files = frozenset(
            path
            for path in self._files
            if any(path.is_relative_to(root) for root in roots)
        )
        self.verify()

    def verify(self):
        roots = _code_roots()
        if self.manifest["sys_path"] != _python_paths() or self.manifest["roots"] != [
            str(path) for path in roots
        ]:
            raise ValueError("local_host_environment_changed")
        if (
            not _native_images() <= self._files.keys()
            or _inventory(roots) != self._root_files
        ):
            raise ValueError("local_host_inventory_changed")
        for path, before in self._files.items():
            if _identity(path) != before:
                raise ValueError("local_host_code_changed")
        if not _loaded_origins() <= self._files.keys():
            raise ValueError("local_host_loaded_origin_unverified")
        return {
            "contract": "V1-LOCAL-HOST-ENVIRONMENT",
            "version": 1,
            "files_verified": len(self._files),
        }
