from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from scene_factory.webapp import SceneWebApplication


EXPECTED_VISUALS = {
    "sofa_basic": "7fe865939497e32bc2f6f0c3dc519954c7c83eb5b9251dbf8e1fc318f5c5ad1a",
    "coffee_table_oak": "3bdbf5235bc60553cf9b7174aa907a20d1284e7374a85d71521ff59f57053a8c",
    "side_cabinet_basic": "081b44f3b98d6903a82625d1941584f1e3bc4b6c366a3dc242649b01d3b3b0f9",
    "entry_bench_basic": "07c739a1e5361b878320ed62ea1bc4b68a6268498663198e78266af3b819c172",
    "kitchen_counter_basic": "1e6fc076b12caea37fa1f1b5302c7a4160bd5aeda5e20f72b4bccb3ea87b4391",
    "kitchen_island_basic": "99e8494b67187126696aa9dc77596d187f5b8d5dc1f3420d1a5e6afe2747d5b1",
    "cutting_board_wood": "5bbcbcf22589a9da54cea73ab00983d7521c2cf9ef14673cc2f70f6cfbe53b06",
    "pot_basic": "ba7c8d3edad6c30b9e50dd436249fe980ab8aa860d1f609793d892cc1f7c6ce4",
}


class HighDetailVisualPackTests(unittest.TestCase):
    def test_manifests_and_glb_hashes_match(self) -> None:
        source_root = Path("data/assets/source")
        for asset_id, expected_hash in EXPECTED_VISUALS.items():
            with self.subTest(asset_id=asset_id):
                asset_root = source_root / asset_id
                manifest = json.loads((asset_root / "SOURCE.json").read_text(encoding="utf-8"))
                visual = asset_root / manifest["source_geometry"]
                digest = hashlib.sha256(visual.read_bytes()).hexdigest()
                self.assertEqual(manifest["asset_id"], asset_id)
                self.assertEqual(manifest["batch_id"], "high_detail_cc0_v1")
                self.assertEqual(manifest["license"], "CC0")
                self.assertEqual(digest, expected_hash)
                self.assertEqual(manifest["source_files"][0]["sha256"], expected_hash)
                self.assertEqual(visual.read_bytes()[:4], b"glTF")

    def test_web_catalog_exposes_the_visual_pack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = SceneWebApplication(directory)
            catalog = {item["asset_id"]: item for item in app.asset_catalog()}
        for asset_id in EXPECTED_VISUALS:
            with self.subTest(asset_id=asset_id):
                self.assertEqual(catalog[asset_id]["visual_url"], f"/assets/{asset_id}/visual")
                self.assertEqual(catalog[asset_id]["license"], "CC0")
                self.assertEqual(app.resolve_asset(f"{asset_id}/visual").suffix, ".glb")

    def test_exported_scene_bundle_preserves_visual_license(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = SceneWebApplication(directory)
            result = app.factory.build_from_recipe("kitchen_after_cooking", 42)
            files = app.factory.write_result(result, Path(directory) / "scene")
            with zipfile.ZipFile(files["bundle"]) as archive:
                manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(manifest["assets"]["kitchen_counter_basic"]["license"], "CC0")
        self.assertEqual(manifest["assets"]["kitchen_island_basic"]["license"], "CC0")
        self.assertEqual(manifest["assets"]["cutting_board_wood"]["license"], "CC0")
        self.assertEqual(manifest["assets"]["pot_basic"]["license"], "CC0")


if __name__ == "__main__":
    unittest.main()
