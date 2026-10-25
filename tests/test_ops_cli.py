import io
import os
import unittest
from contextlib import redirect_stderr
from unittest import mock

import _paths  # noqa: F401
from hallmark import __main__ as cli
from hallmark import ops_mcp


class OpsServeCliTest(unittest.TestCase):
    def test_without_host_exits_2_naming_the_variable(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("HALLMARK_DBX_HOST", None)
            os.environ.pop("HALLMARK_DBX_TOKEN", None)
            with redirect_stderr(err):
                code = cli.main(["ops-serve"], io.StringIO())
        self.assertEqual(code, 2)
        self.assertIn("HALLMARK_DBX_HOST", err.getvalue())

    def test_without_token_exits_2_naming_the_variable(self):
        err = io.StringIO()
        env = {"HALLMARK_DBX_HOST": "https://dbx.example.invalid"}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("HALLMARK_DBX_TOKEN", None)
            with redirect_stderr(err):
                code = cli.main(["ops-serve"], io.StringIO())
        self.assertEqual(code, 2)
        self.assertIn("HALLMARK_DBX_TOKEN", err.getvalue())

    def test_with_both_serves_the_client_built_from_the_environment(self):
        env = {"HALLMARK_DBX_HOST": "https://dbx.example.invalid/", "HALLMARK_DBX_TOKEN": "t"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(ops_mcp, "serve") as serve:
            code = cli.main(["ops-serve"], io.StringIO())
        self.assertEqual(code, 0)
        self.assertEqual(serve.call_args.args[0].host, "https://dbx.example.invalid")


if __name__ == "__main__":
    unittest.main()
