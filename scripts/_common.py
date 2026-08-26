"""Shared helpers for repository check scripts. No network."""

from __future__ import annotations

from pathlib import Path

SKIP_DIRS = frozenset({"__pycache__", ".git", ".venv"})


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def strip_unquoted_comment(s: str) -> str:
    """Strip a `#` comment that is unquoted on a single line.

    `#` starts a comment only at index 0 or when the previous character is
    space or tab. `.isspace()` is intentionally not used: YAML 1.2 `s-white`
    is SP/TAB, and a hash inside a token such as `password=foo#bar` is part
    of the value. Unicode whitespace is not a comment delimiter here.
    """
    in_single = False
    in_double = False
    for i, ch in enumerate(s):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if i == 0 or s[i - 1] in " \t":
                return s[:i]
    return s


def read_utf8(path: Path, errors: list[str], label: str) -> str | None:
    """Read UTF-8 text, appending a stable error string on failure."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"{label}: cannot read file: {exc}")
    except UnicodeDecodeError as exc:
        errors.append(f"{label}: not valid UTF-8: {exc}")
    return None
