import os
import tempfile
import time
import unittest

import _paths
from hallmark import codelinks, store
from test_store import built

SRC = os.path.join(_paths.FIXTURES, "src")


class CodeLinksTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.snap = built()
        self.counts = codelinks.link_code(self.conn, SRC)

    def test_qualified_hit_points_to_file_and_line(self):
        refs = codelinks.code_refs(self.conn, "demo_catalog.sales.orders.order_total")
        qualified = [r for r in refs if r["kind"] == "qualified"]
        self.assertEqual([(r["path"], r["line"]) for r in qualified], [("orders_etl.sql", 5)])

    def test_alias_use_is_a_bare_candidate(self):
        refs = codelinks.code_refs(self.conn, "demo_catalog.sales.orders.order_total")
        bare = [(r["path"], r["line"]) for r in refs if r["kind"] == "bare"]
        self.assertEqual(bare, [("orders_etl.sql", 7)])

    def test_python_string_literal_links_when_the_table_is_named(self):
        refs = codelinks.code_refs(self.conn, "demo_catalog.crm.customers.full_name")
        self.assertEqual([(r["path"], r["line"], r["kind"]) for r in refs],
                         [("customers_job.py", 6, "bare")])

    def test_bare_word_in_file_without_the_table_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "standalone.py"), "w", encoding="utf-8") as handle:
                handle.write("order_total = 0\n")
            codelinks.link_code(self.conn, tmp)
        self.assertEqual(codelinks.code_refs(self.conn, "demo_catalog.sales.orders.order_total"), [])

    def test_other_tables_column_is_not_linked_by_a_qualified_name_of_another(self):
        refs = codelinks.code_refs(self.conn, "demo_catalog.sales.daily_revenue.revenue_total")
        self.assertTrue(any(r["path"] == "orders_etl.sql" for r in refs))

    def test_table_refs(self):
        refs = codelinks.table_refs(self.conn, "demo_catalog.sales.orders")
        self.assertEqual([(r["path"], r["line"]) for r in refs],
                         [("orders_etl.sql", 5), ("orders_etl.sql", 6)])
        self.assertEqual(self.counts["table_refs"], len(self._all_table_refs()))

    def _all_table_refs(self):
        return self.conn.execute("SELECT * FROM table_ref").fetchall()

    def test_relink_is_idempotent(self):
        again = codelinks.link_code(self.conn, SRC)
        self.assertEqual(again, self.counts)
        self.assertEqual(store.stats(self.conn)["code_ref"], self.counts["column_refs"])

    def test_scales_to_many_columns(self):
        conn = store.connect(":memory:")
        tables = [{"fqn": f"demo_catalog.s{t % 5}.t{t}", "last_altered": "x",
                   "columns": [{"name": f"c{c}_val"} for c in range(20)]} for t in range(500)]
        store.refresh(conn, {"tables": tables})
        start = time.perf_counter()
        codelinks.link_code(conn, SRC)
        self.assertLess(time.perf_counter() - start, 5.0)


if __name__ == "__main__":
    unittest.main()
