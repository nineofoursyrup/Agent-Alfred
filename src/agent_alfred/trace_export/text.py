"""Bounded UTF-8 text protection, including matches across IO boundaries."""

import codecs
import re

# A key is bounded even if every character uses a JSON unicode escape.
_NAMES = ("api_key", "authorization", "token", "password", "secret", "access_token")
KEY = re.compile(
    '"(?:'
    + "|".join(
        "".join("(?:" + c + r"|\\u%04x)" % ord(c) for c in name) for name in _NAMES
    )
    + ')"',
    re.I,
)


def sensitive_fields(chunks):
    """Mask JSON-shaped sensitive values without buffering their body."""
    pending = ""
    hiding = False
    after_key = False
    string = False
    escaped = False
    depth = 0
    started = False
    for chunk in chunks:
        pending += chunk
        while pending:
            if after_key:
                whitespace = len(pending) - len(pending.lstrip())
                yield pending[:whitespace]
                pending = pending[whitespace:]
                if not pending:
                    break
                after_key = False
                if pending[0] == ":":
                    yield ':"***"'
                    pending = pending[1:]
                    hiding = True
                    string = escaped = started = False
                    depth = 0
                    continue
            if not hiding:
                match = KEY.search(pending)
                if match:
                    yield pending[: match.end()]
                    pending = pending[match.end() :]
                    after_key = True
                else:
                    # Hold only the bounded key; whitespace is streamed in after_key.
                    cut = max(0, len(pending) - 128)
                    if cut:
                        yield pending[:cut]
                        pending = pending[cut:]
                    break
            else:
                consumed = 0
                complete = False
                for char in pending:
                    if not started:
                        if char.isspace():
                            consumed += 1
                            continue
                        started = True
                        string = char == '"'
                        depth = 1 if char in "{[" else 0
                        consumed += 1
                        continue
                    if string:
                        if escaped:
                            escaped = False
                        elif char == "\\":
                            escaped = True
                        elif char == '"':
                            string = False
                            if depth == 0:
                                consumed += 1
                                complete = True
                                break
                    elif char == '"':
                        string = True
                    elif char in "{[":
                        depth += 1
                    elif char in "}]":
                        if depth == 0:
                            complete = True
                            break
                        depth -= 1
                        if depth == 0:
                            consumed += 1
                            complete = True
                            break
                    elif depth == 0 and (char == "," or char.isspace()):
                        complete = True
                        break
                    consumed += 1
                pending = pending[consumed:]
                if complete:
                    hiding = False
                else:
                    break
    if not hiding:
        yield pending


def protect(chunks, sanitizer):
    decoder = codecs.getincrementaldecoder("utf-8")("strict")

    def decoded():
        for raw in chunks:
            yield decoder.decode(raw)
        yield decoder.decode(b"", final=True)

    if sanitizer.mode == "share":
        # Validate all source text, even though its representation is a placeholder.
        for _ in decoded():
            pass
        yield sanitizer.text("").encode()
        return
    replacements = {v: a for (_, v), a in sanitizer.aliases.items() if v}
    replacements.update({v: "[环境信息]" for v in sanitizer.hidden if v})
    replacements.update({v: "***" for v in sanitizer.redactor.secrets if v})
    keys = sorted(replacements, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(k) for k in keys)) if keys else None
    overlap = max((len(k) for k in keys), default=1)
    pending = ""
    for text in sensitive_fields(decoded()):
        pending += text
        cut = max(0, len(pending) - overlap)
        if pattern is None:
            if cut:
                yield pending[:cut].encode()
                pending = pending[cut:]
            continue
        position = 0
        out = []
        for match in pattern.finditer(pending):
            if match.start() >= cut:
                break
            out.extend((pending[position : match.start()], replacements[match.group()]))
            position = match.end()
        cut = max(cut, position)
        out.append(pending[position:cut])
        if cut:
            yield "".join(out).encode()
            pending = pending[cut:]
    if pattern:
        pending = pattern.sub(lambda m: replacements[m.group()], pending)
    yield pending.encode()
