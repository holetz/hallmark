"""Command line: `python3 -m hallmark refresh|serve`.

refresh  builds or updates the index from a snapshot file or Unity Catalog, then links source code.
serve    answers MCP requests on stdio from the index, read-only, and never imports a connector.
"""
import argparse
import json
import os
import sys

DEFAULT_DB = os.path.join(".hallmark", "index.db")


def _parser():
    parser = argparse.ArgumentParser(prog="hallmark", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    db_default = os.environ.get("HALLMARK_DB", DEFAULT_DB)
    refresh = sub.add_parser("refresh", help="update the index")
    refresh.add_argument("--db", default=db_default)
    source = refresh.add_mutually_exclusive_group(required=True)
    source.add_argument("--snapshot", help="read a snapshot JSON file")
    source.add_argument("--uc", action="store_true",
                        help="read Unity Catalog (HALLMARK_UC_HOST, HALLMARK_UC_TOKEN, "
                             "HALLMARK_UC_WAREHOUSE)")
    refresh.add_argument("--catalog", action="append", help="limit --uc to this catalog (repeatable)")
    refresh.add_argument("--src", help="source tree to link to tables and columns")
    serve = sub.add_parser("serve", help="serve MCP over stdio")
    serve.add_argument("--db", default=db_default)
    return parser


def _snapshot(args):
    if args.snapshot:
        with open(args.snapshot, encoding="utf-8") as handle:
            return json.load(handle)
    from .connectors import unity_catalog as uc  # imported only here: serve stays offline

    return uc.snapshot(uc.RestClient(), uc.SqlClient(), args.catalog)


def main(argv=None, out=None):
    out = out or sys.stdout
    args = _parser().parse_args(argv)
    from . import codelinks, mcp, store

    if args.command == "serve":
        if not os.path.exists(args.db):
            print(f"hallmark: no index at {args.db}; run refresh first", file=sys.stderr)
            return 2
        mcp.serve(args.db)
        return 0
    try:
        snapshot = _snapshot(args)
        directory = os.path.dirname(args.db)
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = store.connect(args.db)
        report = store.refresh(conn, snapshot)
        if args.src:
            report.update(codelinks.link_code(conn, args.src))
    except (OSError, ValueError, RuntimeError) as exc:  # SnapshotError, ConnectorError, JSON
        print(f"hallmark: {exc}", file=sys.stderr)
        return 1
    report.update(store.stats(conn))
    out.write(json.dumps(report, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
