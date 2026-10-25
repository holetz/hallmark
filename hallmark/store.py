"""The SQLite index: tables, columns, tags, lineage, models, features, code refs and FTS5 search.

Refresh is incremental: a table is rewritten only when its `last_altered` or its tag signature
changed. Lineage is append-only, because the source keeps a rolling window and an edge once seen
stays useful. Standard library only; FTS5 is required.
"""
import hashlib
import json
import re
import sqlite3
import time

from . import snapshot as snapshot_mod

SCHEMA = """
CREATE TABLE IF NOT EXISTS tables (
  fqn TEXT PRIMARY KEY, comment TEXT, last_altered TEXT NOT NULL, tag_sig TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS columns (
  fqn TEXT PRIMARY KEY, table_fqn TEXT NOT NULL, name TEXT NOT NULL, type TEXT, comment TEXT,
  position INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS columns_table ON columns (table_fqn);
CREATE TABLE IF NOT EXISTS tags (
  entity_fqn TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (entity_fqn, key));
CREATE TABLE IF NOT EXISTS lineage (
  kind TEXT NOT NULL, src TEXT NOT NULL, dst TEXT NOT NULL,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, PRIMARY KEY (kind, src, dst));
CREATE INDEX IF NOT EXISTS lineage_dst ON lineage (kind, dst);
CREATE TABLE IF NOT EXISTS models (fqn TEXT PRIMARY KEY, comment TEXT, last_altered TEXT);
CREATE TABLE IF NOT EXISTS model_versions (
  model_fqn TEXT NOT NULL, version TEXT NOT NULL, status TEXT, comment TEXT,
  PRIMARY KEY (model_fqn, version));
CREATE TABLE IF NOT EXISTS features (fqn TEXT PRIMARY KEY, table_fqn TEXT NOT NULL, columns TEXT);
CREATE TABLE IF NOT EXISTS code_ref (
  column_fqn TEXT NOT NULL, path TEXT NOT NULL, line INTEGER NOT NULL, kind TEXT NOT NULL,
  snippet TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS code_ref_column ON code_ref (column_fqn);
CREATE TABLE IF NOT EXISTS table_ref (
  table_fqn TEXT NOT NULL, path TEXT NOT NULL, line INTEGER NOT NULL, snippet TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS table_ref_table ON table_ref (table_fqn);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
  fqn UNINDEXED, kind UNINDEXED, name, text, tags,
  tokenize = 'porter unicode61 remove_diacritics 2');
"""


