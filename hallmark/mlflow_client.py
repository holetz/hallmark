"""Read-only MLflow REST client: GET plus the two MLflow reads that happen to be POST.

Nothing here can change a registry, an experiment or a run: `get` only reaches paths under the
MLflow API prefix, and `search` accepts exactly the two search endpoints. Any other path raises
ReadOnlyError before a request exists. Host and token come from HALLMARK_MLFLOW_HOST and
HALLMARK_MLFLOW_TOKEN (the token is optional for a local server) and never appear in an error.

REST fields read (`/api/2.0/mlflow/...`)
  registered-models/search  GET   -> {registered_models: [{name, description, latest_versions,
                                      aliases: [{alias, version}], last_updated_timestamp}]}
  model-versions/search     GET   -> {model_versions: [{name, version, current_stage, status, run_id}]}
  experiments/search        POST  -> {experiments: [{experiment_id, name, lifecycle_stage}]}
  runs/search, runs/get     POST/GET -> {runs|run: {info: {...}, data: {metrics, params, tags}}}
  metrics/get-history       GET   -> {metrics: [{key, value, timestamp, step}]}
Listings page by `next_page_token`.
"""
import json
import os
import urllib.parse
import urllib.request

from .databricks_client import Pages, normalize_host

API = "/api/2.0/mlflow"
SEARCH_PATHS = (f"{API}/experiments/search", f"{API}/runs/search")
MAX_PAGES = 20
PAGE_SIZE = 100


class MlflowError(RuntimeError):
    """A request failed or a response was not what the client reads."""


class ReadOnlyError(MlflowError):
    """A call outside the read-only allowlist was refused before any request."""


def _env(name, required=True):
    value = os.environ.get(name)
    if required and not value:
        raise MlflowError(f"environment variable {name} is not set")
    return value


class MlflowClient:
    def __init__(self, host=None, token=None, opener=urllib.request.urlopen):
        self.host = normalize_host(host or _env("HALLMARK_MLFLOW_HOST"), "HALLMARK_MLFLOW_HOST",
                                   MlflowError)
        self.token = token if token is not None else _env("HALLMARK_MLFLOW_TOKEN", required=False)
        self.opener = opener

    def _send(self, path, query="", body=None):
        url = f"{self.host}{path}" + (f"?{query}" if query else "")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with self.opener(req, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise MlflowError(f"request to {path} failed: {type(exc).__name__}") from None

    def get(self, path, params=None):
        if not path.startswith(API + "/"):
            raise ReadOnlyError(f"refused: {path} is outside {API}")
        query = urllib.parse.urlencode(params or {})
        return self._send(path, query)

    def search(self, path, body):
        if path not in SEARCH_PATHS:
            raise ReadOnlyError(f"refused: POST {path} is not a read-only search")
        return self._send(path, body=body)


def pages(client, path, key, params=None, post=False):
    """A `Pages` over `path`, `PAGE_SIZE` items per request."""
    fetch = (lambda p: client.search(path, p)) if post else (lambda p: client.get(path, p))
    return Pages(fetch, key, {"max_results": PAGE_SIZE, **(params or {})}, MAX_PAGES)
