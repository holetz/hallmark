"""A read-only MCP server over stdio for data operations: jobs, failures, reruns and data quality.

Same wire shape as `hallmark.models_mcp` (newline-delimited JSON-RPC 2.0, no dependency, pure
`handle`), answering live from an injected Databricks client. The client can only GET from a closed
allowlist (see `databricks_client`); this module has no tool that runs, reruns, repairs, cancels or
changes anything: "reruns" reads the retries and repairs that already happened. Every list takes a
`limit` capped at MAX_LIMIT and reports `truncated`; error text is cut to TEXT_CAP characters.

Conventions (assumptions, confirmed against a real workspace):
  failure       result_state FAILED or TIMEDOUT, or life_cycle_state INTERNAL_ERROR; CANCELED is
                counted apart.
  rerun         a retry (a run whose original_attempt_run_id is the failed run) or a REPAIR entry in
                the failed run's repair_history.
  data quality  the expectations of the pipeline's most recent update that logged any, taken from
                flow_progress events; alert when failed_records > 0.
"""
import itertools
import json
import re
import sys
import time

from . import mcp
from .databricks_client import DatabricksError, pages
from .mcp import MAX_LIMIT, _cut, _error, _limit, _result, _schema, _text

TEXT_CAP = 2000
DEFAULT_HOURS = 24
MAX_HOURS = 720
FETCH_CAP = 500
RERUN_RUNS = 10
FAILED_RESULTS = ("FAILED", "TIMEDOUT")
PIPELINE_ID = re.compile(r"[A-Za-z0-9_-]+")
JOBS, RUNS = "/api/2.1/jobs", "/api/2.1/jobs/runs"
PIPELINES = "/api/2.0/pipelines"


def _now():
    return time.time()


def _iso(millis):
    if millis in (None, "", 0):
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(int(millis) / 1000))


def _trunc(value):
    return value if not isinstance(value, str) or len(value) <= TEXT_CAP else value[:TEXT_CAP] + "..."


def _id(args, key):
    value = args.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).strip():
        raise ValueError(f"'{key}' is required")
    return str(value)


def _pipeline_id(args):
    value = _text(args, "pipeline_id")
    if not PIPELINE_ID.fullmatch(value):
        raise ValueError("'pipeline_id' may only contain letters, digits, '-' and '_'")
    return value


def _contains(items, key, needle):
    needle = (needle or "").lower()
    return [i for i in items if needle in (i.get(key) or "").lower()]


def _state(run):
    state = run.get("state") or {}
    return state.get("life_cycle_state") or "", state.get("result_state") or ""


def _failed(run):
    life, result = _state(run)
    return result in FAILED_RESULTS or life == "INTERNAL_ERROR"


def _short(run):
    life, result = _state(run)
    start, end = run.get("start_time"), run.get("end_time")
    out = {"run_id": run.get("run_id"), "job_id": run.get("job_id"), "name": run.get("run_name") or "",
           "life_cycle": life, "result": result,
           "message": _trunc((run.get("state") or {}).get("state_message") or ""),
           "started": _iso(start), "ended": _iso(end),
           "duration_s": round((end - start) / 1000) if start and end else None,
           "attempt": run.get("attempt_number") or 0, "trigger": run.get("trigger") or ""}
    if run.get("original_attempt_run_id"):
        out["retry_of"] = run["original_attempt_run_id"]
    return out


def _failed_tasks(run):
    return [{"task_key": t.get("task_key"), "run_id": t.get("run_id"),
             "message": _trunc((t.get("state") or {}).get("state_message") or "")}
            for t in run.get("tasks") or [] if _state(t)[1] in FAILED_RESULTS]


