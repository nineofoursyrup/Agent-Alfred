"""Request controls and immutable, whole-document Skill input (SKILL-SPEC-r1)."""

import hashlib
import json
import re
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.rules_inline.backticks import backtick

from .catalog import NAME

MAX_SKILLS = 3
MAX_BODY = 8000
MAX_SECTION = 16000
SKILL_INSTRUCTIONS = (
    "Skills are default procedures. The current user's explicit instructions take "
    "precedence. Skills do not grant tool permissions. Order does not establish "
    "instruction priority. Applicable format constraints apply to the entire final "
    "reply. "
    "When exact lines or an exclusive format are required, do not add introductions, "
    "Markdown wrappers, explanations, disclaimers, or follow-up offers outside that "
    "format. Check the complete reply against those constraints before responding. "
    "Combine compatible requirements; if a conflict affects "
    "execution or delivery, ask the user and pause dependent actions while continuing "
    "unaffected work. Only SKILL.md bodies are loaded: attachments and scripts are "
    "not read or executed; obtain them only through existing authorized tools and "
    "report missing resources.\n"
)


class SkillPreparationError(Exception):
    def __init__(self, reason, name=None):
        self.reason = reason
        self.name = name
        super().__init__(
            f"Skill 准备失败：{name + '：' if name else ''}{reason}。"
            "请修正 /skills 首行或 Skill 文档后重新提交。"
        )


@dataclass(frozen=True)
class SkillControl:
    mode: str
    task: str
    names: tuple[str, ...] = ()
    applied: bool = False


def parse_control(message):
    first, separator, task = message.partition("\n")
    if not re.match(r"^/skills(?:[ \t\r]|$)", first):
        return SkillControl("auto", message)
    tokens = []
    rest = first[len("/skills") :].removesuffix("\r")
    while rest:
        match = re.match(
            r'[ \t]+("[a-zA-Z0-9_-]{1,64}"|[a-zA-Z0-9_-]{1,64})(?=[ \t]|$)', rest
        )
        if match is None:
            if not rest.strip(" \t"):
                break
            raise SkillPreparationError("invalid_control_syntax")
        tokens.append(match[1])
        rest = rest[match.end() :]
    if not tokens:
        raise SkillPreparationError("missing_names")
    if not separator or not task.strip():
        raise SkillPreparationError("missing_task")
    if tokens == ["off"]:
        return SkillControl("disabled", task, applied=True)
    names = tuple(dict.fromkeys(token.strip('"') for token in tokens))
    if len(names) > MAX_SKILLS:
        raise SkillPreparationError("too_many_skills")
    return SkillControl("explicit", task, names, True)


@dataclass(frozen=True)
class LoadedSkill:
    name: str
    source: str
    overrides_builtin: bool
    body: str

    def metadata(self):
        return dict(
            name=self.name,
            source=self.source,
            overrides_builtin=self.overrides_builtin,
            body_sha256=hashlib.sha256(self.body.encode("utf-8")).hexdigest(),
            fingerprint_version="sha256-utf8-v1",
            codepoints=len(self.body),
            status="loaded",
        )


def skill_section(skills):
    if not skills:
        return ""
    return (
        "<skills>\n"
        + SKILL_INSTRUCTIONS
        + "".join(
            f'\n<skill name="{s.name}" source="{s.source}" '
            f'overrides_builtin="{str(s.overrides_builtin).lower()}">\n'
            + s.body
            + "\n</skill>\n"
            for s in skills
        )
        + "</skills>"
    )


@dataclass(frozen=True)
class SkillSnapshot:
    loaded: tuple[LoadedSkill, ...] = ()

    @property
    def section(self):
        return skill_section(self.loaded)

    def metadata(self):
        return [skill.metadata() for skill in self.loaded]


