"""Controlled Fargate entry; submit as executor or invoke as worker task role."""

import hashlib
import os
from pathlib import Path

from .authority import validate_submission
from .common import (
    BoundaryError,
    check_digest,
    decode,
    encode,
    exact,
    required_env,
    synthetic_only,
)
from .remote import PrivateApi, RemoteAuthorityDispatch


def package_digest():
    root = Path(__file__).resolve().parent
    rows = []
    for file in sorted(root.glob("*.py")):
        rows.append((file.name, hashlib.sha256(file.read_bytes()).hexdigest()))
    return hashlib.sha256(encode(rows)).hexdigest()


def load_input(path, expected_digest):
    file = Path(path)
    if file.is_symlink() or not file.is_file():
        raise BoundaryError("worker_input_unverifiable")
    raw = file.read_bytes()
    if hashlib.sha256(raw).hexdigest() != check_digest(expected_digest):
        raise BoundaryError("worker_input_digest_mismatch")
    manifest = decode(raw)
    exact(manifest, {"proposal", "batch", "descriptor"})
    validate_submission({"proposal": manifest["proposal"], "batch": manifest["batch"]})
    return manifest


def run(api, manifest, mode, job_id=None, attempt_id=None):
    broker = RemoteAuthorityDispatch(api)
    if mode == "submit":
        return broker.submit_job(
            {
                "proposal": manifest["proposal"],
                "batch": manifest["batch"],
            }
        )
    if not job_id:
        raise BoundaryError("p2_job_id_required")
    if mode == "status":
        return broker.status(job_id)
    if mode == "invoke":
        if not attempt_id:
            raise BoundaryError("p2_attempt_id_required")
        return broker.invoke(job_id, "product", attempt_id, manifest["descriptor"])
    if mode == "finish":
        return broker.finish(job_id, "product")
    raise BoundaryError("worker_mode_invalid")


def main():
    synthetic_only()
    if package_digest() != check_digest(required_env("P2_PACKAGE_SHA256")):
        raise BoundaryError("worker_package_digest_mismatch")
    manifest = load_input(
        required_env("P2_INPUT_PATH"), required_env("P2_INPUT_SHA256")
    )
    api = PrivateApi(required_env("P2_AUTHORITY_API_BASE"), required_env("AWS_REGION"))
    result = run(
        api,
        manifest,
        required_env("P2_RUN_MODE"),
        os.environ.get("P2_JOB_ID"),
        os.environ.get("P2_ATTEMPT_ID"),
    )
    print(encode(result).decode())


if __name__ == "__main__":
    main()
