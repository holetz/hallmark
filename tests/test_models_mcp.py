import io
import json
import unittest

import _mlflow_stub as stub
from hallmark import mlflow_client as mc
from hallmark import models_mcp


class Client(stub.StubClient):
    """The stub, with a missing resource raised as the MlflowError a real 404 becomes."""

    def get(self, path, params=None):
        try:
            return super().get(path, params)
        except (KeyError, StopIteration):
            raise mc.MlflowError(f"{path}: RESOURCE_DOES_NOT_EXIST") from None


def call(client, tool, **args):
    reply = models_mcp.handle(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": tool, "arguments": args}})
    result = reply["result"]
    text = result["content"][0]["text"]
    return result.get("isError", False), (text if result.get("isError") else json.loads(text))


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.client = Client()

    def rpc(self, method, params=None):
        return models_mcp.handle(self.client, {"jsonrpc": "2.0", "id": 1, "method": method,
                                               "params": params or {}})

    def test_initialize_ping_and_notification(self):
        self.assertEqual(self.rpc("initialize")["result"]["serverInfo"]["name"], "hallmark-models")
        self.assertEqual(self.rpc("ping")["result"], {})
        self.assertIsNone(models_mcp.handle(self.client, {"jsonrpc": "2.0",
                                                          "method": "notifications/initialized"}))

    def test_every_tool_is_listed_read_only(self):
        tools = self.rpc("tools/list")["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, set(models_mcp.TOOLS))
        self.assertTrue(all(t["inputSchema"]["type"] == "object" and t["description"] for t in tools))
        for forbidden in ("promote", "transition", "delete", "train", "register"):
            self.assertFalse(any(forbidden in t["name"] for t in tools))

    def test_errors(self):
        self.assertEqual(self.rpc("nope")["error"]["code"], -32601)
        self.assertEqual(self.rpc("tools/call", {"name": "nope"})["error"]["code"], -32602)
        self.assertEqual(models_mcp.handle(self.client, {"id": 3})["error"]["code"], -32600)

    def test_serve_loop_over_streams(self):
        out = io.StringIO()
        lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}), "", "{bad"]
        models_mcp.serve(self.client, io.StringIO("\n".join(lines) + "\n"), out)
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(replies[0]["result"], {})
        self.assertEqual(replies[1]["error"]["code"], -32700)

    def test_no_request_ever_leaves_the_read_only_surface(self):
        for name, args in (("list_models", {}), ("get_model", {"name": "demo_churn"}),
                           ("list_runs", {"experiment_id": "1"}), ("drift", {"model": "demo_churn"}),
                           ("performance", {"model": "demo_churn", "metric": "auc"})):
            call(self.client, name, **args)
        posts = {path for method, path, _ in self.client.calls if method == "POST"}
        self.assertLessEqual(posts, set(mc.SEARCH_PATHS))


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.client = Client()

    def test_list_models_and_filter(self):
        _, body = call(self.client, "list_models")
        self.assertEqual([m["name"] for m in body["items"]], ["demo_churn", "demo_fraud"])
        self.assertEqual(body["items"][0]["aliases"], {"champion": "2"})
        _, body = call(self.client, "list_models", name_filter="FRAUD")
        self.assertEqual([m["name"] for m in body["items"]], ["demo_fraud"])

    def test_limit_truncates_and_validates(self):
        _, body = call(self.client, "list_models", limit=1)
        self.assertEqual((len(body["items"]), body["total"], body["truncated"]), (1, 2, True))
        self.assertTrue(call(self.client, "list_models", limit=0)[0])

    def test_get_model_versions_newest_first(self):
        _, body = call(self.client, "get_model", name="demo_churn")
        self.assertEqual([v["version"] for v in body["versions"]["items"]], ["3", "2", "1"])
        self.assertEqual(body["versions"]["items"][1]["stage"], "Production")

    def test_unknown_model_and_quote_are_errors(self):
        self.assertTrue(call(self.client, "get_model", name="demo_nope")[0])
        error, text = call(self.client, "get_model", name="x' OR '1'='1")
        self.assertTrue(error)
        self.assertIn("quote", text)

    def test_experiments_and_runs(self):
        _, body = call(self.client, "list_experiments")
        self.assertEqual([e["name"] for e in body["items"]], ["demo_churn_training", "demo_fraud_training"])
        _, body = call(self.client, "list_runs", experiment_id="1", limit=2)
        self.assertEqual([r["name"] for r in body["items"]], ["churn_v3", "churn_v2"])
        self.assertTrue(body["truncated"])
        self.assertNotIn("params", body["items"][0])

    def test_get_run(self):
        _, body = call(self.client, "get_run", run_id="run_c2")
        self.assertEqual(body["metrics"]["auc"], 0.86)
        self.assertEqual(body["params"]["total"], 3)
        self.assertTrue(call(self.client, "get_run", run_id="missing")[0])
        self.assertTrue(call(self.client, "get_run")[0])


