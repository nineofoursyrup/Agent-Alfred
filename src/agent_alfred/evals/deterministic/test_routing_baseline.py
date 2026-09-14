"""CE-01: serialized public CLI requests/events/messages versus exact old code."""

import json
import os
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
    assert results[0] == results[1]
