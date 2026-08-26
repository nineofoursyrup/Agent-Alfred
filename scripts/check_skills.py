"""Validate Skill files used as procedural memory. No network."""

from __future__ import annotations

import re
from pathlib import Path

SKILL_FILENAME = "SKILL.md"
PACKAGE_SKILLS = Path("src") / "agent_alfred" / "skills"
ROOT_SKILLS = Path("skills")
IGNORED_DIRS = frozenset({"__pycache__", ".git", ".venv"})

_FRONTMATTER = re.compile(
    r"\A\ufeff?---[ \t]*\r?\n(.*?\r?\n)---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)

_PLAIN_INT = re.compile(r"[-+]?(?:0|[1-9][0-9]*)")
_PLAIN_FLOAT = re.compile(
    r"[-+]?(?:[0-9]*\.[0-9]+|[0-9]+\.[0-9]*)(?:[eE][-+]?[0-9]+)?"
)

_DOUBLE_ESCAPES = {
    "\\": "\\",
    '"': '"',
    "/": "/",
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "0": "\0",
    "a": "\a",
    "b": "\b",
}


class SkillCheckError(Exception):
    """A single Skill file failed validation."""


class YamlParseError(ValueError):
    """Frontmatter is not valid YAML (subset used for Skill files)."""


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


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
            if any(part in IGNORED_DIRS for part in path.parts):
                continue
            if path.is_file():
                found.append(path)
    return sorted(found)


def extract_frontmatter(text: str) -> str:
    match = _FRONTMATTER.match(text)
    if match is None:
        raise SkillCheckError("missing YAML frontmatter delimited by '---'")
    return match.group(1)


def load_yaml(text: str) -> object:
    return _YamlLoader(text).load()


def validate_skill_text(text: str) -> list[str]:
    errors: list[str] = []
    try:
        raw = extract_frontmatter(text)
        data = load_yaml(raw)
    except (SkillCheckError, YamlParseError) as exc:
        if isinstance(exc, YamlParseError):
            return [f"invalid YAML: {exc}"]
        return [str(exc)]
    if not isinstance(data, dict):
        return ["frontmatter must be a YAML mapping"]
    for field in ("name", "description"):
        if field not in data:
            errors.append(f"missing {field!r} in frontmatter")
            continue
        value = data[field]
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{field!r} must be a non-empty string")
    return errors


def _split_indent(line: str, lineno: int) -> tuple[int, str]:
    n = 0
    for ch in line:
        if ch == " ":
            n += 1
        elif ch == "\t":
            raise YamlParseError(f"line {lineno}: tabs in indentation")
        else:
            break
    return n, line[n:]


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t":
        i += 1
    return i


def _strip_unquoted_comment(s: str) -> str:
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


def _parse_double_quoted(s: str, i: int) -> tuple[str, int]:
    if i >= len(s) or s[i] != '"':
        raise YamlParseError("expected double-quoted string")
    i += 1
    out: list[str] = []
    while i < len(s):
        ch = s[i]
        if ch == '"':
            return "".join(out), i + 1
        if ch == "\\":
            if i + 1 >= len(s):
                raise YamlParseError("unterminated escape in double-quoted string")
            esc = s[i + 1]
            if esc in _DOUBLE_ESCAPES:
                out.append(_DOUBLE_ESCAPES[esc])
                i += 2
                continue
            if esc == "x" and i + 3 < len(s):
                hexpart = s[i + 2 : i + 4]
                if re.fullmatch(r"[0-9a-fA-F]{2}", hexpart):
                    out.append(chr(int(hexpart, 16)))
                    i += 4
                    continue
            if esc == "u" and i + 5 < len(s):
                hexpart = s[i + 2 : i + 6]
                if re.fullmatch(r"[0-9a-fA-F]{4}", hexpart):
                    out.append(chr(int(hexpart, 16)))
                    i += 6
                    continue
            raise YamlParseError(f"unknown escape \\{esc} in double-quoted string")
        if ch == "\n":
            raise YamlParseError("unterminated double-quoted string")
        out.append(ch)
        i += 1
    raise YamlParseError("unterminated double-quoted string")


def _parse_single_quoted(s: str, i: int) -> tuple[str, int]:
    if i >= len(s) or s[i] != "'":
        raise YamlParseError("expected single-quoted string")
    i += 1
    out: list[str] = []
    while i < len(s):
        ch = s[i]
        if ch == "'":
            if i + 1 < len(s) and s[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), i + 1
        if ch == "\n":
            raise YamlParseError("unterminated single-quoted string")
        out.append(ch)
        i += 1
    raise YamlParseError("unterminated single-quoted string")


def _interpret_plain(raw: str) -> object:
    s = raw.strip()
    if s == "" or s in {"~", "null", "Null", "NULL"}:
        return None
    if s in {"true", "True", "TRUE"}:
        return True
    if s in {"false", "False", "FALSE"}:
        return False
    if _PLAIN_INT.fullmatch(s):
        try:
            return int(s)
        except ValueError:
            return s
    if _PLAIN_FLOAT.fullmatch(s):
        try:
            return float(s)
        except ValueError:
            return s
    return s


def _parse_quoted(s: str, i: int) -> tuple[str, int]:
    i = _skip_ws(s, i)
    if i >= len(s):
        raise YamlParseError("expected quoted string")
    if s[i] == '"':
        return _parse_double_quoted(s, i)
    if s[i] == "'":
        return _parse_single_quoted(s, i)
    raise YamlParseError("expected quoted string")


def _parse_flow(s: str, i: int) -> tuple[object, int]:
    i = _skip_ws(s, i)
    if i >= len(s):
        raise YamlParseError("unexpected end of flow node")
    ch = s[i]
    if ch == '"':
        return _parse_double_quoted(s, i)
    if ch == "'":
        return _parse_single_quoted(s, i)
    if ch == "{":
        return _parse_flow_map(s, i)
    if ch == "[":
        return _parse_flow_seq(s, i)
    start = i
    while i < len(s) and s[i] not in ",]}#{'\"\n":
        i += 1
    raw = s[start:i].rstrip(" \t")
    if not raw:
        raise YamlParseError("empty flow scalar")
    if raw.endswith(":"):
        raise YamlParseError("unexpected ':' in flow scalar")
    return _interpret_plain(raw), i


def _parse_flow_map(s: str, i: int) -> tuple[dict[object, object], int]:
    i = _skip_ws(s, i)
    if i >= len(s) or s[i] != "{":
        raise YamlParseError("expected '{'")
    i += 1
    result: dict[object, object] = {}
    while True:
        i = _skip_ws(s, i)
        if i < len(s) and s[i] == "#":
            raise YamlParseError("comment inside single-line flow mapping")
        if i >= len(s):
            raise YamlParseError("unterminated flow mapping")
        if s[i] == "}":
            return result, i + 1
        key, i = _parse_flow_key(s, i)
        i = _skip_ws(s, i)
        if i >= len(s) or s[i] != ":":
            raise YamlParseError("expected ':' in flow mapping")
        i += 1
        value, i = _parse_flow(s, i)
        result[key] = value
        i = _skip_ws(s, i)
        if i >= len(s):
            raise YamlParseError("unterminated flow mapping")
        if s[i] == ",":
            i += 1
            i = _skip_ws(s, i)
            if i < len(s) and s[i] == "}":
                return result, i + 1
            continue
        if s[i] == "}":
            return result, i + 1
        raise YamlParseError("expected ',' or '}' in flow mapping")


def _parse_flow_seq(s: str, i: int) -> tuple[list[object], int]:
    i = _skip_ws(s, i)
    if i >= len(s) or s[i] != "[":
        raise YamlParseError("expected '['")
    i += 1
    result: list[object] = []
    while True:
        i = _skip_ws(s, i)
        if i >= len(s):
            raise YamlParseError("unterminated flow sequence")
        if s[i] == "]":
            return result, i + 1
        value, i = _parse_flow(s, i)
        result.append(value)
        i = _skip_ws(s, i)
        if i >= len(s):
            raise YamlParseError("unterminated flow sequence")
        if s[i] == ",":
            i += 1
            i = _skip_ws(s, i)
            if i < len(s) and s[i] == "]":
                return result, i + 1
            continue
        if s[i] == "]":
            return result, i + 1
        raise YamlParseError("expected ',' or ']' in flow sequence")


def _parse_flow_key(s: str, i: int) -> tuple[object, int]:
    i = _skip_ws(s, i)
    if i >= len(s):
        raise YamlParseError("expected mapping key")
    if s[i] in {'"', "'"}:
        return _parse_quoted(s, i)
    start = i
    while i < len(s) and s[i] not in ":{}[],#\n":
        i += 1
    key = s[start:i].strip()
    if not key:
        raise YamlParseError("empty mapping key")
    return _interpret_plain(key), i


def _parse_key_and_rest(content: str) -> tuple[object, str]:
    s = content.rstrip()
    if not s:
        raise YamlParseError("empty mapping entry")
    if s[0] in {'"', "'"}:
        key, idx = _parse_quoted(s, 0)
        rest = s[idx:].lstrip(" \t")
        if not rest.startswith(":"):
            raise YamlParseError("expected ':' after quoted key")
        return key, rest[1:].strip()
    colon = s.find(":")
    if colon < 0:
        raise YamlParseError("expected ':' in mapping entry")
    key = s[:colon].strip()
    if not key or any(ch in key for ch in "{}[]"):
        raise YamlParseError("invalid mapping key")
    return _interpret_plain(key), s[colon + 1 :].strip()


def _looks_like_mapping(content: str) -> bool:
    if not content or content.startswith("-"):
        return False
    if content[0] in {'"', "'"}:
        try:
            _parse_key_and_rest(content)
        except YamlParseError:
            return False
        return True
    return ":" in content


def _parse_scalar_line(content: str) -> object:
    s = content.strip()
    if not s or s.startswith("#"):
        return None
    if s[0] in {'"', "'"}:
        value, idx = _parse_quoted(s, 0)
        leftover = _strip_unquoted_comment(s[idx:]).strip()
        if leftover:
            raise YamlParseError(f"trailing content after quoted scalar: {leftover!r}")
        return value
    return _interpret_plain(_strip_unquoted_comment(s))


def _fold_block(lines: list[str]) -> str:
    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        if line == "":
            if current:
                paragraphs.append(" ".join(current))
                current = []
            paragraphs.append("")
        else:
            current.append(line)
    if current:
        paragraphs.append(" ".join(current))
    text = "\n".join(paragraphs)
    if text and not text.endswith("\n"):
        text += "\n"
    return text


class _YamlLoader:
    def __init__(self, text: str) -> None:
        self.lines = text.splitlines()

    def load(self) -> object:
        self.i = 0
        self._skip_ignorable()
        if self.i >= len(self.lines):
            return None
        node = self._parse_node(min_indent=0)
        self._skip_ignorable()
        if self.i < len(self.lines):
            raise YamlParseError(
                f"line {self.i + 1}: extra content after YAML document"
            )
        return node

    def _skip_ignorable(self) -> None:
        while self.i < len(self.lines):
            _, content = _split_indent(self.lines[self.i], self.i + 1)
            if content == "" or content.startswith("#"):
                self.i += 1
                continue
            break

    def _parse_node(self, min_indent: int) -> object:
        self._skip_ignorable()
        if self.i >= len(self.lines):
            return None
        indent, content = _split_indent(self.lines[self.i], self.i + 1)
        if indent < min_indent:
            return None
        if content.startswith("{") or content.startswith("["):
            value, end = _parse_flow(content, 0)
            leftover = content[end:].strip()
            if leftover and not leftover.startswith("#"):
                raise YamlParseError(
                    f"line {self.i + 1}: trailing content after flow node"
                )
            self.i += 1
            return value
        if content == "-" or content.startswith("- "):
            return self._parse_sequence(indent)
        if _looks_like_mapping(content):
            return self._parse_mapping(indent)
        self.i += 1
        return _parse_scalar_line(content)

    def _parse_mapping(self, indent: int) -> dict[object, object]:
        result: dict[object, object] = {}
        while True:
            self._skip_ignorable()
            if self.i >= len(self.lines):
                break
            line_indent, content = _split_indent(self.lines[self.i], self.i + 1)
            if line_indent < indent:
                break
            if line_indent > indent:
                raise YamlParseError(f"line {self.i + 1}: invalid indentation")
            if content == "-" or content.startswith("- "):
                break
            if not _looks_like_mapping(content):
                raise YamlParseError(f"line {self.i + 1}: expected mapping entry")
            key, rest = _parse_key_and_rest(content)
            self.i += 1
            result[key] = self._value_from_rest(rest, indent)
        return result

    def _parse_sequence(self, indent: int) -> list[object]:
        items: list[object] = []
        while True:
            self._skip_ignorable()
            if self.i >= len(self.lines):
                break
            line_indent, content = _split_indent(self.lines[self.i], self.i + 1)
            if line_indent != indent:
                if line_indent < indent:
                    break
                raise YamlParseError(f"line {self.i + 1}: invalid indentation")
            if not (content == "-" or content.startswith("- ")):
                break
            if content == "-":
                rest_stripped = ""
                key_indent = indent + 2
            else:
                after_dash = content[1:]
                spaces = len(after_dash) - len(after_dash.lstrip(" "))
                rest_stripped = after_dash.strip()
                key_indent = indent + 1 + spaces
            self.i += 1
            if rest_stripped == "":
                items.append(self._parse_node(min_indent=indent + 1))
            elif _looks_like_mapping(rest_stripped):
                key, value_rest = _parse_key_and_rest(rest_stripped)
                first = {key: self._value_from_rest(value_rest, key_indent)}
                first.update(self._parse_mapping(key_indent))
                items.append(first)
            else:
                items.append(self._value_from_rest(rest_stripped, indent))
        return items

    def _value_from_rest(self, rest: str, parent_indent: int) -> object:
        rest = rest.strip()
        if rest == "" or rest.startswith("#"):
            nested = self._parse_node(min_indent=parent_indent + 1)
            return nested
        if rest[0] in "|>":
            return self._parse_block_scalar(parent_indent, rest)
        if rest[0] in "{[":
            value, end = _parse_flow(rest, 0)
            leftover = rest[end:].strip()
            if leftover and not leftover.startswith("#"):
                raise YamlParseError(
                    f"line {self.i}: trailing content after flow value"
                )
            return value
        return _parse_scalar_line(rest)

    def _parse_block_scalar(self, parent_indent: int, header: str) -> str:
        header = _strip_unquoted_comment(header).strip()
        if not header or header[0] not in "|>":
            raise YamlParseError(f"line {self.i}: invalid block scalar header")
        folded = header[0] == ">"
        flags = header[1:]
        if flags not in {"", "+", "-"}:
            raise YamlParseError(
                f"line {self.i}: invalid block scalar header {header!r}"
            )
        chomp = flags[:1]
        collected: list[str] = []
        content_indent: int | None = None
        while self.i < len(self.lines):
            raw = self.lines[self.i]
            if raw.strip() == "":
                collected.append("")
                self.i += 1
                continue
            indent, content = _split_indent(raw, self.i + 1)
            if indent <= parent_indent:
                break
            if content_indent is None:
                content_indent = indent
            if indent < content_indent:
                break
            collected.append(" " * (indent - content_indent) + content)
            self.i += 1
        while collected and collected[-1] == "":
            collected.pop()
        if folded:
            text = _fold_block(collected)
        else:
            text = "\n".join(collected)
            if collected:
                text += "\n"
        if chomp == "+":
            return text
        if chomp == "-":
            return text.rstrip("\n")
        if text:
            return text.rstrip("\n") + "\n"
        return text


def main() -> int:
    root = repo_root()
    skill_files = find_skill_files(root)
    if not skill_files:
        print("PASS: skill check (empty tree; no SKILL.md)")
        return 0

    errors: list[str] = []
    for path in skill_files:
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"{rel}: cannot read file: {exc}")
            continue
        except UnicodeDecodeError as exc:
            errors.append(f"{rel}: not valid UTF-8: {exc}")
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
