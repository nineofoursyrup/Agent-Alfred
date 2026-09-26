"""The candidate's obligation inventory fixes the coverage denominator."""

import hashlib
import json

from .gates import assess_gate

INVENTORY = "docs/acceptance/coverage-inventory.json"


def assess(batch):
    reference = batch["references"].get("coverage_inventory")
    expected = batch["candidate"]["files"].get(INVENTORY, {})
    if not reference or not isinstance(expected, dict):
        return ["requirement_inventory_missing"]
    if hashlib.sha256(reference["content"].encode()).hexdigest() != expected.get(
        "sha256"
    ):
        return ["requirement_inventory_changed"]
    inventory = json.loads(reference["content"])
    required = {
        r["inventory_id"]: r
        for r in inventory["obligations"]
        if r.get("authority_status", "active")
        in ("active", "active_definition", "active_as_amended")
    }
    supplied = {r["id"]: r for r in batch["coverage"]}
    blockers = ["requirement_source_missing"] if inventory.get("source_gaps") else []
    if (
        not required
        or set(required) != set(supplied)
        or len(supplied) != len(batch["coverage"])
    ):
        blockers.append("requirement_coverage_mismatch")
    gates = {g["platform"] + ":" + g["name"]: g for g in batch["gates"]}
    for identity, source in required.items():
        row = supplied.get(identity)
        if not row:
            continue
        for key in (
            "source",
            "source_version",
            "obligation",
            "public_path",
            "applicability",
            "owner",
        ):
            if row.get(key) != source.get(key):
                blockers.append("requirement_definition_changed:" + identity)
        if source.get("gap") or row["gap"] or not row["evidence"]:
            blockers.append("requirement_evidence_missing:" + identity)
            continue
        selectors = source.get("test_selectors", [])
        observed = set()
        for evidence in row["evidence"]:
            if not isinstance(evidence, dict):
                blockers.append("requirement_evidence_invalid:" + identity)
                continue
            gate = gates.get(evidence.get("gate"))
            if (
                not gate
                or assess_gate(gate, batch["candidate_id"])["verdict"] != "PASS"
            ):
                blockers.append("requirement_gate_invalid:" + identity)
                continue
            nodes = evidence.get("test_ids", [])
            if not nodes or any(node not in gate.get("outcomes", {}) for node in nodes):
                blockers.append("requirement_test_missing:" + identity)
                continue
            observed.update(nodes)
            if gate["name"] == "browser":
                locations = browser_locations(gate.get("raw_results", {}))
                observed.update(locations[node] for node in nodes if node in locations)
        if not selectors or any(
            not any(
                n == s or n.startswith(s + "::") or n.startswith(s + "[")
                for n in observed
            )
            for s in selectors
        ):
            blockers.append("requirement_selection_incomplete:" + identity)
    return blockers


def browser_locations(data):
    locations = {}

    def visit(suite):
        for spec in suite.get("specs", []):
            relative = spec["file"].split("tests/browser/")[-1]
            path = "tests/browser/" + relative
            for test in spec["tests"]:
                locations[spec["id"] + ":" + test.get("projectId", "")] = (
                    path + "::" + spec["title"]
                )
        for child in suite.get("suites", []):
            visit(child)

    visit(data)
    return locations