def _runs(client, job_id=None, start_from=None, completed_only=False):
    params = {"expand_tasks": "true"}
    if job_id is not None:
        params["job_id"] = job_id
    if start_from is not None:
        params["start_time_from"] = int(start_from)
    if completed_only:
        params["completed_only"] = "true"
    found = itertools.islice(pages(client, f"{RUNS}/list", "runs", params, size_key="limit"), FETCH_CAP)
    return sorted(found, key=lambda r: r.get("start_time") or 0, reverse=True)


def _hours(args):
    hours = args.get("hours", DEFAULT_HOURS)
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 < hours <= MAX_HOURS:
        raise ValueError(f"hours must be a number above 0 and up to {MAX_HOURS}")
    return hours


def tool_list_jobs(client, args):
    limit = _limit(args)
    jobs = [{"name": j["settings"].get("name") or "", "job_id": j["job_id"],
             "schedule": (j["settings"].get("schedule") or {}).get("quartz_cron_expression"),
             "paused": (j["settings"].get("schedule") or {}).get("pause_status") == "PAUSED",
             "tags": j["settings"].get("tags") or {}}
            for j in pages(client, f"{JOBS}/list", "jobs", size_key="limit")]
    needle = (args.get("name_filter") or "").lower()
    return _cut(sorted((j for j in jobs if needle in j["name"].lower()), key=lambda j: j["name"]), limit)


def tool_job_status(client, args):
    job_id, limit = _id(args, "job_id"), _limit(args)
    job = client.get(f"{JOBS}/get", {"job_id": job_id})
    runs = _runs(client, job_id)
    done = [r for r in runs if _state(r)[0] != "RUNNING"]
    streak = len(list(itertools.takewhile(_failed, done)))
    success = next((r for r in done if _state(r)[1] == "SUCCESS"), None)
    return {"job_id": job["job_id"], "name": job["settings"].get("name") or "",
            "running": sum(1 for r in runs if _state(r)[0] == "RUNNING"),
            "consecutive_failures": streak,
            "last_success": _iso(success.get("start_time")) if success else None,
            "runs": _cut([_short(r) for r in runs], limit)}


def tool_list_failures(client, args):
    limit, hours = _limit(args), _hours(args)
    job_id = _id(args, "job_id") if args.get("job_id") is not None else None
    start_from = _now() * 1000 - hours * 3600 * 1000
    runs = _runs(client, job_id, start_from, completed_only=True)
    failures = [{**_short(r), "failed_tasks": _failed_tasks(r)} for r in runs if _failed(r)]
    return {"window_hours": hours, "runs_checked": len(runs),
            "canceled": sum(1 for r in runs if _state(r)[1] == "CANCELED"),
            **_cut(failures, limit)}


def _output(client, run_id):
    try:
        body = client.get(f"{RUNS}/get-output", {"run_id": run_id})
    except DatabricksError:
        return None
    return {"error": _trunc(body.get("error") or ""), "error_trace": _trunc(body.get("error_trace") or "")}


def tool_get_run(client, args):
    run_id, limit = _id(args, "run_id"), _limit(args)
    run = client.get(f"{RUNS}/get", {"run_id": run_id})
    failed_tasks = _failed_tasks(run)
    targets = failed_tasks[:5] if failed_tasks else ([{"run_id": run.get("run_id")}] if _failed(run) else [])
    tasks = [{"task_key": t.get("task_key"), "run_id": t.get("run_id"), "result": _state(t)[1],
              "life_cycle": _state(t)[0]} for t in run.get("tasks") or []]
    history = [{"type": h.get("type"), "started": _iso(h.get("start_time")),
                "result": ((h.get("state") or {}).get("result_state") or "")}
               for h in run.get("repair_history") or []]
    return {**_short(run), "tasks": _cut(tasks, limit), "repair_history": history,
            "outputs": [{"run_id": t["run_id"], "output": _output(client, t["run_id"])} for t in targets]}


