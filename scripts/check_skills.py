"""Validate Skill files used as procedural memory. No network."""

from __future__ import annotations

import re
from pathlib import Path

from _common import SKIP_DIRS, read_utf8, repo_root, strip_unquoted_comment

SKILL_FILENAME = "SKILL.md"
PACKAGE_SKILLS = Path("src") / "agent_alfred" / "skills"
ROOT_SKILLS = Path("skills")

_FRONTMATTER = re.compile(
    r"\A\ufeff?---[ \t]*\r?\n(.*?\r?\n)---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SkillCheckError(Exception):
    """A single Skill file failed validation."""


class FrontmatterError(ValueError):
    """Frontmatter is not a supported top-level key: value mapping."""


def skill_roots(root: Path) -> list[Path]:
    """Package skills dir, plus repo-root skills/ if present.

    Single-user: there is no per-user namespace directory.
    """
    roots: list[Path] = []
    packaged = root / PACKAGE_SKILLS
    if packaged.is_dir():
        roots.append(packaged)
    extra = root / ROOT_SKILLS
    if extra.is_dir():
        roots.append(extra)
    return roots


def find_skill_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for base in skill_roots(root):
        for path in base.rglob(SKILL_FILENAME):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.is_file():
                found.append(path)
    return sorted(found)


def extract_frontmatter(text: str) -> str:
    match = _FRONTMATTER.match(text)
    if match is None:
        raise SkillCheckError("missing YAML frontmatter delimited by '---'")
    return match.group(1)


def parse_frontmatter_mapping(raw: str) -> dict[str, str]:
    """Parse top-level `key: value` lines (bare, single, or double quoted)."""
    result: dict[str, str] = {}
    for lineno, line in enumerate(raw.splitlines(), 1):
        stripped = strip_unquoted_comment(line).rstrip()
        if not stripped.strip():
            continue
        if stripped[0] in " \t":
            raise FrontmatterError(
                f"line {lineno}: unsupported frontmatter form "
                "(nested or indented keys are not supported)"
            )
        content = stripped.strip()
        if content[0] in "{[|>":
            raise FrontmatterError(
                f"line {lineno}: unsupported frontmatter form "
                f"(flow/block scalar {content[0]!r} is not supported)"
            )
        if ":" not in content:
            raise FrontmatterError(
                f"line {lineno}: unsupported frontmatter form "
                "(expected top-level 'key: value')"
            )
        key, _, rest = content.partition(":")
        key = key.strip()
        if not _KEY.fullmatch(key):
            raise FrontmatterError(
                f"line {lineno}: unsupported frontmatter form "
                f"(invalid key {key!r})"
            )
        result[key] = _parse_scalar(rest, lineno)
    return result


def _parse_scalar(raw: str, lineno: int) -> str:
    s = raw.strip()
    if not s:
        return ""
    if s[0] in "{[|>":
        raise FrontmatterError(
            f"line {lineno}: unsupported frontmatter form "
            f"(flow/block scalar {s[0]!r} is not supported)"
        )
    if s[0] == '"':
        return _parse_double_quoted(s, lineno)
    if s[0] == "'":
        return _parse_single_quoted(s, lineno)
    return s


def _reject_trailing(s: str, i: int, lineno: int) -> None:
    leftover = s[i + 1 :].strip()
    if leftover:
        raise FrontmatterError(
            f"line {lineno}: unsupported frontmatter form "
            f"(trailing content after quoted scalar: {leftover!r})"
        )


def _parse_double_quoted(s: str, lineno: int) -> str:
    out: list[str] = []
    i = 1
    while i < len(s):
        ch = s[i]
        if ch == '"':
            _reject_trailing(s, i, lineno)
            return "".join(out)
        if ch == "\\":
            if i + 1 >= len(s):
                raise FrontmatterError(
                    f"line {lineno}: unterminated escape in double-quoted string"
                )
            esc = s[i + 1]
            simple = {"\\": "\\", '"': '"', "n": "\n", "t": "\t"}
            if esc not in simple:
                raise FrontmatterError(
                    f"line {lineno}: unsupported frontmatter form "
                    f"(unknown escape \\{esc})"
                )
            out.append(simple[esc])
            i += 2
            continue
        out.append(ch)
        i += 1
    raise FrontmatterError(f"line {lineno}: unterminated double-quoted string")


def _parse_single_quoted(s: str, lineno: int) -> str:
    out: list[str] = []
    i = 1
    while i < len(s):
        ch = s[i]
        if ch == "'":
            if i + 1 < len(s) and s[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            _reject_trailing(s, i, lineno)
            return "".join(out)
        out.append(ch)
        i += 1
    raise FrontmatterError(f"line {lineno}: unterminated single-quoted string")


def validate_skill_text(text: str) -> list[str]:
    errors: list[str] = []
    try:
        raw = extract_frontmatter(text)
        data = parse_frontmatter_mapping(raw)
    except SkillCheckError as exc:
        return [str(exc)]
    except FrontmatterError as exc:
        return [f"unsupported frontmatter form: {exc}"]
    for field in ("name", "description"):
        if field not in data:
            errors.append(f"missing {field!r} in frontmatter")
            continue
        value = data[field]
        if not value.strip():
            errors.append(f"{field!r} must be a non-empty string")
    return errors


def main() -> int:
    root = repo_root()
    skill_files = find_skill_files(root)
    if not skill_files:
        print("PASS: skill check (empty tree; no SKILL.md)")
        return 0

    errors: list[str] = []
    for path in skill_files:
        rel = path.relative_to(root).as_posix()
        text = read_utf8(path, errors, rel)
        if text is None:
            continue
        for item in validate_skill_text(text):
            errors.append(f"{rel}: {item}")

    if errors:
        for item in errors:
            print(item)
        print(f"FAIL: skill check ({len(errors)} error(s))")
        return 1

    print(f"PASS: skill check ({len(skill_files)} SKILL.md file(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
