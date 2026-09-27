"""Versioned protected material references; integrity never grants model access.

The local vault demonstrates bounded transfer. Its configured allowlist is an
access boundary, not a signature or proof of a separately administered deployment.
Only a separately verified real adapter may supply that deployment evidence.
"""

import hashlib
import json
import math
import os
import stat
from pathlib import Path, PurePosixPath

from agent_alfred.resource_rollback import (
    ConstructionOwner,
    OwnedDescriptor,
    OwnedResource,
)

from .budget import binding
from .schema import digest, encode, hash_value, identifier
from .store import EvidenceStore

CONTRACT = "V1-CALIBRATION-MATERIAL-REFERENCE"
VERSION = 1
MAX_FILES = 2048
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_METADATA_BYTES = 512 * 1024
MANIFEST = "BUNDLE-MANIFEST.json"


def _closed(value, fields):
    if type(value) is not dict or set(value) != set(fields.split()):
        raise ValueError("invalid_material_contract_fields")


def _version(value):
    if (
        type(value.get("version")) is not int
        or value["version"] != VERSION
        or value.get("contract") != CONTRACT
    ):
        raise ValueError("unknown_material_contract_version")


def strict_json(raw, *, limit=MAX_METADATA_BYTES):
    """Reject ambiguous JSON before interpreting any execution metadata."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= limit:
        raise ValueError("material_size_exceeded")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_material_json_key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("invalid_material_json")

    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("invalid_material_json")
        return number

    try:
        return json.loads(
            raw, object_pairs_hook=unique, parse_constant=constant, parse_float=finite
        )
    except UnicodeError, json.JSONDecodeError, RecursionError:
        raise ValueError("invalid_material_json") from None


def _path(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or "\\" in value
        or ":" in value
        or any(ord(c) < 32 for c in value)
        or value == "."
        or str(PurePosixPath(value)) != value
        or PurePosixPath(value).is_absolute()
        or any(p in (".", "..") for p in PurePosixPath(value).parts)
    ):
        raise ValueError("unsafe_material_path")
    return value


def _length(value, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("material_size_exceeded")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _stream(rollback, descriptor, mode):
    def initialize(holder):
        # The stream borrows the pre-owned token, including when fdopen fails
        # or is interrupted before it can publish the empty wrapper.
        holder.publish(os.fdopen(descriptor.fd, mode, closefd=False))

    return OwnedResource.acquire(rollback, initialize)


def _read(root, relative, maximum):
    """Traverse beneath a configured root without following member symlinks."""
    parts = PurePosixPath(_path(relative)).parts
    owner = ConstructionOwner()
    try:
        try:
            descriptor = OwnedDescriptor.open(
                owner.rollback, root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            for part in parts[:-1]:
                descriptor = OwnedDescriptor.open(
                    owner.rollback,
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor.fd,
                )
            descriptor = OwnedDescriptor.open(
                owner.rollback,
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=descriptor.fd,
            )
            status = os.fstat(descriptor.fd)
            if not stat.S_ISREG(status.st_mode):
                raise ValueError("unsafe_material_path")
            _length(status.st_size, maximum)
            stream = _stream(owner.rollback, descriptor, "rb")
            raw = stream.read(maximum + 1)
            if len(raw) != status.st_size or len(raw) > maximum:
                raise ValueError("material_length_mismatch")
        except OSError:
            raise ValueError("material_object_unavailable") from None
        owner.rollback.close()
        return raw
    except BaseException as failure:
        # A close failure already carries this owner; fail preserves that
        # pending progress instead of attempting cleanup twice.
        owner.fail(failure)


def _write(path, raw):
    owner = ConstructionOwner()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = OwnedDescriptor.open(
            owner.rollback, path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        f = _stream(owner.rollback, descriptor, "wb")
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
        owner.rollback.close()
    except BaseException as failure:
        owner.fail(failure)


def _manifest(raw):
    value = strict_json(raw)
    if (
        type(value) is not dict
        or type(value.get("version")) is not int
        or value["version"] != 1
        or type(value.get("files")) is not list
        or not 0 < len(value["files"]) <= MAX_FILES
        or type(value.get("file_count")) is not int
        or value["file_count"] != len(value["files"])
    ):
        raise ValueError("invalid_material_manifest")
    seen, total = set(), len(raw)
    for entry in value["files"]:
        _closed(entry, "path bytes sha256")
        path = _path(entry["path"])
        if path in seen or path == MANIFEST:
            raise ValueError("duplicate_material_path")
        seen.add(path)
        _length(entry["bytes"], MAX_FILE_BYTES)
        hash_value(entry["sha256"])
        total += entry["bytes"]
    _length(total, MAX_TOTAL_BYTES)
    if any(
        str(parent) in seen for path in seen for parent in PurePosixPath(path).parents
    ):
        raise ValueError("unsafe_material_path")
    return value, total


def parse_reference(value):
    if isinstance(value, bytes):
        value = strict_json(value)
    _closed(
        value, "contract version scope object_sha256 manifest_sha256 bytes file_count"
    )
    _version(value)
    identifier(value["scope"])
    hash_value(value["object_sha256"])
    hash_value(value["manifest_sha256"])
    _length(value["bytes"], MAX_TOTAL_BYTES)
    _length(value["file_count"], MAX_FILES)
    return dict(value)


def read_reference(path):
    """Bound CLI reference reads before allocating their contents."""
    path = Path(path).absolute()
    return parse_reference(_read(path.parent, path.name, MAX_METADATA_BYTES))


class ProtectedMaterialStore:
    """A fixed local content-addressed namespace. References contain no URLs.

    Read authorization is supplied separately to preflight_materials by its
    caller; possession of a reference alone is insufficient. This local adapter
    is not suitable evidence for real cloud identity, IAM or egress isolation.
    """

    def __init__(self, root, *, scope):
        self.root = Path(os.path.abspath(root))
        self.scope = identifier(scope)

    def _private(self, *, create=False):
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            status = self.root.lstat()
        except OSError:
            raise ValueError("material_object_unavailable") from None
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_mode & 0o077
            or status.st_uid != os.getuid()
        ):
            raise ValueError("unprotected_material_store")

    def _put(self, category, raw):
        self._private(create=True)
        identity = _sha(raw)
        directory = self.root / category
        directory.mkdir(mode=0o700, exist_ok=True)
        status = directory.lstat()
        if not stat.S_ISDIR(status.st_mode) or status.st_mode & 0o077:
            raise ValueError("unprotected_material_store")
        path = directory / identity
        if path.exists():
            if _read(self.root, f"{category}/{identity}", len(raw)) != raw:
                raise ValueError("material_digest_mismatch")
        else:
            _write(path, raw)
        return identity

    def _get(self, category, identity, maximum):
        self._private()
        hash_value(identity)
        raw = _read(self.root, f"{category}/{identity}", maximum)
        if _sha(raw) != identity:
            raise ValueError("material_digest_mismatch")
        return raw


def export_materials(
    source,
    *,
    vault,
    manifest_sha256,
    batch_path,
    proposal_path,
    evidence_store_path,
):
    """Copy every pinned manifest member read-only into a separate private vault."""
    source = Path(source).resolve()
    vault_root = vault.root.resolve()
    if (
        source == vault_root
        or source in vault_root.parents
        or vault_root in source.parents
    ):
        raise ValueError("material_store_overlap")
    hash_value(manifest_sha256)
    raw = _read(source, MANIFEST, MAX_METADATA_BYTES)
    if _sha(raw) != manifest_sha256:
        raise ValueError("material_manifest_mismatch")
    manifest, total = _manifest(raw)
    selection = {
        "batch_path": _path(batch_path),
        "proposal_path": _path(proposal_path),
        "evidence_store_path": _path(evidence_store_path),
    }
    members = {e["path"] for e in manifest["files"]}
    if {batch_path, proposal_path} - members:
        raise ValueError("material_selection_missing")
    # Each file is checked on the bytes transferred, not via an earlier stat.
    for entry in manifest["files"]:
        content = _read(source, entry["path"], MAX_FILE_BYTES)
        if len(content) != entry["bytes"]:
            raise ValueError("material_length_mismatch")
        if _sha(content) != entry["sha256"]:
            raise ValueError("material_digest_mismatch")
        vault._put("blobs", content)
    vault._put("blobs", raw)
    envelope = {
        "contract": CONTRACT,
        "version": VERSION,
        "scope": vault.scope,
        "manifest_sha256": manifest_sha256,
        "manifest_bytes": len(raw),
        "files": manifest["files"],
        "selection": selection,
    }
    raw_envelope = encode(envelope)
    if len(raw_envelope) > MAX_METADATA_BYTES:
        raise ValueError("material_size_exceeded")
    return {
        "contract": CONTRACT,
        "version": VERSION,
        "scope": vault.scope,
        "object_sha256": vault._put("objects", raw_envelope),
        "manifest_sha256": manifest_sha256,
        "bytes": total,
        "file_count": len(manifest["files"]),
    }


def _lineage(store, batch_id, seen=None):
    seen = {} if seen is None else seen
    if batch_id in seen:
        return seen
    batch = store.read(batch_id)
    for key in ("parent", "calibration"):
        linked = batch.get(key)
        if linked and linked.get("batch_id"):
            _lineage(store, linked["batch_id"], seen)
    seen[batch_id] = batch
    return seen


def preflight_materials(
    reference,
    *,
    vault,
    allowed_objects,
    expected_manifest_sha256,
    output,
):
    """Fully receive materials into a new namespace and return an audit report.

    allowed_objects and the expected manifest come from receiver policy, never
    from fields inside the object. Callers must not treat this report as an
    approval, runtime binding, source attestation, or model invocation capability.
    """
    reference = parse_reference(reference)
    if not isinstance(allowed_objects, (set, frozenset)):
        raise ValueError("material_read_policy_invalid")
    for identity in allowed_objects:
        hash_value(identity)
    if reference["scope"] != vault.scope:
        raise ValueError("material_scope_mismatch")
    if reference["object_sha256"] not in allowed_objects:
        raise ValueError("material_read_unauthorized")
    hash_value(expected_manifest_sha256)
    if reference["manifest_sha256"] != expected_manifest_sha256:
        raise ValueError("material_manifest_mismatch")
    envelope = strict_json(
        vault._get("objects", reference["object_sha256"], MAX_METADATA_BYTES)
    )
    _closed(
        envelope,
        "contract version scope manifest_sha256 manifest_bytes files selection",
    )
    _version(envelope)
    if envelope["scope"] != vault.scope:
        raise ValueError("material_scope_mismatch")
    if envelope["manifest_sha256"] != expected_manifest_sha256:
        raise ValueError("material_manifest_mismatch")
    _length(envelope["manifest_bytes"], MAX_METADATA_BYTES)
    raw_manifest = vault._get("blobs", expected_manifest_sha256, MAX_METADATA_BYTES)
    if len(raw_manifest) != envelope["manifest_bytes"]:
        raise ValueError("material_length_mismatch")
    manifest, total = _manifest(raw_manifest)
    if encode(envelope["files"]) != encode(manifest["files"]):
        raise ValueError("material_manifest_mismatch")
    if reference["bytes"] != total or reference["file_count"] != len(manifest["files"]):
        raise ValueError("material_length_mismatch")
    selected = envelope["selection"]
    _closed(selected, "batch_path proposal_path evidence_store_path")
    for path in selected.values():
        _path(path)
    members = {e["path"] for e in manifest["files"]}
    if {selected["batch_path"], selected["proposal_path"]} - members:
        raise ValueError("material_selection_missing")
    output = Path(output).absolute()
    resolved = output.resolve()
    vault_root = vault.root.resolve()
    if (
        resolved == vault_root
        or vault_root in resolved.parents
        or resolved in vault_root.parents
    ):
        raise ValueError("material_store_overlap")
    try:
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        raise ValueError("material_output_exists") from None
    # Failure leaves only a fresh incomplete output directory, never a completed
    # admission or mutated source. It cannot be reused as an accepted receipt.
    target = output / "materials"
    for entry in manifest["files"]:
        content = vault._get("blobs", entry["sha256"], MAX_FILE_BYTES)
        if len(content) != entry["bytes"]:
            raise ValueError("material_length_mismatch")
        _write(target / entry["path"], content)
    _write(target / MANIFEST, raw_manifest)
    original = EvidenceStore(target / selected["evidence_store_path"])
    batch = strict_json(
        _read(target, selected["batch_path"], MAX_FILE_BYTES), limit=MAX_FILE_BYTES
    )
    if original.read(batch["batch_id"]) != batch:
        raise ValueError("material_batch_mismatch")
    proposal = strict_json(
        _read(target, selected["proposal_path"], MAX_FILE_BYTES), limit=MAX_FILE_BYTES
    )
    if proposal.get("binding") != binding(batch):
        raise ValueError("material_proposal_mismatch")
    lineage = _lineage(original, batch["batch_id"])
    imported = EvidenceStore(output / "evidence")
    for value in lineage.values():
        imported.import_batch(value)
    received = imported.read(batch["batch_id"])
    status = imported.authorization_status(batch["batch_id"])
    report = {
        "contract": CONTRACT,
        "version": VERSION,
        "scope": vault.scope,
        "object_sha256": reference["object_sha256"],
        "manifest_sha256": expected_manifest_sha256,
        "received_bytes": total,
        "file_count": len(manifest["files"]),
        "batch_id": received["batch_id"],
        "batch_sha256": digest(received),
        "proposal_sha256": digest(proposal),
        "schema_version": received["schema_version"],
        "lineage": list(lineage),
        "material_integrity": "PASS",
        "authorization": status,
        "online_executable": False,
        "real_readiness": {
            "verdict": "BLOCKED",
            "blockers": list(
                dict.fromkeys(
                    [*status["blockers"], "real_execution_prerequisites_unverified"]
                )
            ),
        },
        "v1_release": {"verdict": "BLOCKED", "blockers": ["real_acceptance_required"]},
    }
    report["id"] = digest(report)
    _write(output / "material-admission.json", encode(report))
    return report