def tool_reruns(client, args):
    job_id, limit = _id(args, "job_id"), _limit(args)
    runs = _runs(client, job_id)
    failed = [r for r in runs if _failed(r)]
    items = []
    for run in failed[:RERUN_RUNS]:
        retries = [r for r in runs if r.get("original_attempt_run_id") == run["run_id"]]
        detail = client.get(f"{RUNS}/get", {"run_id": run["run_id"]})
        repairs = [h for h in detail.get("repair_history") or [] if h.get("type") == "REPAIR"]
        outcomes = sorted([(r.get("start_time") or 0, _state(r)[1]) for r in retries]
                          + [(h.get("start_time") or 0, (h.get("state") or {}).get("result_state") or "")
                             for h in repairs])
        last = outcomes[-1][1] if outcomes else _state(run)[1]
        items.append({"run_id": run["run_id"], "started": _iso(run.get("start_time")),
                      "result": _state(run)[1] or _state(run)[0], "attempts": 1 + len(retries),
                      "retried_by": [r["run_id"] for r in retries], "repairs": len(repairs),
                      "last_result": last, "recovered": last == "SUCCESS"})
    return {"job_id": job_id, "failed_runs": len(failed), "reruns_read": len(items), **_cut(items, limit)}


def tool_list_pipelines(client, args):
    limit = _limit(args)
    found = _contains(pages(client, PIPELINES, "statuses"), "name", args.get("name_filter"))
    return _cut([{"pipeline_id": p["pipeline_id"], "name": p.get("name") or "", "state": p.get("state") or "",
                  "health": p.get("health") or "",
                  "latest_update": ((p.get("latest_updates") or [{}])[0]).get("state")}
                 for p in sorted(found, key=lambda p: p.get("name") or "")], limit)


def _events(client, pipeline_id):
    found = itertools.islice(pages(client, f"{PIPELINES}/{pipeline_id}/events", "events"), FETCH_CAP)
    return sorted(found, key=lambda e: e.get("timestamp") or 0, reverse=True)


def tool_pipeline_status(client, args):
    pid, limit = _pipeline_id(args), _limit(args)
    pipeline = client.get(f"{PIPELINES}/{pid}")
    updates = list(itertools.islice(pages(client, f"{PIPELINES}/{pid}/updates", "updates"), limit + 1))
    errors = [{"timestamp": _iso(e.get("timestamp")), "update_id": (e.get("origin") or {}).get("update_id"),
               "flow": (e.get("origin") or {}).get("flow_name"), "message": _trunc(e.get("message") or "")}
              for e in _events(client, pid) if e.get("level") == "ERROR"]
    return {"pipeline_id": pid, "name": pipeline.get("name") or "", "state": pipeline.get("state") or "",
            "health": pipeline.get("health") or "",
            "updates": _cut([{"update_id": u.get("update_id"), "state": u.get("state"),
                              "created": u.get("creation_time"), "cause": u.get("cause")} for u in updates],
                            limit),
            "errors": _cut(errors, limit)}


def _expectations(event):
    quality = ((event.get("details") or {}).get("flow_progress") or {}).get("data_quality") or {}
    return quality.get("expectations") or []


def tool_data_quality(client, args):
    pid, limit = _pipeline_id(args), _limit(args)
    table = args.get("table")
    events = [e for e in _events(client, pid) if _expectations(e)]
    update = (events[0].get("origin") or {}).get("update_id") if events else None
    totals = {}
    for event in events:
        if (event.get("origin") or {}).get("update_id") != update:
            continue
        for exp in _expectations(event):
            dataset = exp.get("dataset") or ""
            if table and table != dataset and table != dataset.rsplit(".", 1)[-1]:
                continue
            entry = totals.setdefault((dataset, exp.get("name") or ""), [0, 0])
            entry[0] += exp.get("passed_records") or 0
            entry[1] += exp.get("failed_records") or 0
    rows = [{"dataset": d, "expectation": n, "passed": p, "failed": f,
             "pass_rate": round(p / (p + f), 4) if p + f else None, "alert": f > 0}
            for (d, n), (p, f) in sorted(totals.items())]
    return {"pipeline_id": pid, "update_id": update, "monitored": bool(events),
            "alerts": sum(1 for r in rows if r["alert"]), **_cut(rows, limit)}


