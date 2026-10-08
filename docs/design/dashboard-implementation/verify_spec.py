"""Read-only validation of this documentation handoff; not a product test."""

import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
ARCHIVE = "9716e0c395c603ccc262284de577cfb8df3f8d48"
PRODUCT = "22c8720e1ec874fd012cc79b086cd8aace4c79b6"
SPEC = "DASHBOARD-IMPLEMENTATION-SPEC-r1"
MAP_PATH = "docs/design/issue-92/acceptance-map.json"
INDEX_PATH = "docs/design/issue-92/source-index.json"
MANIFEST_PATH = "docs/design/issue-92/manifest.json"
EXPECTED_KINDS = {
    "user_path": 72,
    "counterexample": 128,
    "normative_section": 81,
    "migration_capability": 22,
    "interface_gap": 6,
}
EXPECTED_DEPS = {
    "S01": [],
    "S02": [],
    "S03": ["S01", "S02"],
    "S04": ["S01", "S02"],
    **{f"S{i:02}": ["S02"] for i in range(5, 11)},
    "S11": [f"S{i:02}" for i in range(1, 11)],
}


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    failures = []
    counts = {}

    def require(condition, message):
        if not condition:
            failures.append(message)

    def fixed_file(path, expected, commits):
        data = (ROOT / path).read_bytes()
        require(len(data) == expected["bytes"], f"byte count: {path}")
        require(digest(data) == expected["sha256"], f"file digest: {path}")
        for commit in commits:
            require(
                git("show", f"{commit}:{path}") == data,
                f"fixed commit differs: {commit}:{path}",
            )

    source_index = read_json(ROOT / INDEX_PATH)
    source_map = read_json(ROOT / MAP_PATH)
    manifest = read_json(ROOT / MANIFEST_PATH)
    trace = read_json(HERE / "traceability.json")
    require(trace["specification"] == SPEC, "wrong specification identity")
    require(trace["archive_commit"] == ARCHIVE, "wrong archive commit")
    require(trace["product_baseline"] == PRODUCT, "wrong product baseline")
    require(trace["product_result"] == "NOT RUN", "product result changed")

    for key, path in (
        ("source_index", INDEX_PATH),
        ("source_map", MAP_PATH),
        ("archive_manifest", MANIFEST_PATH),
    ):
        require(trace[key]["path"] == path, f"source pointer: {key}")
        fixed_file(path, trace[key], [ARCHIVE])
    total = 0
    for source in source_index["sources"]:
        require(
            trace["source_commits"][str(source["issue"])] == source["commit"],
            f"source commit: {source['issue']}",
        )
        for doc in source["documents"]:
            fixed_file(doc["path"], doc, [source["commit"], ARCHIVE])
            total += 1
    counts["fixed_source_files"] = total
    require(total == 31, "fixed source file count")
    for doc in manifest["files"]:
        fixed_file(doc["path"], doc, [ARCHIVE])
    counts["archive_manifest_files"] = len(manifest["files"])
    require(counts["archive_manifest_files"] == 37, "archive manifest file count")

    items = source_map["source_items"]
    source_ids = [item["source_id"] for item in items]
    require(
        len(set(source_ids)) == len(source_ids) == 309, "source IDs not unique/full"
    )
    for item in items:
        lines = (
            (ROOT / item["source_document"]).read_text(encoding="utf-8").splitlines()
        )
        start = item["source_line"]
        end = item.get("source_end_line", start)
        require(1 <= start <= end <= len(lines), f"source lines: {item['source_id']}")
        excerpt = "\n".join(lines[start - 1 : end])
        if "source_end_line" in item:
            excerpt += "\n"
        require(
            digest(excerpt.encode()) == item["source_text_sha256"],
            f"source excerpt: {item['source_id']}",
        )
        require(
            item["result"] == "NOT RUN", f"source product result: {item['source_id']}"
        )
        require(
            item["integration_owner"] == "S11",
            f"integration owner: {item['source_id']}",
        )
    kinds = dict(Counter(item["source_kind"] for item in items))
    require(kinds == EXPECTED_KINDS, "source kind counts")
    require(trace["source_counts"] == EXPECTED_KINDS, "index source counts")
    counts.update(source_items=len(items), source_kinds=kinds)

    acs = sorted(
        profile["acceptance_id"] for profile in source_map["evidence_profiles"].values()
    )
    mces = source_map["migration_counterexamples"]
    chains = [f"G{i:02}" for i in range(1, 9)]
    require(acs == [f"AC{i:02}" for i in range(1, 27)], "acceptance IDs")
    require(
        [item["id"] for item in mces] == [f"MCE-{i:02}" for i in range(1, 13)],
        "migration counterexample IDs",
    )
    require(all(item["result"] == "NOT RUN" for item in mces), "MCE product result")
    require(trace["integration_chains"] == chains, "G01-G08 integration coverage")
    integration_text = (ROOT / "docs/design/issue-92/ACCEPTANCE.md").read_text()
    require(
        sorted(set(re.findall(r"\| (G\d{2}) \|", integration_text))) == chains,
        "source integration chain IDs",
    )
    counts.update(
        acceptance_criteria=len(acs),
        migration_counterexamples=len(mces),
        integration_chains=len(chains),
    )

    original_deps = {
        entry["id"]: entry["hard_dependencies"] for entry in source_map["slices"]
    }
    new_deps = {entry["id"]: entry["hard_dependencies"] for entry in trace["slices"]}
    require(original_deps == new_deps == EXPECTED_DEPS, "hard dependency graph changed")
    require(len(trace["slices"]) == 11, "slice count")
    visited, active = set(), set()

    def visit(node):
        if node in active:
            failures.append(f"dependency cycle: {node}")
            return
        if node in visited:
            return
        active.add(node)
        for dependency in new_deps[node]:
            visit(dependency)
        active.remove(node)
        visited.add(node)

    for entry in trace["slices"]:
        sid = entry["id"]
        visit(sid)
        owned = [item for item in items if sid in item["owners"]]
        owned_mces = [item for item in mces if sid in item["owners"]]
        expected_acs = sorted(
            {ac for item in owned + owned_mces for ac in item["acceptance_ids"]}
        )
        require(
            entry["source_ids"] == [item["source_id"] for item in owned],
            f"owner mapping: {sid}",
        )
        require(entry["acceptance_ids"] == expected_acs, f"AC mapping: {sid}")
        require(
            entry["migration_counterexample_ids"]
            == [item["id"] for item in owned_mces],
            f"MCE mapping: {sid}",
        )
        require(entry["product_result"] == "NOT RUN", f"slice product result: {sid}")
        require(
            entry["integration_source_ids"] == (source_ids if sid == "S11" else []),
            f"whole-source integration coverage: {sid}",
        )
        require(
            entry["integration_acceptance_ids"] == (acs if sid == "S11" else []),
            f"whole-AC integration coverage: {sid}",
        )
        expected_chains = (
            chains
            if sid == "S11"
            else sorted(
                {chain for item in owned for chain in item["integration_chains"]}
            )
        )
        require(entry["integration_chains"] == expected_chains, f"chain mapping: {sid}")
    counts["slices"] = len(new_deps)
    counts["direct_source_ownerships"] = {
        entry["id"]: len(entry["source_ids"]) for entry in trace["slices"]
    }

    interface_text = (HERE / "INTERFACES.md").read_text(encoding="utf-8")
    interface_ids = set(re.findall(r"^## (I\d{2})：", interface_text, flags=re.M))
    require(interface_ids == {f"I{i:02}" for i in range(10)}, "interface sections")
    for entry in trace["slices"]:
        require(
            set(entry["interface_sections"]) <= interface_ids,
            f"interface reference: {entry['id']}",
        )
    spec_sections = re.findall(
        r"^## (.+)$", (HERE / "SPEC.md").read_text(encoding="utf-8"), flags=re.M
    )
    require(
        spec_sections
        == [
            "Problem Statement",
            "Solution",
            "User Stories",
            "Implementation Decisions",
            "Testing Decisions",
            "Out of Scope",
            "Further Notes",
        ],
        "to-spec template sections",
    )
    link_count = 0
    for document in sorted(HERE.glob("*.md")):
        content = re.sub(r"```.*?```", "", document.read_text(), flags=re.S)
        for target in re.findall(r"\[[^\]\n]+\]\(([^)\n]+)\)", content):
            parsed = urlsplit(target.strip("<>"))
            if parsed.scheme or not parsed.path:
                continue
            local = (document.parent / unquote(parsed.path)).resolve()
            require(
                local.is_relative_to(ROOT),
                f"outside repository link: {document.name}: {target}",
            )
            require(local.exists(), f"missing link: {document.name}: {target}")
            link_count += 1
    counts["local_links"] = link_count

    protected_diff = (
        git(
            "diff",
            "--name-only",
            ARCHIVE,
            "--",
            ".",
            ":(exclude)docs/design/dashboard-implementation/**",
        )
        .decode()
        .splitlines()
    )
    require(
        not protected_diff, f"changed outside specification package: {protected_diff}"
    )
    product_paths = [
        "src",
        "tests",
        "scripts",
        ".github",
        "pyproject.toml",
        "uv.lock",
        "package.json",
        "package-lock.json",
    ]
    product_diff = (
        git("diff", "--name-only", PRODUCT, "--", *product_paths).decode().splitlines()
    )
    require(not product_diff, f"product baseline changed: {product_diff}")
    untracked_outside = [
        name
        for name in git("ls-files", "--others", "--exclude-standard")
        .decode()
        .splitlines()
        if not name.startswith("docs/design/dashboard-implementation/")
    ]
    require(not untracked_outside, f"untracked outside package: {untracked_outside}")
    whitespace_errors = []
    files = []
    for path in sorted(HERE.iterdir()):
        if path.is_file() and path.name != "STATIC-CHECKS.json":
            data = path.read_bytes()
            for number, line in enumerate(data.decode("utf-8").splitlines(), 1):
                if line.rstrip(" \t") != line:
                    whitespace_errors.append(f"{path.name}:{number}")
            files.append(
                {
                    "path": str(path.relative_to(ROOT)),
                    "bytes": len(data),
                    "sha256": digest(data),
                }
            )
    require(not whitespace_errors, f"trailing whitespace: {whitespace_errors}")
    result = {
        "specification": SPEC,
        "scope": (
            "documentation static checks only; "
            "no product execution or independent product review"
        ),
        "observed_utc": datetime.now(timezone.utc).isoformat(),
        "archive_commit": ARCHIVE,
        "product_baseline": PRODUCT,
        "checkout_head": git("rev-parse", "HEAD").decode().strip(),
        "result": "FAIL" if failures else "PASS",
        "counts": counts,
        "dag": "acyclic"
        if len(visited) == 11 and not any("cycle" in f for f in failures)
        else "invalid",
        "protected_files_changed": protected_diff,
        "product_files_changed": product_diff,
        "untracked_outside_specification": untracked_outside,
        "whitespace_errors": whitespace_errors,
        "all_product_verification": "NOT RUN",
        "independent_product_reviews": "NOT RUN",
        "specification_files": files,
        "self_hash_excluded": "STATIC-CHECKS.json",
        "failures": failures,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(
            json.dumps(
                {
                    "result": "FAIL",
                    "scope": "documentation static checks",
                    "tooling_error": str(error),
                    "all_product_verification": "NOT RUN",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        sys.exit(2)
