from __future__ import annotations

import importlib.metadata
import tomllib
import unittest
from pathlib import Path

import scene_factory


class VersionConsistencyTests(unittest.TestCase):
    def test_source_version_matches_pyproject(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with (root / "pyproject.toml").open("rb") as handle:
            project_version = tomllib.load(handle)["project"]["version"]
        self.assertEqual(scene_factory.__version__, project_version)

    def test_installed_distribution_matches_package_when_available(self) -> None:
        try:
            installed_version = importlib.metadata.version("scene-factory")
        except importlib.metadata.PackageNotFoundError:
            self.skipTest("source checkout is not installed as a distribution")
        self.assertEqual(scene_factory.__version__, installed_version)


if __name__ == "__main__":
    unittest.main()
