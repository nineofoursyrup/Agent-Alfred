"""CE-01: serialized public CLI requests/events/messages versus exact old code."""

import json
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

BASE = "8ba7192ee84533aef3deb8453d8fa7364d67f48b"


@pytest.mark.parametrize(
    "mode",
    ["disabled", "explicit", "automatic", "command", "recovery", "probe", "http"],
)
def test_ce01_exact_baseline_cli_requests_events_messages(tmp_path, mode):
    root = Path(__file__).resolve().parents[4]
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    archive = tmp_path / "baseline.tar"
    with archive.open("wb") as out:
        subprocess.run(["git", "archive", BASE], cwd=root, stdout=out, check=True)
    with tarfile.open(archive) as source:
        source.extractall(baseline, filter="data")
    fixture = Path(__file__).with_name(
        "routing_web_baseline_fixture.py"
        if mode == "http"
        else "routing_baseline_fixture.py"
    )
    results = []
    for name, source in [("before", baseline), ("after", root)]:
        env = {**os.environ, "PYTHONPATH": str(source / "src")}
        # Place the same fixture outside either package so Python's script
        # directory cannot accidentally import the candidate in both runs.
        runner = tmp_path / "fixture.py"
        runner.write_bytes(fixture.read_bytes())
        process = subprocess.run(
            [sys.executable, str(runner), str(tmp_path / name), mode],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert process.returncode == 0, process.stderr
        results.append(json.loads(process.stdout))

    # #84 deliberately strengthens the generic Skill format instruction. Amend
    # only the old expected request and step.started text by the approved literal;
    # every other byte of the business contract remains compared below.
    expected = results[0]
    systems = [r["system"] for r in expected["requests"]] + [
        e["payload"]["system"]
        for e in expected["events"]
        if e["payload"]["name"] == "step.started"
    ]
    sections = [
        block
        for system in systems
        for block in system
        if block.get("text", "").startswith("<skills>\n")
    ]
    assert len(sections) == (2 if mode in ("explicit", "automatic") else 0)
    anchor = "instruction priority. Combine compatible requirements;"
    amendment = (
        "instruction priority. Applicable format constraints apply to the entire "
        "final reply. When exact lines or an exclusive format are required, do not "
        "add introductions, Markdown wrappers, explanations, disclaimers, or "
        "follow-up offers outside that format. Check the complete reply against "
        "those constraints before responding. Combine compatible requirements;"
    )
    for section in sections:
        assert section["text"].count(anchor) == 1
        section["text"] = section["text"].replace(anchor, amendment, 1)

    # #81 adds observation-only path events. Compare every old business event,
    # request, result and reference; generated identity offsets are immaterial.
    def business_contract(value):
        value["events"] = [
            e for e in value["events"] if not e["payload"]["name"].startswith("path.")
        ]
        aliases = {}
        for seq, event in enumerate(value["events"], 1):
            event["seq"] = seq

        def normalize(item):
            if isinstance(item, dict):
                return {k: normalize(v) for k, v in item.items()}
            if isinstance(item, list):
                return [normalize(v) for v in item]
            if isinstance(item, str) and re.fullmatch(
                r"(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})",
                item,
            ):
                return aliases.setdefault(item, f"identity-{len(aliases)}")
            return item

        return normalize(value)

    assert business_contract(results[0]) == business_contract(results[1])
