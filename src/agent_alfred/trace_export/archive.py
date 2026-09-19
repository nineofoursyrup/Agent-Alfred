"""Generate an inert ZIP and checksums over exported bytes only."""

import hashlib
import io
import json
import re
import zipfile

from agent_alfred.trace_export.errors import ExportError
from agent_alfred.trace_export.json_stream import StringSpan
from agent_alfred.trace_export.sanitize import Sanitizer, StreamingText
from agent_alfred.trace_export.source import LIMIT
from agent_alfred.trace_export.text import protect

README = """Agent-Alfred 运行追踪导出（人工阅读，非备份）
源追踪完整性：{integrity}
模式：{mode}
完整性证据不构成防篡改认证。运行结局、记录状态、源缺失与净化移除分别表达。
本文件包含整个运行的可验证追踪范围；净化不表示原文完整保留。
净化原因：{reasons}
缺失：{missing}
分享净化以占位表示自由正文；诊断正文仍可能含私人信息，分享前请自行检查。
相对时间允许负值，事件只按 seq 排序。包内身份别名仅在本包有效。
manifest 文件清单的大小和 SHA-256 对应导出后的字节，不覆盖 manifest 自身。
纯文本请用文本编辑器阅读，不执行其中脚本或链接；离线无需网络或服务。
导出不会进入模型、检索或提炼。用户另存副本及残片不能由服务撤回。
源写侧阀门、自动保留、账单导出与完整发布门禁不属于本功能。
"""


class OutputFile(io.FileIO):
    """ZIP writes either accept every encoded byte or fail the task."""

    def write(self, raw):
        count = super().write(raw)
        if count != len(raw):
            raise OSError("short archive write")
        return count


