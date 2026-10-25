import io
import json
import os
import unittest
from unittest import mock

import _mlflow_stub as stub
from hallmark import mlflow_client as mc


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_for(seen):
    def opener(req, timeout=None):
        seen.append(req)
        if req.full_url.endswith("boom"):
            raise OSError("secret-host refused")
        return FakeResponse(json.dumps({"ok": True}).encode())
    return opener


class AllowlistTest(unittest.TestCase):
    def setUp(self):
        self.seen = []
        self.client = mc.MlflowClient("https://mlflow.invalid", "tok", opener_for(self.seen))

    def test_write_shaped_paths_are_refused_before_any_request(self):
        for path in (f"{mc.API}/registered-models/delete", f"{mc.API}/model-versions/transition-stage",
                     f"{mc.API}/runs/create", f"{mc.API}/runs/search/../delete"):
            with self.assertRaises(mc.ReadOnlyError):
                self.client.search(path, {})
        with self.assertRaises(mc.ReadOnlyError):
            self.client.get("/api/2.0/other/thing")
        self.assertEqual(self.seen, [])

    def test_search_paths_and_gets_are_sent_with_bearer(self):
        self.client.search(f"{mc.API}/runs/search", {"experiment_ids": ["1"]})
        self.client.get(f"{mc.API}/runs/get", {"run_id": "r"})
        self.assertEqual([r.get_method() for r in self.seen], ["POST", "GET"])
        self.assertEqual(self.seen[0].get_header("Authorization"), "Bearer tok")
        self.assertTrue(self.seen[1].full_url.endswith("/runs/get?run_id=r"))

    def test_error_does_not_leak_host_or_token(self):
        with self.assertRaises(mc.MlflowError) as ctx:
            self.client.get(f"{mc.API}/boom")
        self.assertNotIn("secret-host", str(ctx.exception))
        self.assertNotIn("tok", str(ctx.exception))

    def test_missing_host_is_named(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                self.assertRaisesRegex(mc.MlflowError, "HALLMARK_MLFLOW_HOST"):
            mc.MlflowClient()


class PagesTest(unittest.TestCase):
    def test_pagination_follows_the_token(self):
        client = stub.StubClient()
        names = [m["name"] for m in mc.pages(client, f"{mc.API}/registered-models/search",
                                             "registered_models", {"max_results": 1})]
        self.assertEqual(names, ["demo_churn", "demo_fraud"])
        self.assertEqual([c[2].get("page_token") for c in client.calls], [None, "1"])

    def test_post_listing(self):
        runs = list(mc.pages(stub.StubClient(), f"{mc.API}/runs/search", "runs",
                             {"experiment_ids": ["1"]}, post=True))
        self.assertEqual(len(runs), 3)


class LocalServerTest(unittest.TestCase):
    """The production urllib path against a fixture served on 127.0.0.1."""

    def test_client_over_http_matches_the_stub(self):
        server, url = stub.serve_http()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = mc.MlflowClient(url, "tok")
        over_http = list(mc.pages(client, f"{mc.API}/model-versions/search", "model_versions",
                                  {"filter": "name='demo_churn'", "max_results": 2}))
        direct = list(mc.pages(stub.StubClient(), f"{mc.API}/model-versions/search", "model_versions",
                               {"filter": "name='demo_churn'"}))
        self.assertEqual(over_http, direct)
        self.assertEqual(len(direct), 3)
        self.assertEqual(server.seen[0][2], "Bearer tok")

    def test_missing_resource_is_an_error(self):
        server, url = stub.serve_http()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with self.assertRaises(mc.MlflowError):
            mc.MlflowClient(url).get(f"{mc.API}/runs/get", {"run_id": "nope"})


if __name__ == "__main__":
    unittest.main()
