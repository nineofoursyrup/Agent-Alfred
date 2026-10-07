"""Build/inspect an offline native candidate; never launch it or install it.

Uses explicitly named existing Python/site-packages, closes Mach-O dependency
paths inside the new bundle and signs only the new output with an ad-hoc key.
No downloads, package installation, Keychain access or signing identity lookup.
"""

import argparse
import base64
import hashlib
import json
import os
import plistlib
import secrets
import shutil
import subprocess
import sys
import tomllib
from importlib.metadata import distributions
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agent_alfred.evals.acceptance.candidate import capture, verify  # noqa: E402
from agent_alfred.evals.acceptance.controlled import local_sandbox  # noqa: E402
from agent_alfred.evals.acceptance.controlled.native import (  # noqa: E402
    BUNDLE_CONTRACT,
    ENTRYPOINTS,
    HELPER_ENTITLEMENTS,
    LAUNCHER_ENTITLEMENTS,
    RUNNER_ENTRYPOINTS,
    _case_root,
    bundle_inventory,
    verify_bundle,
)
from agent_alfred.evals.acceptance.schema import digest, encode  # noqa: E402

MACHO = {
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}


def command(*args):
    try:
        return subprocess.run(
            [str(arg) for arg in args],
            check=True,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            close_fds=True,
            timeout=300,
        ).stdout
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"native_build_failed: {args[0]}\n{error.stderr}") from error


def is_macho(path):
    with path.open("rb") as stream:
        return stream.read(4) in MACHO


def ignore(directory, names):
    return [name for name in names if name == "__pycache__" or name.endswith(".pyc")]


def copy_tree(source, destination, *, exclude=()):
    """A copied dependency must not retain a link to an ambient installation."""
    source = source.resolve(strict=True)
    for path in source.rglob("*"):
        if path.relative_to(source).parts[0] in exclude:
            continue
        if path.is_symlink() and not path.resolve(strict=True).is_relative_to(source):
            raise ValueError(f"runtime_symlink_outside_explicit_tree: {path}")

    def selected(directory, names):
        return ignore(directory, names) + (
            [name for name in names if name in exclude]
            if Path(directory) == source
            else []
        )

    shutil.copytree(source, destination, symlinks=False, ignore=selected)
    for path in destination.rglob("*"):
        path.chmod(0o755 if path.is_dir() or os.access(path, os.X_OK) else 0o644)


def copy_production_packages(source, destination, *, with_mcp):
    """Copy only locked production RECORD files, never ambient .pth or dev extras."""
    source = source.resolve(strict=True)
    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    locked = tomllib.loads((REPO / "uv.lock").read_text())["package"]
    versions = {
        (canonicalize_name(row["name"]), row["version"])
        for row in locked
        if "version" in row
    }
    available = {
        canonicalize_name(dist.metadata["Name"]): dist
        for dist in distributions(path=[str(source)])
    }
    requirements = list(project["dependencies"])
    if with_mcp:
        requirements.extend(project["optional-dependencies"]["mcp"])
    pending = [(Requirement(value), "") for value in requirements]
    selected = {}
    while pending:
        requirement, parent_extra = pending.pop()
        if requirement.marker and not requirement.marker.evaluate(
            {"extra": parent_extra}
        ):
            continue
        name = canonicalize_name(requirement.name)
        dist = available.get(name)
        if (
            dist is None
            or (name, dist.version) not in versions
            or not requirement.specifier.contains(dist.version, prereleases=True)
        ):
            raise ValueError(f"locked_production_dependency_missing: {name}")
        extras = {"", *requirement.extras}
        previous = selected.setdefault(name, set())
        for extra in extras - previous:
            pending.extend(
                (Requirement(value), extra) for value in (dist.requires or ())
            )
        previous.update(extras)
    destination.mkdir(parents=True)
    copied = set()
    rows = []
    for name in sorted(selected):
        dist = available[name]
        if dist.files is None:
            raise ValueError(f"production_record_missing: {name}")
        for declared in dist.files:
            relative = Path(str(declared))
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or relative.suffix in (".pyc", ".pth")
                or relative.name == "direct_url.json"
            ):
                continue
            original = source / relative
            if original.is_symlink() or not original.is_file():
                raise ValueError(f"production_record_file_invalid: {name}: {relative}")
            data = original.read_bytes()
            if declared.hash is not None:
                if declared.hash.mode != "sha256":
                    raise ValueError("production_record_hash_algorithm_invalid")
                expected = base64.urlsafe_b64decode(declared.hash.value + "==")
                if hashlib.sha256(data).digest() != expected:
                    raise ValueError(
                        f"production_record_hash_mismatch: {name}: {relative}"
                    )
            target = destination / relative
            if relative in copied:
                if target.read_bytes() != data:
                    raise ValueError("production_namespace_collision")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o755 if data[:4] in MACHO else 0o644)
            copied.add(relative)
        rows.append({"name": name, "version": dist.version})
    return rows


