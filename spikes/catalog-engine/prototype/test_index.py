import copy
import os
import tempfile
import unittest

import index

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
SRC = os.path.join(FIXTURE, "src")


def built():
    conn = index.connect(":memory:")
    snapshot = index.load_snapshot(FIXTURE)
    index.refresh(conn, snapshot)
    index.link_code(conn, SRC)
    return conn, snapshot


class IndexTest(unittest.TestCase):
    def test_build_counts(self):
        conn, _ = built()
        counts = index.stats(conn)
        self.assertEqual(counts["tables"], 3)
        self.assertEqual(counts["columns"], 9)
        self.assertEqual(counts["lineage"], 3)

    def test_column_links_to_file_and_line(self):
        conn, _ = built()
        refs = index.code_refs(conn, "demo_catalog.sales.orders.order_total")
        self.assertEqual(refs, [("orders_etl.sql", 3, "bare")])
        refs = index.code_refs(conn, "demo_catalog.crm.customers.segment")
        self.assertEqual(refs, [("customers_job.py", 5, "bare")])

    def test_qualified_reference_is_marked_qualified(self):
        conn, _ = built()
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "q.sql"), "w", encoding="utf-8") as handle:
                handle.write("SELECT 1\nFROM x WHERE sales.orders.order_id > 0\n")
            index.link_code(conn, tmp)
        refs = index.code_refs(conn, "demo_catalog.sales.orders.order_id")
        self.assertEqual(refs, [("q.sql", 2, "qualified")])

    def test_column_without_code_has_no_refs(self):
        conn, _ = built()
        self.assertEqual(index.code_refs(conn, "demo_catalog.sales.orders.order_id"), [])

    def test_refresh_rewrites_only_changed_tables(self):
        conn, snapshot = built()
        self.assertEqual(index.refresh(conn, snapshot), {"rewritten": 0, "dropped": 0})
        changed = copy.deepcopy(snapshot)
        changed["tables"][2]["last_altered"] = "2026-02-01T00:00:00"
        changed["tables"][2]["columns"].append({"name": "region", "type": "string", "comment": "Sales region"})
        self.assertEqual(index.refresh(conn, changed), {"rewritten": 1, "dropped": 0})
        self.assertEqual(index.stats(conn)["columns"], 10)

    def test_dropped_table_leaves_the_index_but_lineage_stays(self):
        conn, snapshot = built()
        smaller = copy.deepcopy(snapshot)
        smaller["tables"] = smaller["tables"][:2]
        smaller["lineage"] = []
        self.assertEqual(index.refresh(conn, smaller)["dropped"], 1)
        self.assertEqual(index.stats(conn)["tables"], 2)
        self.assertEqual(index.stats(conn)["lineage"], 3)

    def test_search_finds_column_by_description_word(self):
        conn, _ = built()
        found = {fqn for fqn, _ in index.search(conn, "marketing")}
        self.assertEqual(found, {"demo_catalog.crm.customers.segment"})


if __name__ == "__main__":
    unittest.main()
