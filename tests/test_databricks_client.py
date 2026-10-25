import io
import inspect
import json
import os
import unittest
from unittest import mock

import _dbx_stub as stub
from hallmark import databricks_client as dc


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_for(seen):
    def opener(req, timeout=None):
        seen.append(req)
        if req.full_url.endswith("/api/2.1/jobs/get?job_id=boom"):
            raise OSError("secret-host refused")
        return FakeResponse(json.dumps({"ok": True}).encode())
    return opener


class AllowlistTest(unittest.TestCase):
    def setUp(self):
        self.seen = []
        self.client = dc.DatabricksClient("https://dbx.example.invalid", "tok", opener_for(self.seen))

    def test_write_shaped_paths_are_refused_before_any_request(self):
        for path in ("/api/2.1/jobs/run-now", "/api/2.1/jobs/runs/cancel", "/api/2.1/jobs/runs/repair",
                     "/api/2.1/jobs/runs/submit", "/api/2.1/jobs/create", "/api/2.1/jobs/delete",
                     "/api/2.1/jobs/reset", "/api/2.1/jobs/update", "/api/2.0/pipelines/pipe-orders/stop",
                     "/api/2.0/pipelines/pipe-orders/updates/start", "/api/2.0/pipelines/a/../b/stop",
                     "/api/2.0/sql/alerts/al1/delete", "/api/2.1/jobs/list/../delete",
                     "/api/2.1/jobs/list?x=1", "/api/2.0/other/thing"):
            with self.assertRaises(dc.ReadOnlyError, msg=path):
                self.client.get(path)
        self.assertEqual(self.seen, [])

    def test_the_client_has_no_method_that_sends_a_body(self):
        public = [n for n, _ in inspect.getmembers(dc.DatabricksClient, inspect.isfunction)
                  if not n.startswith("_")]
        self.assertEqual(public, ["get"])
        self.assertNotIn("data=", inspect.getsource(dc.DatabricksClient))

    def test_allowed_reads_are_sent_as_get_with_bearer(self):
        self.client.get("/api/2.1/jobs/runs/get", {"run_id": "9001"})
        self.client.get("/api/2.0/pipelines/pipe-orders/events")
        self.assertEqual([r.get_method() for r in self.seen], ["GET", "GET"])
        self.assertEqual(self.seen[0].get_header("Authorization"), "Bearer tok")
        self.assertTrue(self.seen[0].full_url.endswith("/runs/get?run_id=9001"))

    def test_error_does_not_leak_host_or_token(self):
        with self.assertRaises(dc.DatabricksError) as ctx:
            self.client.get("/api/2.1/jobs/get", {"job_id": "boom"})
        self.assertNotIn("secret-host", str(ctx.exception))
        self.assertNotIn("tok", str(ctx.exception))

    def test_missing_host_and_token_are_named(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                self.assertRaisesRegex(dc.DatabricksError, "HALLMARK_DBX_HOST"):
            dc.DatabricksClient()
        with mock.patch.dict(os.environ, {"HALLMARK_DBX_HOST": "https://dbx.example.invalid"}, clear=True), \
                self.assertRaisesRegex(dc.DatabricksError, "HALLMARK_DBX_TOKEN"):
            dc.DatabricksClient()


class HostTest(unittest.TestCase):
    def test_a_scheme_less_host_gets_https_and_sends_a_request(self):
        seen = []
        client = dc.DatabricksClient("adb-x.example.invalid/", "tok", opener_for(seen))
        client.get("/api/2.1/jobs/list")
        self.assertEqual(client.host, "https://adb-x.example.invalid")
        self.assertEqual(seen[0].full_url, "https://adb-x.example.invalid/api/2.1/jobs/list")

    def test_a_host_that_is_not_https_is_refused_without_echoing_it(self):
        for bad in ("http://secret-host.example.invalid", "ftp://secret-host.example.invalid",
                    "https://user:pw@secret-host.example.invalid", "https://", "https://h:notaport"):
            with self.assertRaises(dc.DatabricksError, msg=bad) as ctx:
                dc.DatabricksClient(bad, "tok")
            self.assertNotIn("secret-host", str(ctx.exception))
            self.assertIn("HALLMARK_DBX_HOST", str(ctx.exception))

    def test_a_request_that_cannot_be_built_is_a_fixed_error(self):
        client = dc.DatabricksClient("https://dbx.example.invalid", "tok", opener_for([]))
        client.host = "secret-host.example.invalid"
        with self.assertRaises(dc.DatabricksError) as ctx:
            client.get("/api/2.1/jobs/list")
        self.assertNotIn("secret-host", str(ctx.exception))


class PagesTest(unittest.TestCase):
    def test_pagination_follows_the_token(self):
        client = stub.StubClient()
        ids = [j["job_id"] for j in dc.pages(client, "/api/2.1/jobs/list", "jobs", size_key="limit",
                                             params={"limit": 2})]
        self.assertEqual(ids, [101, 102, 103])
        self.assertEqual([c[1].get("page_token") for c in client.calls], [None, "2"])


class CapTest(unittest.TestCase):
    class Endless:
        def __init__(self):
            self.calls = 0

        def get(self, path, params=None):
            self.calls += 1
            return {"jobs": [{"job_id": self.calls}], "next_page_token": "more"}

    def test_the_page_cap_truncates_instead_of_raising(self):
        client = self.Endless()
        listing = dc.pages(client, "/api/2.1/jobs/list", "jobs")
        self.assertEqual(len(list(listing)), dc.MAX_PAGES)
        self.assertTrue(listing.truncated)
        self.assertEqual(client.calls, dc.MAX_PAGES)

    def test_a_listing_that_ends_is_not_truncated(self):
        listing = dc.pages(stub.StubClient(), "/api/2.1/jobs/list", "jobs", params={"limit": 2},
                           size_key="limit")
        self.assertEqual(len(list(listing)), 3)
        self.assertFalse(listing.truncated)


class LocalServerTest(unittest.TestCase):
    """The production urllib path against a fixture served on 127.0.0.1."""

    def test_client_over_http_matches_the_stub_and_only_gets(self):
        server, url = stub.serve_http()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = dc.DatabricksClient(url, "tok")
        over_http = list(dc.pages(client, "/api/2.1/jobs/runs/list", "runs", {"job_id": "101", "limit": 2}))
        direct = list(dc.pages(stub.StubClient(), "/api/2.1/jobs/runs/list", "runs", {"job_id": "101"}))
        self.assertEqual(over_http, direct)
        self.assertEqual(len(direct), 3)
        self.assertEqual({m for m, _, _ in server.seen}, {"GET"})
        self.assertEqual(server.seen[0][2], "Bearer tok")

    def test_missing_resource_is_an_error(self):
        server, url = stub.serve_http()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with self.assertRaises(dc.DatabricksError):
            dc.DatabricksClient(url, "tok").get("/api/2.1/jobs/runs/get", {"run_id": "nope"})


if __name__ == "__main__":
    unittest.main()