class RunSkills:
    """One preparation owner; metadata contains no body copies."""

    def __init__(self, message, catalog, state, redactor):
        self.catalog = catalog
        self.state = state
        self.redactor = redactor
        self.evidence = dict(
            mode="explicit",
            status="preparing",
            selected=[],
            loaded=[],
            excluded=[],
            control_line_applied=False,
        )
        self.snapshot = SkillSnapshot()
        try:
            self.control = parse_control(message)
        except SkillPreparationError as error:
            self.fail(error)
            raise
        self.evidence.update(
            mode="model" if self.control.mode == "auto" else self.control.mode,
            control_line_applied=self.control.applied,
        )
        self.publish()

    def publish(self):
        self.evidence["notice"] = skill_notice(self.evidence)
        self.state["skills"] = self.redactor.redact_jsonable(self.evidence)

    def fail(self, error):
        self.evidence.update(status="failed", reason=error.reason, loaded=[])
        self.publish()

    def prepare(self, select=None):
        if self.control.mode == "disabled":
            self.evidence.update(status="prepared")
            self.publish()
            return self.snapshot
        try:
            if self.catalog is None:
                raise OSError("unavailable")
            entries = {meta.name: meta for meta in self.catalog.list()}
        except Exception:
            if self.control.mode == "explicit":
                error = SkillPreparationError("catalog_unavailable")
                self.fail(error)
                raise error from None
            self.evidence.update(
                mode="unavailable", reason="catalog_unavailable", status="prepared"
            )
            self.publish()
            return self.snapshot
        if self.control.mode == "explicit":
            names = self.control.names
        else:
            listing = json.dumps(
                [
                    dict(name=name, description=entries[name].description)
                    for name in sorted(entries)
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if not entries:
                names = ()
                self.evidence["mode"] = "empty"
            else:
                reason = (
                    "catalog_count_limit"
                    if len(entries) > 100
                    else "catalog_character_limit"
                    if len(listing) > 16000
                    else None
                )
                if reason is None and select is not None:
                    try:
                        names, reason = select(listing, entries)
                    except Exception:
                        self.evidence.update(
                            status="failed", reason="selector_preparation_failed"
                        )
                        self.publish()
                        raise
                if reason is not None or select is None:
                    names = fallback_names(self.control.task, entries)
                    self.evidence.update(
                        mode="fallback", reason=reason or "step_budget"
                    )
                    self.evidence["excluded"].extend(
                        dict(name=name, reason="count_limit_exceeded")
                        for name in names[MAX_SKILLS:]
                    )
                    names = names[:MAX_SKILLS]
                else:
                    self.evidence["mode"] = "model"
        self.evidence["selected"] = list(names)
        loaded = []
        for name in names:
            reason = None
            meta = entries.get(name)
            if meta is None or not NAME.fullmatch(name):
                reason = "unknown_name"
            else:
                try:
                    body = self.catalog.load(name)
                    candidate = LoadedSkill(
                        name, meta.source, meta.overrides_builtin, body
                    )
                    if len(body) > MAX_BODY:
                        reason = "body_limit_exceeded"
                    elif len(skill_section((*loaded, candidate))) > MAX_SECTION:
                        reason = "section_limit_exceeded"
                except Exception:
                    reason = "load_failed"
            if reason:
                self.evidence["excluded"].append(dict(name=name, reason=reason))
                if self.control.mode == "explicit":
                    error = SkillPreparationError(reason, name)
                    self.fail(error)
                    raise error
            else:
                loaded.append(candidate)
        self.snapshot = SkillSnapshot(tuple(loaded))
        self.evidence.update(status="prepared", loaded=self.snapshot.metadata())
        self.publish()
        return self.snapshot


def parse_selection(text, entries):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    result = json.loads(text, object_pairs_hook=unique)
    if not isinstance(result, dict) or set(result) != {"skills"}:
        raise ValueError("invalid_selection")
    names = result["skills"]
    if (
        not isinstance(names, list)
        or len(names) > MAX_SKILLS
        or any(not isinstance(name, str) or name not in entries for name in names)
        or len(set(names)) != len(names)
    ):
        raise ValueError("invalid_selection")
    return tuple(names)


def _record_code_span(state, silent):
    start, count = state.pos, len(state.tokens)
    matched = backtick(state, silent)
    if matched and not silent and len(state.tokens) > count:
        token = state.tokens[-1]
        if token.type == "code_inline":
            token.meta["source_span"] = (start, state.pos)
    return matched


def _without_inline_code(token):
    # Preserve raw prose, punctuation and markup, including clause negations.
    # Rendering/flattening child tokens could invent a direct phrase across
    # emphasis or links. Record spans using the parser's actual backtick rule.
    text = token.content
    for child in reversed(token.children or ()):
        if "source_span" in child.meta:
            start, end = child.meta["source_span"]
            text = text[:start] + re.sub(r"[^\n]", " ", text[start:end]) + text[end:]
    return text


def fallback_names(task, entries):
    """Finite lexical rule on prose, no semantic/description matching."""
    visible, quote_depth = [], 0
    parser = MarkdownIt("commonmark")
    parser.inline.ruler.at("backticks", _record_code_span)
    for token in parser.parse(task):
        if token.type == "blockquote_open":
            quote_depth += 1
        elif token.type == "blockquote_close":
            quote_depth -= 1
        elif token.type == "inline" and not quote_depth:
            # Fences and indented code have no inline token; the parser also
            # identifies lazy quotes and nested list fences before this point.
            visible.append(_without_inline_code(token))
    text = "\n".join(visible)
    names = "|".join(re.escape(name) for name in sorted(entries, key=len, reverse=True))
    if not names:
        return ()
    pattern = re.compile(
        r"(?:使用|用|按)[ \t]*(" + names + r")(?![a-zA-Z0-9_-])"
        r"|(?i:\buse)[ \t]+(" + names + r")(?![a-zA-Z0-9_-])"
    )
    negative = re.compile(
        r"不|别|勿|禁止|无需|无须|\b(?:no|not|never|without|don['’]t)\b", re.IGNORECASE
    )
    selected = []
    for clause in re.split(r"[，,。.!！?？;；\n]", text):
        matches = list(pattern.finditer(clause))
        masked = clause
        for match in reversed(matches):
            group = 1 if match[1] is not None else 2
            start, end = match.span(group)
            masked = masked[:start] + "SKILLNAME" + masked[end:]
        if negative.search(masked):
            continue
        for match in matches:
            name = match[1] or match[2]
            if name not in selected:
                selected.append(name)
    return tuple(selected)


def skill_notice(evidence):
    if evidence.get("status") == "failed":
        return None
    if evidence.get("mode") in ("fallback", "unavailable") or evidence.get("excluded"):
        return (
            "Skill 已降级：部分流程未使用或选择器不可用；请查看运行详情中的本次输入。"
        )
    return None
