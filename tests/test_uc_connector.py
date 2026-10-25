import io
import json
import os
import unittest

import _paths
from hallmark import snapshot as snapshot_mod
from hallmark import store
from hallmark.connectors import unity_catalog as uc


def fixture(name):
    with open(os.path.join(_paths.FIXTURES, "uc", name), encoding="utf-8") as handle:
        return json.load(handle)


class StubRest:
    def __init__(self):
        self.calls = []

    def get(self, path, params=None):
        params = params or {}
        self.calls.append((path, dict(params)))
        if path.endswith("/catalogs"):
            return fixture("catalogs.json")
        if path.endswith("/schemas"):
            return fixture("schemas.json")
        if path.endswith("/tables"):
            if params["schema_name"] != "sales":
                return {}
            return fixture("tables_page2.json" if params.get("page_token") else "tables_page1.json")
        if path.endswith("/models"):
            return fixture("models.json") if params["schema_name"] == "ml" else {}
        if path.endswith("/versions"):
            return fixture("models.json")["versions"][path.split("/models/")[1][:-len("/versions")]]
        raise AssertionError(path)


class StubSql:
    def __init__(self):
        self.statements = []

    def query(self, statement):
        self.statements.append(statement)
        tags, lineage = fixture("tags.json"), fixture("lineage.json")
        for key, rows in (("table_tags", tags), ("column_tags", tags),
                          ("table_lineage", lineage), ("column_lineage", lineage)):
            if key in statement:
                return rows[key]
        raise AssertionError(statement)


def build():
    rest, sql = StubRest(), StubSql()
    return uc.snapshot(rest, sql), rest, sql


class ConnectorTest(unittest.TestCase):
    def test_snapshot_is_valid_and_complete(self):
        snap, _, _ = build()
        snapshot_mod.validate(snap)
        fqns = [t["fqn"] for t in snap["tables"]]
        self.assertEqual(fqns, ["demo_catalog.sales.orders", "demo_catalog.sales.customers",
                                "demo_catalog.sales.daily_revenue"])

    def test_pagination_follows_the_token(self):
        _, rest, _ = build()
        pages = [p for path, p in rest.calls if path.endswith("/tables")]
        self.assertEqual([p.get("page_token") for p in pages if p["schema_name"] == "sales"],
                         [None, "page2-token"])

    def test_system_catalog_and_information_schema_are_skipped(self):
        snap, rest, _ = build()
        schemas = {p["schema_name"] for path, p in rest.calls if path.endswith("/tables")}
        self.assertEqual(schemas, {"sales", "ml"})
        self.assertFalse(any("system" == p.get("catalog_name") for _, p in rest.calls))

    def test_catalog_identifier_is_quoted_and_validated(self):
        seen = []

        class Recorder:
            def query(self, statement):
                seen.append(statement)
                return []

        uc._tags(Recorder(), "demo-sales")
        self.assertTrue(all("`demo-sales`.information_schema" in s for s in seen))
        self.assertEqual(len(seen), 2)
        with self.assertRaises(uc.ConnectorError):
            uc._tags(Recorder(), "bad`name")
        self.assertEqual(len(seen), 2)

    def test_tags_and_column_tags_are_mapped(self):
        snap, _, _ = build()
        orders = snap["tables"][0]
        self.assertEqual(orders["tags"], {"domain": "sales"})
        total = [c for c in orders["columns"] if c["name"] == "order_total"][0]
        self.assertEqual(total["tags"], {"sensitivity": "financial"})

    def test_updated_at_becomes_iso_and_columns_keep_order(self):
        snap, _, _ = build()
        self.assertEqual(snap["tables"][0]["last_altered"], "2026-01-10T08:00:00")
        self.assertEqual([c["name"] for c in snap["tables"][0]["columns"]],
                         ["order_id", "customer_id", "order_total"])

    def test_models_with_versions(self):
        snap, _, _ = build()
        model = snap["models"][0]
        self.assertEqual(model["fqn"], "demo_catalog.ml.churn_model")
        self.assertEqual([v["version"] for v in model["versions"]], ["1", "2"])

    def test_feature_derived_from_marker(self):
        snap, _, _ = build()
        self.assertEqual(snap["features"], [{
            "fqn": "demo_catalog.sales.customers_features",
            "table_fqn": "demo_catalog.sales.customers", "columns": ["customer_id", "segment"]}])

    def test_lineage_drops_external_ends_and_qualifies_columns(self):
        snap, _, _ = build()
        self.assertEqual(len(snap["lineage"]), 3)
        self.assertIn({"kind": "column", "src": "demo_catalog.sales.orders.order_total",
                       "dst": "demo_catalog.sales.daily_revenue.revenue_total"}, snap["lineage"])

    def test_missing_required_field_names_it(self):
        class Broken(StubRest):
            def get(self, path, params=None):
                if path.endswith("/tables"):
                    return {"tables": [{"comment": "no name"}]}
                return super().get(path, params)

        with self.assertRaisesRegex(uc.ConnectorError, "full_name"):
            uc.snapshot(Broken(), StubSql())

    def test_snapshot_feeds_the_store_and_refresh_is_idempotent(self):
        snap, _, _ = build()
        conn = store.connect(":memory:")
        first = store.refresh(conn, snap)
        self.assertEqual(first["rewritten"], 3)
        self.assertEqual(store.refresh(conn, snap)["rewritten"], 0)
        self.assertEqual(store.stats(conn)["features"], 1)

    def test_connector_is_a_callable(self):
        snap = uc.connector(StubRest(), StubSql())()
        self.assertEqual(len(snap["tables"]), 3)


class ProductionClientTest(unittest.TestCase):
    def opener(self, payload, seen):
        def open_(req, timeout=None):
            seen.append(req)
            return io.BytesIO(json.dumps(payload).encode("utf-8"))
        return open_

    def test_rest_client_builds_url_and_bearer(self):
        seen = []
        client = uc.RestClient("https://uc.example.invalid/", "synthetic-token",
                               self.opener({"ok": 1}, seen))
        self.assertEqual(client.get("/api/2.1/unity-catalog/tables", {"catalog_name": "c"}), {"ok": 1})
        self.assertEqual(seen[0].full_url,
                         "https://uc.example.invalid/api/2.1/unity-catalog/tables?catalog_name=c")
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer synthetic-token")

    def test_sql_client_zips_columns_and_rows(self):
        seen = []
        payload = {"status": {"state": "SUCCEEDED"},
                   "manifest": {"schema": {"columns": [{"name": "a"}, {"name": "b"}]}},
                   "result": {"data_array": [["1", "2"]]}}
        client = uc.SqlClient("https://uc.example.invalid", "t", "wh", self.opener(payload, seen))
        self.assertEqual(client.query("SELECT 1"), [{"a": "1", "b": "2"}])

    def test_sql_client_raises_on_failed_statement(self):
        client = uc.SqlClient("https://uc.example.invalid", "t", "wh",
                              self.opener({"status": {"state": "FAILED"}}, []))
        with self.assertRaises(uc.ConnectorError):
            client.query("SELECT 1")

    def test_missing_environment_is_a_named_error(self):
        os.environ.pop("HALLMARK_UC_HOST", None)
        with self.assertRaisesRegex(uc.ConnectorError, "HALLMARK_UC_HOST"):
            uc.RestClient()


if __name__ == "__main__":
    unittest.main()