def linked(path):
    lines = command("/usr/bin/otool", "-L", path).splitlines()[1:]
    return list(
        dict.fromkeys(
            line.strip().split(" (compatibility version", 1)[0]
            for line in lines
            if line.startswith("\t")
        )
    )


def rpaths(path):
    lines = command("/usr/bin/otool", "-l", path).splitlines()
    result = []
    for index, line in enumerate(lines):
        if line.strip() == "cmd LC_RPATH":
            value = lines[index + 2].strip()
            if not value.startswith("path "):
                raise ValueError("macho_rpath_invalid")
            result.append(value[5:].split(" (offset ", 1)[0])
    return result


def system_library(value):
    return value.startswith(("/usr/lib/", "/System/Library/"))


def resolve_link(value, original):
    if value.startswith("@loader_path/"):
        return (original.parent / value[len("@loader_path/") :]).resolve(strict=True)
    if value.startswith("@rpath/"):
        for candidate in rpaths(original):
            if candidate.startswith("@loader_path/"):
                base = original.parent / candidate[len("@loader_path/") :]
            elif candidate.startswith("/"):
                base = Path(candidate)
            else:
                continue
            resolved = base / value[len("@rpath/") :]
            if system_library(str(resolved)):
                return resolved
            if resolved.is_file():
                return resolved.resolve(strict=True)
        raise ValueError(f"unresolved_macho_rpath: {original}: {value}")
    if value.startswith("/"):
        return Path(value).resolve(strict=True)
    # Ambiguous @executable_path is not silently resolved against the build host.
    raise ValueError(f"unsupported_macho_dependency: {original}: {value}")


def relocate(bundle, originals):
    """Copy every non-system loaded image; then remove ambient search paths."""
    frameworks = bundle / "Contents/Frameworks"
    frameworks.mkdir(exist_ok=True)
    source_to_destination = {original: dest for dest, original in originals.items()}
    pending = list(originals)
    done = set()
    while pending:
        destination = pending.pop()
        if destination in done:
            continue
        done.add(destination)
        original = originals[destination]
        identity = command("/usr/bin/otool", "-D", destination).splitlines()[1:]
        for value in linked(destination):
            if system_library(value):
                continue
            if value in identity:
                command(
                    "/usr/bin/install_name_tool", "-id", destination.name, destination
                )
                continue
            dependency = resolve_link(value, original)
            if system_library(str(dependency)):
                command(
                    "/usr/bin/install_name_tool",
                    "-change",
                    value,
                    dependency,
                    destination,
                )
                continue
            target = source_to_destination.get(dependency)
            if target is None:
                name = hashlib.sha256(str(dependency).encode()).hexdigest()[:16]
                target = frameworks / f"{name}-{dependency.name}"
                shutil.copyfile(dependency, target)
                target.chmod(0o755)
                originals[target] = dependency
                source_to_destination[dependency] = target
                pending.append(target)
            new = "@loader_path/" + os.path.relpath(target, destination.parent)
            command("/usr/bin/install_name_tool", "-change", value, new, destination)
        for search in rpaths(destination):
            command("/usr/bin/install_name_tool", "-delete_rpath", search, destination)
    # Validate the relocated closure, including every extension, without loading it.
    for path in originals:
        for value in linked(path):
            if system_library(value) or value == path.name:
                continue
            if not value.startswith("@loader_path/"):
                raise ValueError(f"ambient_macho_dependency: {path}: {value}")
            target = (path.parent / value[len("@loader_path/") :]).resolve(strict=True)
            if not target.is_relative_to(bundle) or target not in originals:
                raise ValueError(f"unsealed_macho_dependency: {path}: {value}")
    return sorted(originals)


