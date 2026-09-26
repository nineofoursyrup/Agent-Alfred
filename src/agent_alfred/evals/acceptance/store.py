"""Immutable batch packages; completion marker precedes the latest pointer."""

import hashlib
import json
import os
import shutil
import uuid
from copy import deepcopy
from pathlib import Path

from .safety import ensure_safe
from .schema import digest, encode, identifier, scoring_rubric, validate


class EvidenceStore:
    def __init__(self, root):
        self.root = Path(os.path.abspath(root))

    def import_batch(self, batch):
        ensure_safe(batch)
        validate(batch)
        self._validate_links(batch, ())
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / identifier(batch["batch_id"])
        try:
            path.mkdir()
        except FileExistsError:
            raise ValueError("batch_exists") from None
        payload = encode(batch)
        self._write(path / "batch.json", payload)
        self._write(
            path / "complete.json",
            encode(
                {
                    "batch.json": hashlib.sha256(payload).hexdigest(),
                }
            ),
        )
        self.read(batch["batch_id"])
        pending = self.root / (".latest-" + uuid.uuid4().hex)
        self._write(pending, encode({"batch_id": batch["batch_id"]}))
        os.replace(pending, self.root / "latest.json")

    @staticmethod
    def _write(path, data):
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    def read(self, batch_id, *, _seen=()):
        # Memoize only fully validated packages within this one traversal. Every
        # new public read must recheck disk, including deletion and corruption.
        return self._read(batch_id, _seen, {})

    def _read(self, batch_id, _seen, verified):
        if batch_id in _seen:
            raise ValueError("cyclic_reference")
        if batch_id in verified:
            return verified[batch_id]
        _seen = (*_seen, batch_id)
        path = self.root / identifier(batch_id)
        if (path / "deleted.json").exists():
            raise ValueError("deleted_batch")
        if any(
            p.is_symlink()
            for p in (self.root, path, path / "batch.json", path / "complete.json")
        ):
            raise ValueError("unsafe_evidence_path")
        try:
            payload = (path / "batch.json").read_bytes()
            complete = json.loads((path / "complete.json").read_bytes())
        except OSError, ValueError:
            raise ValueError("missing_or_incomplete_batch") from None
        if complete != {"batch.json": hashlib.sha256(payload).hexdigest()}:
            raise ValueError("integrity_mismatch")
        batch = validate(json.loads(payload))
        ensure_safe(batch)
        if batch["batch_id"] != batch_id:
            raise ValueError("batch_identity_mismatch")
        self._validate_links(batch, _seen, verified=verified)
        verified[batch_id] = batch
        return batch

    def authorization_status(self, batch_id):
        """Read facts unchanged and separately expose current evidence eligibility."""
        from .authorization_history import assess

        return assess(self.read(batch_id), store=self)

    def delete(self, batch_id):
        path = self.root / identifier(batch_id)
        if path.is_symlink():
            raise ValueError("unsafe_evidence_path")
        path.mkdir(parents=True, exist_ok=True)
        marker = path / "deleted.json"
        if not marker.exists():
            self._write(marker, encode({"batch_id": batch_id, "deleted": True}))
        # The marker is durable before removal. Retrying deletion finishes cleanup.
        for operation in ("product", "judge"):
            journal = self.root / (".journal-" + operation + "-" + identifier(batch_id))
            if journal.exists():
                shutil.rmtree(journal)
        for name in ("batch.json", "complete.json"):
            (path / name).unlink(missing_ok=True)

    def revise(
        self,
        batch_id,
        new_id,
        *,
        grades=None,
        adjudications=(),
        gates=(),
        execution=None,
        configuration=None,
        reviews=(),
        review_adjudications=(),
    ):
        original = self.read(batch_id)
        revised = deepcopy(original)
        revised["batch_id"] = identifier(new_id)
        revised["parent"] = {
            "batch_id": batch_id,
            "sha256": digest(original),
            "relation": "regrade",
        }
        if configuration is not None:
            allowed = {
                "rubric",
                "calibration",
                "judge_profile",
                "cases",
                "case_set_approval",
                "calibration_approval",
            }
            if original["schema_version"] == 3:
                allowed |= {"semantic_rubric", "aggregation_policy", "review_policy"}
            if set(configuration) - allowed:
                raise ValueError("invalid_regrade_configuration")
            aggregation_only = set(configuration) == {"aggregation_policy"}
            revised.update(deepcopy(configuration))
            if aggregation_only:
                if original["phase"] == "formal" and original["results"]:
                    raise ValueError("aggregation_after_formal_sampling")
                if grades is not None or adjudications or review_adjudications:
                    raise ValueError("aggregation_changed_scores")
                revised["authorization"] = None
            else:
                revised["grades"] = []
                revised["adjudications"] = []
                revised["request_history"] = deepcopy(
                    original.get("request_history", []) + original.get("requests", [])
                )
                revised["requests"] = []
                revised.pop("budget_started_at", None)
                revised["budget_scope"] = new_id
                revised["authorization"] = None
        if grades is not None:
            revised["grades"] = grades
        revised["adjudications"].extend(adjudications)
        if reviews:
            revised.setdefault("review_disputes", []).extend(deepcopy(reviews))
        if review_adjudications:
            revised.setdefault("review_adjudications", []).extend(
                deepcopy(review_adjudications)
            )
        revised["gates"].extend(gates)
        if execution is not None:
            for key in (
                "authorization",
                "requests",
                "budget_started_at",
                "stop_reason",
                "budget_scope",
                "request_history",
            ):
                if key in execution:
                    revised[key] = deepcopy(execution[key])
            previous = original.get("requests", [])
            if revised.get("requests", [])[: len(previous)] != previous:
                raise ValueError("request_history_replaced")
        self.import_batch(revised)
        return revised

    def _validate_links(self, batch, seen, *, verified=None):
        verified = {} if verified is None else verified

        def read_link(identity):
            return self._read(identity, seen, verified)

        parent = None
        if batch["parent"]:
            parent = read_link(batch["parent"]["batch_id"])
            if digest(parent) != batch["parent"]["sha256"]:
                raise ValueError("parent_identity_mismatch")
            if batch["parent"]["relation"] == "regrade":
                prior_history = parent.get("request_history", [])
                previous = parent.get("requests", [])
                new_scope = batch.get("budget_scope") == batch[
                    "batch_id"
                ] and batch.get("budget_scope") != parent.get("budget_scope")
                if new_scope:
                    if batch.get("request_history", []) != prior_history + previous:
                        raise ValueError("request_history_replaced")
                    auth = batch.get("authorization")
                    if auth and auth["binding"]["batch_id"] != batch["batch_id"]:
                        raise ValueError("authorization_mismatch")
                elif (
                    batch.get("request_history", []) != prior_history
                    or batch.get("requests", [])[: len(previous)] != previous
                ):
                    raise ValueError("request_history_replaced")
                elif (
                    parent.get("budget_started_at")
                    and batch.get("budget_started_at") != parent["budget_started_at"]
                ):
                    raise ValueError("budget_time_replaced")
            if batch["parent"]["relation"] == "regrade" and (
                batch["results"] != parent["results"]
                or batch["candidate"] != parent["candidate"]
                or batch["profiles"] != parent["profiles"]
                or [
                    {
                        k: v
                        for k, v in c.items()
                        if k not in ("gold", "material_id", "source")
                    }
                    for c in batch["cases"]
                ]
                != [
                    {
                        k: v
                        for k, v in c.items()
                        if k not in ("gold", "material_id", "source")
                    }
                    for c in parent["cases"]
                ]
            ):
                raise ValueError("regrade_changed_product_samples")
        if parent and batch["schema_version"] == 3:
            if batch["schema_version"] != parent["schema_version"]:
                raise ValueError("regrade_schema_changed")
            changed_semantics = (
                scoring_rubric(batch) != scoring_rubric(parent)
                or batch["review_policy"] != parent["review_policy"]
            )
            if changed_semantics and batch["grades"]:
                if any(g in parent["grades"] for g in batch["grades"]):
                    raise ValueError("semantic_change_requires_regrade")
            if batch.get("aggregation_policy") != parent.get("aggregation_policy"):
                if parent["phase"] == "formal" and parent["results"]:
                    raise ValueError("aggregation_after_formal_sampling")
                if not changed_semantics and batch["grades"] != parent["grades"]:
                    raise ValueError("aggregation_changed_scores")
        inherited_start = None
        if batch["schema_version"] == 3:
            from .report import instant

            ancestor_ref = batch["parent"]
            while ancestor_ref and ancestor_ref["relation"] == "regrade":
                ancestor = read_link(ancestor_ref["batch_id"])
                if ancestor.get("budget_started_at") is not None:
                    start = instant(ancestor["budget_started_at"])
                    inherited_start = (
                        start
                        if inherited_start is None
                        else min(inherited_start, start)
                    )
                ancestor_ref = ancestor["parent"]
            if inherited_start is not None:
                inherited_start = inherited_start.isoformat()
            if batch["phase"] == "calibration":
                from .budget import validate_material_approval_timing

                validate_material_approval_timing(batch, started_at=inherited_start)
        from .reviews import validate_links

        validate_links(batch, parent, read_link)
        calibration = batch["calibration"]
        if calibration and "batch_id" in calibration:
            source = read_link(calibration["batch_id"])
            from .authorization_history import assess as authorization_status

            if authorization_status(source, store=self)["validity"] == "INVALID":
                raise ValueError("execution_authorization_invalid")
            if digest(source) != calibration["sha256"]:
                raise ValueError("calibration_identity_mismatch")
            if source["candidate"] != batch["candidate"]:
                raise ValueError("calibration_candidate_changed")
            if source["phase"] != "calibration":
                raise ValueError("invalid_calibration_phase")
            if source["simulation"] != batch["simulation"]:
                raise ValueError("calibration_source_mismatch")
            if (
                source["schema_version"] != batch["schema_version"]
                or scoring_rubric(source) != scoring_rubric(batch)
                or source.get("review_policy") != batch.get("review_policy")
            ):
                raise ValueError("rubric_requires_recalibration")
            if source.get("judge_profile") != batch.get("judge_profile"):
                raise ValueError("judge_requires_recalibration")
            if {c["input"].strip() for c in source["cases"]} & {
                c["input"].strip() for c in batch["cases"]
            }:
                raise ValueError("calibration_overlap")
            if source["profiles"] != batch["profiles"]:
                raise ValueError("judge_requires_recalibration")
            from .review_policy import approval_valid
            from .schema import calibration_identity

            proof = source.get("calibration_approval")
            results = {r["id"]: r for r in source["results"]}
            if (
                not proof
                or proof["evidence_sha256"] != calibration_identity(source)
                or not approval_valid(proof, source)
                or len(results) != len(source["cases"])
                or len(source["grades"]) != len(results)
                or any(
                    g["result_hash"] != digest(results[g["result_id"]])
                    or g["rubric_id"] != scoring_rubric(source)["id"]
                    for g in source["grades"]
                )
            ):
                raise ValueError("calibration_incomplete")
            from .report import instant
            from .reviews import assess

            if assess(source, instant(proof["at"]))["blockers"]:
                raise ValueError("calibration_adjudication_required")

            rulings = {a["grade_id"]: a for a in source["adjudications"]}
            cases = {c["id"]: c for c in source["cases"]}
            for grade in source["grades"]:
                ruling = rulings.get(grade["id"])
                effective = grade
                if ruling is not None:
                    if ruling["rubric_id"] != scoring_rubric(source)["id"] or instant(
                        ruling["at"]
                    ) > instant(proof["at"]):
                        raise ValueError("calibration_adjudication_required")
                    effective = ruling
                elif grade["disputed"] or grade["suspected_safety"]:
                    raise ValueError("calibration_adjudication_required")
                elif grade["status"] != "scored":
                    raise ValueError("calibration_incomplete")
                case = cases[results[grade["result_id"]]["case_id"]]
                if (
                    set(effective["dimensions"]) != set(case["applicability"])
                    or set(effective["prohibitions"]) != set(case["forbidden"])
                    or any(
                        effective["dimensions"][key]["status"]
                        not in (("pass", "fail") if applies else ("na",))
                        for key, applies in case["applicability"].items()
                    )
                    or any(
                        value["status"] not in ("pass", "fail")
                        for value in effective["prohibitions"].values()
                    )
                ):
                    raise ValueError("calibration_incomplete")
            if not source["simulation"]:
                from .calibration import validate_source_facts

                validate_source_facts(source, self)
            if batch["schema_version"] == 3:
                source_families = {c["source_family_id"] for c in source["cases"]}
                target_families = {c["source_family_id"] for c in batch["cases"]}
                if source_families & target_families:
                    raise ValueError("calibration_family_overlap")
                if not source_families <= set(batch["seen_families"]):
                    raise ValueError("calibration_families_not_registered")
                aggregation = batch.get("aggregation_policy")
                if aggregation and (
                    aggregation["calibration_evidence_sha256"]
                    != calibration_identity(source)
                ):
                    raise ValueError("aggregation_calibration_mismatch")
                from .budget import validate_formal_approval_timing

                validate_formal_approval_timing(
                    batch, source, started_at=inherited_start
                )
            elif any(
                r.get("kind") == "agent_review_adjudication"
                for r in source.get("review_adjudications", [])
            ):
                raise ValueError("agent_policy_requires_recalibration")
            if set(calibration["material_ids"]) != {
                c["material_id"] for c in source["cases"]
            }:
                raise ValueError("calibration_material_mismatch")

        from .review_policy import validate_role_independence

        # The memo contains the entire verified parent/calibration closure for
        # this public read/import, including actors absent from the latest copy.
        validate_role_independence(batch, *verified.values())

    def reserve_execution(self, batch_id, operation, *, output_batch=None):
        """Consume one explicit execution identity before constructing clients.

        Failed/interrupted reservations remain consumed. Recovery needs a new batch.
        This is scoped to the explicitly selected evidence store, not production state.
        """
        identifier(batch_id)
        if operation not in ("product", "judge"):
            raise ValueError("invalid_execution_operation")
        if output_batch and (self.root / identifier(output_batch)).exists():
            raise ValueError("batch_exists")
        if operation == "product" and (self.root / batch_id).exists():
            raise ValueError("batch_exists")
        self.root.mkdir(parents=True, exist_ok=True)
        claim = self.root / (".execution-" + operation + "-" + batch_id)
        try:
            self._write(claim, encode({"batch_id": batch_id, "operation": operation}))
        except FileExistsError:
            raise ValueError("batch_already_executed") from None

        return ExecutionJournal(self.root, operation + "-" + batch_id)

    def publish_report(self, batch_id, *, candidate_root=None, now=None):
        from .report import report

        batch = self.read(batch_id)
        summary = report(batch, candidate_root=candidate_root, now=now, store=self)
        report_id = uuid.uuid4().hex
        reports = self.root / "reports"
        reports.mkdir(exist_ok=True)
        document = {
            "report_id": report_id,
            "batch_id": batch_id,
            "batch_sha256": digest(batch),
            "summary": summary,
        }
        self._write(reports / (report_id + ".json"), encode(document))
        pending = self.root / (".report-latest-" + report_id)
        self._write(pending, encode({"report_id": report_id, "batch_id": batch_id}))
        os.replace(pending, self.root / "latest-report.json")
        return summary

    def recover(self, batch_id, operation, *, new_batch=None):
        identifier(batch_id)
        if operation not in ("product", "judge"):
            raise ValueError("invalid_execution_operation")
        journal = self.root / (".journal-" + operation + "-" + batch_id)
        if journal.is_symlink():
            raise ValueError("unsafe_evidence_path")
        events, previous = [], None
        for index, path in enumerate(sorted(journal.glob("*.json"))):
            envelope = json.loads(path.read_bytes())
            if (
                path.name != f"{index:08d}.json"
                or envelope["previous"] != previous
                or envelope["sha256"] != digest(envelope["payload"])
            ):
                raise ValueError("execution_journal_corrupt")
            events.append(envelope["payload"])
            previous = digest(envelope)
        if not events or events[0]["event"] != "manifest":
            raise ValueError("execution_manifest_missing")
        batch = events[0]["value"]
        for event in events[1:]:
            if event["event"] == "budget":
                batch.update(event["value"])
            elif event["event"] == "product_result":
                batch["results"].append(event["value"])
            elif event["event"] == "grade":
                batch["grades"].append(event["value"])
        batch["stop_reason"] = "recovered_interrupted_execution"
        if operation == "judge":
            return self.revise(
                batch_id, new_batch, grades=batch["grades"], execution=batch
            )
        path = self.root / identifier(batch_id)
        if path.exists() and not (path / "complete.json").exists():
            if path.is_symlink() or (path / "batch.json").is_symlink():
                raise ValueError("unsafe_evidence_path")
            if (path / "batch.json").exists():
                recorded = json.loads((path / "batch.json").read_bytes())

                def compare(value):
                    return {k: v for k, v in value.items() if k != "stop_reason"}

                if compare(recorded) != compare(batch):
                    raise ValueError("partial_package_journal_mismatch")
                batch = recorded
            else:
                self._write(path / "batch.json", encode(batch))
            validate(batch)
            self._validate_links(batch, ())
            self._write(path / "complete.json", encode({"batch.json": digest(batch)}))
            return self.read(batch_id)
        self.import_batch(batch)
        return batch


class ExecutionJournal:
    """Append-only fsynced events survive failure to publish the final package."""

    def __init__(self, root, identity):
        self.path = root / (".journal-" + identity)
        self.path.mkdir()
        self.sequence = 0
        self.previous = None

    def __call__(self, event, value):
        ensure_safe(value)
        payload = {"event": event, "value": value}
        envelope = {
            "previous": self.previous,
            "sha256": digest(payload),
            "payload": payload,
        }
        EvidenceStore._write(self.path / f"{self.sequence:08d}.json", encode(envelope))
        self.previous = digest(envelope)
        self.sequence += 1
