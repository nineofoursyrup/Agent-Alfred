#!/usr/bin/env python3
"""Build-time generator for the model-level static price table (price chain tier 4).

Fetches https://models.dev/api.json, keeps only the endpoints we actually ship,
and emits a small TOML snapshot into the package.

This table is a FALLBACK ONLY. The runtime model catalog must still be fetched
online. Nothing here is a substitute for a live catalog. A committed snapshot
is a historical fixture, not a claim of current list prices.

Source data: https://models.dev/api.json  (repo: anomalyco/models.dev, MIT)
Standard library only -- no third-party dependencies.

Usage:
    python3 scripts/gen_prices.py --out src/agent_alfred/data/prices.toml
    python3 scripts/gen_prices.py --from-file path/api.json --out -
        --snapshot-date 2026-08-26
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
import urllib.request
from typing import Any

SOURCE_URL = "https://models.dev/api.json"
SOURCE_REPO = "https://github.com/anomalyco/models.dev"
SOURCE_LICENSE = "MIT"
SOURCE_COPYRIGHT = "Copyright (c) 2025 models.dev"

# models.dev catalog keys equal our endpoint_id values for these rows.
DEFAULT_ENDPOINTS = ("opencode-go", "opencode")
COST_FIELDS = ("input", "output", "cache_read", "cache_write")
TIERED_COST_MARKERS = (
    "tiers",
    "context_over_200k",
    "reasoning",
    "input_audio",
    "output_audio",
)
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")

# models.dev sits behind Cloudflare, which answers the stock `Python-urllib/3.x`
# User-Agent with HTTP 403. Any non-default UA gets through.
USER_AGENT = (
    "agent-alfred-price-snapshot/0.1 "
    "(+https://github.com/nineofoursyrup/Agent-Alfred)"
)


def toml_key(key: str) -> str:
    """Quote a TOML key unless it is safe as a bare key."""
    return key if _BARE_KEY.match(key) else json.dumps(key)


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    raise TypeError(f"unsupported TOML value: {value!r}")


def fetch(url: str) -> tuple[dict[str, Any], str | None, str | None]:
    """Return (payload, etag, http_date). Raises on any non-200."""
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        if resp.status != 200:
            raise RuntimeError(f"{url} returned HTTP {resp.status}")
        raw = resp.read()
        etag = resp.headers.get("ETag")
        http_date = resp.headers.get("Date")
    return json.loads(raw), etag, http_date


def extract(catalog: dict[str, Any], endpoint_ids: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    missing = [p for p in endpoint_ids if p not in catalog]
    if missing:
        raise SystemExit(
            f"endpoint id(s) not found in {SOURCE_URL}: {', '.join(missing)}\n"
            "models.dev keys must match endpoint_id; "
            "check for a rename before editing this list."
        )

    for pid in endpoint_ids:
        source = catalog[pid]
        models: dict[str, Any] = {}
        for mid, model in sorted(source.get("models", {}).items()):
            cost = model.get("cost")
            if cost is None:
                continue
            entry: dict[str, Any] = {}
            for field in COST_FIELDS:
                if field in cost:
                    entry[field] = float(cost[field])
            if "input" not in entry or "output" not in entry:
                continue
            limit = model.get("limit", {})
            if "context" in limit:
                entry["context"] = int(limit["context"])
            if "output" in limit:
                entry["max_output"] = int(limit["output"])
            if any(marker in cost for marker in TIERED_COST_MARKERS):
                entry["tiered"] = True
            if entry["input"] == 0.0 and entry["output"] == 0.0:
                entry["free"] = True
            models[mid] = entry

        out[pid] = {
            "api": source.get("api"),
            "env": source.get("env", []),
            "name": source.get("name"),
            "models": models,
        }
    return out


def render(data: dict[str, Any], *, snapshot_date: str, etag: str | None) -> str:
    lines: list[str] = []
    w = lines.append

    w("# GENERATED FILE -- do not edit by hand.")
    w("# Regenerate with: python3 scripts/gen_prices.py")
    w("# Historical snapshot. Not a live quote.")
    w("#")
    w("# Model-level static prices: used only when the online catalog lookup")
    w("# came back without a price. The runtime model catalog is always fetched")
    w("# online; this file never stands in for it.")
    w("#")
    w(f"# Source:    {SOURCE_URL}")
    w(f"# Repo:      {SOURCE_REPO}")
    w(f"# License:   {SOURCE_LICENSE} -- {SOURCE_COPYRIGHT}")
    w("# Units:     USD per 1,000,000 tokens.")
    w("")
    w("[meta]")
    w(f"source_url = {toml_value(SOURCE_URL)}")
    w(f"snapshot_date = {toml_value(snapshot_date)}")
    if etag:
        w(f"source_etag = {toml_value(etag)}")
    w(f"license = {toml_value(SOURCE_LICENSE)}")
    w(f"copyright = {toml_value(SOURCE_COPYRIGHT)}")
    w('unit = "usd_per_million_tokens"')
    w("")

    for pid, endpoint in data.items():
        w(f"[endpoints.{toml_key(pid)}]")
        for field in ("name", "api"):
            if endpoint.get(field):
                w(f"{field} = {toml_value(endpoint[field])}")
        if endpoint.get("env"):
            w(f"env = {toml_value(list(endpoint['env']))}")
        w(f"model_count = {len(endpoint['models'])}")
        w("")
        for mid, entry in endpoint["models"].items():
            w(f"[endpoints.{toml_key(pid)}.models.{toml_key(mid)}]")
            for key, value in entry.items():
                w(f"{key} = {toml_value(value)}")
            w("")

    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="output path, or - for stdout")
    parser.add_argument(
        "--endpoint-ids",
        nargs="+",
        default=list(DEFAULT_ENDPOINTS),
        help="endpoint_id values; must match models.dev catalog keys",
    )
    parser.add_argument(
        "--from-file",
        help="read api.json from a local path instead of the network (offline test)",
    )
    parser.add_argument(
        "--snapshot-date",
        help="YYYY-MM-DD recorded in [meta]; default is HTTP Date or today",
    )
    args = parser.parse_args(argv)

    if args.from_file:
        with open(args.from_file, "rb") as fh:
            catalog = json.load(fh)
        etag = None
        snapshot_date = args.snapshot_date or _dt.date.today().isoformat()
    else:
        catalog, etag, http_date = fetch(SOURCE_URL)
        snapshot_date = args.snapshot_date or _dt.date.today().isoformat()
        if args.snapshot_date is None and http_date:
            try:
                parsed = _dt.datetime.strptime(http_date, "%a, %d %b %Y %H:%M:%S %Z")
                snapshot_date = parsed.date().isoformat()
            except ValueError:
                pass

    data = extract(catalog, tuple(args.endpoint_ids))
    text = render(data, snapshot_date=snapshot_date, etag=etag)

    if args.out == "-":
        sys.stdout.write(text)
    else:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        total = sum(len(p["models"]) for p in data.values())
        print(
            f"wrote {args.out}: {len(data)} endpoint(s), {total} model(s), "
            f"snapshot {snapshot_date}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
