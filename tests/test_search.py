import unittest

import _paths
from hallmark import search
from test_store import built


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.conn, _ = built()

    def fqns(self, query, **kw):
        return [r["fqn"] for r in search.search(self.conn, query, **kw)]

    def test_identifier_is_found_by_its_words(self):
        self.assertEqual(self.fqns("order total")[0], "demo_catalog.sales.orders.order_total")

    def test_comment_words_rank_the_right_column_first(self):
        self.assertEqual(
            self.fqns("gross amount charged", kind="column")[0],
            "demo_catalog.sales.orders.order_total",
        )

    def test_stemming_matches_plural(self):
        self.assertIn("demo_catalog.sales.orders", self.fqns("orders", kind="table"))

    def test_tag_text_is_searchable(self):
        self.assertIn("demo_catalog.crm.customers.full_name", self.fqns("pii", kind="column"))

    def test_kind_filter(self):
        kinds = {r["kind"] for r in search.search(self.conn, "customer", kind="model")}
        self.assertEqual(kinds, {"model"})

    def test_models_and_features_are_indexed(self):
        self.assertEqual(self.fqns("churn")[0], "demo_catalog.ml.churn_model")
        self.assertIn("demo_catalog.ml.customer_features", self.fqns("segment", kind="feature"))

    def test_or_fallback_when_and_finds_nothing(self):
        top = self.fqns("buyer zzzunknownzzz", kind="column")[0]
        self.assertIn(top.rsplit(".", 1)[1], ("customer_id", "full_name"))

    def test_hostile_query_never_reaches_fts_syntax(self):
        for query in ['"; DROP TABLE tables; --', "NEAR(a b)", "a OR", "*", "(", '"']:
            search.search(self.conn, query)

    def test_empty_query_is_empty(self):
        self.assertEqual(search.search(self.conn, "   "), [])

    def test_comment_is_returned(self):
        hit = search.search(self.conn, "order total", kind="column")[0]
        self.assertEqual(hit["comment"], "Gross amount charged")

    def test_bad_kind_raises(self):
        with self.assertRaises(ValueError):
            search.search(self.conn, "x", kind="row")


if __name__ == "__main__":
    unittest.main()