def encode(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode()


def encoded_chunks(value):
    if isinstance(value, StreamingText):
        yield b'"'
        for raw in value.chunks():
            yield json.dumps(raw.decode(), ensure_ascii=False)[1:-1].encode()
        yield b'"'
    elif isinstance(value, dict):
        yield b"{"
        for index, (key, item) in enumerate(value.items()):
            if index:
                yield b","
            yield encode(key).rstrip(b"\n") + b":"
            yield from encoded_chunks(item)
        yield b"}"
    elif isinstance(value, list):
        yield b"["
        for index, item in enumerate(value):
            if index:
                yield b","
            yield from encoded_chunks(item)
        yield b"]"
    else:
        yield encode(value).rstrip(b"\n")


def encoded_events(source, sanitizer, references):
    for event in source.events():
        yield from encoded_chunks(sanitizer.event(event, references))
        yield b"\n"


def build(source, target, mode, redactor, root, facts, check):
    sanitizer = Sanitizer(mode, redactor, root, source.meta)
    references = {}
    last = first = None
    starts = finishes = 0
    for event in source.events():
        check()
        sanitizer.learn(event)
        starts += event.get("payload_name") == "run.started"
        finishes += event.get("payload_name") == "run.finished"
        if starts > 1 or finishes > 1:
            raise ExportError("corrupt_trace")
        first = first or event.get("payload_name")
        last = event.get("payload_name")
        payload = event.get("payload", {})
        audit = payload.get("audit_content")
        if isinstance(audit, dict):
            expected = f"artifacts/tool-{event['seq']}.txt"
            if (
                payload.get("name") != "tool.finished"
                or set(audit) != {"artifact", "bytes"}
                or audit.get("artifact") != expected
                or not re.fullmatch(r"artifacts/tool-[1-9][0-9]*\.txt", expected)
                or type(audit.get("bytes")) is not int
                or audit["bytes"] < 0
            ):
                raise ExportError("unsafe_source")
            if audit["bytes"] != payload.get("original_bytes"):
                raise ExportError("corrupt_trace")
            references[expected] = (audit["bytes"], payload.get("content_digest"))
        elif payload.get("name") == "tool.finished":
            if not isinstance(audit, (str, StringSpan)):
                raise ExportError("corrupt_trace")
            chunks = (
                audit.chunks() if isinstance(audit, StringSpan) else [audit.encode()]
            )
            digest, size = hashlib.sha256(), 0
            for raw in chunks:
                check()
                digest.update(raw)
                size += len(raw)
            if size != payload.get(
                "original_bytes"
            ) or digest.hexdigest() != payload.get("content_digest"):
                raise ExportError("corrupt_trace")
    missing = []
    if source.truncated:
        missing.append("truncated_tail")
    if first is None:
        missing.append("empty_trace")
    elif first != "run.started" or last != "run.finished":
        missing.append("lifecycle_events_missing")
    if facts["trace_incomplete"] or facts["recording_state"] == "failed":
        missing.append("recording_or_barrier_failed")
    files = {}
    exported_artifacts = {}
    total = 0
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:

        def member(name, chunks, *, directory=False, inventory=True):
            nonlocal total
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = (0o40700 if directory else 0o100600) << 16
            digest = hashlib.sha256()
            size = 0
            with archive.open(info, "w", force_zip64=True) as output:
                for chunk in chunks:
                    check()
                    size += len(chunk)
                    total += len(chunk)
                    if total > LIMIT:
                        raise ExportError("output_limit")
                    output.write(chunk)
                    digest.update(chunk)
            value = {"bytes": size, "sha256": digest.hexdigest()}
            if inventory:
                files[name] = value
            return value

        member("bundle/artifacts/", [], directory=True)
        for index, (original, (declared, checksum)) in enumerate(references.items(), 1):
            check()
            name = f"bundle/artifacts/artifact-{index}.txt"
            lease = source.artifacts.get(original)
            if lease is None:
                missing.append(f"artifact-{index}_missing")
                exported_artifacts[original] = {
                    "missing": True,
                    "id": f"artifact-{index}",
                }
                continue
            if lease.stat().st_size != declared:
                raise ExportError("corrupt_trace")

            def verified_chunks():
                digest = hashlib.sha256()
                for raw in source.chunks(lease):
                    digest.update(raw)
                    yield raw
                if digest.hexdigest() != checksum:
                    raise ExportError("corrupt_trace")

            info = member(name, protect(verified_chunks(), sanitizer))
            exported_artifacts[original] = {
                "artifact": name.removeprefix("bundle/"),
                **info,
            }
        if source.artifacts.keys() - references.keys():
            sanitizer.reasons.add("unreferenced_artifacts_excluded")
        meta = {
            "export_schema": 1,
            "source_format": "legacy-unversioned",
            "run_id": sanitizer.alias("run_id", source.run_id),
            "process_instance_id": sanitizer.alias(
                "process_instance_id", source.meta["process_instance_id"]
            ),
        }
        if mode == "diagnostic":
            meta["created_at"] = source.meta["created_at"]
        member("bundle/meta.json", [encode(meta)])
        member(
            "bundle/trace.jsonl", encoded_events(source, sanitizer, exported_artifacts)
        )
        integrity = (
            "known_incomplete"
            if missing
            else "verified_complete"
            if facts["recording_state"] == "recorded"
            and facts["trace_incomplete"] is False
            else "unknown"
        )
        manifest = {
            "export_schema": 1,
            "sanitization_version": 1,
            "source_layout": "legacy-unversioned",
            "source_events": "legacy-unversioned",
            "mode": mode,
            "scope": "whole_run",
            "source_integrity": integrity,
            "observations": facts,
            "missing": missing,
            "sanitization": sorted(sanitizer.reasons),
            "files": files,
        }
        member(
            "README.txt",
            [
                README.format(
                    integrity=integrity,
                    mode=mode,
                    missing=", ".join(missing) or "未发现已知缺失",
                    reasons=", ".join(sanitizer.reasons),
                ).encode()
            ],
        )
        member("manifest.json", [encode(manifest)], inventory=False)
    if target.tell() > LIMIT:
        raise ExportError("zip_limit")
    source.verify()
    return {k: manifest[k] for k in ("source_integrity", "missing", "mode")}
