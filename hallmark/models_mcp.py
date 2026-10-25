"""A read-only MCP server over stdio for MLflow: registry, experiments, performance and drift.

Same wire shape as `hallmark.mcp` (newline-delimited JSON-RPC 2.0, no dependency, pure `handle`),
but it answers live from an injected MLflow client instead of the local index, because metrics
change after every run. The client can only read (see `mlflow_client`); this module has no tool
that writes, promotes or trains. Every list takes a `limit` capped at MAX_LIMIT and reports
`truncated`.

Drift convention (assumption, confirmed against a real monitoring job): a metric is a drift metric
when its key contains one of DRIFT_MARKERS (case-insensitive). Its threshold is the run param
`<key>.threshold`, else the call's `threshold` (default DEFAULT_THRESHOLD). A value above the
threshold is an alert, `high` at twice the threshold or more, else `medium`.
"""
import json
import statistics
import sys
import time

from . import mcp
from .mcp import MAX_LIMIT, _cut, _error, _limit, _result, _schema, _text
from .mlflow_client import API, MlflowError, pages

DRIFT_MARKERS = ("drift", "psi", "ks_stat")
DEFAULT_THRESHOLD = 0.2
CHAMPION = "champion"
MAX_VERSIONS = 5
HISTORY_CAP = 1000


def _iso(millis):
    if millis in (None, ""):
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(int(millis) / 1000))


def _name(args, key="name"):
    value = _text(args, key)
    if "'" in value:
        raise ValueError(f"'{key}' must not contain a quote")
    return value


def _contains(items, args):
    needle = (args.get("name_filter") or "").lower()
    return [i for i in items if needle in i["name"].lower()]


def _short_version(v):
    return {"version": str(v["version"]), "stage": v.get("current_stage") or "",
            "status": v.get("status") or "", "run_id": v.get("run_id") or ""}


def _kv(entries):
    return {e["key"]: e["value"] for e in entries or []}


def _run(raw):
    info, data = raw.get("info") or {}, raw.get("data") or {}
    return {"run_id": info.get("run_id"), "name": info.get("run_name") or "",
            "experiment_id": info.get("experiment_id"), "status": info.get("status") or "",
            "started": _iso(info.get("start_time")),
            "metrics": _kv(data.get("metrics")), "params": _kv(data.get("params")),
            "tags": _kv(data.get("tags"))}


def _versions(client, name):
    found = pages(client, f"{API}/model-versions/search", "model_versions", {"filter": f"name='{name}'"})
    return sorted(found, key=lambda v: int(v["version"]), reverse=True)


def _get_run(client, run_id):
    return _run(client.get(f"{API}/runs/get", {"run_id": run_id}).get("run") or {})


def _select(client, name, args):
    """Pick the version: the requested one, else the champion alias, else the highest number."""
    versions = _versions(client, name)
    if not versions:
        raise LookupError(f"model {name} has no versions")
    wanted = args.get("version")
    if wanted is None:
        model = client.get(f"{API}/registered-models/get", {"name": name}).get("registered_model") or {}
        wanted = next((a["version"] for a in model.get("aliases") or [] if a["alias"] == CHAMPION),
                      versions[0]["version"])
    chosen = next((v for v in versions if str(v["version"]) == str(wanted)), None)
    if chosen is None:
        raise LookupError(f"model {name} has no version {wanted}")
    return versions, chosen


def tool_list_models(client, args):
    limit = _limit(args)
    models = sorted(_contains(pages(client, f"{API}/registered-models/search", "registered_models"),
                              args), key=lambda m: m["name"])
    shaped = [{"name": m["name"], "description": m.get("description") or "",
               "updated": _iso(m.get("last_updated_timestamp")),
               "aliases": {a["alias"]: str(a["version"]) for a in m.get("aliases") or []},
               "latest_versions": [_short_version(v) for v in m.get("latest_versions") or []]}
              for m in models]
    return _cut(shaped, limit)


def tool_get_model(client, args):
    name, limit = _name(args), _limit(args)
    model = client.get(f"{API}/registered-models/get", {"name": name}).get("registered_model") or {}
    versions = [{**_short_version(v), "created": _iso(v.get("creation_timestamp")),
                 "description": v.get("description") or ""} for v in _versions(client, name)]
    return {"name": name, "description": model.get("description") or "",
            "aliases": {a["alias"]: str(a["version"]) for a in model.get("aliases") or []},
            "versions": _cut(versions, limit)}