def tool_alerts(client, args):
    limit, state = _limit(args), args.get("state")
    found = _contains(pages(client, "/api/2.0/sql/alerts", "results"), "display_name", args.get("name_filter"))
    if state:
        found = [a for a in found if a.get("state") == state]
    return _cut([{"id": a.get("id"), "name": a.get("display_name") or "", "state": a.get("state") or "",
                  "lifecycle_state": a.get("lifecycle_state") or "", "updated": a.get("update_time")}
                 for a in sorted(found, key=lambda a: a.get("display_name") or "")], limit)


_LIMIT = {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "description": "Max items, default 10"}
_JOB = {"type": ["string", "integer"], "description": "Job id"}
_PIPELINE = {"type": "string", "description": "Pipeline id"}
TOOLS = {
    "list_jobs": (tool_list_jobs, "Jobs with schedule and tags, filtered by a name substring.",
                  _schema({"name_filter": {"type": "string"}, "limit": _LIMIT})),
    "job_status": (tool_job_status, "A job's latest runs, runs in progress, consecutive failures and "
                   "the time of its last success.", _schema({"job_id": _JOB, "limit": _LIMIT}, ["job_id"])),
    "list_failures": (tool_list_failures, "Failed runs (FAILED, TIMEDOUT, INTERNAL_ERROR) in the last "
                      "'hours' (default 24, max 720), for one job or all, with the failed tasks and "
                      "their message; canceled runs are counted apart.",
                      _schema({"job_id": _JOB, "hours": {"type": "number", "exclusiveMinimum": 0,
                                                          "maximum": MAX_HOURS}, "limit": _LIMIT})),
    "get_run": (tool_get_run, "One run: tasks, repair history and the error and trace of its failed "
                "tasks (cut to 2000 characters). Read-only.", _schema({"run_id": _JOB, "limit": _LIMIT},
                                                                     ["run_id"])),
    "reruns": (tool_reruns, "For a job's latest failed runs: retries, repairs that already happened "
               "and whether the last attempt succeeded. It reads history and starts nothing.",
               _schema({"job_id": _JOB, "limit": _LIMIT}, ["job_id"])),
    "list_pipelines": (tool_list_pipelines, "Pipelines with state, health and latest update state.",
                       _schema({"name_filter": {"type": "string"}, "limit": _LIMIT})),
    "pipeline_status": (tool_pipeline_status, "A pipeline's recent updates and recent ERROR events.",
                        _schema({"pipeline_id": _PIPELINE, "limit": _LIMIT}, ["pipeline_id"])),
    "data_quality": (tool_data_quality, "Expectation results of a pipeline's most recent update that "
                     "logged any, per table and expectation (passed, failed, pass rate); 'alert' is "
                     "true when failed records exist. Optional 'table' matches the dataset name or its "
                     "last segment. 'monitored' is false when no expectation was logged.",
                     _schema({"pipeline_id": _PIPELINE, "table": {"type": "string"}, "limit": _LIMIT},
                             ["pipeline_id"])),
    "alerts": (tool_alerts, "SQL alerts with their state, filtered by a name substring (a table or "
               "pipeline name) and optionally by state (TRIGGERED, OK, UNKNOWN).",
               _schema({"name_filter": {"type": "string"}, "state": {"type": "string"}, "limit": _LIMIT})),
}


def _call(client, params):
    name, args = params.get("name"), params.get("arguments") or {}
    if name not in TOOLS or not isinstance(args, dict):
        return None
    try:
        body = TOOLS[name][0](client, args)
        return {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}]}
    except (ValueError, LookupError, DatabricksError) as exc:
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
                             "serverInfo": {**mcp.SERVER, "name": "hallmark-ops"}})
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
