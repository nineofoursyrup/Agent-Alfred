"""Lock shared check-script helpers used by both check scripts."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[4]
_SCRIPTS = _REPO / "scripts"


def _load_common():
    scripts = str(_SCRIPTS)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    path = _SCRIPTS / "_common.py"
    spec = importlib.util.spec_from_file_location("_common", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


common = _load_common()


def test_hash_inside_a_token_is_part_of_the_value() -> None:
    # The .env side: a password may legitimately contain '#'.
    assert common.strip_unquoted_comment("KEY=foo#bar") == "KEY=foo#bar"


def test_hash_after_space_or_tab_starts_a_comment() -> None:
    assert common.strip_unquoted_comment("KEY=foo #note") == "KEY=foo "
    assert common.strip_unquoted_comment("KEY=foo\t#note") == "KEY=foo\t"


def test_hash_at_index_zero_starts_a_comment() -> None:
    assert common.strip_unquoted_comment("# whole line") == ""


def test_hash_inside_quotes_is_kept() -> None:
    assert common.strip_unquoted_comment('name: "a#b" ') == 'name: "a#b" '
    assert common.strip_unquoted_comment("name: 'a#b' ") == "name: 'a#b' "
