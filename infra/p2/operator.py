"""A's human-operated synthetic view, issue and revoke client.

An ISSUE requires a byte-exact hash of a separately saved review page. The
remote A issuer still re-reads the bound B proposal before signing.
"""

import argparse
import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

from .ctl import client_session, parse_config


def signed_request(session, region, url, method, body=None):
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    raw = (
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        if body
        else None
    )
    credentials = session.get_credentials()
    if credentials is None:
        raise ValueError("operator IAM identity unavailable")
    request = AWSRequest(
        method=method,
        url=url,
        data=raw,
        headers={"Content-Type": "application/json"} if raw else {},
    )
    SigV4Auth(credentials.get_frozen_credentials(), "execute-api", region).add_auth(
        request
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        type(
            "NoRedirect",
            (urllib.request.HTTPRedirectHandler,),
            {"redirect_request": lambda *_: None},
        )(),
    )
    try:
        with opener.open(
            urllib.request.Request(
                url, data=raw, headers=dict(request.headers), method=method
            ),
            timeout=8,
        ) as response:
            if response.status != 200:
                raise ValueError("issuer API rejected request")
            return response.read(512 * 1024 + 1)
    except urllib.error.HTTPError as error:
        raise ValueError("issuer API HTTP " + str(error.code)) from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["view", "issue", "revoke"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--reviewed-page-sha256")
    parser.add_argument("--activation-deadline")
    args = parser.parse_args()
    config = parse_config(args.config, require_active=args.action == "issue")
    state = json.loads(args.state.read_text())
    session = client_session(config, "a", role="operator")
    base = state["service-a"]["IssuerApiBase"]
    page_url = base + "/v1/proposal?" + urlencode({"job_id": args.job_id})
    if args.action == "view":
        if args.output is None or args.output.exists():
            parser.error("fresh --output required")
        raw = signed_request(session, config["region"], page_url, "GET")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(raw)
        args.output.with_suffix(args.output.suffix + ".sha256").write_text(
            hashlib.sha256(raw).hexdigest() + "\n"
        )
        print(args.output)
        return
    if args.action == "issue":
        if not args.reviewed_page_sha256 or not args.activation_deadline:
            parser.error("ISSUE requires reviewed page SHA256 and deadline")
        current = signed_request(session, config["region"], page_url, "GET")
        if hashlib.sha256(current).hexdigest() != args.reviewed_page_sha256:
            raise ValueError("synthetic proposal changed since review")
        body = {"job_id": args.job_id, "activation_deadline": args.activation_deadline}
    else:
        body = {"job_id": args.job_id}
    result = signed_request(
        session, config["region"], base + "/v1/" + args.action, "POST", body
    )
    print(result.decode())


if __name__ == "__main__":
    main()
