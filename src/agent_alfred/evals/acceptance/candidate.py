"""Capture tracked and included untracked bytes without staging or committing."""

import hashlib
import os
import platform
import stat
import subprocess
from pathlib import Path

EXCLUDED = ("tmp/", ".scratch/", "output/", "dist/", "node_modules/", ".venv/")


def git(root, *args):
    env = dict(os.environ)
    clt = "/Library/Developer/CommandLineTools"
    if platform.system() == "Darwin" and Path(clt).is_dir():
        env["DEVELOPER_DIR"] = clt
    return subprocess.check_output(["git", "-C", str(root), *args], env=env)


def capture(root):
    root = Path(root).resolve()
    names = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    files = {}
    for name in sorted(set(names.decode().rstrip("\0").split("\0"))):
        if not name or name.startswith(EXCLUDED):
            continue
        path = root / name
        if not path.exists() and not path.is_symlink():
            files[name] = {"kind": "deleted"}
            continue
        info = path.lstat()
        content = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
        files[name] = {
            "kind": "symlink" if path.is_symlink() else "file",
            "mode": stat.S_IMODE(info.st_mode),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    return {
        "commit": git(root, "rev-parse", "HEAD").decode().strip(),
        "tree": git(root, "rev-parse", "HEAD^{tree}").decode().strip(),
        "files": files,
        "dependencies": {
            name: files[name]
            for name in ("uv.lock", "package-lock.json")
            if name in files
        },
        "environment": {
            "platform": platform.system(),
            "python": platform.python_version(),
        },
    }


def verify(candidate, root):
    current = capture(root)
    return all(
        candidate[key] == current[key]
        for key in ("commit", "tree", "files", "dependencies")
    )


def verify_runtime(candidate):
    """Bind the loaded package, including installed copies, to candidate bytes.

    A second checkout matching a supplied manifest cannot vouch for this process.
    Bytecode caches are derived; every other package file must match exactly.
    """
    import sys

    import agent_alfred

    package = Path(agent_alfred.__file__).resolve().parent
    prefix = "src/agent_alfred/"
    expected = {
        name.removeprefix(prefix): value.get("sha256")
        for name, value in candidate["files"].items()
        if name.startswith(prefix)
        and isinstance(value, dict)
        and value.get("kind") == "file"
    }
    actual = {}
    for path in package.rglob("*"):
        relative = path.relative_to(package)
        if "__pycache__" in relative.parts or path.suffix in (".pyc", ".pyo"):
            continue
        if path.is_symlink():
            return False
        if path.is_file():
            actual[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not expected or expected != actual:
        return False
    for name, module in tuple(sys.modules.items()):
        if name == "agent_alfred" or name.startswith("agent_alfred."):
            origin = getattr(module, "__file__", None)
            if origin and not Path(origin).resolve().is_relative_to(package):
                return False
    return True