def tool_list_experiments(client, args):
    limit = _limit(args)
    found = _contains(pages(client, f"{API}/experiments/search", "experiments", post=True), args)
    return _cut([{"experiment_id": e["experiment_id"], "name": e["name"],
                  "lifecycle_stage": e.get("lifecycle_stage") or "",
                  "updated": _iso(e.get("last_update_time"))} for e in sorted(found, key=lambda e: e["name"])],
                limit)


def tool_list_runs(client, args):
    limit = _limit(args)
    body = client.search(f"{API}/runs/search", {"experiment_ids": [_text(args, "experiment_id")],
                                                "max_results": limit + 1,
                                                "order_by": ["attributes.start_time DESC"]})
    runs = sorted((_run(r) for r in body.get("runs") or []), key=lambda r: r["started"] or "",
                  reverse=True)
    for run in runs:
        del run["params"], run["tags"]
    return _cut(runs, limit)


def tool_get_run(client, args):
    run, limit = _get_run(client, _text(args, "run_id")), _limit(args)
    return {**run, "params": _cut(sorted(run["params"].items()), limit),
            "tags": _cut(sorted(run["tags"].items()), limit)}


def _stability(points):
    values = [p["value"] for p in sorted(points, key=lambda p: (p["step"], p["timestamp"]))]
    if not values:
        return {"points": 0}
    return {"points": len(values), "min": min(values), "max": max(values),
            "mean": round(statistics.fmean(values), 6), "stdev": round(statistics.pstdev(values), 6),
            "last": values[-1]}


def _history(client, run_id, key):
    points = pages(client, f"{API}/metrics/get-history", "metrics", {"run_id": run_id, "metric_key": key})
    found = []
    for point in points:
        found.append(point)
        if len(found) >= HISTORY_CAP:
            break
    return found


def tool_metric_history(client, args):
    run_id, key, limit = _text(args, "run_id"), _text(args, "metric"), _limit(args)
    points = _history(client, run_id, key)
    latest = sorted(points, key=lambda p: (p["step"], p["timestamp"]), reverse=True)
    return {"run_id": run_id, "metric": key, "summary": _stability(points),
            "history": _cut([{"step": p["step"], "value": p["value"],
                              "at": _iso(p["timestamp"])} for p in latest], limit)}


def _is_drift(key):
    return any(marker in key.lower() for marker in DRIFT_MARKERS)


def tool_performance(client, args):
    name = _name(args, "model")
    versions, chosen = _select(client, name, args)
    shown = versions[:MAX_VERSIONS]
    runs = {}
    for v in shown + ([chosen] if chosen not in shown else []):
        runs[v["version"]] = _get_run(client, v["run_id"]) if v.get("run_id") else None
    entries = [{**_short_version(v), "metrics": {k: x for k, x in (runs[v["version"]] or {}).get(
        "metrics", {}).items() if not _is_drift(k)}} for v in shown]
    older = next((v for v in versions if int(v["version"]) < int(chosen["version"])), None)
    changes = {}
    if older and runs.get(chosen["version"]):
        before = _get_run(client, older["run_id"])["metrics"] if older.get("run_id") else {}
        now = runs[chosen["version"]]["metrics"]
        changes = {k: round(now[k] - before[k], 6) for k in sorted(now)
                   if k in before and not _is_drift(k)}
    out = {"model": name, "selected": str(chosen["version"]), "compared_to": older and str(older["version"]),
           "changes": changes, "versions": {"items": entries, "total": len(versions),
                                            "truncated": len(versions) > len(shown)}}
    if args.get("metric") and chosen.get("run_id"):
        out["stability"] = {"metric": args["metric"],
                            **_stability(_history(client, chosen["run_id"], args["metric"]))}
    return out


