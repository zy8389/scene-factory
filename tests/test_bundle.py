from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scene_factory.bundle import BUNDLE_SCHEMA
from scene_factory.factory import SceneFactory
from scene_factory.webapp import SceneWebApplication


class SceneBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with patch.dict(os.environ, {"SCENE_FACTORY_LLM_MODE": "off"}):
            cls.factory = SceneFactory()

    def test_export_contains_hashed_scene_mjcf_and_real_visual_assets(self) -> None:
        result = self.factory.build_from_recipe("kitchen_after_cooking", 42)
        with tempfile.TemporaryDirectory() as directory:
            files = self.factory.write_result(result, directory, export_mjcf=True)
            bundle_path = Path(files["bundle"])
            self.assertTrue(bundle_path.is_file())
            with zipfile.ZipFile(bundle_path) as archive:
                names = set(archive.namelist())
                manifest = json.loads(archive.read("manifest.json"))
                descriptors = list(manifest["artifacts"].values()) + [
                    asset["visual"]
                    for asset in manifest["assets"].values()
                    if asset["visual"] is not None
                ]
                for descriptor in descriptors:
                    content = archive.read(descriptor["path"])
                    self.assertEqual(len(content), descriptor["size"])
                    self.assertEqual(hashlib.sha256(content).hexdigest(), descriptor["sha256"])

            self.assertEqual(manifest["schema"], BUNDLE_SCHEMA)
            self.assertEqual(manifest["scene_id"], result.scene.scene_id)
            self.assertIn("scene/scene.xml", names)
            self.assertIn("scene/preview.svg", names)
            self.assertEqual(
                set(manifest["assets"]),
                {item.asset_id for item in result.scene.objects},
            )
            visual_paths = {
                item["visual"]["path"]
                for item in manifest["assets"].values()
                if item["visual"] is not None
            }
            self.assertGreaterEqual(len(visual_paths), 4)
            self.assertTrue(visual_paths.issubset(names))

    def test_web_generation_exposes_downloadable_scene_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"SCENE_FACTORY_LLM_MODE": "off"}):
                app = SceneWebApplication(directory)
            response = app.generate(
                {
                    "prompt": "刚做完饭，厨房里有杯子、碗、盘子和刀。",
                    "seed": 51,
                    "count": 1,
                    "export_mjcf": True,
                    "export_usd": False,
                }
            )
            item = response["items"][0]
            self.assertIn("bundle", item["files"])
            relative = item["files"]["bundle"].removeprefix("/outputs/")
            bundle_path = app.resolve_output(relative)
            self.assertEqual(bundle_path.suffix, ".zip")
            self.assertGreater(bundle_path.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
