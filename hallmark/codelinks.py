"""Link catalog tables and columns to the file and line of the source code that mentions them.

Each line is split into identifier tokens (dotted names kept whole, backticks stripped, case
insensitive) and looked up in a name map, so cost grows with file size, not with columns times
lines. A column hit is `qualified` when a dotted token ends in the column and its qualifier names
the table (`table`, `schema.table` or the full name); it is `bare` when the column name appears on
its own in a file that also names the table. Bare hits are candidates, never proofs: SQL aliases and
dict keys hide the table.
"""
import os
import re


SOURCE_SUFFIXES = (".sql", ".py", ".scala", ".sc", ".r", ".yml", ".yaml")
SKIP_DIRS = {"node_modules", "__pycache__", "venv", "build", "dist"}
MAX_BYTES = 1_000_000
TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")


def _aliases(table_fqn):
    catalog, schema, tail = table_fqn.lower().split(".")
    return {table_fqn.lower(), f"{schema}.{tail}", tail}


def _source_files(root):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS)
        for name in sorted(files):
            path = os.path.join(dirpath, name)
            if name.lower().endswith(SOURCE_SUFFIXES) and os.path.getsize(path) <= MAX_BYTES:
                yield path


def link_code(conn, src_root):
    """Rebuild code_ref and table_ref from src_root. Returns {column_refs, table_refs}."""
    by_name = {}
    for fqn, name, table_fqn in conn.execute("SELECT fqn, name, table_fqn FROM columns"):
        by_name.setdefault(name.lower(), []).append((fqn, table_fqn))
    alias_of = {}
    for (table_fqn,) in conn.execute("SELECT fqn FROM tables"):
        for alias in _aliases(table_fqn):
            alias_of.setdefault(alias, set()).add(table_fqn)

    column_rows, table_rows = {}, {}
    for path in _source_files(src_root):
        rel = os.path.relpath(path, src_root).replace(os.sep, "/")
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
        tokens = [TOKEN.findall(line.replace("`", "")) for line in lines]
        mentioned = set()
        for number, toks in enumerate(tokens, 1):
            for tok in toks:
                low = tok.lower()
                for name in (low, low.rpartition(".")[0]):
                    for table_fqn in alias_of.get(name, ()):
                        mentioned.add(table_fqn)
                        table_rows[(table_fqn, rel, number)] = lines[number - 1].strip()[:200]
        for number, toks in enumerate(tokens, 1):
            snippet = lines[number - 1].strip()[:200]
            for tok in toks:
                low = tok.lower()
                prefix, _, last = low.rpartition(".")
                for col_fqn, table_fqn in by_name.get(last, ()):
                    if prefix and prefix in _aliases(table_fqn):
                        kind = "qualified"
                    elif table_fqn in mentioned:
                        kind = "bare"
                    else:
                        continue
                    key = (col_fqn, rel, number)
                    if column_rows.get(key, ("", ""))[0] != "qualified":
                        column_rows[key] = (kind, snippet)
    with conn:
        conn.execute("DELETE FROM code_ref")
        conn.execute("DELETE FROM table_ref")
        conn.executemany(
            "INSERT INTO code_ref VALUES (?, ?, ?, ?, ?)",
            [(c, p, n, k, s) for (c, p, n), (k, s) in sorted(column_rows.items())],
        )
        conn.executemany(
            "INSERT INTO table_ref VALUES (?, ?, ?, ?)",
            [(t, p, n, s) for (t, p, n), s in sorted(table_rows.items())],
        )
    return {"column_refs": len(column_rows), "table_refs": len(table_rows)}


def code_refs(conn, column_fqn):
    return [
        {"path": p, "line": n, "kind": k, "snippet": s}
        for p, n, k, s in conn.execute(
            "SELECT path, line, kind, snippet FROM code_ref WHERE column_fqn = ? "
            "ORDER BY kind DESC, path, line",
            (column_fqn,),
        )
    ]


def table_refs(conn, table_fqn):
    return [
        {"path": p, "line": n, "snippet": s}
        for p, n, s in conn.execute(
            "SELECT path, line, snippet FROM table_ref WHERE table_fqn = ? ORDER BY path, line",
            (table_fqn,),
        )
    ]
