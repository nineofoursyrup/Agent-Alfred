"""Bounded literal Han-bigram recall, only after an empty original FTS search."""

import re

METHOD = "han-bigram-fallback-v1"
MAX_QUERY_CHARACTERS = 128
MAX_FRAGMENTS = 32
MAX_CANDIDATES = 1000
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002fa1f]+")


def fragments(text):
    if not text or len(text) > MAX_QUERY_CHARACTERS:
        return ()
    values = tuple(dict.fromkeys(
        run[i:i + 2] for run in _HAN.findall(text) for i in range(len(run) - 1)
    ))
    # Do not silently truncate a query into a different relevance criterion.
    return values if len(values) <= MAX_FRAGMENTS else ()


def search_rows(store, text, *, subject=None):
    parts = fragments(text)
    if not parts:
        return []
    table = store.table
    columns = {"facts": ("subject", "fact"), "episodes": ("summary",)}[table]
    term = " OR ".join(f"instr({column}, ?) > 0" for column in columns)
    score = " + ".join(f"CASE WHEN {term} THEN 1 ELSE 0 END" for _ in parts)
    scope, params = "", []
    if subject is not None:
        scope = " WHERE subject = ?"
        params.append(subject)
    # The bounded inner selection precedes literal matching. No FTS grammar,
    # wildcard expansion, cross-store score, or persistent derivative index.
    sql = (
        f"WITH candidates AS MATERIALIZED (SELECT rowid AS position, * "
        f"FROM {table}{scope} ORDER BY rowid DESC LIMIT ?) "
        f"SELECT *, -({score}) AS rank FROM candidates "
        "WHERE rank < 0 ORDER BY rank, position DESC"
    )
    params.append(MAX_CANDIDATES)
    params.extend(part for part in parts for _ in columns)
    return store._rows(sql, params)
