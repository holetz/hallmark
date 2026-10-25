"""Prototype of the lightweight catalog index: SQLite + FTS5, standard library only.

A connector is any function returning a snapshot dict (tables, columns, lineage); the fixture
loader below is the synthetic one. Nothing here talks to a workspace.
"""
import argparse
import json
import os
import random
import re
import resource
import sqlite3
import tempfile
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS tables (fqn TEXT PRIMARY KEY, comment TEXT, last_altered TEXT);
CREATE TABLE IF NOT EXISTS columns (
  fqn TEXT PRIMARY KEY, table_fqn TEXT NOT NULL, name TEXT NOT NULL, type TEXT, comment TEXT);
CREATE INDEX IF NOT EXISTS columns_table ON columns (table_fqn);
CREATE TABLE IF NOT EXISTS lineage (
  kind TEXT NOT NULL, src TEXT NOT NULL, dst TEXT NOT NULL, PRIMARY KEY (kind, src, dst));
CREATE TABLE IF NOT EXISTS code_ref (
  column_fqn TEXT NOT NULL, path TEXT NOT NULL, line INTEGER NOT NULL, kind TEXT NOT NULL,
  snippet TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS code_ref_column ON code_ref (column_fqn);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(fqn UNINDEXED, kind UNINDEXED, text);
"""

SOURCE_SUFFIXES = (".sql", ".py")


def load_snapshot(fixture_dir):
    with open(os.path.join(fixture_dir, "metadata.json"), encoding="utf-8") as handle:
        return json.load(handle)


def connect(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def _write_table(conn, table, existed):
    fqn = table["fqn"]
    if existed:
        conn.execute("DELETE FROM columns WHERE table_fqn = ?", (fqn,))
        conn.execute("DELETE FROM search WHERE fqn = ? OR fqn LIKE ?", (fqn, fqn + ".%"))
    conn.execute(
        "INSERT OR REPLACE INTO tables VALUES (?, ?, ?)",
        (fqn, table.get("comment"), table["last_altered"]),
    )
    conn.execute(
        "INSERT INTO search VALUES (?, 'table', ?)", (fqn, f"{fqn} {table.get('comment', '')}")
    )
    for col in table["columns"]:
        col_fqn = f"{fqn}.{col['name']}"
        conn.execute(
            "INSERT INTO columns VALUES (?, ?, ?, ?, ?)",
            (col_fqn, fqn, col["name"], col.get("type"), col.get("comment")),
        )
        conn.execute(
            "INSERT INTO search VALUES (?, 'column', ?)",
            (col_fqn, f"{col['name']} {col.get('comment', '')}"),
        )


def refresh(conn, snapshot):
    """Rewrite only the tables whose last_altered changed; drop vanished ones.

    Lineage edges are only added: the source system keeps a rolling window, so an edge once seen
    is kept.
    """
    stored = dict(conn.execute("SELECT fqn, last_altered FROM tables"))
    seen = set()
    rewritten = 0
    with conn:
        for table in snapshot["tables"]:
            seen.add(table["fqn"])
            if stored.get(table["fqn"]) != table["last_altered"]:
                _write_table(conn, table, table["fqn"] in stored)
                rewritten += 1
        dropped = set(stored) - seen
        for fqn in dropped:
            conn.execute("DELETE FROM columns WHERE table_fqn = ?", (fqn,))
            conn.execute("DELETE FROM tables WHERE fqn = ?", (fqn,))
            conn.execute("DELETE FROM search WHERE fqn = ? OR fqn LIKE ?", (fqn, fqn + ".%"))
        conn.executemany(
            "INSERT OR IGNORE INTO lineage VALUES (?, ?, ?)",
            [(e["kind"], e["src"], e["dst"]) for e in snapshot.get("lineage", [])],
        )
    return {"rewritten": rewritten, "dropped": len(dropped)}


def link_code(conn, src_root):
    """Rebuild code_ref from the source files under src_root.

    A qualified token (table.column or fqn.column) is a 'qualified' hit. In a file that mentions a
    table (by fqn or by its short name) a bare, word-bounded column name is a 'bare' hit, because
    SQL aliases and dict keys hide the table. Bare hits are candidates, not proofs.
    """
    columns = conn.execute(
        "SELECT c.fqn, c.name, c.table_fqn FROM columns c"
    ).fetchall()
    by_table = {}
    for col_fqn, name, table_fqn in columns:
        by_table.setdefault(table_fqn, []).append((col_fqn, name))
    rows = []
    for dirpath, _dirs, files in os.walk(src_root):
        for fname in sorted(files):
            if not fname.endswith(SOURCE_SUFFIXES):
                continue
            path = os.path.join(dirpath, fname)
            with open(path, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
            text = "\n".join(lines)
            rel = os.path.relpath(path, src_root)
            for table_fqn, cols in by_table.items():
                short = table_fqn.split(".", 1)[1]
                tail = table_fqn.rsplit(".", 1)[1]
                mentioned = re.search(rf"\b({re.escape(table_fqn)}|{re.escape(short)})\b", text)
                for col_fqn, name in cols:
                    qualified = re.compile(
                        rf"\b({re.escape(table_fqn)}|{re.escape(short)}|{re.escape(tail)})\.{re.escape(name)}\b"
                    )
                    bare = re.compile(rf"\b{re.escape(name)}\b")
                    for number, line in enumerate(lines, 1):
                        if qualified.search(line):
                            rows.append((col_fqn, rel, number, "qualified", line.strip()))
                        elif mentioned and bare.search(line):
                            rows.append((col_fqn, rel, number, "bare", line.strip()))
    with conn:
        conn.execute("DELETE FROM code_ref")
        conn.executemany("INSERT INTO code_ref VALUES (?, ?, ?, ?, ?)", rows)
    return len(rows)


def code_refs(conn, column_fqn):
    return conn.execute(
        "SELECT path, line, kind FROM code_ref WHERE column_fqn = ? ORDER BY path, line",
        (column_fqn,),
    ).fetchall()


def search(conn, query, limit=10):
    return conn.execute(
        "SELECT fqn, kind FROM search WHERE search MATCH ? ORDER BY rank LIMIT ?", (query, limit)
    ).fetchall()


def stats(conn):
    names = ("tables", "columns", "lineage", "code_ref")
    return {n: conn.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in names}


def synthetic_snapshot(tables, columns_per_table, version=0, seed=7):
    rng = random.Random(seed)
    words = ["amount", "status", "created", "owner", "region", "score", "flag", "code"]
    out = []
    for t in range(tables):
        fqn = f"demo_catalog.s{t % 20}.t{t}"
        cols = [
            {
                "name": f"c{c}_{rng.choice(words)}",
                "type": "string",
                "comment": f"{rng.choice(words)} {rng.choice(words)} value",
            }
            for c in range(columns_per_table)
        ]
        altered = f"2026-01-01T00:00:{version if t < 10 else 0:02d}"
        out.append({"fqn": fqn, "comment": f"table {t}", "last_altered": altered, "columns": cols})
    return {"tables": out, "lineage": []}


def bench(tables=2000, columns_per_table=20):
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        conn = connect(db)
        snap = synthetic_snapshot(tables, columns_per_table)
        start = time.perf_counter()
        refresh(conn, snap)
        build_s = time.perf_counter() - start
        start = time.perf_counter()
        changed = refresh(conn, synthetic_snapshot(tables, columns_per_table, version=1))
        incr_s = time.perf_counter() - start
        start = time.perf_counter()
        hits = search(conn, "amount AND region")
        search_s = time.perf_counter() - start
        size_mb = os.path.getsize(db) / 1e6
        peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        print(f"tables={tables} columns={tables * columns_per_table}")
        print(f"full_build_s={build_s:.2f} incremental_s={incr_s:.3f} rewritten={changed['rewritten']}")
        print(f"search_s={search_s:.4f} hits={len(hits)}")
        print(f"db_mb={size_mb:.1f} peak_rss_mb={peak_mb:.0f}")
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", help="directory with metadata.json and src/")
    parser.add_argument("--db", default=":memory:")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--search")
    parser.add_argument("--bench", action="store_true")
    args = parser.parse_args(argv)
    if args.bench:
        bench()
        return 0
    if not args.fixture:
        parser.error("--fixture or --bench is required")
    conn = connect(args.db)
    refresh(conn, load_snapshot(args.fixture))
    link_code(conn, os.path.join(args.fixture, "src"))
    if args.stats:
        print(json.dumps(stats(conn)))
    if args.search:
        for fqn, kind in search(conn, args.search):
            print(kind, fqn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
