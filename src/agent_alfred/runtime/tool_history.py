"""Ephemeral, verified manual history reads. No searchable or reusable body copy."""

import base64
import hashlib
import hmac
import json
import os
import secrets
from contextlib import contextmanager
from datetime import datetime
from pathlib import PurePath

from agent_alfred.managed_state import ManagedPathSecurityError, ManagedStateDirectory
from agent_alfred.resource_rollback import ResumableRollback, RollbackSlot

SEGMENT_BYTES = 256 * 1024


class HistoryError(ValueError):
    pass


class ToolHistory:
    def __init__(self, store, redactor):
        self.store, self.redactor = store, redactor
        self.key = secrets.token_bytes(32)
        self._cleanup = ResumableRollback()
        self._nested_cleanup = RollbackSlot()

    def close(self):
        return (
            self._nested_cleanup.retry_propagating()
            and self._cleanup.retry_propagating()
        )

    @contextmanager
    def resources(self):
        self._nested_cleanup.close()
        self._cleanup.close()
        try:
            yield self._cleanup
        except BaseException as exc:
            self._nested_cleanup.capture_failure(exc)
            raise
        finally:
            self._nested_cleanup.close()
            self._cleanup.close()

    @staticmethod
    def lines(fd, size):
        position, pending = 0, b""
        while position < size:
            chunk = os.pread(fd, SEGMENT_BYTES, position)
            if not chunk:
                raise HistoryError("trace_short_read")
            position += len(chunk)
            parts = (pending + chunk).split(b"\n")
            pending = parts.pop()
            for line in parts:
                yield line + b"\n"
        if pending:
            raise HistoryError("trace_incomplete")

    def _cursor(self, value):
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(
            hmac.digest(self.key, raw, "sha256") + raw
        ).decode()

    def _decode(self, cursor):
        try:
            data = base64.b64decode(cursor, altchars=b"-_", validate=True)
            if not hmac.compare_digest(
                data[:32], hmac.digest(self.key, data[32:], "sha256")
            ):
                raise ValueError()
            return json.loads(data[32:])
        except ValueError, TypeError:
            raise HistoryError("invalid_history_cursor") from None

    def read(self, root, run_id, step_index, call_id, projection="model", cursor=None):
        if (
            projection not in ("model", "audit", "parameters")
            or type(step_index) is not int
            or step_index < 0
        ):
            raise HistoryError("invalid_history_request")
        target = [run_id, step_index, call_id, projection]
        old = self._decode(cursor) if cursor else None
        if old and old["target"] != target:
            raise HistoryError("invalid_history_cursor")
        # Reading owns the store lock until all descriptors have closed. Prune
        # facts use this same owner; file replacement is additionally checked
        # by every open capability before and after reading.
        with self.store.reading() as conn:
            run = conn.execute(
                "SELECT phase FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise HistoryError("unknown_run")
            if conn.execute(
                "SELECT 1 FROM trace_prunes WHERE run_id=?", (run_id,)
            ).fetchone():
                raise HistoryError("trace_pruned")
            if run[0] != "finished":
                raise HistoryError("history_not_finalized")
            request = conn.execute(
                "SELECT tool_name FROM tool_metering "
                "WHERE run_id=? AND step_index=? AND call_id=?",
                (run_id, step_index, call_id),
            ).fetchone()
            if request is None:
                raise HistoryError("unknown_tool_request")
            try:
                return self._read(root, target, old, request[0])
            except HistoryError:
                raise
            except ManagedPathSecurityError:
                raise HistoryError("history_path_refused") from None
            except OSError, ValueError, KeyError, TypeError, AttributeError:
                raise HistoryError("history_unreadable") from None

    def _read(self, root, target, old, expected_tool):
        run_id, step_index, call_id, projection = target
        digest = hashlib.sha256(run_id.encode()).hexdigest()[:32]
        candidates = list(root.glob(f"????-??-??/??????Z-{digest}"))
        if len(candidates) != 1:
            raise HistoryError("trace_missing")
        path = candidates[0]
        with self.resources() as resources:
            leases = []

            def own(lease):
                leases.append(lease)
                return lease

            base = own(ManagedStateDirectory.acquire(root, _rollback=resources))
            day = own(
                base.open_directory(PurePath(path.parent.name), _rollback=resources)
            )
            bundle = own(day.open_directory(PurePath(path.name), _rollback=resources))
            meta = own(
                bundle.open_regular(
                    PurePath("meta.json"),
                    access="read",
                    create=False,
                    _rollback=resources,
                )
            )
            meta_before = meta.stat()
            info = json.loads(os.pread(meta.fd, 8193, 0))
            created = datetime.fromisoformat(info["created_at"].replace("Z", "+00:00"))
            if (
                info["run_id"] != run_id
                or info["run_storage_id"] != digest
                or info["run_dir_name"] != path.name
                or created.strftime("%Y-%m-%d") != path.parent.name
                or created.strftime("%H%M%S") + "Z-" + digest != path.name
            ):
                raise HistoryError("bundle_identity_mismatch")
            trace = own(
                bundle.open_regular(
                    PurePath("trace.jsonl"),
                    access="read",
                    create=False,
                    _rollback=resources,
                )
            )
            trace_before = trace.stat()
            if trace_before.st_size > 64 * 1024 * 1024:
                raise HistoryError("history_resource_limit")
            hasher = hashlib.sha256()
            selected, parameters, committed, duplicates = [], [], set(), set()
            previous = 0
            for line in self.lines(trace.fd, trace_before.st_size):
                hasher.update(line)
                if not line.endswith(b"\n"):
                    raise HistoryError("trace_incomplete")
                event = json.loads(line)
                if (
                    event["run_id"] != run_id
                    or event["process_instance_id"] != info["process_instance_id"]
                    or type(event["seq"]) is not int
                    or event["seq"] <= previous
                ):
                    raise HistoryError("event_identity_mismatch")
                previous = event["seq"]
                payload = event["payload"]
                if event["step_index"] != step_index:
                    continue
                if payload.get("name") == "attempt.committed":
                    attempt_id = event.get("attempt_id") or payload.get("attempt_id")
                    if attempt_id in committed:
                        duplicates.add(attempt_id)
                    committed.add(attempt_id)
                    for block in payload.get("blocks", []):
                        if (
                            block.get("type") == "tool_call"
                            and block.get("id") == call_id
                        ):
                            parameters.append((attempt_id, block))
                if (
                    payload.get("name") == "tool.finished"
                    and payload.get("call_id") == call_id
                ):
                    if payload.get("tool_name") != expected_tool:
                        raise HistoryError("projection_identity_mismatch")
                    selected.append((event["seq"], payload))
            if previous == 0:
                raise HistoryError("trace_empty")
            if projection == "parameters":
                if len(parameters) != 1 or duplicates or len(committed) != 1:
                    raise HistoryError("parameters_unrecorded_or_ambiguous")
                block = parameters[0][1]
                if block.get("name") != expected_tool:
                    raise HistoryError("parameters_identity_mismatch")
                if len(selected) == 1 and block.get("name") != selected[0][1].get(
                    "tool_name"
                ):
                    raise HistoryError("parameters_identity_mismatch")
                content = json.dumps(
                    self.redactor.redact_jsonable(block["input"]),
                    ensure_ascii=False,
                    indent=2,
                )
            else:
                if len(selected) != 1:
                    raise HistoryError("projection_unrecorded_or_ambiguous")
                seq, payload = selected[0]
                content = payload[
                    "model_content" if projection == "model" else "audit_content"
                ]
            artifact_identity = None
            offset = old["offset"] if old else 0
            if isinstance(content, dict):
                if (
                    projection != "audit"
                    or content.get("artifact") != f"artifacts/tool-{seq}.txt"
                ):
                    raise HistoryError("artifact_identity_mismatch")
                directory = own(
                    bundle.open_directory(PurePath("artifacts"), _rollback=resources)
                )
                try:
                    artifact = own(
                        directory.open_regular(
                            PurePath(f"tool-{seq}.txt"),
                            access="read",
                            create=False,
                            _rollback=resources,
                        )
                    )
                except FileNotFoundError:
                    raise HistoryError("artifact_missing") from None
                before = artifact.stat()
                if (
                    before.st_size != content["bytes"]
                    or before.st_size != payload["original_bytes"]
                ):
                    raise HistoryError("artifact_size_changed")
                total = before.st_size
                checksum = hashlib.sha256()
                position = 0
                while position < total:
                    chunk = os.pread(artifact.fd, 256 * 1024, position)
                    if not chunk:
                        raise HistoryError("artifact_short_read")
                    checksum.update(chunk)
                    position += len(chunk)
                if checksum.hexdigest() != payload["content_digest"]:
                    raise HistoryError("artifact_content_changed")
                data = os.pread(artifact.fd, SEGMENT_BYTES, offset)
                after = artifact.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ino,
                ):
                    raise HistoryError("artifact_changed")
                artifact_identity = [
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                ]
            else:
                if not isinstance(content, str):
                    raise HistoryError("projection_unreadable")
                encoded = content.encode("utf-8")
                if projection == "audit" and (
                    len(encoded) != payload["original_bytes"]
                    or hashlib.sha256(encoded).hexdigest() != payload["content_digest"]
                ):
                    raise HistoryError("audit_content_changed")
                total, data = len(encoded), encoded[offset : offset + SEGMENT_BYTES]
            if offset < 0 or offset > total:
                raise HistoryError("invalid_history_cursor")
            identity = [
                trace_before.st_dev,
                trace_before.st_ino,
                trace_before.st_size,
                trace_before.st_mtime_ns,
                hasher.hexdigest(),
                artifact_identity,
            ]
            if old and old["identity"] != identity:
                raise HistoryError("history_changed")
            # A byte ceiling never splits a multibyte codepoint.
            while True:
                try:
                    text = data.decode("utf-8")
                    break
                except UnicodeDecodeError as error:
                    if (
                        offset + len(data) >= total
                        or error.reason != "unexpected end of data"
                        or error.end != len(data)
                        or len(data) - error.start > 3
                    ):
                        raise HistoryError("invalid_utf8") from None
                    data = data[: error.start]
            for lease in leases:
                lease.verify_identity()
            if (meta.stat().st_size, meta.stat().st_mtime_ns) != (
                meta_before.st_size,
                meta_before.st_mtime_ns,
            ):
                raise HistoryError("bundle_metadata_changed")
            after = trace.stat()
            if (after.st_size, after.st_mtime_ns) != (
                trace_before.st_size,
                trace_before.st_mtime_ns,
            ):
                raise HistoryError("trace_changed")
            end = offset + len(data)
            next_cursor = (
                self._cursor({"target": target, "offset": end, "identity": identity})
                if end < total
                else None
            )
            return {
                "run_id": run_id,
                "step_index": step_index,
                "call_id": call_id,
                "projection": projection,
                "text": text,
                "start": offset,
                "end": end,
                "total_bytes": total,
                "next_cursor": next_cursor,
                "revalidate_cursor": self._cursor(
                    {"target": target, "offset": offset, "identity": identity}
                ),
                "delivery": "saved_projection_does_not_prove_delivery",
            }
