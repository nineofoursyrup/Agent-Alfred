"""Build a Linux Lambda zip and, with Docker, a pinned synthetic task image.

No cloud calls, credentials, provider calls or push happen here. The output
contains immutable hashes for the account administrators to verify.
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from agent_alfred.p2.common import encode
from agent_alfred.p2.worker import package_digest

from .fixture import manifest

ROOT = Path(__file__).resolve().parents[2]
BASE = Path(__file__).resolve().parent


def run(*argv):
    subprocess.run(argv, cwd=ROOT, check=True)


def write_zip(source, target):
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            info = zipfile.ZipInfo(str(path.relative_to(source)), (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-image")
    parser.add_argument("--lambda-only", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "build/p2")
    args = parser.parse_args()
    if not args.lambda_only and (
        not args.base_image
        or not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", args.base_image)
    ):
        parser.error("base image must be pinned by sha256 digest")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    run(
        "uv",
        "export",
        "--quiet",
        "--frozen",
        "--no-dev",
        "--extra",
        "p2",
        "--no-emit-project",
        "--format",
        "requirements.txt",
        "--output-file",
        str(output / "requirements.txt"),
    )
    raw = encode(manifest())
    (output / "input.json").write_bytes(raw)
    if (output / "python").exists():
        raise ValueError("output/python already exists; use a fresh output directory")
    image_tag = None
    if args.lambda_only:
        run(
            "uv",
            "pip",
            "install",
            "--require-hashes",
            "--python-version",
            "3.14",
            "--python-platform",
            "x86_64-manylinux2014",
            "--target",
            str(output / "python"),
            "-r",
            str(output / "requirements.txt"),
        )
        package = output / "python/agent_alfred"
        for source in sorted((ROOT / "src/agent_alfred").rglob("*.py")):
            destination = package / source.relative_to(ROOT / "src/agent_alfred")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    else:
        with tempfile.TemporaryDirectory(prefix="p2-build-") as temporary:
            context = Path(temporary)
            shutil.copy2(BASE / "Dockerfile", context / "Dockerfile")
            shutil.copy2(output / "requirements.txt", context / "requirements.txt")
            shutil.copy2(output / "input.json", context / "input.json")
            package = context / "agent_alfred"
            for source in sorted((ROOT / "src/agent_alfred").rglob("*.py")):
                relative = source.relative_to(ROOT / "src/agent_alfred")
                destination = package / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            image_tag = "p2-synthetic-local:" + hashlib.sha256(raw).hexdigest()[:16]
            run(
                "docker",
                "build",
                "--platform",
                "linux/amd64",
                "--build-arg",
                "BASE_IMAGE=" + args.base_image,
                "--tag",
                image_tag,
                str(context),
            )
            container = subprocess.check_output(
                ["docker", "create", image_tag], text=True
            ).strip()
            try:
                subprocess.run(
                    [
                        "docker",
                        "cp",
                        container + ":/opt/p2/python",
                        str(output / "python"),
                    ],
                    check=True,
                )
            finally:
                subprocess.run(
                    ["docker", "rm", "-f", container], check=True, capture_output=True
                )
    write_zip(output / "python", output / "lambda.zip")

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    metadata = {
        "base_image": args.base_image,
        "image_tag": image_tag,
        "input_sha256": digest(output / "input.json"),
        "lambda_zip_sha256": digest(output / "lambda.zip"),
        "package_sha256": package_digest(),
        "source_merge_sha": "b7826a12144ec77e8656b01b02e49e0ce740f01e",
    }
    (output / "manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(output / "manifest.json")


if __name__ == "__main__":
    main()
