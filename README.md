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
