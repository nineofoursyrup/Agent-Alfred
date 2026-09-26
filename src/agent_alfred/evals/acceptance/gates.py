"""Mechanical coverage evidence, distinct from test process exit status."""

import hashlib
import json
from pathlib import PurePath

from .report import axis, instant

UBUNTU = (
    "sync",
    "ruff",
    "skills",
    "env",
    "pytest",
    "build",
    "installations",
    "typecheck",
    "browser",
)
REQUIRED = [("Ubuntu", name) for name in UBUNTU] + [("macOS", "installations")]


def assess_gate(gate, candidate_id):
    failures, blockers = [], []
    if gate.get("candidate_changed_during_gate"):
        blockers.append("candidate_changed_during_gate")
    if gate["candidate_id"] != candidate_id:
        blockers.append("wrong_candidate")
    environment = gate.get("environment", {})
    if (
        gate["name"] in ("pytest", "installations")
        and environment.get("sqlite_shared_api") is not True
    ):
        blockers.append("sqlite_shared_api_unverified")
    if gate["name"] in ("typecheck", "browser") and not environment.get(
        "node", ""
    ).startswith("v22."):
        blockers.append("wrong_node")
    if not gate["python"].startswith("3.14."):
        blockers.append("wrong_python")
    if not gate["command"] or not gate["log"]:
        blockers.append("command_log_missing")
    if instant(gate["finished_at"]) < instant(gate["started_at"]):
        blockers.append("invalid_gate_time")
    if gate["exit_code"] != 0:
        failures.append("command_failed")
    if gate.get("log_sha256") != hashlib.sha256(gate["log"].encode()).hexdigest():
        blockers.append("log_integrity_missing")
    command = gate["command"]
    required_tokens = {
        "sync": ("uv", "sync", "--locked", "dev", "mcp"),
        "ruff": ("uv", "run", "ruff", "check"),
        "build": ("uv", "build"),
        "skills": ("scripts/check_skills.py",),
        "env": ("scripts/check_env_example.py",),
        "installations": ("scripts/check_mcp_installations.py",),
        "typecheck": ("npm", "run", "typecheck"),
        "browser": ("npx", "playwright", "test", "--reporter=json"),
        "pytest": (
            "-m",
            "pytest",
            "-p",
            "agent_alfred.evals.acceptance.pytest_evidence",
        ),
    }
    if not isinstance(command, list) or not set(
        required_tokens.get(gate["name"], ())
    ) <= set(command):
        blockers.append("required_command_missing")
    if gate["name"] in ("pytest", "browser"):
        planned = gate.get("expected_ids", [])
        if gate["name"] == "pytest":
            plan = gate.get("plan", {})
            raw = gate.get("raw_results", {})
            allowed = set(plan.get("requires_key_ids", []))
            full = set(plan.get("collection", []))
            if not full or plan.get("exit_code") != 0 or plan.get("deselected"):
                blockers.append("unfiltered_collection_missing")
            if set(planned) != full - allowed or not allowed <= full:
                blockers.append("applicability_plan_mismatch")
            if gate.get("requires_key_exclusion") != "pyproject.toml: not requires_key":
                blockers.append("exclusion_rule_missing")
            if set(raw.get("deselected", [])) != allowed:
                blockers.append("unexpected_deselection")
            if any(
                set(raw.get("phases", {}).get(node, {}))
                != {"setup", "call", "teardown"}
                for node in planned
            ):
                blockers.append("test_execution_phases_missing")
            for node, phases in raw.get("phases", {}).items():
                if "failed" in phases.values():
                    failures.append("test_failed:" + node)
                if any(status != "passed" for status in phases.values()):
                    blockers.append("test_not_passed:" + node)
                observed = [
                    phases[p] for p in ("setup", "call", "teardown") if p in phases
                ]
                if raw.get("outcomes", {}).get(node) != observed:
                    blockers.append("phase_result_mismatch:" + node)
            if raw.get("exit_code") not in (None, 0):
                failures.append("command_failed")
            if any(
                gate.get(k) != raw.get(k)
                for k in ("collection", "outcomes", "exit_code")
            ):
                blockers.append("raw_result_mismatch")
        else:
            from .collect import _browser

            raw_plan = gate.get("raw_plan", {})
            raw_result = gate.get("raw_results", {})
            if _browser(raw_plan)[0] != planned or _browser(raw_result) != (
                gate.get("collection"),
                gate.get("outcomes"),
            ):
                blockers.append("raw_result_mismatch")
            config = raw_result.get("config", {})
            if (
                config.get("grepInvert")
                or config.get("shard")
                or any(p.get("retries") for p in config.get("projects", []))
            ):
                blockers.append("browser_selection_changed")
        if not planned or len(planned) != len(set(planned)):
            blockers.append("collection_plan_missing")
        if set(planned) != set(gate.get("collection", [])):
            blockers.append("collection_mismatch")
        if set(planned) != set(gate.get("outcomes", {})):
            blockers.append("execution_missing")
        for node, outcomes in gate.get("outcomes", {}).items():
            if "failed" in outcomes:
                failures.append("test_failed:" + node)
            if not outcomes or any(s != "passed" for s in outcomes):
                blockers.append("test_not_passed:" + node)
    if gate["name"] == "installations":
        rows = gate.get("installations", [])
        logged = []
        for line in gate["log"].splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and "artifact_sha256" in item:
                logged.append(item)
        if rows != logged:
            blockers.append("installation_raw_result_mismatch")
        combinations = {(r["kind"], r["mode"]) for r in rows}
        if combinations != {
            (k, m) for k in ("wheel", "sdist") for m in ("base", "mcp")
        }:
            blockers.append("installation_combinations_missing")
        if len(rows) != 4:
            blockers.append("installation_combinations_missing")
        for row in rows:
            artifacts = gate.get("artifacts", {})
            artifact_hash = row.get("artifact_sha256", "")
            if (
                len(artifact_hash) != 64
                or artifacts.get(row.get("artifact")) != artifact_hash
            ):
                blockers.append("installation_artifact_mismatch")
            source = PurePath(gate.get("source_root", "/missing-source"))
            prefix = PurePath(row.get("prefix", "/missing-prefix"))
            imported = PurePath(row.get("import_path", "/missing-import"))
            python = PurePath(row.get("python_path", "/missing-python"))
            cwd = PurePath(row.get("cwd", "/missing-cwd"))
            if (
                not imported.is_relative_to(prefix)
                or not python.is_relative_to(prefix)
                or imported.is_relative_to(source)
                or cwd.is_relative_to(source)
                or not row.get("sys_path")
                or any(
                    PurePath(p).is_relative_to(source) for p in row.get("sys_path", [])
                )
            ):
                blockers.append("source_contamination")
            if not row.get("package_files"):
                blockers.append("installation_package_identity_missing")
            if (
                not row.get("artifact_sha256")
                or not row.get("import_path")
                or not row.get("python_path")
                or not row.get("cwd")
            ):
                blockers.append("installation_identity_missing")
            if row.get("source_contamination") is not False:
                blockers.append("source_contamination")
            if any(
                row.get(k) != "PASS"
                for k in (
                    "cli",
                    "files",
                    "database_dashboard",
                    "configured",
                    "unconfigured",
                )
            ):
                blockers.append("installation_path_missing")
    return axis(failures, blockers)


def engineering(batch):
    failures, blockers, items = [], [], []
    for system, name in REQUIRED:
        matches = [
            g for g in batch["gates"] if g["platform"] == system and g["name"] == name
        ]
        label = system + ":" + name
        if len(matches) != 1:
            blockers.append("gate_missing:" + label)
            continue
        gate = matches[0]
        judged = assess_gate(gate, batch["candidate_id"])
        if name == "installations":
            prefix = "src/agent_alfred/"
            expected = {
                name[len(prefix) :]: info.get("sha256")
                for name, info in batch["candidate"]["files"].items()
                if name.startswith(prefix) and info.get("kind") == "file"
            }
            if not expected or any(
                row.get("package_files") != expected
                for row in gate.get("installations", [])
            ):
                judged["blockers"].append("installed_candidate_mismatch")
                judged.update(axis(judged["failures"], judged["blockers"]))
        items.append({"gate": label, **judged})
        failures.extend(label + ":" + s for s in judged["failures"])
        blockers.extend(label + ":" + s for s in judged["blockers"])
    return {**axis(failures, blockers), "gates": items}
