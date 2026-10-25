"""A read-only MCP server over stdio: newline-delimited JSON-RPC 2.0, no dependency.

Methods: initialize, ping, tools/list, tools/call (plus ignored notifications). `handle` is a pure
function of (connection, message) so tests drive it without a process. The server never opens a
network socket and never imports a connector: it answers from the local index only. Every list
tool takes a `limit` capped at MAX_LIMIT and reports `truncated`.
"""
import json
import sys

from . import codelinks, search, store

PROTOCOL = "2025-06-18"
MAX_LIMIT = 50
DEFAULT_LIMIT = 10
SERVER = {"name": "hallmark", "version": "0.1.0"}


def _limit(args):
    value = args.get("limit", DEFAULT_LIMIT)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("limit must be a positive integer")
    return min(value, MAX_LIMIT)


def _cut(items, limit):
    return {"items": items[:limit], "total": len(items), "truncated": len(items) > limit}


def _text(args, key):
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return value


def _tags(conn, fqn):
    return dict(conn.execute("SELECT key, value FROM tags WHERE entity_fqn = ? ORDER BY key", (fqn,)))


def tool_search(conn, args):
    limit = _limit(args)
    hits = search.search(conn, _text(args, "query"), args.get("kind"), limit + 1)
    return _cut(hits, limit)


def tool_get_table(conn, args):
    fqn = _text(args, "fqn")
    row = conn.execute("SELECT comment, last_altered FROM tables WHERE fqn = ?", (fqn,)).fetchone()
    if not row:
        raise LookupError(f"no table {fqn}")
    limit = _limit(args)
    cols = [
        {"name": n, "type": t, "comment": c, "tags": _tags(conn, f"{fqn}.{n}")}
        for n, t, c in conn.execute(
            "SELECT name, type, comment FROM columns WHERE table_fqn = ? ORDER BY position", (fqn,))
    ]
    out = {"fqn": fqn, "comment": row[0], "last_altered": row[1], "tags": _tags(conn, fqn),
           "code_references": _cut(codelinks.table_refs(conn, fqn), limit)}
    out["columns"] = _cut(cols, limit)
    return out


def tool_get_column(conn, args):
    fqn = _text(args, "fqn")
    row = conn.execute("SELECT type, comment FROM columns WHERE fqn = ?", (fqn,)).fetchone()
    if not row:
        raise LookupError(f"no column {fqn}")
    return {"fqn": fqn, "type": row[0], "comment": row[1], "tags": _tags(conn, fqn)}


def tool_code_links(conn, args):
    fqn = _text(args, "fqn")
    limit = _limit(args)
    if conn.execute("SELECT 1 FROM columns WHERE fqn = ?", (fqn,)).fetchone():
        refs = codelinks.code_refs(conn, fqn)
    elif conn.execute("SELECT 1 FROM tables WHERE fqn = ?", (fqn,)).fetchone():
        refs = codelinks.table_refs(conn, fqn)
    else:
        raise LookupError(f"no table or column {fqn}")
    return {"fqn": fqn, "note": "kind 'bare' hits are candidates, not proofs", **_cut(refs, limit)}


def tool_lineage(conn, args):
    fqn = _text(args, "fqn")
    direction = args.get("direction", "upstream")
    if direction not in ("upstream", "downstream"):
        raise ValueError("direction must be 'upstream' or 'downstream'")
    depth = args.get("depth", 1)
    if not isinstance(depth, int) or isinstance(depth, bool) or not 1 <= depth <= 5:
        raise ValueError("depth must be an integer from 1 to 5")
    limit = _limit(args)
    kind = "column" if conn.execute("SELECT 1 FROM columns WHERE fqn = ?", (fqn,)).fetchone() else "table"
    near, far = ("dst", "src") if direction == "upstream" else ("src", "dst")
    seen, frontier, edges = {fqn}, [fqn], []
    for _ in range(depth):
        nxt = []
        for node in frontier:
            for other, first, last in conn.execute(
                f"SELECT {far}, first_seen, last_seen FROM lineage WHERE kind = ? AND {near} = ? "
                f"ORDER BY {far}", (kind, node)):
                edges.append({"from": other if direction == "upstream" else node,
                              "to": node if direction == "upstream" else other,
                              "first_seen": first, "last_seen": last})
                if other not in seen:
                    seen.add(other)
                    nxt.append(other)
        frontier = nxt
    return {"fqn": fqn, "kind": kind, "direction": direction, **_cut(edges, limit)}


def tool_models(conn, args):
    limit = _limit(args)
    models = []
    for fqn, comment, altered in conn.execute("SELECT fqn, comment, last_altered FROM models ORDER BY fqn"):
        versions = [{"version": v, "status": s, "comment": c} for v, s, c in conn.execute(
            "SELECT version, status, comment FROM model_versions WHERE model_fqn = ? ORDER BY version",
            (fqn,))]
        models.append({"fqn": fqn, "comment": comment, "last_altered": altered, "versions": versions})
    features = [{"fqn": f, "table_fqn": t, "columns": json.loads(c or "[]")} for f, t, c in
                conn.execute("SELECT fqn, table_fqn, columns FROM features ORDER BY fqn")]
    return {"models": _cut(models, limit), "features": _cut(features, limit)}


