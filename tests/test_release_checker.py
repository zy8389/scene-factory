from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ReleaseCheckerTests(unittest.TestCase):
    def test_structural_release_checker_is_offline_and_passes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment["PYTHONNOUSERSITE"] = "1"
        environment.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory(prefix="scene_factory_release_checker_") as directory:
            completed = subprocess.run(
                [sys.executable, str(root / "tools" / "check_release.py")],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertIn('"result": "passed"', completed.stdout)


if __name__ == "__main__":
    unittest.main()