def build(*, output, python_prefix, site_packages, slots=30, with_mcp=False):
    if sys.platform != "darwin":
        raise ValueError("native_macos_required")
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("native_output_must_be_new")
    if type(slots) is not int or slots not in (1, 30):
        raise ValueError("native_slots_must_be_30_or_inspection_smoke_1")
    prefix = python_prefix.resolve(strict=True)
    stdlib = prefix / "lib/python3.14"
    headers = prefix / "include/python3.14"
    library = prefix / "Python"
    if not library.is_file():
        library = prefix / "lib/libpython3.14.dylib"
    if not all(path.exists() for path in (stdlib, headers, library)):
        raise ValueError("explicit_cpython_314_prefix_required")
    source_candidate = capture(REPO)
    source_id = digest(source_candidate)
    boundary = local_sandbox.boundary_contract()
    # A future reviewed package can allocate fresh OS containers. This identifier
    # is frozen in the manifest, never generated by a running job to reset slots.
    package_id = digest({"source_id": source_id, "nonce": secrets.token_hex(32)})
    output.mkdir(parents=True, mode=0o700)
    (output / "source-candidate.json").write_bytes(encode(source_candidate) + b"\n")
    package = output / "AlfredLocalNative"
    bundle = package / "slots/00.app"
    for relative in ("MacOS", "Helpers", "Resources", "Frameworks"):
        (bundle / "Contents" / relative).mkdir(parents=True, mode=0o755)
    bundle_id = f"local.agent-alfred.runner.{package_id[:24]}.slot00"
    info = {
        "CFBundleIdentifier": bundle_id,
        "CFBundleExecutable": "AlfredRunner",
        "CFBundleName": "Alfred Local Runner",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "14.0",
        "NSFaceIDUsageDescription": "Authenticate the exact local approval object.",
    }
    (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    (bundle / local_sandbox.PROFILE_PATH).write_text(
        local_sandbox.runner_profile(bundle, _case_root(bundle_id))
    )
    resources = bundle / "Contents/Resources"
    # A separate explicit packages directory replaces the prefix's potentially
    # symlinked site-packages; that ambient tree is never traversed or copied.
    copy_tree(
        stdlib,
        resources / "python/lib/python3.14",
        exclude=("site-packages", "config-3.14-darwin"),
    )
    production = copy_production_packages(
        site_packages, resources / "site-packages", with_mcp=with_mcp
    )
    copy_tree(REPO / "src/agent_alfred", resources / "packages/agent_alfred")
    shutil.copyfile(
        REPO / "native/macos/runner.py", bundle / RUNNER_ENTRYPOINTS["runner"]
    )
    originals = {}
    for dest_root, original_root in (
        (resources / "python/lib/python3.14", stdlib),
        (resources / "site-packages", site_packages.resolve()),
    ):
        for path in dest_root.rglob("*"):
            if path.is_file() and is_macho(path):
                originals[path] = (
                    original_root / path.relative_to(dest_root)
                ).resolve()
    clang = command("/usr/bin/xcrun", "--find", "clang").strip()
    swift = command("/usr/bin/xcrun", "--find", "swiftc").strip()
    sdk = command("/usr/bin/xcrun", "--show-sdk-path").strip()
    launcher = bundle / RUNNER_ENTRYPOINTS["launcher"]
    helper = bundle / RUNNER_ENTRYPOINTS["python"]
    owner = package / ENTRYPOINTS["owner_helper"]
    owner.parent.mkdir(parents=True, mode=0o755)
    owner_app = package / "OwnerApproval.app"
    owner_info = {
        **info,
        "CFBundleIdentifier": f"local.agent-alfred.owner.{package_id[:24]}",
        "CFBundleExecutable": "AlfredOwnerApproval",
        "CFBundleName": "Alfred Owner Approval",
    }
    (owner_app / "Contents/Info.plist").write_bytes(plistlib.dumps(owner_info))
    command(
        clang,
        "-isysroot",
        sdk,
        "-Wall",
        "-Wextra",
        "-Werror",
        "-mmacosx-version-min=14.0",
        REPO / "native/macos/AlfredRunner.c",
        "-o",
        launcher,
    )
    command(
        clang,
        "-isysroot",
        sdk,
        "-Wall",
        "-Wextra",
        "-Werror",
        "-Wno-deprecated-declarations",
        "-mmacosx-version-min=14.0",
        "-I",
        headers,
        REPO / "native/macos/AlfredPython.c",
        library,
        "-lsandbox",
        "-lffi",
        "-Wl,-headerpad_max_install_names",
        "-o",
        helper,
    )
    command(
        swift,
        "-sdk",
        sdk,
        "-O",
        REPO / "native/macos/AlfredOwnerApproval.swift",
        "-o",
        owner,
    )
    # New native binaries have no original relocation directory; their linked
    # dependencies are absolute/system paths, otherwise relocation refuses them.
    originals.update({path: path for path in (launcher, helper)})
    binaries = relocate(bundle, originals)
    owner_binaries = relocate(owner_app, {owner: owner})
    for path in binaries:
        if path in (launcher, helper):
            continue
        command("/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", path)
    for path in owner_binaries:
        command("/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", path)
    for path, entitlements in (
        (helper, HELPER_ENTITLEMENTS),
        (launcher, LAUNCHER_ENTITLEMENTS),
    ):
        profile = output / f"{path.name}.entitlements.plist"
        profile.write_bytes(plistlib.dumps(entitlements))
        command(
            "/usr/bin/codesign",
            "--force",
            "--sign",
            "-",
            "--timestamp=none",
            "--entitlements",
            profile,
            path,
        )
    command(
        "/usr/bin/codesign",
        "--force",
        "--sign",
        "-",
        "--timestamp=none",
        "--entitlements",
        output / "AlfredRunner.entitlements.plist",
        bundle,
    )
    command(
        "/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", owner_app
    )
    slot_rows = []
    for index in range(slots):
        app = package / f"slots/{index:02d}.app"
        identity = f"local.agent-alfred.runner.{package_id[:24]}.slot{index:02d}"
        if index:
            # APFS clones are distinct regular files, not symlinks or hardlinks.
            # A filesystem lacking clone support fails; it is not silently
            # replaced with writable shared code or a large hidden copy.
            command("/bin/cp", "-cR", bundle, app)
            (app / "Contents/Info.plist").write_bytes(
                plistlib.dumps({**info, "CFBundleIdentifier": identity})
            )
            (app / local_sandbox.PROFILE_PATH).write_text(
                local_sandbox.runner_profile(app, _case_root(identity))
            )
            command(
                "/usr/bin/codesign",
                "--force",
                "--sign",
                "-",
                "--timestamp=none",
                "--entitlements",
                output / "AlfredRunner.entitlements.plist",
                app,
            )
        slot_rows.append(
            {
                "index": index,
                "bundle_id": identity,
                "path": f"slots/{index:02d}.app",
                "entrypoints": RUNNER_ENTRYPOINTS,
            }
        )
    probe_app = package / "Probe.app"
    command("/bin/cp", "-cR", bundle, probe_app)
    probe_id = f"local.agent-alfred.runner.{package_id[:24]}.probe"
    (probe_app / "Contents/Info.plist").write_bytes(
        plistlib.dumps({**info, "CFBundleIdentifier": probe_id})
    )
    (probe_app / local_sandbox.PROFILE_PATH).write_text(
        local_sandbox.runner_profile(probe_app, _case_root(probe_id))
    )
    command(
        "/usr/bin/codesign",
        "--force",
        "--sign",
        "-",
        "--timestamp=none",
        "--entitlements",
        output / "AlfredRunner.entitlements.plist",
        probe_app,
    )
    manifest = {
        "contract": BUNDLE_CONTRACT,
        "version": 3,
        "boundary": boundary,
        "package_id": package_id,
        "source_id": source_id,
        "entrypoints": ENTRYPOINTS,
        "slots": slot_rows,
        "probe": {
            "bundle_id": probe_id,
            "path": "Probe.app",
            "entrypoints": RUNNER_ENTRYPOINTS,
        },
        "files": bundle_inventory(package),
    }
    if not verify(source_candidate, REPO):
        raise ValueError("native_source_candidate_changed_during_build")
    manifest_path = output / "bundle-manifest.json"
    manifest_path.write_bytes(encode(manifest) + b"\n")
    result = verify_bundle(package, manifest)
    report = {
        "candidate_id": result["candidate_id"],
        "source_candidate_id": source_id,
        "bundle_root": str(package),
        "manifest": str(manifest_path),
        "inspection": result["inspection"],
        "actual_isolation": result["actual_isolation"],
        "run_grant": False,
        "auth_helper_invoked": False,
        "runner_launched": False,
        "files": len(manifest["files"]),
        "macho_images": len(binaries) * (slots + 1) + len(owner_binaries),
        "slot_count": slots,
        "production_distributions": production,
    }
    (output / "build-report.json").write_bytes(encode(report) + b"\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--python-prefix", required=True, type=Path)
    parser.add_argument("--site-packages", required=True, type=Path)
    parser.add_argument(
        "--slots",
        type=int,
        choices=(1, 30),
        default=30,
        help="1 is inspection smoke only; production requires 30",
    )
    parser.add_argument(
        "--with-mcp",
        action="store_true",
        help="Include locked optional MCP validation dependencies",
    )
    args = parser.parse_args()
    print(json.dumps(build(**vars(args)), indent=2))


if __name__ == "__main__":
    main()
