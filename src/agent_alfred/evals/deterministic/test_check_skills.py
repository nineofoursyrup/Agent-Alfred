"""Lock Skill-check CLI behaviour used by CI."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[4]
_SCRIPTS = _REPO / "scripts"


def _load_check_skills():
    scripts = str(_SCRIPTS)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    path = _SCRIPTS / "check_skills.py"
    spec = importlib.util.spec_from_file_location("check_skills", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_skills = _load_check_skills()


def _write_skill(root: Path, body: str) -> None:
    dest = root / "src" / "agent_alfred" / "skills" / "demo"
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text(body, encoding="utf-8")


def test_empty_tree_is_pass(monkeypatch, capsys, tmp_path: Path) -> None:
    monkeypatch.setattr(check_skills, "repo_root", lambda: tmp_path)
    assert check_skills.main() == 0
    assert capsys.readouterr().out == "PASS: skill check (empty tree; no SKILL.md)\n"


def test_valid_skill_md_is_pass(monkeypatch, capsys, tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "---\nname: demo\ndescription: does a thing\n---\nbody\n",
    )
    monkeypatch.setattr(check_skills, "repo_root", lambda: tmp_path)
    assert check_skills.main() == 0
    assert capsys.readouterr().out == "PASS: skill check (1 SKILL.md file(s))\n"


def test_missing_name_fails(monkeypatch, capsys, tmp_path: Path) -> None:
    _write_skill(tmp_path, "---\ndescription: does a thing\n---\n")
    monkeypatch.setattr(check_skills, "repo_root", lambda: tmp_path)
    assert check_skills.main() == 1
    out = capsys.readouterr().out
    assert "missing 'name' in frontmatter" in out
    assert out.strip().endswith("FAIL: skill check (1 error(s))")


def test_missing_description_fails(monkeypatch, capsys, tmp_path: Path) -> None:
    _write_skill(tmp_path, "---\nname: demo\n---\n")
    monkeypatch.setattr(check_skills, "repo_root", lambda: tmp_path)
    assert check_skills.main() == 1
    out = capsys.readouterr().out
    assert "missing 'description' in frontmatter" in out
    assert out.strip().endswith("FAIL: skill check (1 error(s))")


def test_missing_frontmatter_fails(monkeypatch, capsys, tmp_path: Path) -> None:
    _write_skill(tmp_path, "# just markdown\n")
    monkeypatch.setattr(check_skills, "repo_root", lambda: tmp_path)
    assert check_skills.main() == 1
    out = capsys.readouterr().out
    assert "missing YAML frontmatter delimited by '---'" in out
    assert out.strip().endswith("FAIL: skill check (1 error(s))")


def test_quoted_scalars_are_accepted() -> None:
    body = "---\nname: \"demo\"\ndescription: 'it''s fine'\n---\n"
    assert check_skills.validate_skill_text(body) == []


def test_trailing_content_after_quoted_scalar_fails() -> None:
    expected = [
        "line 1: unsupported frontmatter form "
        "(trailing content after quoted scalar: 'junk')"
    ]
    double = '---\nname: "demo" junk\ndescription: d\n---\n'
    single = "---\nname: 'demo' junk\ndescription: d\n---\n"
    assert check_skills.validate_skill_text(double) == expected
    assert check_skills.validate_skill_text(single) == expected
