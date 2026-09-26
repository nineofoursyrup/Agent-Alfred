"""C administrator signs a reviewed, synthetic-only preflight receipt.

The A/B deployers can verify with C KMS but cannot use this signing role.
The C administrator must independently inspect the inputs before execution.
"""

import argparse
import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .ctl import (
    client_session,
    parse_config,
    preflight_payload,
    preflight_signature_message,
    required,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--preflight-result", type=Path, required=True)
    parser.add_argument("--preflight-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("fresh signature output path required")
    if not args.execute:
        parser.error("C KMS signing requires explicit --execute")
    config = parse_config(args.config, require_active=True)
    preflight_payload(args.config, args.preflight_result, args.preflight_sha256)
    state = json.loads(args.state.read_text())
    key_arn = required(state, "service-c", "PreflightSignerKeyArn")
    if not key_arn.startswith(
        f"arn:aws:kms:{config['region']}:{config['accounts']['c']}:key/"
    ):
        raise ValueError("preflight key is outside independent C account")
    session = client_session(config, "c")
    receipt = {
        "key_arn": key_arn,
        "receipt_sha256": args.preflight_sha256,
        "signer_role_arn": config["admin_role_arns"]["c"],
        "signed_at": datetime.now(timezone.utc).isoformat(),
        "signing_algorithm": "ECDSA_SHA_256",
    }
    signed = session.client("kms").sign(
        KeyId=key_arn,
        Message=preflight_signature_message(receipt),
        MessageType="DIGEST",
        SigningAlgorithm="ECDSA_SHA_256",
    )
    if signed.get("KeyId") != key_arn:
        raise ValueError("C KMS signed with a different key")
    receipt["signature"] = base64.b64encode(signed["Signature"]).decode("ascii")
    raw = json.dumps(receipt, sort_keys=True, indent=2).encode() + b"\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as output:
        output.write(raw)
    args.output.with_suffix(args.output.suffix + ".sha256").write_text(
        hashlib.sha256(raw).hexdigest() + "\n"
    )
    print(json.dumps({"output": str(args.output), "key_arn": key_arn}))


if __name__ == "__main__":
    main()
