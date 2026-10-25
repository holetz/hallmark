"""Unity Catalog connector: REST responses and SQL rows in, one snapshot dict out.

All knowledge of the source shapes lives in this module. Fields read:

REST (`GET /api/2.1/unity-catalog/...`, paged by `next_page_token`)
  catalogs            -> {"catalogs": [{name}]}
  schemas             -> {"schemas": [{name}]}                  params: catalog_name
  tables              -> {"tables": [{full_name, comment, updated_at (epoch ms), properties,
                          columns: [{name, type_text, comment, position}]}]}
                                                                params: catalog_name, schema_name
  models              -> {"registered_models": [{full_name, comment, updated_at}]}
                                                                params: catalog_name, schema_name
  models/<fqn>/versions -> {"model_versions": [{version, status, comment}]}

SQL rows (statement results as dicts)
  information_schema.table_tags   -> catalog_name, schema_name, table_name, tag_name, tag_value
  information_schema.column_tags  -> the same plus column_name
  system.access.table_lineage     -> source_table_full_name, target_table_full_name
  system.access.column_lineage    -> the table pair plus source_column_name, target_column_name

A table's `feature_table` property set to "true" marks a feature table (an assumption, to confirm
against a real workspace). A missing optional field is tolerated; a missing required one raises
ConnectorError naming it. The production clients below use urllib and are only exercised with a
stub transport: no network call is made by this repository's tests.
"""
import json
import os
import time
import urllib.parse
import urllib.request

API = "/api/2.1/unity-catalog"
SKIP_SCHEMAS = {"information_schema"}
SKIP_CATALOGS = {"system", "samples", "hive_metastore", "__databricks_internal"}
FEATURE_MARKER = "feature_table"
MAX_PAGES = 1000


class ConnectorError(RuntimeError):
    """A source response lacks a field the connector requires."""


def _require(obj, key, where):
    if not isinstance(obj, dict) or obj.get(key) in (None, ""):
        raise ConnectorError(f"{where}: missing required field '{key}'")
    return obj[key]


def _iso(millis):
    if millis is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(int(millis) / 1000))


def _pages(rest, path, key, params=None):
    """Yield every item of a paginated listing, following next_page_token."""
    params = dict(params or {})
    for _ in range(MAX_PAGES):
        body = rest.get(path, params)
        yield from body.get(key) or []
        token = body.get("next_page_token")
        if not token:
            return
        params["page_token"] = token
    raise ConnectorError(f"{path}: more than {MAX_PAGES} pages")


def _sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _table(raw, tags):
    fqn = _require(raw, "full_name", "table")
    columns = []
    for col in sorted(raw.get("columns") or [], key=lambda c: c.get("position", 0)):
        name = _require(col, "name", f"table {fqn} column")
        entry = {"name": name, "type": col.get("type_text") or col.get("type_name") or "",
                 "comment": col.get("comment") or ""}
        col_tags = tags["column"].get((fqn, name))
        if col_tags:
            entry["tags"] = col_tags
        columns.append(entry)
    out = {"fqn": fqn, "comment": raw.get("comment") or "",
           "last_altered": _iso(raw.get("updated_at") or raw.get("created_at")) or "unknown",
           "columns": columns}
    if tags["table"].get(fqn):
        out["tags"] = tags["table"][fqn]
    return out


def _identifier(name):
    """Backtick-quote a catalog name for SQL; a name with a backtick or control character is refused."""
    if not name or "`" in name or any(ord(ch) < 32 for ch in name):
        raise ConnectorError(f"catalog name {name!r} cannot be used in SQL")
    return f"`{name}`"


def _tags(sql, catalog):
    quoted = _identifier(catalog)
    where = f"WHERE catalog_name = {_sql_literal(catalog)}"
    table, column = {}, {}
    for row in sql.query(f"SELECT * FROM {quoted}.information_schema.table_tags {where}"):
        fqn = f"{row['catalog_name']}.{row['schema_name']}.{row['table_name']}"
        table.setdefault(fqn, {})[row["tag_name"]] = row.get("tag_value") or ""
    for row in sql.query(f"SELECT * FROM {quoted}.information_schema.column_tags {where}"):
        fqn = f"{row['catalog_name']}.{row['schema_name']}.{row['table_name']}"
        column.setdefault((fqn, row["column_name"]), {})[row["tag_name"]] = row.get("tag_value") or ""
    return {"table": table, "column": column}


