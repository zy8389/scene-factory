from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest.mock import patch

from scene_factory.cli import main


class RuntimeCliTests(unittest.TestCase):
    def test_complete_failure_evidence_is_not_reported_as_task_success(self) -> None:
        report = {"valid": True, "summary": {"all_task_pass": False}}
        output = io.StringIO()
        with patch("scene_factory.runtime_batch.run_runtime_batch", return_value=report), \
                contextlib.redirect_stdout(output):
            code = main(["dataset", "run-isaac", "collection", "--output", "run",
                         "--limit-layouts", "5"])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output.getvalue())["summary"]["all_task_pass"])

    def test_invalid_evidence_returns_validation_failure(self) -> None:
        with patch("scene_factory.runtime_batch.validate_runtime_batch",
                   return_value={"valid": False, "errors": ["worker_trace_sha256_mismatch"]}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["dataset", "runtime-validate", "run"]), 2)

    def test_runtime_error_is_distinct_from_validation_failure(self) -> None:
        with patch("scene_factory.runtime_batch.run_runtime_batch",
                   side_effect=ValueError("official robot assets are missing")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["dataset", "run-isaac", "collection", "--output", "run"]), 1)


if __name__ == "__main__":
    unittest.main()
