import io
import json
import os
import sqlite3
import tempfile
import unittest

import _paths
from hallmark import codelinks, mcp, store
from test_store import built


def indexed():
    conn, _ = built()
    codelinks.link_code(conn, os.path.join(_paths.FIXTURES, "src"))
    return conn


def call(conn, name, **args):
    reply = mcp.handle(conn, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": args}})
    result = reply["result"]
    return result.get("isError", False), json.loads(result["content"][0]["text"]) \
        if not result.get("isError") else result["content"][0]["text"]


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.conn = indexed()

    def rpc(self, method, params=None, id_=1):
        return mcp.handle(self.conn, {"jsonrpc": "2.0", "id": id_, "method": method,
                                      "params": params or {}})

    def test_initialize(self):
        reply = self.rpc("initialize")
        self.assertEqual(reply["jsonrpc"], "2.0")
        self.assertEqual(reply["id"], 1)
        self.assertIn("tools", reply["result"]["capabilities"])
        self.assertEqual(reply["result"]["serverInfo"]["name"], "hallmark")

    def test_ping_and_notification(self):
        self.assertEqual(self.rpc("ping")["result"], {})
        self.assertIsNone(mcp.handle(self.conn, {"jsonrpc": "2.0",
                                                 "method": "notifications/initialized"}))

    def test_tools_list_has_every_tool_with_a_schema(self):
        tools = self.rpc("tools/list")["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, set(mcp.TOOLS))
        self.assertTrue(all(t["inputSchema"]["type"] == "object" and t["description"] for t in tools))

    def test_unknown_method_and_tool(self):
        self.assertEqual(self.rpc("nope")["error"]["code"], -32601)
        self.assertEqual(self.rpc("tools/call", {"name": "nope"})["error"]["code"], -32602)
        self.assertEqual(mcp.handle(self.conn, {"id": 3})["error"]["code"], -32600)


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.conn = indexed()

    def test_search(self):
        err, body = call(self.conn, "search", query="order total")
        self.assertFalse(err)
        self.assertEqual(body["items"][0]["fqn"], "demo_catalog.sales.orders.order_total")

    def test_table_to_code_answer(self):
        err, body = call(self.conn, "get_table", fqn="demo_catalog.sales.orders")
        self.assertFalse(err)
        self.assertEqual(len(body["columns"]["items"]), 4)
        self.assertTrue(body["code_references"]["items"])
        err, links = call(self.conn, "code_links", fqn="demo_catalog.sales.orders.order_total")
        self.assertTrue(links["items"])
        self.assertTrue(all({"path", "line", "kind"} <= set(i) for i in links["items"]))
        self.assertIn("candidates", links["note"])

    def test_get_column(self):
        _, body = call(self.conn, "get_column", fqn="demo_catalog.sales.orders.order_total")
        self.assertEqual(body["tags"], {"sensitivity": "financial"})

    def test_lineage_directions_and_depth(self):
        _, up = call(self.conn, "lineage", fqn="demo_catalog.sales.daily_revenue")
        self.assertEqual([e["from"] for e in up["items"]], ["demo_catalog.sales.orders"])
        _, deep = call(self.conn, "lineage", fqn="demo_catalog.sales.daily_revenue", depth=2)
        self.assertEqual(len(deep["items"]), 2)
        _, down = call(self.conn, "lineage", fqn="demo_catalog.crm.customers", direction="downstream")
        self.assertEqual(down["items"][0]["to"], "demo_catalog.sales.orders")
        _, col = call(self.conn, "lineage", fqn="demo_catalog.sales.daily_revenue.revenue_total")
        self.assertEqual(col["kind"], "column")

    def test_models_and_status(self):
        _, body = call(self.conn, "models")
        self.assertEqual(body["models"]["items"][0]["versions"][1]["version"], "2")
        self.assertEqual(len(body["features"]["items"]), 1)
        _, status = call(self.conn, "status")
        self.assertEqual(status["tables"], 3)

    def test_errors_are_tool_errors_not_crashes(self):
        for name, args in (("get_table", {"fqn": "x.y.z"}), ("search", {}),
                           ("lineage", {"fqn": "a", "direction": "sideways"}),
                           ("search", {"query": "x", "limit": 0})):
            err, text = call(self.conn, name, **args)
            self.assertTrue(err, name)
            self.assertIsInstance(text, str)

    def test_output_is_bounded(self):
        _, body = call(self.conn, "search", query="id", limit=1)
        self.assertEqual(len(body["items"]), 1)
        _, big = call(self.conn, "search", query="id", limit=10_000)
        self.assertLessEqual(len(big["items"]), mcp.MAX_LIMIT)
        _, cut = call(self.conn, "get_table", fqn="demo_catalog.sales.orders", limit=2)
        self.assertTrue(cut["columns"]["truncated"])
        self.assertEqual(len(cut["columns"]["items"]), 2)


class ServeTest(unittest.TestCase):
    def test_serve_over_streams_with_readonly_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "idx.db")
            conn = store.connect(path)
            store.refresh(conn, built()[1])
            conn.close()
            lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
                     "not json",
                     json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                     json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                 "params": {"name": "status", "arguments": {}}})]
            out = io.StringIO()
            mcp.serve(path, io.StringIO("\n".join(lines) + "\n"), out)
            replies = [json.loads(l) for l in out.getvalue().splitlines()]
            self.assertEqual([r["id"] for r in replies], [1, None, 2])
            self.assertEqual(replies[1]["error"]["code"], -32700)

    def test_index_is_opened_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "idx.db")
            store.connect(path).close()
            ro = store.connect(path, readonly=True)
            with self.assertRaises(sqlite3.OperationalError):
                ro.execute("INSERT INTO meta VALUES ('k', 'v')")

    def test_module_imports_no_connector_or_network(self):
        with open(mcp.__file__, encoding="utf-8") as handle:
            imports = [l for l in handle.read().splitlines() if l.startswith(("import ", "from "))]
        self.assertEqual(sorted(imports), ["from . import codelinks, search, store", "import json",
                                           "import sys"])


if __name__ == "__main__":
    unittest.main()