def _lineage(sql, catalogs):
    edges = []
    for statement, kind in (
        ("SELECT * FROM system.access.table_lineage", "table"),
        ("SELECT * FROM system.access.column_lineage", "column"),
    ):
        for row in sql.query(statement):
            src, dst = row.get("source_table_full_name"), row.get("target_table_full_name")
            if not src or not dst:
                continue  # an external source or sink has no catalog entity to link
            if kind == "column":
                src += "." + (row.get("source_column_name") or "")
                dst += "." + (row.get("target_column_name") or "")
                if src.endswith(".") or dst.endswith("."):
                    continue
            if src.split(".")[0] in catalogs and dst.split(".")[0] in catalogs:
                edges.append({"kind": kind, "src": src, "dst": dst})
    return edges


def snapshot(rest, sql, catalogs=None):
    """Build the snapshot from an injected REST client and SQL client.

    rest.get(path, params) -> dict;  sql.query(statement) -> list[dict].
    `catalogs` limits the scan; by default every catalog except the system ones.
    """
    names = catalogs or [
        _require(c, "name", "catalog") for c in _pages(rest, f"{API}/catalogs", "catalogs")
    ]
    names = [n for n in names if catalogs or n not in SKIP_CATALOGS]
    tables, models, features = [], [], []
    for catalog in names:
        tags = _tags(sql, catalog)
        schemas = [_require(s, "name", f"catalog {catalog} schema") for s in
                   _pages(rest, f"{API}/schemas", "schemas", {"catalog_name": catalog})]
        for schema in schemas:
            if schema in SKIP_SCHEMAS:
                continue
            scope = {"catalog_name": catalog, "schema_name": schema}
            for raw in _pages(rest, f"{API}/tables", "tables", scope):
                table = _table(raw, tags)
                tables.append(table)
                if (raw.get("properties") or {}).get(FEATURE_MARKER) == "true":
                    features.append({"fqn": table["fqn"] + "_features", "table_fqn": table["fqn"],
                                     "columns": [c["name"] for c in table["columns"]]})
            for raw in _pages(rest, f"{API}/models", "registered_models", scope):
                fqn = _require(raw, "full_name", "registered model")
                versions = [
                    {"version": str(_require(v, "version", f"model {fqn} version")),
                     "status": v.get("status") or "", "comment": v.get("comment") or ""}
                    for v in _pages(rest, f"{API}/models/{fqn}/versions", "model_versions")
                ]
                models.append({"fqn": fqn, "comment": raw.get("comment") or "",
                               "last_altered": _iso(raw.get("updated_at")), "versions": versions})
    return {"tables": tables, "models": models, "features": features,
            "lineage": _lineage(sql, set(names))}


def connector(rest, sql, catalogs=None):
    """A connector is a callable returning a snapshot."""
    return lambda: snapshot(rest, sql, catalogs)


def _env(name):
    value = os.environ.get(name)
    if not value:
        raise ConnectorError(f"environment variable {name} is not set")
    return value


def _request(url, token, body=None, opener=urllib.request.urlopen):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with opener(req, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


class RestClient:
    """Production REST client. Host and token come from HALLMARK_UC_HOST / HALLMARK_UC_TOKEN."""

    def __init__(self, host=None, token=None, opener=urllib.request.urlopen):
        self.host = (host or _env("HALLMARK_UC_HOST")).rstrip("/")
        self.token = token or _env("HALLMARK_UC_TOKEN")
        self.opener = opener

    def get(self, path, params=None):
        query = urllib.parse.urlencode(params or {})
        return _request(f"{self.host}{path}" + (f"?{query}" if query else ""), self.token,
                        opener=self.opener)


class SqlClient:
    """Production SQL client over the statement-execution API (HALLMARK_UC_WAREHOUSE)."""

    def __init__(self, host=None, token=None, warehouse=None, opener=urllib.request.urlopen):
        self.host = (host or _env("HALLMARK_UC_HOST")).rstrip("/")
        self.token = token or _env("HALLMARK_UC_TOKEN")
        self.warehouse = warehouse or _env("HALLMARK_UC_WAREHOUSE")
        self.opener = opener

    def query(self, statement):
        body = _request(f"{self.host}/api/2.0/sql/statements", self.token, {
            "warehouse_id": self.warehouse, "statement": statement, "wait_timeout": "50s",
            "format": "JSON_ARRAY", "disposition": "INLINE"}, self.opener)
        state = (body.get("status") or {}).get("state")
        if state != "SUCCEEDED":
            raise ConnectorError(f"statement did not succeed (state {state})")
        names = [c["name"] for c in body["manifest"]["schema"]["columns"]]
        return [dict(zip(names, row)) for row in (body.get("result") or {}).get("data_array") or []]
