import io
import os
import unittest
from contextlib import redirect_stderr
from unittest import mock

import _paths  # noqa: F401
from hallmark import __main__ as cli
from hallmark import models_mcp


class ModelsServeCliTest(unittest.TestCase):
    def test_without_host_exits_2_naming_the_variable(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("HALLMARK_MLFLOW_HOST", None)
            with redirect_stderr(err):
                code = cli.main(["models-serve"], io.StringIO())
        self.assertEqual(code, 2)
        self.assertIn("HALLMARK_MLFLOW_HOST", err.getvalue())

    def test_with_host_serves_the_client_built_from_the_environment(self):
        env = {"HALLMARK_MLFLOW_HOST": "https://mlflow.example.invalid/", "HALLMARK_MLFLOW_TOKEN": "t"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(models_mcp, "serve") as serve:
            code = cli.main(["models-serve"], io.StringIO())
        self.assertEqual(code, 0)
        client = serve.call_args.args[0]
        self.assertEqual(client.host, "https://mlflow.example.invalid")


if __name__ == "__main__":
    unittest.main()
