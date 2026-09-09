"""Read-only Skill directory snapshot; names resolve exclusively through the index."""

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path

NAME = re.compile(r"[a-zA-Z0-9_-]{1,64}\Z")


@dataclass(frozen=True)
class SkillMeta:
    name: str
    description: str
    source: str
    overrides_builtin: bool = False


def parse_skill(text):
    match = re.match(r"\A\ufeff?---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if match is None:
        raise ValueError("skill_frontmatter_missing")
    fields = {}
    for line in match[1].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator or key.strip() in fields:
            raise ValueError("invalid_skill_frontmatter")
        value = value.strip()
        if value.startswith('"'):
            value = json.loads(value)
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("''", "'")
        fields[key.strip()] = value
    if not isinstance(fields.get("name"), str) or not NAME.fullmatch(fields["name"]):
        raise ValueError("invalid_skill_name")
    if (
        not isinstance(fields.get("description"), str)
        or not fields["description"].strip()
    ):
        raise ValueError("invalid_skill_description")
    return fields["name"], fields["description"], text[match.end() :]


def read_directory(root, source, excluded=(), checkpoint=None):
    entries = {}
    if not root.exists():
        return entries
    if root.is_symlink() or not root.is_dir():
        raise ValueError("invalid_skill_directory")
    for path in root.rglob("SKILL.md"):
        if checkpoint is not None:
            checkpoint()
        if path in excluded:
            continue
        if path.is_symlink():
            raise ValueError("skill_symlink_not_supported")
        for parent in path.parents:
            if parent == root:
                break
            if parent.is_symlink():
                raise ValueError("skill_symlink_not_supported")
        text = path.read_text(encoding="utf-8")
        if checkpoint is not None:
            checkpoint()
        name, description, body = parse_skill(text)
        if name in entries:
            raise ValueError("duplicate_skill_name")
        entries[name] = (SkillMeta(name, description, source), body, text)
    return entries


class SkillCatalog:
    def __init__(self, *, builtin: Path, user: Path, excluded=()):
        builtins = read_directory(builtin, "builtin")
        users = read_directory(user, "user", excluded)
        self._entries = dict(builtins)
        for name, (meta, body, text) in users.items():
            self._entries[name] = (
                replace(meta, overrides_builtin=name in builtins),
                body,
                text,
            )

    def list(self):
        return tuple(entry[0] for entry in self._entries.values())

    def load(self, name):
        return self._entries[name][1]