def tool_drift(client, args):
    name = _name(args, "model")
    threshold = args.get("threshold", DEFAULT_THRESHOLD)
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or threshold <= 0:
        raise ValueError("threshold must be a positive number")
    _, chosen = _select(client, name, args)
    if not chosen.get("run_id"):
        raise LookupError(f"version {chosen['version']} of {name} has no run")
    run = _get_run(client, chosen["run_id"])
    checks = []
    for key in sorted(k for k in run["metrics"] if _is_drift(k)):
        raw = run["params"].get(f"{key}.threshold")
        try:
            limit_ = float(raw) if raw is not None else float(threshold)
        except ValueError:
            limit_ = float(threshold)
        value = run["metrics"][key]
        status = "ok" if value <= limit_ else ("high" if value >= 2 * limit_ else "medium")
        checks.append({"metric": key, "value": value, "threshold": limit_, "status": status})
    return {"model": name, "version": str(chosen["version"]), "run_id": run["run_id"],
            "convention": "metric keys containing " + ", ".join(DRIFT_MARKERS),
            "alerts": [c for c in checks if c["status"] != "ok"], "checks": checks,
            "monitored": bool(checks)}


_LIMIT = {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "description": "Max items, default 10"}
_VERSION = {"type": "string", "description": "Version number; default the champion alias, else the highest"}
TOOLS = {
    "list_models": (tool_list_models, "Registered models with aliases and latest versions.",
                    _schema({"name_filter": {"type": "string"}, "limit": _LIMIT})),
    "get_model": (tool_get_model, "One registered model with all its versions (newest first).",
                  _schema({"name": {"type": "string"}, "limit": _LIMIT}, ["name"])),
    "list_experiments": (tool_list_experiments, "Experiments by name.",
                         _schema({"name_filter": {"type": "string"}, "limit": _LIMIT})),
    "list_runs": (tool_list_runs, "Latest runs of an experiment with their metrics.",
                  _schema({"experiment_id": {"type": "string"}, "limit": _LIMIT}, ["experiment_id"])),
    "get_run": (tool_get_run, "One run: metrics, params and tags.",
                _schema({"run_id": {"type": "string"}, "limit": _LIMIT}, ["run_id"])),
    "metric_history": (tool_metric_history, "A run metric over its steps, with min, max, mean, "
                       "stdev and last (stability).",
                       _schema({"run_id": {"type": "string"}, "metric": {"type": "string"},
                                "limit": _LIMIT}, ["run_id", "metric"])),
    "performance": (tool_performance, "Performance metrics of a model's latest versions side by "
                    "side, change of the selected version against the previous one, and optional "
                    "stability of one metric over its history.",
                    _schema({"model": {"type": "string"}, "version": _VERSION,
                             "metric": {"type": "string"}}, ["model"])),
    "drift": (tool_drift, "Drift and monitoring alerts for a model version. Drift metrics are the "
              "run metrics whose key contains 'drift', 'psi' or 'ks_stat'; the threshold is the run "
              "param '<metric>.threshold', else the 'threshold' argument (default 0.2). "
              "'monitored' is false when the run logged no drift metric.",
              _schema({"model": {"type": "string"}, "version": _VERSION,
                       "threshold": {"type": "number"}}, ["model"])),
}


def _call(client, params):
    name, args = params.get("name"), params.get("arguments") or {}
    if name not in TOOLS or not isinstance(args, dict):
        return None
    try:
        body = TOOLS[name][0](client, args)
        return {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}]}
    except (ValueError, LookupError, MlflowError) as exc:
        return {"isError": True, "content": [{"type": "text", "text": str(exc.args[0])}]}


def handle(client, message):
    """Return the response for one JSON-RPC message, or None for a notification."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
        return _error(message.get("id") if isinstance(message, dict) else None, -32600,
                      "invalid request")
    id_, method = message.get("id"), message["method"]
    if "id" not in message:
        return None
    params = message.get("params") or {}
    if method == "initialize":
        return _result(id_, {"protocolVersion": mcp.PROTOCOL, "capabilities": {"tools": {}},
                             "serverInfo": {**mcp.SERVER, "name": "hallmark-models"}})
    if method == "ping":
        return _result(id_, {})
    if method == "tools/list":
        return _result(id_, {"tools": [
            {"name": n, "description": d, "inputSchema": s} for n, (_, d, s) in TOOLS.items()]})
    if method == "tools/call":
        result = _call(client, params)
        return _result(id_, result) if result else _error(id_, -32602, "unknown tool or arguments")
    return _error(id_, -32601, f"method not found: {method}")


def serve(client, stdin=None, stdout=None):
    """Answer requests on stdio until EOF."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        if not line.strip():
            continue
        try:
            response = handle(client, json.loads(line))
        except json.JSONDecodeError:
            response = _error(None, -32700, "parse error")
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
