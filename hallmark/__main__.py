"""Command line: `python3 -m hallmark refresh|serve|models-serve|ops-serve`.

refresh  builds or updates the index from a snapshot file or Unity Catalog, then links source code.
serve    answers MCP requests on stdio from the index, read-only, and never imports a connector.
models-serve  answers MCP requests on stdio live from MLflow (HALLMARK_MLFLOW_HOST), read-only.
ops-serve  answers MCP requests on stdio live from Databricks jobs and pipelines, read-only.
"""
import argparse
import json
import os
import subprocess
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
    sub.add_parser("models-serve", help="serve MLflow models, runs and drift over MCP stdio")
    sub.add_parser("ops-serve", help="serve Databricks jobs, failures and data quality over MCP stdio")
    return parser


def _git(directory, *args):
    try:
        return subprocess.run(["git", "-C", directory, *args], capture_output=True, text=True)
    except OSError:  # no git installed: nothing to guard against
        return None


def _db_unsafe(db):
    """Why `db` must not be written, or None: inside a git work tree it must be ignored and untracked."""
    path = os.path.abspath(db)
    directory = os.path.dirname(path)
    while directory and not os.path.isdir(directory):
        directory = os.path.dirname(directory)
    inside = _git(directory, "rev-parse", "--is-inside-work-tree") if directory else None
    if inside is None or inside.returncode != 0:
        return None
    if _git(directory, "ls-files", "--error-unmatch", path).returncode == 0:
        return "is tracked by git"
    if _git(directory, "check-ignore", "-q", path).returncode == 1:
        return "is not ignored by git (add it to .gitignore)"
    return None


def _snapshot(args):
    if args.snapshot:
        with open(args.snapshot, encoding="utf-8") as handle:
            return json.load(handle)
    from .connectors import unity_catalog as uc  # imported only here: serve stays offline

    return uc.snapshot(uc.RestClient(), uc.SqlClient(), args.catalog)


def main(argv=None, out=None):
    out = out or sys.stdout
    args = _parser().parse_args(argv)
    if args.command == "models-serve":
        from . import mlflow_client, models_mcp

        try:
            client = mlflow_client.MlflowClient()
        except mlflow_client.MlflowError as exc:
            print(f"hallmark: {exc}", file=sys.stderr)
            return 2
        models_mcp.serve(client)
        return 0
    if args.command == "ops-serve":
        from . import databricks_client, ops_mcp

        try:
            client = databricks_client.DatabricksClient()
        except databricks_client.DatabricksError as exc:
            print(f"hallmark: {exc}", file=sys.stderr)
            return 2
        ops_mcp.serve(client)
        return 0
    from . import codelinks, mcp, store

    if args.command == "serve":
        if not os.path.exists(args.db):
            print(f"hallmark: no index at {args.db}; run refresh first", file=sys.stderr)
            return 2
        mcp.serve(args.db)
        return 0
    reason = _db_unsafe(args.db)
    if reason:
        print(f"hallmark: refusing --db {args.db}: it {reason}; an index can carry catalog metadata",
              file=sys.stderr)
        return 2
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
