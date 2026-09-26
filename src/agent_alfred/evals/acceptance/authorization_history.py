"""Additive audit projection; never rewrite historical consent or grant new IO.

Bundled incident facts can deny, not grant. A future authoritative registry must
live outside the executor's boundary. Until then real evidence stays audit-only.
"""

import hashlib
import json
from pathlib import Path

from .schema import digest

HISTORY_PATH = Path(__file__).with_name("authorization_history.json")
HISTORY_SHA256 = "a19d6f7c6a8a00bccf4e1fa6e70df653946a0dfda9335e3bd44548d3478a81e3"


def history():
    try:
        raw = HISTORY_PATH.read_bytes()
        if hashlib.sha256(raw).hexdigest() != HISTORY_SHA256:
            raise ValueError("authorization_registry_unverifiable")
        return json.loads(raw)
    except OSError, ValueError:
        raise ValueError("authorization_registry_unverifiable") from None


def assess(batch, *, store=None, _seen=()):
    result = {
        "status": "SOURCE_UNVERIFIABLE_AUDIT_ONLY",
        "validity": "UNVERIFIABLE",
        "new_execution_allowed": False,
        "release_eligible": False,
        "trusted_registry": "NOT AVAILABLE",
        "blockers": ["approval_source_unverifiable"],
    }
    try:
        facts = history()
    except ValueError:
        result["blockers"] = ["authorization_registry_unverifiable"]
        return result
    parent = batch.get("parent") or {}
    requests = batch.get("requests", []) + batch.get("request_history", [])
    invalid = (
        batch["candidate_id"] in facts["candidate_ids"]
        or batch["batch_id"] in facts["batch_ids"]
        or parent.get("batch_id") in facts["batch_ids"]
        or parent.get("sha256") in facts["parent_digests"]
        or digest(batch.get("authorization")) in facts["authorization_digests"]
        or any(r["attempt_id"] in facts["attempt_ids"] for r in requests)
        or any(r.get("run_id") in facts["run_ids"] for r in batch["results"])
    )
    if parent and not invalid and store is not None:
        if batch["batch_id"] in _seen:
            result["blockers"] = ["authorization_ancestry_unverifiable"]
            return result
        ancestor = assess(
            store.read(parent["batch_id"]),
            store=store,
            _seen=(*_seen, batch["batch_id"]),
        )
        invalid = ancestor["validity"] == "INVALID"
    if invalid:
        result.update(
            status="QUARANTINED_AUDIT_ONLY",
            validity="INVALID",
            blockers=["execution_authorization_invalid"],
            incident="c25_unsigned_template",
            actual_bill="unknown",
            retroactive_approval=False,
        )
    elif digest(batch) in facts["historical_valid_c21_packages"]:
        result.update(
            status="HISTORICAL_AUTHORIZATION_RECORDED",
            validity="VALID_HISTORICAL",
            history_note="c21 authorization preserved; no new grant or registry proof",
        )
    elif batch["simulation"] and not parent:
        result.update(status="SIMULATION_ONLY", validity="SYNTHETIC", blockers=[])
    result["seen_case_ids"] = [
        c["id"]
        for c in batch["cases"]
        if c.get("source_family_id") in facts["seen_families"]
        or digest({"input": c["input"], "setup": c["setup"]}) in facts["seen_materials"]
    ]
    if (
        not batch["simulation"]
        and not batch["results"]
        and batch["phase"] in ("calibration", "formal")
        and result["seen_case_ids"]
    ):
        result["blockers"].append("seen_material_reused")
    return result
