"""Strict bounded MCP launch configuration; never expose expanded environment."""

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from agent_alfred.atomic_config import read_bytes
from agent_alfred.mcp.transport import decode

INHERITED = (
    "HOME",
    "PATH",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LOGNAME",
    "USER",
    "SHELL",
    "TERM",
)


@dataclass(frozen=True)
class Definition:
    key: str
    command: str
    args: tuple[str, ...]
    cwd: str
    env: dict[str, str]
    references: tuple[str, ...]
    enabled: bool
    timeout_s: int
    raw: dict

    def source(self):
        return [self.key, self.command, self.args, self.cwd, self.env]


def resolve(key, raw, env, directory, redactor):
    if not isinstance(key, str) or not key or len(key.encode()) > 1024:
        raise ValueError("server_key_invalid")
    if not isinstance(raw, dict) or set(raw) - {
        "command",
        "args",
        "env",
        "cwd",
        "enabled",
        "timeout_s",
    }:
        raise ValueError("server_fields_invalid")
    enabled, timeout = raw.get("enabled", False), raw.get("timeout_s", 60)
    if type(enabled) is not bool or type(timeout) is not int or not 1 <= timeout <= 120:
        raise ValueError("server_options_invalid")
    command = raw.get("command")
    args = raw.get("args", [])
    if not isinstance(command, str) or not command or "\0" in command:
        raise ValueError("command_invalid")
    if not isinstance(args, list) or any(
        not isinstance(a, str) or "\0" in a for a in args
    ):
        raise ValueError("args_invalid")
    cwd = raw.get("cwd", str(directory))
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or not Path(cwd).is_dir():
        raise ValueError("cwd_invalid")
    values = {k: env[k] for k in INHERITED if k in env}
    explicit = raw.get("env", {})
    if not isinstance(explicit, dict):
        raise ValueError("env_invalid")
    refs = []
    for name, reference in explicit.items():
        if (
            not isinstance(name, str)
            or re.fullmatch("[A-Za-z_][A-Za-z0-9_]*", name) is None
        ):
            raise ValueError("env_name_invalid")
        match = (
            re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", reference)
            if isinstance(reference, str)
            else None
        )
        if match is None or match[1] not in env:
            raise ValueError("env_reference_invalid")
        value = env[match[1]]
        redactor.remember(value, credential=True)
        if not isinstance(value, str) or "\0" in value:
            raise ValueError("env_value_invalid")
        refs.append(match[1])
        values[name] = value
    if not os.path.isabs(command):
        if "/" in command:
            raise ValueError("command_absolute_or_path_required")
        command = shutil.which(command, path=values.get("PATH", ""))
    if not command or not Path(command).is_file() or not os.access(command, os.X_OK):
        raise ValueError("command_unresolvable")
    # Keep the selected executable entry: dereferencing a venv Python symlink
    # before exec changes sys.prefix and silently selects another environment.
    command = os.path.abspath(command)
    return Definition(
        key,
        command,
        tuple(args),
        str(Path(cwd).resolve()),
        values,
        tuple(sorted(refs)),
        enabled,
        timeout,
        dict(raw),
    )


def read(directory, env, redactor):
    if directory is None:
        return {}, None
    raw, fingerprint = read_bytes(directory / "mcp.json")
    if raw is None:
        return {}, fingerprint
    if len(raw) > 256 * 1024:
        raise ValueError("configuration_limit")
    config = decode(raw)
    if (
        not isinstance(config, dict)
        or set(config) != {"mcpServers"}
        or not isinstance(config["mcpServers"], dict)
    ):
        raise ValueError("configuration_root_invalid")
    if len(config["mcpServers"]) > 8:
        raise ValueError("server_limit")
    return {
        key: resolve(key, definition, env, directory, redactor)
        for key, definition in sorted(config["mcpServers"].items())
    }, fingerprint
