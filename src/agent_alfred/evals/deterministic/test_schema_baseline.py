"""CE-01: same initial databases upgraded by pinned baseline and candidate."""

import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

BASE = "e52d83290e0d5690025e5f907760a8c3592065ee"


def test_ce01_published_ddl_data_and_public_signatures_match_baseline(tmp_path):
    root = Path(__file__).resolve().parents[4]
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    archive = tmp_path / "baseline.tar"
    with archive.open("wb") as out:
        subprocess.run(["git", "archive", BASE], cwd=root, stdout=out, check=True)
    with tarfile.open(archive) as source:
        source.extractall(baseline, filter="data")
    runner = tmp_path / "fixture.py"
    runner.write_bytes(
        Path(__file__).with_name("schema_baseline_fixture.py").read_bytes()
    )
    observations = []
    for mode, tree in (("baseline", baseline), ("candidate", root)):
        result = subprocess.run(
            [
                sys.executable,
                str(runner),
                mode,
                str(tmp_path / "databases"),
                str(tmp_path / mode / "observed"),
            ],
            env={**os.environ, "PYTHONPATH": str(tree / "src")},
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        observations.append(
            json.loads((tmp_path / mode / "observed/observations.json").read_text())
        )
    assert observations[0] == observations[1]
