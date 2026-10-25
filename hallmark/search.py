"""Search over names, comments and tags: FTS5 with BM25, no model call.

The query is sanitised into quoted prefix terms (user text never reaches FTS5 syntax), tried as
AND first and as OR when AND finds nothing. Column weights favour the name, then tags, then the
comment. This is the whole "semantic" level shipped here; embeddings are an open decision.
"""
import re

from .store import split_identifier

KINDS = ("table", "column", "model", "feature")
WEIGHTS = "0.0, 0.0, 5.0, 1.0, 2.0"  # fqn, kind (unindexed), name, text, tags
STOPWORDS = {"a", "an", "the", "of", "in", "on", "for", "to", "and", "or", "is", "are", "with"}


def terms(query):
    words = re.findall(r"[A-Za-z0-9]+", split_identifier(query or ""))
    kept = [w.lower() for w in words if w.lower() not in STOPWORDS]
    return kept or [w.lower() for w in words]


def _match(words, joiner):
    return f" {joiner} ".join(f'"{w}"*' for w in words)


def _run(conn, match, kind, limit):
    sql = (
        "SELECT fqn, kind, bm25(search, " + WEIGHTS + ") AS score FROM search "
        "WHERE search MATCH ?"
    )
    params = [match]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY score LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def _comment(conn, fqn, kind):
    table = {"table": "tables", "column": "columns", "model": "models"}.get(kind)
    if not table:
        return None
    row = conn.execute(f"SELECT comment FROM {table} WHERE fqn = ?", (fqn,)).fetchone()
    return row[0] if row else None


def search(conn, query, kind=None, limit=10):
    """Return [{fqn, kind, score, comment}] best first; [] for an empty query."""
    if kind is not None and kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    words = terms(query)
    if not words:
        return []
    rows = _run(conn, _match(words, "AND"), kind, limit)
    if not rows and len(words) > 1:
        rows = _run(conn, _match(words, "OR"), kind, limit)
    return [
        {"fqn": fqn, "kind": k, "score": round(score, 4), "comment": _comment(conn, fqn, k)}
        for fqn, k, score in rows
    ]