class MetricsTest(unittest.TestCase):
    def setUp(self):
        self.client = Client()

    def test_metric_history_summary_and_cap(self):
        _, body = call(self.client, "metric_history", run_id="run_c2", metric="auc", limit=2)
        self.assertEqual(body["summary"]["points"], 6)
        self.assertEqual((body["summary"]["min"], body["summary"]["max"], body["summary"]["last"]),
                         (0.80, 0.86, 0.85))
        self.assertEqual([p["step"] for p in body["history"]["items"]], [5, 4])
        self.assertTrue(body["history"]["truncated"])

    def test_metric_history_without_points(self):
        _, body = call(self.client, "metric_history", run_id="run_c2", metric="nothing")
        self.assertEqual(body["summary"], {"points": 0})

    def test_performance_defaults_to_champion_and_compares_to_previous(self):
        _, body = call(self.client, "performance", model="demo_churn")
        self.assertEqual((body["selected"], body["compared_to"]), ("2", "1"))
        self.assertEqual(body["changes"]["auc"], 0.05)
        self.assertEqual([v["version"] for v in body["versions"]["items"]], ["3", "2", "1"])
        self.assertNotIn("psi_score", body["versions"]["items"][0]["metrics"])
        self.assertNotIn("stability", body)

    def test_performance_explicit_version_with_stability(self):
        _, body = call(self.client, "performance", model="demo_churn", version="3", metric="auc")
        self.assertEqual((body["selected"], body["compared_to"]), ("3", "2"))
        self.assertAlmostEqual(body["changes"]["auc"], -0.02)
        self.assertEqual(body["stability"]["points"], 0)

    def test_performance_single_version_has_nothing_to_compare(self):
        _, body = call(self.client, "performance", model="demo_fraud")
        self.assertEqual((body["compared_to"], body["changes"]), (None, {}))

    def test_performance_unknown_version(self):
        self.assertTrue(call(self.client, "performance", model="demo_churn", version="9")[0])


class DriftTest(unittest.TestCase):
    def setUp(self):
        self.client = Client()

    def test_champion_within_threshold_flags_only_the_breach(self):
        _, body = call(self.client, "drift", model="demo_churn")
        self.assertEqual(body["version"], "2")
        self.assertEqual([a["metric"] for a in body["alerts"]], ["ks_stat_age"])
        self.assertEqual(body["alerts"][0]["status"], "medium")
        self.assertTrue(body["monitored"])

    def test_param_threshold_wins_and_severity_scales(self):
        _, body = call(self.client, "drift", model="demo_churn", version="3")
        psi = [c for c in body["checks"] if c["metric"] == "psi_score"][0]
        self.assertEqual((psi["threshold"], psi["status"]), (0.2, "high"))
        ks = [c for c in body["checks"] if c["metric"] == "ks_stat_age"][0]
        self.assertEqual((ks["threshold"], ks["status"]), (0.2, "ok"))

    def test_argument_threshold_applies_without_a_param(self):
        _, body = call(self.client, "drift", model="demo_churn", version="1", threshold=0.01)
        self.assertEqual(body["alerts"][0]["metric"], "psi_score")

    def test_run_without_drift_metrics_is_not_monitored(self):
        _, body = call(self.client, "drift", model="demo_fraud")
        self.assertEqual((body["monitored"], body["alerts"]), (False, []))

    def test_bad_threshold(self):
        self.assertTrue(call(self.client, "drift", model="demo_churn", threshold=-1)[0])
        self.assertTrue(call(self.client, "drift", model="demo_churn", threshold=True)[0])


if __name__ == "__main__":
    unittest.main()
