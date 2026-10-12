from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from scene_factory import SceneFactory
from scene_factory.exporters.blender import BlenderExporter
from scene_factory.exporters.blender_render import _fit_bbox_transform
from scene_factory.geometry import inverse_rotate_xy, rotated_half_extents_xy
from scene_factory.webapp import SceneWebApplication


class VisualSupportAlignmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with patch.dict(os.environ, {"SCENE_FACTORY_LLM_MODE": "off"}):
            cls.factory = SceneFactory()

    def test_real_web_assets_and_proxies_contact_visible_support_surfaces(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is needed for the real Three.js geometry regression")
        with tempfile.TemporaryDirectory() as directory:
            app = SceneWebApplication(directory, factory=self.factory)
            for recipe in ("kitchen_after_cooking", "living_room_recent_snacking", "kitchen_franka_mug_pick_place"):
                with self.subTest(recipe=recipe):
                    result = self.factory.build_from_recipe(recipe, 42)
                    payload = {
                        "scene": result.scene.to_dict(),
                        "assets": {item.asset_id: app._asset_payload(item.asset_id) for item in result.scene.objects},
                        "visual_paths": {visual["url"]: str(visual["path"]) for visual in app.visual_assets.values()},
                        "surfaces": {item.object_id: [asdict(surface) for surface in self.factory.registry.get(item.asset_id).support_surfaces]
                                     for item in result.scene.objects},
                    }
                    path = Path(directory) / "geometry.json"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    process = subprocess.run([node, str(Path(__file__).with_name("scene_viewer_alignment.mjs")), str(path)],
                                             capture_output=True, text=True, check=False, timeout=60)
                    self.assertEqual(process.returncode, 0, process.stderr[-3000:])
                    self.assertEqual(json.loads(process.stdout)["result"], "passed")

    def test_kitchen_support_footprints_are_inside_the_registered_counters(self) -> None:
        for seed in range(1000, 1020):
            result = self.factory.build_from_recipe("kitchen_after_cooking", seed)
            self.assertTrue(result.valid)
            objects = {item.object_id: item for item in result.scene.objects}
            for item in result.scene.objects:
                if not item.support or not item.support.startswith("counter_1:"):
                    continue
                support = objects["counter_1"]
                offset = inverse_rotate_xy((item.pose.position[0] - support.pose.position[0],
                                            item.pose.position[1] - support.pose.position[1]), support.pose.yaw_deg)
                half = rotated_half_extents_xy(item.bbox_m, item.pose.yaw_deg - support.pose.yaw_deg)
                for axis in (0, 1):
                    self.assertLessEqual(abs(offset[axis]) + half[axis], support.bbox_m[axis] / 2 + 0.003)

    def test_blender_support_transform_matches_declared_dimensions_and_bottom(self) -> None:
        scales, location = _fit_bbox_transform((-1, -2, -3), (1, 2, 3), (4, 6, 8))
        self.assertEqual(scales, (2, 1.5, 8 / 6))
        self.assertAlmostEqual(location[2] + scales[2] * -3, -4)
        self.assertAlmostEqual(location[2] + scales[2] * 3, 4)
        with tempfile.TemporaryDirectory() as directory:
            result = self.factory.build_from_recipe("kitchen_after_cooking", 42)
            files = BlenderExporter(self.factory.registry).export(result.scene, directory)
            manifest = json.loads(Path(files["blender_manifest"]).read_text("utf-8"))
            by_id = {item["object_id"]: item for item in manifest["objects"]}
            self.assertEqual(by_id["counter_1"]["visual_transform"]["scale_mode"], "fit_bbox")
            self.assertEqual(by_id["island_1"]["visual_transform"]["scale_mode"], "fit_bbox")
            self.assertEqual(by_id["mug_1"]["visual_transform"]["scale_mode"], "uniform_contain")


if __name__ == "__main__":
    unittest.main()
