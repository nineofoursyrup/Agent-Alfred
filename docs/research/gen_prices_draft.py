#!/usr/bin/env python3
"""Build-time generator for the model-level static price table (price chain tier 3).

Fetches https://models.dev/api.json, keeps only the providers we actually ship,
and emits a small TOML snapshot into the package.

This table is a FALLBACK ONLY. The runtime model catalog must still be fetched
online (see issue #1, note 3). Nothing here is a substitute for a live catalog.

Source data: https://models.dev/api.json  (repo: anomalyco/models.dev, MIT)
Standard library only -- no third-party dependencies.

Usage:
    python3 gen_prices_draft.py --out src/agent_alfred/data/prices.toml
    python3 gen_prices_draft.py --out - --providers opencode-go   # dry run to stdout
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

# Providers we ship a static fallback for. Keep this list short on purpose:
# every entry is a price we are promising to keep roughly honest.
DEFAULT_PROVIDERS = ("opencode-go", "opencode")

# Cost fields we carry over, in emit order. models.dev quotes all of these in
# USD per 1M tokens.
COST_FIELDS = ("input", "output", "cache_read", "cache_write")

# Presence of any of these means the real price depends on context length or
# request shape, so a flat per-token rate would understate the bill.
TIERED_COST_MARKERS = ("tiers", "context_over_200k", "reasoning", "input_audio", "output_audio")

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def toml_key(key: str) -> str:
    """Quote a TOML key unless it is safe as a bare key.

    Model ids such as `gpt-5.1` contain dots, which TOML would otherwise read as
    a nesting separator.
    """
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


# models.dev sits behind Cloudflare, which answers the stock `Python-urllib/3.x`
# User-Agent with HTTP 403. Any non-default UA gets through.
USER_AGENT = "agent-alfred-price-snapshot/0.1 (+https://github.com/nineofoursyrup/Agent-Alfred)"


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


def extract(catalog: dict[str, Any], provider_ids: tuple[str, ...]) -> dict[str, Any]:
    """Pull the providers we care about out of the full 4.3 MB catalog."""
    out: dict[str, Any] = {}
    missing = [p for p in provider_ids if p not in catalog]
    if missing:
        raise SystemExit(
            f"provider(s) not found in {SOURCE_URL}: {', '.join(missing)}\n"
            "models.dev provider ids are exact; check for a rename before editing this list."
        )

    for pid in provider_ids:
        provider = catalog[pid]
        models: dict[str, Any] = {}
        for mid, model in sorted(provider.get("models", {}).items()):
            cost = model.get("cost")
            if cost is None:
                # No price published. Emit nothing rather than guess a number --
                # a miss here must fall through the price chain, not resolve to 0.
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
            # Flags the ledger needs so it can label instead of quietly rounding.
            if any(marker in cost for marker in TIERED_COST_MARKERS):
                entry["tiered"] = True
            if entry["input"] == 0.0 and entry["output"] == 0.0:
                entry["free"] = True
            models[mid] = entry

        out[pid] = {
            "api": provider.get("api"),
            "env": provider.get("env", []),
            "name": provider.get("name"),
            "models": models,
        }
    return out


def render(data: dict[str, Any], *, snapshot_date: str, etag: str | None) -> str:
    lines: list[str] = []
    w = lines.append

    w("# GENERATED FILE -- do not edit by hand.")
    w(f"# Regenerate with: python3 docs/research/gen_prices_draft.py")
    w("#")
    w("# Model-level static prices: tier 3 of the price chain, used only when the")
    w("# online catalog lookup came back without a price. The runtime model catalog")
    w("# is always fetched online; this file never stands in for it.")
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

    for pid, provider in data.items():
        w(f"[providers.{toml_key(pid)}]")
        for field in ("name", "api"):
            if provider.get(field):
                w(f"{field} = {toml_value(provider[field])}")
        if provider.get("env"):
            w(f"env = {toml_value(list(provider['env']))}")
        w(f"model_count = {len(provider['models'])}")
        w("")
        for mid, entry in provider["models"].items():
            w(f"[providers.{toml_key(pid)}.models.{toml_key(mid)}]")
            for key, value in entry.items():
                w(f"{key} = {toml_value(value)}")
            w("")

    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="output path, or - for stdout")
    parser.add_argument(
        "--providers",
        nargs="+",
        default=list(DEFAULT_PROVIDERS),
        help="models.dev provider ids to snapshot",
    )
    parser.add_argument(
        "--from-file",
        help="read api.json from a local path instead of the network (offline test)",
    )
    args = parser.parse_args(argv)

    if args.from_file:
        with open(args.from_file, "rb") as fh:
            catalog = json.load(fh)
        etag = None
        snapshot_date = _dt.date.today().isoformat()
    else:
        catalog, etag, http_date = fetch(SOURCE_URL)
        # Prefer the server's own Date header over local clock skew.
        snapshot_date = _dt.date.today().isoformat()
        if http_date:
            try:
                parsed = _dt.datetime.strptime(http_date, "%a, %d %b %Y %H:%M:%S %Z")
                snapshot_date = parsed.date().isoformat()
            except ValueError:
                pass

    data = extract(catalog, tuple(args.providers))
    text = render(data, snapshot_date=snapshot_date, etag=etag)

    if args.out == "-":
        sys.stdout.write(text)
    else:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        total = sum(len(p["models"]) for p in data.values())
        print(
            f"wrote {args.out}: {len(data)} provider(s), {total} model(s), "
            f"snapshot {snapshot_date}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
