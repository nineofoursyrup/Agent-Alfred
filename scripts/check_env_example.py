"""Check .env.example for required empty keys and source lookups. No network."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ENV_EXAMPLE = ".env.example"
SRC_DIR = Path("src")

REQUIRED_KEYS = (
    "OPENCODE_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "XAI_API_KEY",
    "TAVILY_API_KEY",
)

_ASSIGN = re.compile(
    r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$"
)
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]+$")

# OS / CI / runtime variables that are not project settings.
_WELL_KNOWN = frozenset(
    {
        "ALL_PROXY",
        "CI",
        "COLORTERM",
        "COLUMNS",
        "CONDA_DEFAULT_ENV",
        "CONDA_PREFIX",
        "DISPLAY",
        "EDITOR",
        "FORCE_COLOR",
        "GH_TOKEN",
        "GITHUB_ACTIONS",
        "GITHUB_ENV",
        "GITHUB_OUTPUT",
        "GITHUB_PATH",
        "GITHUB_REF",
        "GITHUB_REPOSITORY",
        "GITHUB_SHA",
        "GITHUB_STEP_SUMMARY",
        "GITHUB_TOKEN",
        "GITHUB_WORKSPACE",
        "HOME",
        "HOSTNAME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "LINES",
        "LOGNAME",
        "NO_COLOR",
        "NO_PROXY",
        "OLDPWD",
        "OSTYPE",
        "PAGER",
        "PATH",
        "PWD",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSAFEPATH",
        "PYTHONSTARTUP",
        "PYTHONUNBUFFERED",
        "RUNNER_ARCH",
        "RUNNER_OS",
        "RUNNER_TEMP",
        "RUNNER_TOOL_CACHE",
        "SHELL",
        "SHLVL",
        "SSH_AUTH_SOCK",
        "SSH_TTY",
        "TEMP",
        "TERM",
        "TERM_PROGRAM",
        "TERMINFO",
        "TMP",
        "TMPDIR",
        "TZ",
        "USER",
        "USERNAME",
        "UV_CACHE_DIR",
        "UV_PYTHON",
        "UV_SYSTEM_PYTHON",
        "VIRTUAL_ENV",
        "VIRTUAL_ENV_PROMPT",
        "VISUAL",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "XDG_STATE_HOME",
    }
)

_SKIP_DIRS = frozenset({"__pycache__", ".venv", ".git"})


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _strip_unquoted_comment(s: str) -> str:
    in_single = False
    in_double = False
    for i, ch in enumerate(s):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if i == 0 or s[i - 1].isspace():
                return s[:i]
    return s


def normalized_env_value(raw: str) -> str:
    """Return the assignment value after stripping quotes and whitespace."""
    s = _strip_unquoted_comment(raw).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in {'"', "'"}:
        s = s[1:-1]
    return s.strip()


def parse_env_example(text: str) -> tuple[dict[str, str], list[str]]:
    """Parse KEY=VALUE lines. Comments are ignored. All values must be empty."""
    keys: dict[str, str] = {}
    errors: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ASSIGN.match(stripped)
        if match is None:
            errors.append(
                f"{ENV_EXAMPLE}:{lineno}: not a comment or assignment: {raw!r}"
            )
            continue
        key, value = match.group(1), match.group(2)
        if normalized_env_value(value) != "":
            errors.append(
                f"{ENV_EXAMPLE}:{lineno}: {key} has a non-empty value "
                "(secrets must not be committed)"
            )
        keys[key] = value
    return keys, errors


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted(node.value)
        if parent is None:
            return node.attr
        return f"{parent}.{node.attr}"
    return None


def _is_environ(node: ast.AST) -> bool:
    dotted = _dotted(node)
    if dotted is None:
        return False
    return dotted == "environ" or dotted.endswith(".environ")


def _call_arg(call: ast.Call, index: int, keyword: str) -> ast.AST | None:
    if len(call.args) > index:
        return call.args[index]
    for kw in call.keywords:
        if kw.arg == keyword:
            return kw.value
    return None


def _lookup_key_node(call: ast.Call) -> ast.AST | None:
    func = call.func
    if isinstance(func, ast.Name):
        if func.id == "getenv":
            return _call_arg(call, 0, "key")
        if func.id == "get_key":
            return _call_arg(call, 1, "key_to_get")
        return None
    if isinstance(func, ast.Attribute):
        if func.attr == "getenv":
            return _call_arg(call, 0, "key")
        if func.attr == "get_key":
            return _call_arg(call, 1, "key_to_get")
        if func.attr == "get" and _is_environ(func.value):
            return _call_arg(call, 0, "key")
    return None


class _EnvLookupVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.hits: list[tuple[int, str]] = []

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if _is_environ(node.value):
            name = _const_str(node.slice)
            if name is not None:
                self.hits.append((node.lineno, name))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        key_node = _lookup_key_node(node)
        if key_node is not None:
            name = _const_str(key_node)
            if name is not None:
                self.hits.append((node.lineno, name))
        self.generic_visit(node)


def is_tracked_env_name(name: str) -> bool:
    if name.endswith("_API_KEY"):
        return True
    if not _ENV_NAME.fullmatch(name):
        return False
    if name in _WELL_KNOWN:
        return False
    return True


def iter_python_files(src_root: Path) -> list[Path]:
    files: list[Path] = []
    for path in src_root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.is_file():
            files.append(path)
    return sorted(files)


def collect_source_env_hits(
    src_root: Path, repo: Path
) -> tuple[dict[str, list[str]], list[str]]:
    """Map tracked env names to source locations."""
    hits: dict[str, list[str]] = {}
    errors: list[str] = []
    for path in iter_python_files(src_root):
        rel = path.relative_to(repo).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"{rel}: cannot read file: {exc}")
            continue
        except UnicodeDecodeError as exc:
            errors.append(f"{rel}: not valid UTF-8: {exc}")
            continue
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError as exc:
            loc = f"{rel}:{exc.lineno}" if exc.lineno else rel
            errors.append(f"{loc}: syntax error while scanning env lookups: {exc.msg}")
            continue
        visitor = _EnvLookupVisitor()
        visitor.visit(tree)
        for lineno, name in visitor.hits:
            if is_tracked_env_name(name):
                hits.setdefault(name, []).append(f"{rel}:{lineno}")
    return hits, errors


def main() -> int:
    root = repo_root()
    example_path = root / ENV_EXAMPLE
    errors: list[str] = []
    example_keys: dict[str, str] = {}

    if not example_path.is_file():
        errors.append(f"{ENV_EXAMPLE} is missing at the repository root")
    else:
        try:
            text = example_path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"{ENV_EXAMPLE}: cannot read file: {exc}")
        except UnicodeDecodeError as exc:
            errors.append(f"{ENV_EXAMPLE}: not valid UTF-8: {exc}")
        else:
            example_keys, parse_errors = parse_env_example(text)
            errors.extend(parse_errors)
            for key in REQUIRED_KEYS:
                if key not in example_keys:
                    errors.append(f"{ENV_EXAMPLE}: missing required key {key}")

    src_root = root / SRC_DIR
    if src_root.is_dir():
        hits, scan_errors = collect_source_env_hits(src_root, root)
        errors.extend(scan_errors)
        for name, locs in sorted(hits.items()):
            if name not in example_keys:
                shown = ", ".join(locs[:5])
                extra = "" if len(locs) <= 5 else f" (+{len(locs) - 5} more)"
                errors.append(
                    f"{name} is looked up in {shown}{extra} but missing from "
                    f"{ENV_EXAMPLE}"
                )
    elif not errors:
        errors.append(f"{SRC_DIR.as_posix()}/ is missing; cannot scan env lookups")

    if errors:
        for item in errors:
            print(item)
        print(f"FAIL: .env.example consistency check ({len(errors)} error(s))")
        return 1

    print("PASS: .env.example consistency check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
