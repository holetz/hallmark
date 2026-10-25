"""A synthetic MLflow-shaped REST surface for tests: a stub client and a localhost HTTP server.

Both answer from tests/fixtures/mlflow/server.json by the same router, so the production client
(over urllib) and the injected stub are checked against identical data. Listings are paged by
`max_results` and an opaque `page_token` (an offset), like the real API.
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import _paths
from hallmark.mlflow_client import API

with open(os.path.join(_paths.FIXTURES, "mlflow", "server.json"), encoding="utf-8") as handle:
    DATA = json.load(handle)


def _page(items, key, args):
    size = int(args.get("max_results") or 1000)
    start = int(args.get("page_token") or 0)
    body = {key: items[start:start + size]}
    if start + size < len(items):
        body["next_page_token"] = str(start + size)
    return body


def _name_filter(args):
    text = args.get("filter") or ""
    return text.split("'")[1] if "name='" in text else None


def route(method, path, args):
    """Return the JSON body for one request, or raise KeyError for an unknown route."""
    short = path[len(API):] if path.startswith(API) else path
    if method == "GET" and short == "/registered-models/search":
        return _page(DATA["registered_models"], "registered_models", args)
    if method == "GET" and short == "/registered-models/get":
        return {"registered_model": next(m for m in DATA["registered_models"] if m["name"] == args["name"])}
    if method == "GET" and short == "/model-versions/search":
        name = _name_filter(args)
        return _page([v for v in DATA["model_versions"] if name in (None, v["name"])],
                     "model_versions", args)
    if method == "POST" and short == "/experiments/search":
        return _page(DATA["experiments"], "experiments", args)
    if method == "POST" and short == "/runs/search":
        ids = args.get("experiment_ids") or []
        return _page([r for r in DATA["runs"] if not ids or r["info"]["experiment_id"] in ids],
                     "runs", args)
    if method == "GET" and short == "/runs/get":
        return {"run": next(r for r in DATA["runs"] if r["info"]["run_id"] == args["run_id"])}
    if method == "GET" and short == "/metrics/get-history":
        return _page(DATA["history"].get(f"{args['run_id']}|{args['metric_key']}", []), "metrics", args)
    raise KeyError(f"{method} {path}")


class StubClient:
    """Same interface as MlflowClient; records every call."""

    def __init__(self):
        self.calls = []

    def get(self, path, params=None):
        self.calls.append(("GET", path, dict(params or {})))
        return route("GET", path, params or {})

    def search(self, path, body):
        self.calls.append(("POST", path, dict(body)))
        return route("POST", path, body)


class _Handler(BaseHTTPRequestHandler):
    def _reply(self, method, args):
        from urllib.parse import urlsplit
        try:
            body, code = route(method, urlsplit(self.path).path, args), 200
        except (KeyError, StopIteration):
            body, code = {"error_code": "RESOURCE_DOES_NOT_EXIST"}, 404
        self.server.seen.append((method, self.path, self.headers.get("Authorization")))
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        from urllib.parse import parse_qsl, urlsplit
        self._reply("GET", dict(parse_qsl(urlsplit(self.path).query)))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self._reply("POST", json.loads(self.rfile.read(length) or b"{}"))

    def log_message(self, *args):
        pass


def serve_http():
    """Start a server on 127.0.0.1 and return (server, base_url); the caller shuts it down."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"