def tool_status(conn, args):
    return store.stats(conn)


def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


_LIMIT = {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "description": "Max items, default 10"}
_FQN = {"type": "string", "description": "Fully qualified name"}
TOOLS = {
    "search": (tool_search, "Search tables, columns, models and features by name, comment and tag "
               "(BM25, stemming, identifier splitting). Returns best matches first.",
               _schema({"query": {"type": "string"}, "kind": {"enum": list(search.KINDS)},
                        "limit": _LIMIT}, ["query"])),
    "get_table": (tool_get_table, "A table with its columns, tags and the code that mentions it.",
                  _schema({"fqn": _FQN, "limit": _LIMIT}, ["fqn"])),
    "get_column": (tool_get_column, "One column: type, comment and tags.",
                   _schema({"fqn": _FQN}, ["fqn"])),
    "code_links": (tool_code_links, "File and line where source code mentions a table or column. "
                   "Hits of kind 'bare' are candidates, not proofs.",
                   _schema({"fqn": _FQN, "limit": _LIMIT}, ["fqn"])),
    "lineage": (tool_lineage, "Walk table or column lineage upstream or downstream.",
                _schema({"fqn": _FQN, "direction": {"enum": ["upstream", "downstream"]},
                         "depth": {"type": "integer", "minimum": 1, "maximum": 5},
                         "limit": _LIMIT}, ["fqn"])),
    "models": (tool_models, "Registered models with versions, and feature tables.",
               _schema({"limit": _LIMIT})),
    "status": (tool_status, "Row counts and the time of the last refresh.", _schema({})),
}


def _result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict,
          "array": list}


def check_args(args, schema):
    """Raise ValueError naming the first argument whose type or enum value breaks the schema."""
    for key, spec in schema.get("properties", {}).items():
        if key not in args:
            continue
        value, declared = args[key], spec.get("type")
        if declared is not None:
            names = declared if isinstance(declared, list) else [declared]
            if not any(isinstance(value, _TYPES[n]) and not (isinstance(value, bool) and n != "boolean")
                       for n in names):
                raise ValueError(f"'{key}' must be of type {' or '.join(names)}")
        if "enum" in spec and value not in spec["enum"]:
            raise ValueError(f"'{key}' must be one of {', '.join(map(str, spec['enum']))}")


def call_tool(tools, target, params, expected=(ValueError, LookupError)):
    """Run one tools/call; None when the tool or arguments are unknown (the caller answers -32602).

    `expected` exceptions are tool errors carrying their own text. Any other exception becomes a
    fixed error, so one bad call never ends the server and no unexpected text reaches the caller.
    """
    if not isinstance(params, dict):
        return None
    name, args = params.get("name"), params.get("arguments")
    args = {} if args is None else args
    if not isinstance(name, str) or name not in tools or not isinstance(args, dict):
        return None
    try:
        check_args(args, tools[name][2])
        body = tools[name][0](target, args)
        return {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}]}
    except expected as exc:
        text = str(exc.args[0]) if exc.args else type(exc).__name__
    except Exception:  # noqa: BLE001 - the server must survive any single call
        text = f"internal error in tool {name}"
    return {"isError": True, "content": [{"type": "text", "text": text}]}


def respond(handler, target, line):
    """Answer one input line: a parse error or an internal error is a response, never a crash."""
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        return _error(None, -32700, "parse error")
    try:
        return handler(target, message)
    except Exception:  # noqa: BLE001
        return _error(message.get("id") if isinstance(message, dict) else None, -32603,
                      "internal error")


def _call(conn, params):
    return call_tool(TOOLS, conn, params)


def handle(conn, message):
    """Return the response for one JSON-RPC message, or None for a notification."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
        return _error(message.get("id") if isinstance(message, dict) else None, -32600,
                      "invalid request")
    id_, method = message.get("id"), message["method"]
    if "id" not in message:
        return None
    params = message.get("params") or {}
    if method == "initialize":
        return _result(id_, {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                             "serverInfo": SERVER})
    if method == "ping":
        return _result(id_, {})
    if method == "tools/list":
        return _result(id_, {"tools": [
            {"name": n, "description": d, "inputSchema": s} for n, (_, d, s) in TOOLS.items()]})
    if method == "tools/call":
        result = _call(conn, params)
        return _result(id_, result) if result else _error(id_, -32602, "unknown tool or arguments")
    return _error(id_, -32601, f"method not found: {method}")


def serve(db_path, stdin=None, stdout=None):
    """Answer requests on stdio until EOF. The index is opened read-only."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    conn = store.connect(db_path, readonly=True)
    for line in stdin:
        if not line.strip():
            continue
        response = respond(handle, conn, line)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