def split_identifier(name):
    """`order_total` and `orderTotal` both become 'order total'."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return re.sub(r"[_\-.]+", " ", spaced).strip()


def connect(path, readonly=False):
    try:
        if readonly:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        else:
            conn = sqlite3.connect(path)
            conn.executescript(SCHEMA)
        conn.execute("SELECT count(*) FROM search")
    except sqlite3.OperationalError as exc:
        raise RuntimeError(f"cannot open the index ({exc}); FTS5 is required") from exc
    return conn


def _tag_text(tags):
    return " ".join(f"{k} {v}" for k, v in sorted((tags or {}).items()))


def _tag_signature(table):
    parts = [("table", _tag_text(table.get("tags")))]
    parts += [(c["name"], _tag_text(c.get("tags"))) for c in table["columns"]]
    return hashlib.sha1(json.dumps(parts).encode("utf-8")).hexdigest()


def _drop_search(conn, fqn, descendants=True):
    conn.execute("DELETE FROM search WHERE fqn = ?", (fqn,))
    if descendants:
        prefix = fqn + "."
        conn.execute("DELETE FROM search WHERE substr(fqn, 1, ?) = ?", (len(prefix), prefix))


def _index(conn, fqn, kind, name, text, tags=None):
    conn.execute(
        "INSERT INTO search (fqn, kind, name, text, tags) VALUES (?, ?, ?, ?, ?)",
        (fqn, kind, f"{name} {split_identifier(name)}", text or "", _tag_text(tags)),
    )


def _drop_table(conn, fqn):
    conn.execute(
        "DELETE FROM tags WHERE entity_fqn = ? OR (substr(entity_fqn, 1, ?) = ?)",
        (fqn, len(fqn) + 1, fqn + "."),
    )
    conn.execute("DELETE FROM columns WHERE table_fqn = ?", (fqn,))
    _drop_search(conn, fqn)


def _write_table(conn, table):
    fqn = table["fqn"]
    _drop_table(conn, fqn)
    conn.execute(
        "INSERT OR REPLACE INTO tables VALUES (?, ?, ?, ?)",
        (fqn, table.get("comment"), table["last_altered"], _tag_signature(table)),
    )
    tags = table.get("tags") or {}
    conn.executemany("INSERT INTO tags VALUES (?, ?, ?)", [(fqn, k, v) for k, v in tags.items()])
    _index(conn, fqn, "table", fqn.rsplit(".", 1)[1], table.get("comment"), tags)
    for position, col in enumerate(table["columns"]):
        col_fqn = f"{fqn}.{col['name']}"
        conn.execute(
            "INSERT INTO columns VALUES (?, ?, ?, ?, ?, ?)",
            (col_fqn, fqn, col["name"], col.get("type"), col.get("comment"), position),
        )
        col_tags = col.get("tags") or {}
        conn.executemany(
            "INSERT INTO tags VALUES (?, ?, ?)", [(col_fqn, k, v) for k, v in col_tags.items()]
        )
        _index(conn, col_fqn, "column", col["name"], col.get("comment"), col_tags)


def _write_models(conn, models):
    for fqn in [r[0] for r in conn.execute("SELECT fqn FROM models")]:
        _drop_search(conn, fqn, descendants=False)
    conn.execute("DELETE FROM models")
    conn.execute("DELETE FROM model_versions")
    for model in models:
        fqn = model["fqn"]
        conn.execute(
            "INSERT INTO models VALUES (?, ?, ?)", (fqn, model.get("comment"), model.get("last_altered"))
        )
        for version in model.get("versions", []):
            conn.execute(
                "INSERT INTO model_versions VALUES (?, ?, ?, ?)",
                (fqn, str(version["version"]), version.get("status"), version.get("comment")),
            )
        _index(conn, fqn, "model", fqn.rsplit(".", 1)[-1], model.get("comment"))


def _write_features(conn, features):
    for fqn in [r[0] for r in conn.execute("SELECT fqn FROM features")]:
        _drop_search(conn, fqn, descendants=False)
    conn.execute("DELETE FROM features")
    for feature in features:
        fqn = feature["fqn"]
        cols = feature.get("columns", [])
        conn.execute(
            "INSERT INTO features VALUES (?, ?, ?)", (fqn, feature["table_fqn"], json.dumps(cols))
        )
        _index(conn, fqn, "feature", fqn.rsplit(".", 1)[-1], " ".join(cols))


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def refresh(conn, snapshot):
    """Apply a snapshot. Returns {rewritten, dropped, lineage_added}."""
    snapshot_mod.validate(snapshot)
    stored = {fqn: (altered, sig) for fqn, altered, sig in conn.execute(
        "SELECT fqn, last_altered, tag_sig FROM tables")}
    seen = set()
    rewritten = 0
    now = _now()
    with conn:
        for table in snapshot["tables"]:
            fqn = table["fqn"]
            seen.add(fqn)
            if stored.get(fqn) != (table["last_altered"], _tag_signature(table)):
                _write_table(conn, table)
                rewritten += 1
        dropped = set(stored) - seen
        for fqn in dropped:
            _drop_table(conn, fqn)
            conn.execute("DELETE FROM tables WHERE fqn = ?", (fqn,))
        if "models" in snapshot:
            _write_models(conn, snapshot["models"])
        if "features" in snapshot:
            _write_features(conn, snapshot["features"])
        before = conn.execute("SELECT count(*) FROM lineage").fetchone()[0]
        edges = [(e["kind"], e["src"], e["dst"]) for e in snapshot.get("lineage", [])]
        conn.executemany(
            "INSERT OR IGNORE INTO lineage VALUES (?, ?, ?, ?, ?)",
            [(k, s, d, now, now) for k, s, d in edges],
        )
        conn.executemany(
            "UPDATE lineage SET last_seen = ? WHERE kind = ? AND src = ? AND dst = ?",
            [(now, k, s, d) for k, s, d in edges],
        )
        added = conn.execute("SELECT count(*) FROM lineage").fetchone()[0] - before
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('last_refresh', ?)", (now,))
    return {"rewritten": rewritten, "dropped": len(dropped), "lineage_added": added}


def stats(conn):
    names = ("tables", "columns", "tags", "lineage", "models", "features", "code_ref", "table_ref")
    out = {n: conn.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in names}
    row = conn.execute("SELECT value FROM meta WHERE key = 'last_refresh'").fetchone()
    out["last_refresh"] = row[0] if row else None
    return out
