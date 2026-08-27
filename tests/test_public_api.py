from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import scene_factory


class PublicApiTests(unittest.TestCase):
    def test_all_exports_are_unique_and_accessible(self) -> None:
        self.assertEqual(len(scene_factory.__all__), len(set(scene_factory.__all__)))
        for name in scene_factory.__all__:
            self.assertTrue(hasattr(scene_factory, name), name)

    def test_public_import_does_not_load_optional_runtime(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        code = (
            "import sys; import scene_factory; "
            "blocked = ('isaacsim', 'omni', 'pxr', 'carb', 'numpy'); "
            "loaded = [name for name in blocked if name in sys.modules]; "
            "print(','.join(loaded))"
        )
        environment = {
            key: value
            for key, value in __import__("os").environ.items()
            if key != "PYTHONPATH"
        }
        environment["PYTHONPATH"] = str(source_root)
        environment["PYTHONNOUSERSITE"] = "1"
        with tempfile.TemporaryDirectory(prefix="scene_factory_api_import_") as directory:
            completed = subprocess.run(
                [sys.executable, "-B", "-c", code],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
