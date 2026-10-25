# hallmark
Data catalog MCP server linking tables and columns to their source code, over a local metadata index with pluggable connectors (Unity Catalog first)

## What it does

Models invent table and column names, and Unity Catalog does not say which file and line produce a
column. Hallmark keeps a local SQLite index of catalogs, tables, columns, tags, lineage, registered
models and features, links each table and column to the `file:line` in source code that mentions it,
and serves it to agents over MCP. Standard library only (Python 3 with SQLite FTS5); no model call.

## Usage

```bash
# Build or update the index from a snapshot file, and link a source tree
python3 -m hallmark refresh --db .hallmark/index.db --snapshot snapshot.json --src ./src

# Or read Unity Catalog (host, token and SQL warehouse come from the environment)
export HALLMARK_UC_HOST=... HALLMARK_UC_TOKEN=... HALLMARK_UC_WAREHOUSE=...
python3 -m hallmark refresh --db .hallmark/index.db --uc --catalog my_catalog --src ./src

# Serve the index over MCP (stdio, read-only, never opens the network)
python3 -m hallmark serve --db .hallmark/index.db
```

`refresh` is incremental: a table is rewritten only when its `last_altered` or its tags changed, and
lineage edges once seen are kept. `HALLMARK_DB` sets the default `--db`.

MCP tools: `search`, `get_table`, `get_column`, `code_links`, `lineage`, `models`, `status`. List
tools take `limit` (default 10, capped at 50) and report `truncated`. Code references of kind `bare`
are candidates, not proofs.

### Models server (MLflow)

```bash
export HALLMARK_MLFLOW_HOST=https://mlflow.example.invalid HALLMARK_MLFLOW_TOKEN=...
python3 -m hallmark models-serve
```

A second MCP server over stdio, live from the MLflow REST API (registry, experiments, runs), with no
local index because metrics change after every run. It is read-only: the client only issues GETs and
the two search POSTs, and no tool writes, promotes or trains. `HALLMARK_MLFLOW_HOST` is required
(exit 2 when unset); `HALLMARK_MLFLOW_TOKEN` is optional.

MCP tools: `list_models`, `get_model`, `list_experiments`, `list_runs`, `get_run`, `metric_history`,
`performance`, `drift`. List tools take `limit` (default 10, capped at 50) and report `truncated`.
A version defaults to the `champion` alias, else the highest.

Drift convention (an assumption, to confirm against a real monitoring job): a metric whose key
contains `drift`, `psi` or `ks_stat` (case-insensitive) is a drift metric. Its threshold is the run
param `<key>.threshold`, else the call's `threshold` (default 0.2). A value above the threshold is
an alert, `high` at twice the threshold or more, else `medium`. Proven on synthetic fixtures only.

### Data operations server (Databricks jobs and pipelines)

```bash
export HALLMARK_DBX_HOST=https://dbx.example.invalid HALLMARK_DBX_TOKEN=...
python3 -m hallmark ops-serve
```

A third MCP server over stdio, live from the Databricks Jobs, Pipelines and SQL-alerts REST APIs,
with no local index because run state changes by the minute. It is read-only by construction: the
client has a single GET method matched against a closed allowlist, so a write verb raises before
any request exists, and no tool triggers, repairs or cancels a job. `HALLMARK_DBX_HOST` and
`HALLMARK_DBX_TOKEN` are both required (exit 2 naming the missing one); neither appears in an error.

MCP tools: `list_jobs`, `job_status`, `list_failures`, `get_run`, `reruns`, `list_pipelines`,
`pipeline_status`, `data_quality`, `alerts`. List tools take `limit` (default 10, capped at 50) and
report `truncated`; error text is cut to 2000 characters.

Failure convention: a run failed when `result_state` is `FAILED` or `TIMEDOUT`, or its
`life_cycle_state` is `INTERNAL_ERROR`; `CANCELED` is reported apart. `list_failures` takes `hours`
(default 24, max 720). `data_quality` aggregates pipeline expectations from the event log and flags
`alert` where records failed. Proven on synthetic fixtures only; field names are confirmed in the
first live run.

## Design notes

- The connector contract is a plain snapshot dict (`hallmark/snapshot.py`); the Unity Catalog
  connector and a JSON file are interchangeable, and only `refresh` ever imports a connector.
- The Unity Catalog connector is proven on synthetic fixtures only (`tests/fixtures/uc/`); the REST
  field names and the feature-table marker are confirmed in the first live run.
- Search is FTS5 with BM25, stemming and identifier splitting (`order_total` is found by "order
  total"). Embeddings are deliberately not built; see the spec's open decisions.
- The spike that chose this design is kept as a record in `spikes/catalog-engine/`.

## Checks

`bash scripts/verify_repo.sh` is the single gate: unit tests plus the leak gate.
