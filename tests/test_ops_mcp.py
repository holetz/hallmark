import io
import json
import unittest
from unittest import mock

import _dbx_stub as stub
from hallmark import databricks_client as dc
from hallmark import ops_mcp

NOW = stub.DATA["now_ms"] / 1000
OUTPUTS = {"9101": {"error": "Source table demo_raw.orders is empty", "error_trace": "T" * 3000},
           "9203": {"error": "Column demo_amount not found"}}


class Client(stub.StubClient):
    """The stub, with task outputs added and a missing resource raised as the error a 404 becomes."""

    def get(self, path, params=None):
        try:
            if path.endswith("/get-output") and str(params["run_id"]) in OUTPUTS:
                return OUTPUTS[str(params["run_id"])]
            return super().get(path, params)
        except (KeyError, StopIteration):
            raise dc.DatabricksError(f"{path}: RESOURCE_DOES_NOT_EXIST") from None


def call(client, tool, **args):
    with mock.patch.object(ops_mcp, "_now", return_value=args.pop("now", NOW)):
        reply = ops_mcp.handle(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": tool, "arguments": args}})
    result = reply["result"]
    text = result["content"][0]["text"]
    return result.get("isError", False), (text if result.get("isError") else json.loads(text))


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.client = Client()

    def rpc(self, method, params=None):
        return ops_mcp.handle(self.client, {"jsonrpc": "2.0", "id": 1, "method": method,
                                            "params": params or {}})

    def test_initialize_ping_and_notification(self):
        self.assertEqual(self.rpc("initialize")["result"]["serverInfo"]["name"], "hallmark-ops")
        self.assertEqual(self.rpc("ping")["result"], {})
        self.assertIsNone(ops_mcp.handle(self.client, {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tools_list_has_no_write_tool(self):
        names = [t["name"] for t in self.rpc("tools/list")["result"]["tools"]]
        self.assertEqual(len(names), 9)
        for word in ("run", "trigger", "cancel", "repair", "delete", "create", "update", "start", "stop"):
            self.assertFalse([n for n in names if n.split("_")[0] == word], word)

    def test_unknown_method_tool_and_invalid_request(self):
        self.assertEqual(self.rpc("nope")["error"]["code"], -32601)
        self.assertEqual(self.rpc("tools/call", {"name": "run_now"})["error"]["code"], -32602)
        self.assertEqual(ops_mcp.handle(self.client, {"id": 1})["error"]["code"], -32600)

    def test_serve_answers_lines_and_reports_parse_errors(self):
        out = io.StringIO()
        ops_mcp.serve(self.client, io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n\nnot json\n'), out)
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(lines[0]["result"], {})
        self.assertEqual(lines[1]["error"]["code"], -32700)

    def test_only_reads_reach_the_client(self):
        for tool, args in (("list_jobs", {}), ("job_status", {"job_id": 101}), ("list_failures", {}),
                           ("get_run", {"run_id": 9001}), ("reruns", {"job_id": 101}),
                           ("list_pipelines", {}), ("pipeline_status", {"pipeline_id": "pipe-orders"}),
                           ("data_quality", {"pipeline_id": "pipe-orders"}), ("alerts", {})):
            call(self.client, tool, **args)
        self.assertTrue(self.client.calls)
        for path, _ in self.client.calls:
            self.assertTrue(path in dc.READ_PATHS or dc.PIPELINE_PATH.fullmatch(path), path)


class JobsTest(unittest.TestCase):
    def setUp(self):
        self.client = Client()

    def test_list_jobs_filters_and_truncates(self):
        err, body = call(self.client, "list_jobs")
        self.assertEqual([j["name"] for j in body["items"]],
                         ["demo_hourly_sync", "demo_nightly_load", "demo_report_build"])
        err, body = call(self.client, "list_jobs", name_filter="NIGHTLY")
        self.assertEqual(body["total"], 1)
        err, body = call(self.client, "list_jobs", limit=2)
        self.assertTrue(body["truncated"])
        self.assertEqual(len(body["items"]), 2)

    def test_job_status_summarises_runs(self):
        err, body = call(self.client, "job_status", job_id=101)
        self.assertEqual(body["name"], "demo_nightly_load")
        self.assertEqual(body["running"], 1)
        self.assertEqual(body["consecutive_failures"], 0)
        self.assertIsNotNone(body["last_success"])
        self.assertEqual(body["runs"]["items"][0]["life_cycle"], "RUNNING")
        err, body = call(self.client, "job_status", job_id="102")
        self.assertEqual(body["consecutive_failures"], 3)
        self.assertIsNone(body["last_success"])

    def test_missing_job_is_a_tool_error(self):
        err, text = call(self.client, "job_status", job_id=999)
        self.assertTrue(err)
        self.assertIn("RESOURCE_DOES_NOT_EXIST", text)

    def test_bad_arguments_are_tool_errors(self):
        self.assertTrue(call(self.client, "job_status")[0])
        self.assertTrue(call(self.client, "job_status", job_id=True)[0])
        self.assertTrue(call(self.client, "list_failures", hours=0)[0])
        self.assertTrue(call(self.client, "list_failures", hours=721)[0])
        self.assertTrue(call(self.client, "list_jobs", limit=0)[0])
        self.assertTrue(call(self.client, "pipeline_status", pipeline_id="a/../stop")[0])


class FailuresTest(unittest.TestCase):
    def setUp(self):
        self.client = Client()

    def test_failures_in_the_default_window(self):
        err, body = call(self.client, "list_failures")
        ids = [f["run_id"] for f in body["items"]]
        self.assertEqual(ids, [9004, 9003, 9001, 9008])
        self.assertEqual(body["canceled"], 1)
        failed = {f["run_id"]: f for f in body["items"]}
        self.assertEqual(failed[9003]["failed_tasks"][0]["task_key"], "transform")
        self.assertIn("demo_amount", failed[9003]["failed_tasks"][0]["message"])
        self.assertEqual(failed[9004]["result"], "TIMEDOUT")
        self.assertEqual(failed[9008]["life_cycle"], "INTERNAL_ERROR")

    def test_window_and_job_filter(self):
        err, body = call(self.client, "list_failures", hours=1 / 36, now=(stub.DATA["now_ms"] - 340000) / 1000)
        self.assertEqual([f["run_id"] for f in body["items"]], [9004, 9003])
        err, body = call(self.client, "list_failures", job_id=101)
        self.assertEqual([f["run_id"] for f in body["items"]], [9001])
        self.assertEqual(self.client.calls[-1][1]["job_id"], "101")

    def test_limit_truncates(self):
        err, body = call(self.client, "list_failures", limit=1)
        self.assertEqual((len(body["items"]), body["total"], body["truncated"]), (1, 4, True))


class RunAndRerunsTest(unittest.TestCase):
    def setUp(self):
        self.client = Client()

    def test_get_run_reads_the_output_of_failed_tasks_and_cuts_the_trace(self):
        err, body = call(self.client, "get_run", run_id=9001)
        self.assertEqual(body["outputs"][0]["run_id"], 9101)
        out = body["outputs"][0]["output"]
        self.assertIn("demo_raw.orders", out["error"])
        self.assertLessEqual(len(out["error_trace"]), ops_mcp.TEXT_CAP + 3)

    def test_get_run_with_repair_history(self):
        err, body = call(self.client, "get_run", run_id=9003)
        self.assertEqual([h["type"] for h in body["repair_history"]], ["ORIGINAL", "REPAIR"])
        self.assertEqual(len(body["tasks"]["items"]), 2)

    def test_get_run_without_readable_output(self):
        err, body = call(self.client, "get_run", run_id=9008)
        self.assertEqual(body["outputs"], [{"run_id": 9008, "output": None}])

    def test_successful_run_reads_no_output(self):
        err, body = call(self.client, "get_run", run_id=9005)
        self.assertEqual(body["outputs"], [])
        self.assertFalse([c for c in self.client.calls if c[0].endswith("get-output")])

    def test_reruns_report_retries_and_repairs(self):
        err, body = call(self.client, "reruns", job_id=101)
        item = body["items"][0]
        self.assertEqual((item["run_id"], item["attempts"], item["retried_by"], item["recovered"]),
                         (9001, 2, [9002], True))
        err, body = call(self.client, "reruns", job_id=102)
        by_id = {i["run_id"]: i for i in body["items"]}
        self.assertEqual((by_id[9003]["repairs"], by_id[9003]["recovered"]), (1, True))
        self.assertEqual((by_id[9004]["attempts"], by_id[9004]["recovered"]), (1, False))
        self.assertEqual(body["failed_runs"], 3)

    def test_reruns_start_nothing(self):
        call(self.client, "reruns", job_id=102)
        self.assertFalse([c for c in self.client.calls if "run-now" in c[0] or "repair" in c[0]])


class PipelinesTest(unittest.TestCase):
    def setUp(self):
        self.client = Client()

    def test_list_pipelines(self):
        err, body = call(self.client, "list_pipelines", name_filter="orders")
        self.assertEqual(body["items"][0]["pipeline_id"], "pipe-orders")
        self.assertEqual(body["items"][0]["health"], "UNHEALTHY")
        self.assertEqual(call(self.client, "list_pipelines")[1]["total"], 2)

    def test_pipeline_status_lists_updates_and_errors(self):
        err, body = call(self.client, "pipeline_status", pipeline_id="pipe-orders")
        self.assertEqual([u["state"] for u in body["updates"]["items"]], ["COMPLETED", "FAILED"])
        self.assertEqual(body["errors"]["total"], 1)
        self.assertIn("expectation violated", body["errors"]["items"][0]["message"])

    def test_data_quality_uses_the_latest_update_with_expectations(self):
        err, body = call(self.client, "data_quality", pipeline_id="pipe-orders")
        rows = {(r["dataset"], r["expectation"]): r for r in body["items"]}
        self.assertEqual(body["update_id"], "u2")
        self.assertTrue(body["monitored"])
        bad = rows[("demo_main.sales.orders", "valid_amount")]
        self.assertEqual((bad["passed"], bad["failed"], bad["alert"], bad["pass_rate"]), (990, 10, True, 0.99))
        self.assertFalse(rows[("demo_main.sales.customers", "has_email_shape")]["alert"])
        self.assertEqual(body["alerts"], 1)

    def test_data_quality_table_filter_by_full_or_last_segment(self):
        for table in ("demo_main.sales.orders", "orders"):
            err, body = call(self.client, "data_quality", pipeline_id="pipe-orders", table=table)
            self.assertEqual({r["dataset"] for r in body["items"]}, {"demo_main.sales.orders"})

    def test_data_quality_unmonitored_pipeline(self):
        client = Client()
        with mock.patch.dict(stub.DATA["events"], {"pipe-events": []}):
            err, body = call(client, "data_quality", pipeline_id="pipe-events")
        self.assertFalse(body["monitored"])
        self.assertEqual(body["items"], [])


class AlertsTest(unittest.TestCase):
    def test_alerts_filter_by_name_and_state_and_hide_owner(self):
        client = Client()
        err, body = call(client, "alerts", name_filter="demo_orders")
        self.assertEqual([a["name"] for a in body["items"]], ["demo_orders freshness", "demo_orders nulls"])
        err, body = call(client, "alerts", state="TRIGGERED")
        self.assertEqual([a["id"] for a in body["items"]], ["al1"])
        self.assertNotIn("owner", json.dumps(body))


if __name__ == "__main__":
    unittest.main()
