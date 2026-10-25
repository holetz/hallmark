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
        self.host = (host or _env("HALLMARK_MLFLOW_HOST")).rstrip("/")
        self.token = token if token is not None else _env("HALLMARK_MLFLOW_TOKEN", required=False)
        self.opener = opener

    def _send(self, url, body=None):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with self.opener(req, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise MlflowError(f"request to {urllib.parse.urlsplit(url).path} failed: "
                              f"{type(exc).__name__}") from None

    def get(self, path, params=None):
        if not path.startswith(API + "/"):
            raise ReadOnlyError(f"refused: {path} is outside {API}")
        query = urllib.parse.urlencode(params or {})
        return self._send(f"{self.host}{path}" + (f"?{query}" if query else ""))

    def search(self, path, body):
        if path not in SEARCH_PATHS:
            raise ReadOnlyError(f"refused: POST {path} is not a read-only search")
        return self._send(f"{self.host}{path}", body)


def pages(client, path, key, params=None, post=False):
    """Yield every item of a paginated listing, following next_page_token."""
    params = {"max_results": PAGE_SIZE, **(params or {})}
    for _ in range(MAX_PAGES):
        body = client.search(path, params) if post else client.get(path, params)
        yield from body.get(key) or []
        token = body.get("next_page_token")
        if not token:
            return
        params["page_token"] = token
    raise MlflowError(f"{path}: more than {MAX_PAGES} pages")
