import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

root = Path.cwd()
scratch = root / ".scratch/v11"
label, *command = sys.argv[1:]
record_path = scratch / "gates" / (label + ".json")
record_path.parent.mkdir(parents=True, exist_ok=True)
if record_path.exists():
    raise RuntimeError("Refusing to overwrite gate evidence")
paths = subprocess.check_output(
    ["git", "ls-files", "src", "tests", "scripts", ".github", "skills",
     "package.json", "package-lock.json", "pyproject.toml", "uv.lock",
     "playwright.config.js", "tsconfig.json"], text=True
).splitlines()
inputs = {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in paths}
record = {
    "command": command, "cwd": str(root),
    "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    "environment": {"ALFRED_BROWSER_TEST_PORT": os.environ.get("ALFRED_BROWSER_TEST_PORT")},
    "input_manifest_sha256": hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest(),
    "inputs": inputs,
}
start = time.monotonic()
raw = scratch / "gates" / (label + ".raw")
with raw.open("wb") as stream:
    result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT)
data = raw.read_bytes()
record.update(exit_code=result.returncode, elapsed_seconds=round(time.monotonic() - start, 2),
              output=data.decode(errors="replace"), output_sha256=hashlib.sha256(data).hexdigest())
if result.returncode and "test:browser" in command:
    destination = scratch / "failures" / label
    destination.mkdir(parents=True, exist_ok=False)
    members = {}
    for folder in (root / "output/playwright/results").iterdir():
        if not folder.is_dir():
            continue
        if not any(folder.glob("test-failed-*.png")) and not (folder / "error-context.md").exists():
            continue
        target = destination / folder.name
        target.mkdir()
        for source in folder.iterdir():
            if source.name not in ("error-context.md", "trace.zip") and not source.name.startswith("test-failed-"):
                continue
            value = source.read_bytes()
            members[str(source.relative_to(root / "output/playwright/results"))] = {
                "bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()
            }
            if source.name == "error-context.md":
                (target / "error-context.md.json").write_text(json.dumps({
                    "encoding": "UTF-8", "sha256": hashlib.sha256(value).hexdigest(),
                    "text": value.decode()
                }, ensure_ascii=False, indent=2) + "\n")
            else:
                shutil.copyfile(source, target / source.name)
    (destination / "manifest.json").write_text(json.dumps(members, ensure_ascii=False, indent=2) + "\n")
record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({k: record[k] for k in ("command", "exit_code", "elapsed_seconds", "head")}), flush=True)
print(record["output"][-2000:], flush=True)
sys.exit(result.returncode)
