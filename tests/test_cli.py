import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

import _paths
from hallmark.__main__ import main

SNAPSHOT = os.path.join(_paths.FIXTURES, "snapshot.json")
SRC = os.path.join(_paths.FIXTURES, "src")


def refresh(db, *extra):
    out = io.StringIO()
    code = main(["refresh", "--db", db, "--snapshot", SNAPSHOT, *extra], out)
    return code, json.loads(out.getvalue()) if out.getvalue() else None


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "nested", "index.db")

    def test_refresh_builds_then_is_incremental(self):
        code, first = refresh(self.db, "--src", SRC)
        self.assertEqual(code, 0)
        self.assertEqual((first["rewritten"], first["tables"]), (3, 3))
        self.assertGreater(first["column_refs"], 0)
        code, second = refresh(self.db)
        self.assertEqual(second["rewritten"], 0)

    def _repo(self, ignore):
        repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q", repo], check=True)
        if ignore:
            with open(os.path.join(repo, ".gitignore"), "w") as handle:
                handle.write(ignore)
        return repo

    def test_refresh_refuses_a_db_git_does_not_ignore(self):
        repo = self._repo("")
        err = io.StringIO()
        with redirect_stderr(err):
            code, _ = refresh(os.path.join(repo, "index.db"))
        self.assertEqual(code, 2)
        self.assertIn("not ignored", err.getvalue())
        self.assertFalse(os.path.exists(os.path.join(repo, "index.db")))

    def test_refresh_refuses_a_tracked_db(self):
        repo = self._repo("*.db\n")
        db = os.path.join(repo, "index.db")
        open(db, "w").close()
        subprocess.run(["git", "-C", repo, "add", "-f", "index.db"], check=True)
        err = io.StringIO()
        with redirect_stderr(err):
            code, _ = refresh(db)
        self.assertEqual(code, 2)
        self.assertIn("tracked", err.getvalue())

    def test_refresh_allows_an_ignored_default_db(self):
        repo = self._repo(".hallmark/\n")
        code, report = refresh(os.path.join(repo, ".hallmark", "index.db"))
        self.assertEqual(code, 0)
        self.assertTrue(report)

    def test_refresh_reports_a_bad_snapshot(self):
        bad = os.path.join(self.tmp.name, "bad.json")
        with open(bad, "w", encoding="utf-8") as handle:
            json.dump({"tables": "no"}, handle)
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["refresh", "--db", self.db, "--snapshot", bad], io.StringIO())
        self.assertEqual(code, 1)
        self.assertIn("tables", err.getvalue())

    def test_uc_without_environment_fails_cleanly(self):
        for name in ("HALLMARK_UC_HOST", "HALLMARK_UC_TOKEN", "HALLMARK_UC_WAREHOUSE"):
            os.environ.pop(name, None)
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["refresh", "--db", self.db, "--uc"], io.StringIO())
        self.assertEqual(code, 1)
        self.assertIn("HALLMARK_UC_HOST", err.getvalue())

    def test_serve_without_index_exits_2(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["serve", "--db", self.db]), 2)

    def test_serve_end_to_end_as_a_process(self):
        refresh(self.db, "--src", SRC)
        request = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                              "params": {"name": "get_table",
                                         "arguments": {"fqn": "demo_catalog.sales.orders"}}})
        proc = subprocess.run([sys.executable, "-m", "hallmark", "serve", "--db", self.db],
                              input=request + "\n", capture_output=True, text=True,
                              cwd=_paths.ROOT, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        reply = json.loads(proc.stdout)
        self.assertEqual(reply["id"], 7)
        body = json.loads(reply["result"]["content"][0]["text"])
        self.assertTrue(body["code_references"]["items"])


if __name__ == "__main__":
    unittest.main()
