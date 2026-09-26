"""Run existing offline gates and bind command logs to captured candidate bytes."""

import hashlib
import json
import os
import platform
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .candidate import verify, verify_runtime
from .safety import ensure_safe
from .schema import digest


def collect_gate(name, candidate, root, output):
    if name == "pytest" and any(
        os.environ.get(k)
        for k in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD")
    ):
        raise ValueError("test_selection_environment")
    root, output = Path(root).resolve(), Path(output).resolve()
    if not verify(candidate, root):
        raise ValueError("candidate_changed")
    if not verify_runtime(candidate):
        raise ValueError("runtime_candidate_mismatch")
    output.mkdir(parents=True, exist_ok=True)
    run = output / uuid.uuid4().hex
    run.mkdir()
    commands = {
        "sync": [
            "uv",
            "sync",
            "--python",
            sys.executable,
            "--extra",
            "dev",
            "--extra",
            "mcp",
            "--locked",
        ],
        "ruff": ["uv", "run", "ruff", "check"],
        "skills": [sys.executable, "scripts/check_skills.py"],
        "env": [sys.executable, "scripts/check_env_example.py"],
        "build": ["uv", "build"],
        "installations": [
            sys.executable,
            "scripts/check_mcp_installations.py",
            "--output",
            str(run / "installations.json"),
        ],
        "typecheck": ["npm", "run", "typecheck"],
        "browser": ["npx", "playwright", "test", "--reporter=json"],
        "pytest": [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "agent_alfred.evals.acceptance.pytest_evidence",
            "--acceptance-log",
            str(run / "pytest.json"),
        ],
    }
    command = commands[name]
    env = {
        k: v
        for k, v in os.environ.items()
        if not any(s in k.lower() for s in ("api_key", "secret", "access_token"))
    }
    if platform.system() == "Darwin":
        env["DEVELOPER_DIR"] = "/Library/Developer/CommandLineTools"
    expected = None
    if name == "pytest":
        collect_command = [
            *command[:-1],
            str(run / "collection.json"),
            "--collect-only",
            "-o",
            "addopts=",
            "-q",
        ]
        preflight = subprocess.run(
            collect_command, cwd=root, env=env, capture_output=True, text=True
        )
        if preflight.returncode != 0:
            raise ValueError("collection_failed")
        plan = json.loads((run / "collection.json").read_text())
        expected = sorted(set(plan["collection"]) - set(plan["requires_key_ids"]))
    if name == "browser":
        listing = subprocess.run(
            [*command, "--list"], cwd=root, env=env, capture_output=True, text=True
        )
        if listing.returncode != 0:
            raise ValueError("collection_failed")
        expected = _browser(json.loads(listing.stdout))[0]
    start = datetime.now(UTC).isoformat()
    completed = subprocess.run(
        command, cwd=root, env=env, capture_output=True, text=True
    )
    log = completed.stdout + "\n" + completed.stderr
    ensure_safe(log)
    (run / "command.log").write_text(log, encoding="utf-8")
    import _sqlite3
    import ctypes
    import sqlite3

    library = ctypes.CDLL(_sqlite3.__file__)
    native = all(
        hasattr(library, name)
        for name in ("sqlite3_limit", "sqlite3_prepare_v3", "sqlite3_step")
    )
    environment = {
        "python_executable": sys.executable,
        "python_build": list(platform.python_build()),
        "sqlite_version": sqlite3.sqlite_version,
        "sqlite_shared_api": native,
        "system": platform.platform(),
    }
    if name in ("typecheck", "browser"):
        environment["node"] = subprocess.check_output(
            ["node", "--version"], env=env, text=True
        ).strip()
    record = {
        "name": name,
        "environment": environment,
        "candidate_id": digest(candidate),
        "platform": "macOS" if platform.system() == "Darwin" else platform.system(),
        "python": platform.python_version(),
        "command": command,
        "started_at": start,
        "finished_at": datetime.now(UTC).isoformat(),
        "exit_code": completed.returncode,
        "log": log,
    }
    if (
        platform.system() == "Linux"
        and platform.freedesktop_os_release().get("ID") == "ubuntu"
    ):
        record["platform"] = "Ubuntu"
    if name == "pytest" and (run / "pytest.json").exists():
        data = json.loads((run / "pytest.json").read_text())
        record.update(
            data,
            expected_ids=expected,
            plan=plan,
            raw_results=data,
            requires_key_exclusion="pyproject.toml: not requires_key",
        )
    if name == "browser" and completed.stdout:
        nodes, outcomes = _browser(json.loads(completed.stdout))
        record.update(
            collection=nodes,
            outcomes=outcomes,
            expected_ids=expected,
            raw_results=json.loads(completed.stdout),
            raw_plan=json.loads(listing.stdout),
        )
    if name == "installations" and (run / "installations.json").exists():
        record["installations"] = json.loads((run / "installations.json").read_text())
    if name in ("build", "installations"):
        record["artifacts"] = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (root / "dist").glob("agent_alfred-*")
        }
    record["source_root"] = str(root)
    record["log_sha256"] = hashlib.sha256(log.encode()).hexdigest()
    if not verify(candidate, root):
        record["candidate_changed_during_gate"] = True
    (run / "gate.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def _browser(data):
    nodes, outcomes = [], {}

    def visit(suite):
        for spec in suite.get("specs", []):
            for test in spec["tests"]:
                identity = spec["id"] + ":" + test.get("projectId", "")
                nodes.append(identity)
                outcomes[identity] = [r["status"] for r in test.get("results", [])]
        for child in suite.get("suites", []):
            visit(child)

    visit(data)
    return nodes, outcomes
