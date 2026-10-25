"""Read-only Databricks REST client: GET only, against a closed allowlist of read paths.

Nothing here can run, rerun, repair, cancel, create, reset, update or delete a job or a pipeline:
the class has one transport method and it sends no body, and `get` matches the path against the
patterns below. Any other path raises ReadOnlyError before a request exists. Host and token come
from HALLMARK_DBX_HOST and HALLMARK_DBX_TOKEN and never appear in an error.

REST fields read
  /api/2.1/jobs/list         -> {jobs: [{job_id, settings: {name, schedule, tags}}], next_page_token}
  /api/2.1/jobs/get          -> {job_id, settings: {...}}
  /api/2.1/jobs/runs/list    -> {runs: [{run_id, job_id, run_name, state: {life_cycle_state,
                                 result_state, state_message}, start_time, end_time, attempt_number,
                                 original_attempt_run_id, trigger, tasks}], next_page_token}
  /api/2.1/jobs/runs/get     -> a run plus {tasks: [{task_key, run_id, state}], repair_history:
                                 [{type: ORIGINAL|REPAIR, start_time, state}]}
  /api/2.1/jobs/runs/get-output -> {error, error_trace}
  /api/2.0/pipelines         -> {statuses: [{pipeline_id, name, state, latest_updates}]}
  /api/2.0/pipelines/<id>    -> {pipeline_id, name, state, health, latest_updates}
  .../<id>/updates           -> {updates: [{update_id, state, creation_time, cause}]}
  .../<id>/events            -> {events: [{event_type, level, message, timestamp, origin: {flow_name},
                                 details: {flow_progress: {data_quality: {expectations: [...]}}}}]}
  /api/2.0/sql/alerts        -> {results: [{id, display_name, state, lifecycle_state, update_time}]}
Listings page by `next_page_token`.
"""
import json
import os
import re
import urllib.parse
import urllib.request

READ_PATHS = frozenset((
    "/api/2.1/jobs/list", "/api/2.1/jobs/get", "/api/2.1/jobs/runs/list", "/api/2.1/jobs/runs/get",
    "/api/2.1/jobs/runs/get-output", "/api/2.0/pipelines", "/api/2.0/sql/alerts"))
PIPELINE_PATH = re.compile(r"/api/2\.0/pipelines/[A-Za-z0-9_-]+(/(events|updates))?")
MAX_PAGES = 20
PAGE_SIZE = 25


class DatabricksError(RuntimeError):
    """A request failed or a response was not what the client reads."""


class ReadOnlyError(DatabricksError):
    """A call outside the read-only allowlist was refused before any request."""


LOOPBACK = frozenset(("127.0.0.1", "localhost", "::1"))


def normalize_host(raw, env_name, error_cls):
    """Return an https base URL for `raw`, adding the scheme when it is missing.

    Anything else is refused with a fixed message naming `env_name`, never the value; plain http is
    allowed only for a loopback host, which is how the local test servers are reached.
    """
    text = str(raw).strip()
    try:
        parts = urllib.parse.urlsplit(text if "://" in text else f"https://{text}")
        host, scheme = parts.hostname, parts.scheme
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        raise error_cls(f"{env_name} is not a valid host") from None
    valid = (host and parts.username is None and not parts.query and not parts.fragment
             and (scheme == "https" or (scheme == "http" and host in LOOPBACK)))
    if not valid:
        raise error_cls(f"{env_name} must be an https host name or URL")
    return parts._replace(path=parts.path.rstrip("/")).geturl()


def _env(name):
    value = os.environ.get(name)
    if not value:
        raise DatabricksError(f"environment variable {name} is not set")
    return value


class DatabricksClient:
    def __init__(self, host=None, token=None, opener=urllib.request.urlopen):
        self.host = normalize_host(host or _env("HALLMARK_DBX_HOST"), "HALLMARK_DBX_HOST",
                                   DatabricksError)
        self.token = token or _env("HALLMARK_DBX_TOKEN")
        self.opener = opener

    def get(self, path, params=None):
        if path not in READ_PATHS and not PIPELINE_PATH.fullmatch(path):
            raise ReadOnlyError(f"refused: {path} is not on the read-only allowlist")
        query = urllib.parse.urlencode(params or {})
        url = f"{self.host}{path}" + (f"?{query}" if query else "")
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"},
                                         method="GET")
            with self.opener(req, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise DatabricksError(f"request to {path} failed: {type(exc).__name__}") from None


class Pages:
    """Iterate a paginated listing, following next_page_token up to `max_pages`.

    Hitting the cap with a token still pending stops the iteration and sets `truncated`; it never
    raises. `truncated` is meaningful once the iteration has finished.
    """

    def __init__(self, fetch, key, params, max_pages=MAX_PAGES):
        self.fetch, self.key, self.params, self.max_pages = fetch, key, params, max_pages
        self.truncated = False

    def __iter__(self):
        params = dict(self.params)
        for _ in range(self.max_pages):
            body = self.fetch(params)
            yield from body.get(self.key) or []
            token = body.get("next_page_token")
            if not token:
                return
            params["page_token"] = token
        self.truncated = True


def pages(client, path, key, params=None, size_key="max_results"):
    """A `Pages` over `path`, `PAGE_SIZE` items per request."""
    return Pages(lambda p: client.get(path, p), key, {size_key: PAGE_SIZE, **(params or {})})
