"""A synthetic Databricks-Jobs-API-shaped REST surface for tests: a stub client and a localhost server.

Both answer from tests/fixtures/databricks/server.json by the same router. The surface is GET only,
like the production client. Listings page by `limit` or `max_results` and an opaque `page_token`
(an offset), like the real APIs.
"""
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import _paths

with open(os.path.join(_paths.FIXTURES, "databricks", "server.json"), encoding="utf-8") as handle:
    DATA = json.load(handle)


def _page(items, key, args):
    size = int(args.get("limit") or args.get("max_results") or 1000)
    start = int(args.get("page_token") or 0)
    body = {key: items[start:start + size]}
    if start + size < len(items):
        body["next_page_token"] = str(start + size)
    return body


def route(path, args):
    """Return the JSON body for one GET, or raise KeyError for an unknown route."""
    if path == "/api/2.1/jobs/list":
        name = (args.get("name") or "").lower()
        return _page([j for j in DATA["jobs"] if name in j["settings"]["name"].lower()], "jobs", args)
    if path == "/api/2.1/jobs/get":
        return next(j for j in DATA["jobs"] if str(j["job_id"]) == str(args["job_id"]))
    if path == "/api/2.1/jobs/runs/list":
        runs = [r for r in DATA["runs"]
                if str(args.get("job_id", r["job_id"])) == str(r["job_id"])
                and r["start_time"] >= int(args.get("start_time_from") or 0)
                and (not args.get("completed_only") or r["state"]["life_cycle_state"] != "RUNNING")]
        return _page(runs, "runs", args)
    if path == "/api/2.1/jobs/runs/get":
        return next(r for r in DATA["runs"] if str(r["run_id"]) == str(args["run_id"]))
    if path == "/api/2.1/jobs/runs/get-output":
        return DATA["outputs"][str(args["run_id"])]
    if path == "/api/2.0/pipelines":
        return _page(DATA["pipelines"], "statuses", args)
    if path == "/api/2.0/sql/alerts":
        return _page(DATA["alerts"], "results", args)
    found = re.fullmatch(r"/api/2\.0/pipelines/([^/]+)(?:/(events|updates))?", path)
    if found:
        pid, part = found.groups()
        if part == "events":
            return _page(DATA["events"][pid], "events", args)
        if part == "updates":
            return _page(DATA["updates"][pid], "updates", args)
        return next(p for p in DATA["pipelines"] if p["pipeline_id"] == pid)
    raise KeyError(path)


class StubClient:
    """Same interface as DatabricksClient; records every call."""

    def __init__(self):
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        return route(path, params or {})


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        from urllib.parse import parse_qsl, urlsplit
        parts = urlsplit(self.path)
        self.server.seen.append((self.command, parts.path, self.headers.get("Authorization")))
        try:
            body, code = route(parts.path, dict(parse_qsl(parts.query))), 200
        except (KeyError, StopIteration):
            body, code = {"error_code": "RESOURCE_DOES_NOT_EXIST"}, 404
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _refuse(self):
        self.server.seen.append((self.command, self.path, None))
        self.send_response(405)
        self.end_headers()

    do_POST = do_PUT = do_PATCH = do_DELETE = _refuse

    def log_message(self, *args):
        pass


def serve_http():
    """Start a server on 127.0.0.1 and return (server, base_url); the caller shuts it down."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"
