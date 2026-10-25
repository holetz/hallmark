import copy
import json
import os
import unittest

import _paths
from hallmark import snapshot as snapshot_mod
from hallmark import store


def load():
    with open(os.path.join(_paths.FIXTURES, "snapshot.json"), encoding="utf-8") as handle:
        return json.load(handle)


def built():
    conn = store.connect(":memory:")
    snap = load()
    store.refresh(conn, snap)
    return conn, snap


class StoreTest(unittest.TestCase):
    def test_build_counts(self):
        conn, _ = built()
        counts = store.stats(conn)
        self.assertEqual(counts["tables"], 3)
        self.assertEqual(counts["columns"], 9)
        self.assertEqual(counts["lineage"], 3)
        self.assertEqual(counts["models"], 1)
        self.assertEqual(counts["features"], 1)
        self.assertEqual(counts["tags"], 5)
        self.assertIsNotNone(counts["last_refresh"])

    def test_second_identical_refresh_rewrites_nothing(self):
        conn, snap = built()
        result = store.refresh(conn, snap)
        self.assertEqual(result, {"rewritten": 0, "dropped": 0, "lineage_added": 0})

    def test_changed_last_altered_rewrites_only_that_table(self):
        conn, snap = built()
        snap = copy.deepcopy(snap)
        snap["tables"][1]["last_altered"] = "2026-02-01T00:00:00"
        snap["tables"][1]["columns"].append({"name": "revenue_note", "type": "string"})
        self.assertEqual(store.refresh(conn, snap)["rewritten"], 1)
        names = [r[0] for r in conn.execute(
            "SELECT name FROM columns WHERE table_fqn = 'demo_catalog.sales.daily_revenue'")]
        self.assertIn("revenue_note", names)

    def test_tag_change_alone_rewrites_the_table(self):
        conn, snap = built()
        snap = copy.deepcopy(snap)
        snap["tables"][2]["columns"][1]["tags"] = {"pii": "false"}
        self.assertEqual(store.refresh(conn, snap)["rewritten"], 1)
        value = conn.execute(
            "SELECT value FROM tags WHERE entity_fqn = 'demo_catalog.crm.customers.full_name'"
        ).fetchone()[0]
        self.assertEqual(value, "false")

    def test_vanished_table_is_dropped_with_columns_tags_and_search(self):
        conn, snap = built()
        snap = copy.deepcopy(snap)
        del snap["tables"][2]
        self.assertEqual(store.refresh(conn, snap)["dropped"], 1)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM columns WHERE table_fqn = 'demo_catalog.crm.customers'"
        ).fetchone()[0], 0)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM tags WHERE entity_fqn LIKE 'demo_catalog.crm.customers%'"
        ).fetchone()[0], 0)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM search WHERE fqn LIKE 'demo_catalog.crm.customers%'"
        ).fetchone()[0], 0)

    def test_lineage_edges_survive_a_snapshot_that_omits_them(self):
        conn, snap = built()
        snap = copy.deepcopy(snap)
        snap["lineage"] = []
        store.refresh(conn, snap)
        self.assertEqual(store.stats(conn)["lineage"], 3)

    def test_new_lineage_edge_is_added(self):
        conn, snap = built()
        snap = copy.deepcopy(snap)
        snap["lineage"] = [{"kind": "table", "src": "demo_catalog.crm.customers",
                            "dst": "demo_catalog.sales.daily_revenue"}]
        self.assertEqual(store.refresh(conn, snap)["lineage_added"], 1)

    def test_models_are_replaced_wholesale(self):
        conn, snap = built()
        snap = copy.deepcopy(snap)
        snap["models"] = []
        store.refresh(conn, snap)
        self.assertEqual(store.stats(conn)["models"], 0)

    def test_split_identifier(self):
        self.assertEqual(store.split_identifier("order_total"), "order total")
        self.assertEqual(store.split_identifier("orderTotal"), "order Total")


class SnapshotContractTest(unittest.TestCase):
    def test_fixture_conforms(self):
        snapshot_mod.validate(load())

    def test_rejects_bad_shapes(self):
        bad = [
            {},
            {"tables": [{"fqn": "a.b", "last_altered": "x", "columns": []}]},
            {"tables": [{"fqn": "a.b.c", "columns": []}]},
            {"tables": [{"fqn": "a.b.c", "last_altered": "x", "columns": [{"name": "k"}, {"name": "k"}]}]},
            {"tables": [], "lineage": [{"kind": "row", "src": "a", "dst": "b"}]},
            {"tables": [{"fqn": "a.b.c", "last_altered": "x", "columns": [], "tags": {"k": 1}}]},
        ]
        for snap in bad:
            with self.assertRaises(snapshot_mod.SnapshotError, msg=str(snap)):
                snapshot_mod.validate(snap)


if __name__ == "__main__":
    unittest.main()
